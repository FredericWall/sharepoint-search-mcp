"""PostgreSQL writer for the local indexing pipeline.

Embeddings stay compatible with the MCP backend: 384 little-endian float32
values stored as a 1,536-byte BYTEA value. This module stays independent from
the MCP server because the components are deployed separately.
"""

from __future__ import annotations

import math
import re
import struct
from collections.abc import Sequence

import psycopg
from psycopg import sql

_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_WRITER_LOCK_ID = 741_147_384_001
_MIGRATION_LOCK_ID = 741_147_384_002


def validate_schema_name(schema: str) -> str:
    if not _SCHEMA_RE.fullmatch(schema):
        raise ValueError(f"Invalid PostgreSQL schema name: {schema!r}")
    return schema


def embedding_to_bytes(embedding: Sequence[float], embedding_dim: int) -> bytes:
    if len(embedding) != embedding_dim:
        raise ValueError(
            f"Embedding has dimension {len(embedding)}; expected {embedding_dim}"
        )
    if not all(math.isfinite(value) for value in embedding):
        raise ValueError("Embedding contains a non-finite value")
    return struct.pack(f"<{embedding_dim}f", *embedding)


class PostgresDatabase:
    """Incremental writer with one transaction per indexed file."""

    def __init__(
        self,
        database_url: str = "",
        *,
        schema: str = "sharepoint_mcp",
        embedding_dim: int = 384,
    ):
        self.embedding_dim = embedding_dim
        self._schema = validate_schema_name(schema)
        connect_args = {
            "connect_timeout": 10,
            "application_name": "sharepoint-search-indexer",
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 3,
            "options": (
                "-c statement_timeout=120000 "
                "-c lock_timeout=30000 "
                "-c idle_in_transaction_session_timeout=120000"
            ),
        }
        self._conn = (
            psycopg.connect(database_url, **connect_args)
            if database_url
            else psycopg.connect(**connect_args)
        )
        self._has_run_lock = False

    @property
    def backend_name(self) -> str:
        return "postgres"

    def acquire_run_lock(self) -> bool:
        """Prevent two indexers from publishing concurrently."""
        with self._conn.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (_WRITER_LOCK_ID,))
            locked = bool(cursor.fetchone()[0])
        self._conn.commit()
        self._has_run_lock = locked
        return locked

    def init_schema(self) -> None:
        """Apply idempotent incremental-writer schema migrations."""
        namespace = sql.Identifier(self._schema)
        files_sequence = sql.Identifier(self._schema, "files_id_seq")
        chunks_sequence = sql.Identifier(self._schema, "chunks_id_seq")
        files_sequence_name = f"{self._schema}.files_id_seq"
        chunks_sequence_name = f"{self._schema}.chunks_id_seq"
        embedding_bytes = self.embedding_dim * 4

        try:
            with self._conn.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_ID,)
                )
                cursor.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(namespace))
                cursor.execute(
                    sql.SQL("CREATE SEQUENCE IF NOT EXISTS {} AS BIGINT").format(
                        files_sequence
                    )
                )
                cursor.execute(
                    sql.SQL("CREATE SEQUENCE IF NOT EXISTS {} AS BIGINT").format(
                        chunks_sequence
                    )
                )
                cursor.execute(
                    sql.SQL(
                        """
                        CREATE TABLE IF NOT EXISTS {}.files (
                            id BIGINT PRIMARY KEY,
                            sharepoint_path TEXT UNIQUE NOT NULL,
                            filename TEXT NOT NULL,
                            file_type TEXT NOT NULL,
                            size_bytes BIGINT,
                            modified_at TEXT,
                            document_author TEXT,
                            document_created_at TEXT,
                            document_last_modified_by TEXT,
                            document_modified_at TEXT,
                            metadata_scanned_at TEXT,
                            web_url TEXT,
                            indexed_at TEXT
                        )
                        """
                    ).format(namespace)
                )
                cursor.execute(
                    sql.SQL(
                        f"""
                        CREATE TABLE IF NOT EXISTS {{}}.chunks (
                            id BIGINT PRIMARY KEY,
                            file_id BIGINT NOT NULL REFERENCES {{}}.files(id) ON DELETE CASCADE,
                            chunk_index INTEGER NOT NULL,
                            page_or_slide INTEGER,
                            text TEXT NOT NULL,
                            embedding BYTEA NOT NULL,
                            CONSTRAINT embedding_size CHECK (octet_length(embedding) = {embedding_bytes})
                        )
                        """
                    ).format(namespace, namespace)
                )
                cursor.execute(
                    sql.SQL(
                        "CREATE INDEX IF NOT EXISTS chunks_file_position_idx "
                        "ON {}.chunks (file_id, chunk_index)"
                    ).format(namespace)
                )
                cursor.execute(
                    sql.SQL(
                        """
                        CREATE TABLE IF NOT EXISTS {}.index_state (
                            singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
                            revision BIGINT NOT NULL DEFAULT 0,
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                        """
                    ).format(namespace)
                )
                cursor.execute(
                    sql.SQL(
                        """
                        CREATE TABLE IF NOT EXISTS {}.schema_migrations (
                            version INTEGER PRIMARY KEY,
                            description TEXT NOT NULL,
                            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                        """
                    ).format(namespace)
                )
                cursor.execute(
                    sql.SQL(
                        "ALTER TABLE {}.files ALTER COLUMN id "
                        "SET DEFAULT nextval({}::regclass)"
                    ).format(namespace, sql.Literal(files_sequence_name))
                )
                cursor.execute(
                    sql.SQL(
                        "ALTER TABLE {}.chunks ALTER COLUMN id "
                        "SET DEFAULT nextval({}::regclass)"
                    ).format(namespace, sql.Literal(chunks_sequence_name))
                )
                cursor.execute(
                    sql.SQL(
                        """
                        ALTER TABLE {}.files
                            ADD COLUMN IF NOT EXISTS document_author TEXT,
                            ADD COLUMN IF NOT EXISTS document_created_at TEXT,
                            ADD COLUMN IF NOT EXISTS document_last_modified_by TEXT,
                            ADD COLUMN IF NOT EXISTS document_modified_at TEXT,
                            ADD COLUMN IF NOT EXISTS metadata_scanned_at TEXT
                        """
                    ).format(namespace)
                )
                self._align_sequence(cursor, "files", "files_id_seq")
                self._align_sequence(cursor, "chunks", "chunks_id_seq")
                cursor.execute(
                    sql.SQL(
                        """
                        INSERT INTO {}.schema_migrations (version, description)
                        VALUES (1, 'add incremental index writer sequences')
                        ON CONFLICT (version) DO NOTHING
                        """
                    ).format(namespace)
                )
                cursor.execute(
                    sql.SQL(
                        """
                        INSERT INTO {}.schema_migrations (version, description)
                        VALUES (2, 'add embedded Office document metadata')
                        ON CONFLICT (version) DO NOTHING
                        """
                    ).format(namespace)
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _align_sequence(self, cursor, table: str, sequence: str) -> None:
        namespace = sql.Identifier(self._schema)
        cursor.execute(
            sql.SQL("SELECT COALESCE(MAX(id), 0) FROM {}.{}").format(
                namespace, sql.Identifier(table)
            )
        )
        max_id = int(cursor.fetchone()[0])
        cursor.execute(
            sql.SQL("SELECT last_value, is_called FROM {}.{}").format(
                namespace, sql.Identifier(sequence)
            )
        )
        last_value, is_called = cursor.fetchone()
        sequence_next = int(last_value) + (1 if is_called else 0)
        next_value = max(1, max_id + 1, sequence_next)
        cursor.execute(
            "SELECT setval(%s::regclass, %s, FALSE)",
            (f"{self._schema}.{sequence}", next_value),
        )

    def verify_schema(self) -> None:
        namespace = sql.Identifier(self._schema)
        with self._conn.cursor() as cursor:
            cursor.execute(sql.SQL("SELECT 1 FROM {}.files LIMIT 0").format(namespace))
            cursor.execute(sql.SQL("SELECT 1 FROM {}.chunks LIMIT 0").format(namespace))
            cursor.execute(
                sql.SQL("SELECT 1 FROM {}.index_state LIMIT 0").format(namespace)
            )
        self._conn.commit()

    def get_indexed_versions(self, sharepoint_prefix: str) -> dict[str, str | None]:
        namespace = sql.Identifier(self._schema)
        prefix = sharepoint_prefix.rstrip("/")
        folder_prefix = f"{prefix}/"
        with self._conn.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "SELECT sharepoint_path, modified_at FROM {}.files "
                    "WHERE sharepoint_path = %s "
                    "OR left(sharepoint_path, length(%s)) = %s"
                ).format(namespace),
                (prefix, folder_prefix, folder_prefix),
            )
            rows = cursor.fetchall()
        self._conn.commit()
        return {str(path): modified for path, modified in rows}

    def is_indexed(self, sharepoint_path: str, modified_at: str) -> bool:
        namespace = sql.Identifier(self._schema)
        with self._conn.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "SELECT modified_at FROM {}.files WHERE sharepoint_path = %s"
                ).format(namespace),
                (sharepoint_path,),
            )
            row = cursor.fetchone()
        self._conn.commit()
        return row is not None and row[0] == modified_at

    def upsert_file(
        self,
        sharepoint_path: str,
        filename: str,
        file_type: str,
        size_bytes: int,
        modified_at: str,
        web_url: str = "",
        document_author: str | None = None,
        document_created_at: str | None = None,
        document_last_modified_by: str | None = None,
        document_modified_at: str | None = None,
        metadata_scanned_at: str | None = None,
    ) -> int:
        namespace = sql.Identifier(self._schema)
        with self._conn.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                    INSERT INTO {}.files
                        (sharepoint_path, filename, file_type, size_bytes, modified_at,
                         web_url, document_author, document_created_at,
                         document_last_modified_by, document_modified_at,
                         metadata_scanned_at, indexed_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW()::text)
                    ON CONFLICT (sharepoint_path) DO UPDATE SET
                        filename = EXCLUDED.filename,
                        file_type = EXCLUDED.file_type,
                        size_bytes = EXCLUDED.size_bytes,
                        modified_at = EXCLUDED.modified_at,
                        web_url = EXCLUDED.web_url,
                        document_author = CASE WHEN EXCLUDED.metadata_scanned_at IS NOT NULL
                            THEN EXCLUDED.document_author ELSE files.document_author END,
                        document_created_at = CASE WHEN EXCLUDED.metadata_scanned_at IS NOT NULL
                            THEN EXCLUDED.document_created_at ELSE files.document_created_at END,
                        document_last_modified_by = CASE
                            WHEN EXCLUDED.metadata_scanned_at IS NOT NULL
                            THEN EXCLUDED.document_last_modified_by
                            ELSE files.document_last_modified_by END,
                        document_modified_at = CASE WHEN EXCLUDED.metadata_scanned_at IS NOT NULL
                            THEN EXCLUDED.document_modified_at ELSE files.document_modified_at END,
                        metadata_scanned_at = COALESCE(
                            EXCLUDED.metadata_scanned_at, files.metadata_scanned_at
                        ),
                        indexed_at = NOW()::text
                    RETURNING id
                    """
                ).format(namespace),
                (
                    sharepoint_path, filename, file_type, size_bytes, modified_at, web_url,
                    document_author, document_created_at, document_last_modified_by,
                    document_modified_at, metadata_scanned_at,
                ),
            )
            return int(cursor.fetchone()[0])

    def get_metadata_pending_paths(self, sharepoint_prefix: str) -> set[str]:
        namespace = sql.Identifier(self._schema)
        prefix = sharepoint_prefix.rstrip("/")
        folder_prefix = f"{prefix}/"
        with self._conn.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "SELECT sharepoint_path FROM {}.files "
                    "WHERE file_type IN ('pptx', 'docx') "
                    "AND metadata_scanned_at IS NULL "
                    "AND (sharepoint_path = %s "
                    "OR left(sharepoint_path, length(%s)) = %s)"
                ).format(namespace),
                (prefix, folder_prefix, folder_prefix),
            )
            rows = cursor.fetchall()
        self._conn.commit()
        return {str(row[0]) for row in rows}

    def update_file_metadata(
        self,
        updates: Sequence[
            tuple[str | None, str | None, str | None, str | None, str, str]
        ],
    ) -> int:
        if not updates:
            return 0
        namespace = sql.Identifier(self._schema)
        with self._conn.cursor() as cursor:
            cursor.executemany(
                sql.SQL(
                    """
                    UPDATE {}.files SET
                        document_author = %s,
                        document_created_at = %s,
                        document_last_modified_by = %s,
                        document_modified_at = %s,
                        metadata_scanned_at = %s
                    WHERE sharepoint_path = %s
                    """
                ).format(namespace),
                updates,
            )
            return int(cursor.rowcount)

    def delete_file_chunks(self, file_id: int) -> None:
        namespace = sql.Identifier(self._schema)
        with self._conn.cursor() as cursor:
            cursor.execute(
                sql.SQL("DELETE FROM {}.chunks WHERE file_id = %s").format(namespace),
                (file_id,),
            )

    def insert_chunk(
        self,
        file_id: int,
        chunk_index: int,
        page_or_slide: int | None,
        text: str,
        embedding: Sequence[float],
    ) -> None:
        namespace = sql.Identifier(self._schema)
        blob = embedding_to_bytes(embedding, self.embedding_dim)
        with self._conn.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                    INSERT INTO {}.chunks
                        (file_id, chunk_index, page_or_slide, text, embedding)
                    VALUES (%s, %s, %s, %s, %s)
                    """
                ).format(namespace),
                (file_id, chunk_index, page_or_slide, text, blob),
            )

    def delete_files(self, sharepoint_paths: Sequence[str]) -> int:
        if not sharepoint_paths:
            return 0
        namespace = sql.Identifier(self._schema)
        with self._conn.cursor() as cursor:
            cursor.execute(
                sql.SQL("DELETE FROM {}.files WHERE sharepoint_path = ANY(%s)").format(
                    namespace
                ),
                (list(sharepoint_paths),),
            )
            return int(cursor.rowcount)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def publish_revision(self) -> int:
        namespace = sql.Identifier(self._schema)
        try:
            with self._conn.cursor() as cursor:
                cursor.execute(
                    sql.SQL(
                        """
                        INSERT INTO {}.index_state (singleton, revision, updated_at)
                        VALUES (TRUE, 1, NOW())
                        ON CONFLICT (singleton) DO UPDATE
                        SET revision = index_state.revision + 1, updated_at = NOW()
                        RETURNING revision
                        """
                    ).format(namespace)
                )
                revision = int(cursor.fetchone()[0])
            self._conn.commit()
            return revision
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        if self._has_run_lock:
            try:
                with self._conn.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", (_WRITER_LOCK_ID,))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
        self._conn.close()
