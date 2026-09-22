"""Read-only PostgreSQL query layer with application-side vector search."""

from __future__ import annotations

import logging
import time
from threading import Lock, RLock

import numpy as np
from psycopg import sql
from psycopg_pool import ConnectionPool

from mcp_server.db import _fix_web_url
from mcp_server.postgres_config import validate_schema_name

EMBEDDING_DIM = 384
logger = logging.getLogger(__name__)


def build_normalized_matrix(
    rows: list[tuple[int, bytes | memoryview]], embedding_dim: int = EMBEDDING_DIM
) -> tuple[np.ndarray, np.ndarray]:
    """Decode float32 BYTEA rows into an ID array and normalized matrix."""
    ids = np.empty(len(rows), dtype=np.int64)
    matrix = np.empty((len(rows), embedding_dim), dtype=np.float32)
    for position, (chunk_id, embedding) in enumerate(rows):
        vector = np.frombuffer(embedding, dtype="<f4")
        if vector.size != embedding_dim:
            raise ValueError(
                f"Chunk {chunk_id} has dimension {vector.size}; expected {embedding_dim}"
            )
        if not np.all(np.isfinite(vector)):
            raise ValueError(f"Chunk {chunk_id} contains a non-finite embedding")
        ids[position] = chunk_id
        matrix[position] = vector

    if not len(rows):
        return ids, matrix

    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    if np.any(norms == 0):
        raise ValueError("Zero-length embedding cannot be used for cosine search")
    matrix /= norms
    return ids, matrix


def cosine_top_k(
    ids: np.ndarray, matrix: np.ndarray, query_embedding: list[float], top_k: int
) -> list[tuple[int, float]]:
    """Return chunk IDs and cosine scores in descending score order."""
    if top_k <= 0 or matrix.shape[0] == 0:
        return []
    query = np.asarray(query_embedding, dtype=np.float32)
    expected_dim = matrix.shape[1]
    if query.shape != (expected_dim,):
        raise ValueError(f"Query has dimension {query.size}; expected {expected_dim}")
    if not np.all(np.isfinite(query)):
        raise ValueError("Query embedding contains non-finite values")
    norm = float(np.linalg.norm(query))
    if norm == 0:
        raise ValueError("Zero-length query cannot be used for cosine search")

    scores = matrix @ (query / norm)
    count = min(top_k, len(scores))
    candidates = np.argpartition(scores, -count)[-count:]
    ordered = candidates[np.argsort(scores[candidates])[::-1]]
    return [(int(ids[i]), float(scores[i])) for i in ordered]


class PostgresQueryDatabase:
    """Query PostgreSQL metadata and cache vectors for fast cosine search."""

    def __init__(
        self,
        database_url: str,
        *,
        schema: str = "sharepoint_mcp",
        embedding_dim: int = EMBEDDING_DIM,
        preload: bool = True,
        pool_min_size: int = 1,
        pool_max_size: int = 5,
        pool_timeout: float = 10.0,
    ):
        if pool_min_size < 0 or pool_max_size < max(1, pool_min_size):
            raise ValueError("Invalid PostgreSQL pool size")
        if pool_timeout <= 0:
            raise ValueError("PostgreSQL pool timeout must be positive")
        self._database_url = database_url
        self._schema = validate_schema_name(schema)
        self._embedding_dim = embedding_dim
        self._cache_lock = RLock()
        self._pool_open_lock = Lock()
        self._pool_started = False
        self._pool_timeout = pool_timeout
        self._pool = ConnectionPool(
            database_url,
            min_size=pool_min_size,
            max_size=pool_max_size,
            open=False,
            timeout=pool_timeout,
            max_waiting=20,
            max_lifetime=1_800,
            max_idle=300,
            reconnect_timeout=30,
            check=ConnectionPool.check_connection,
            kwargs={
                "autocommit": True,
                "connect_timeout": 10,
                "application_name": "sharepoint-search-mcp",
                "options": "-c statement_timeout=30000",
            },
            name="sharepoint-search-query",
        )
        self._revision: int | None = None
        self._ids = np.empty(0, dtype=np.int64)
        self._matrix = np.empty((0, embedding_dim), dtype=np.float32)
        if preload:
            try:
                self.refresh(force=True)
            except Exception:
                self.close()
                raise

    def _connect(self):
        if not self._pool_started:
            with self._pool_open_lock:
                if not self._pool_started:
                    self._pool.open(wait=True, timeout=30)
                    self._pool_started = True
        return self._pool.connection(timeout=self._pool_timeout)

    def _read_revision(self, connection) -> int:
        namespace = sql.Identifier(self._schema)
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "SELECT revision FROM {}.index_state WHERE singleton = TRUE"
                ).format(namespace)
            )
            row = cursor.fetchone()
        return int(row[0]) if row else 0

    def refresh(self, *, force: bool = False) -> bool:
        """Reload vectors when the committed index revision changes."""
        started = time.perf_counter()
        namespace = sql.Identifier(self._schema)
        with self._connect() as connection:
            revision = self._read_revision(connection)
            with self._cache_lock:
                if not force and revision == self._revision:
                    return False
                with connection.cursor() as cursor:
                    cursor.execute(
                        sql.SQL(
                            "SELECT id, embedding FROM {}.chunks ORDER BY id"
                        ).format(namespace)
                    )
                    rows = cursor.fetchall()
                ids, matrix = build_normalized_matrix(rows, self._embedding_dim)
                self._ids = ids
                self._matrix = matrix
                self._revision = revision
        logger.info(
            "Loaded %d vectors (%d bytes) for index revision %d in %.3f seconds",
            len(ids),
            matrix.nbytes,
            revision,
            time.perf_counter() - started,
        )
        return True

    def search(self, query_embedding: list[float], top_k: int = 5) -> list[dict]:
        """Cosine-search the cached vectors and fetch metadata for top chunks."""
        self.refresh()
        with self._cache_lock:
            ids = self._ids
            matrix = self._matrix
        matches = cosine_top_k(ids, matrix, query_embedding, top_k)
        if not matches:
            return []

        match_ids = [chunk_id for chunk_id, _ in matches]
        namespace = sql.Identifier(self._schema)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                        SELECT ch.id, ch.text, ch.page_or_slide, f.filename,
                               f.sharepoint_path, f.web_url, f.document_author,
                               f.document_created_at, f.document_last_modified_by,
                               f.document_modified_at
                        FROM {}.chunks ch
                        JOIN {}.files f ON f.id = ch.file_id
                        WHERE ch.id = ANY(%s)
                        """
                ).format(namespace, namespace),
                (match_ids,),
            )
            rows = cursor.fetchall()

        by_id = {row[0]: row for row in rows}
        results = []
        for chunk_id, score in matches:
            row = by_id.get(chunk_id)
            if row is None:
                continue
            results.append(
                {
                    "text": row[1],
                    "page_or_slide": row[2],
                    "filename": row[3],
                    "sharepoint_path": row[4],
                    "web_url": _fix_web_url(row[5], row[4]),
                    "document_author": row[6],
                    "document_created_at": row[7],
                    "document_last_modified_by": row[8],
                    "document_modified_at": row[9],
                    "score": score,
                }
            )
        return results

    def get_document_content(self, sharepoint_path: str) -> list[dict]:
        namespace = sql.Identifier(self._schema)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                        SELECT ch.chunk_index, ch.page_or_slide, ch.text
                        FROM {}.chunks ch
                        JOIN {}.files f ON f.id = ch.file_id
                        WHERE f.sharepoint_path = %s
                        ORDER BY ch.chunk_index
                        """
                ).format(namespace, namespace),
                (sharepoint_path,),
            )
            rows = cursor.fetchall()
        return [
            {"chunk_index": row[0], "page_or_slide": row[1], "text": row[2]}
            for row in rows
        ]

    def list_files(
        self,
        folder_path: str | None = None,
        file_type: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        if limit is not None and limit <= 0:
            return []
        namespace = sql.Identifier(self._schema)
        # Zero-chunk rows mark unchanged empty source files and stay out of MCP lists.
        clauses = [
            sql.SQL(
                "EXISTS (SELECT 1 FROM {}.chunks indexed_chunk "
                "WHERE indexed_chunk.file_id = files.id)"
            ).format(namespace)
        ]
        params = []
        if folder_path:
            clauses.append(sql.SQL("strpos(sharepoint_path, %s) > 0"))
            params.append(folder_path)
        if file_type:
            clauses.append(sql.SQL("file_type = %s"))
            params.append(file_type)
        where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(clauses)
        query = (
            sql.SQL(
                """
                SELECT filename, sharepoint_path, file_type, size_bytes,
                       modified_at, web_url, document_author, document_created_at,
                       document_last_modified_by, document_modified_at
                FROM {}.files
                """
            ).format(namespace)
            + where
            + sql.SQL(" ORDER BY filename")
        )
        if limit is not None:
            query += sql.SQL(" LIMIT %s")
            params.append(limit)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(query, params)
            rows = cursor.fetchall()
        return [
            {
                "filename": row[0],
                "sharepoint_path": row[1],
                "file_type": row[2],
                "size_bytes": row[3],
                "modified_at": str(row[4]) if row[4] else None,
                "web_url": _fix_web_url(row[5], row[1]),
                "document_author": row[6],
                "document_created_at": row[7],
                "document_last_modified_by": row[8],
                "document_modified_at": row[9],
            }
            for row in rows
        ]

    def get_file_metadata(self, sharepoint_path: str) -> dict | None:
        namespace = sql.Identifier(self._schema)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                        SELECT filename, sharepoint_path, file_type, size_bytes,
                               modified_at, web_url, document_author,
                               document_created_at, document_last_modified_by,
                               document_modified_at
                        FROM {}.files WHERE sharepoint_path = %s
                        """
                ).format(namespace),
                (sharepoint_path,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return {
            "filename": row[0],
            "sharepoint_path": row[1],
            "file_type": row[2],
            "size_bytes": row[3],
            "modified_at": str(row[4]) if row[4] else None,
            "web_url": _fix_web_url(row[5], row[1]),
            "document_author": row[6],
            "document_created_at": row[7],
            "document_last_modified_by": row[8],
            "document_modified_at": row[9],
        }

    def close(self) -> None:
        """Close pooled PostgreSQL connections during process shutdown/tests."""
        if self._pool_started:
            self._pool.close()
            self._pool_started = False

    @property
    def cached_vector_count(self) -> int:
        return int(len(self._ids))
