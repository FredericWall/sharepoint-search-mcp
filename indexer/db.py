"""SQLite/sqlite-vec Datenbank-Layer für die Index-Pipeline.

Ersetzt den früheren PostgreSQL/pgvector-Layer: der Index liegt jetzt als
einzelne Datei (embedded), damit der MCP-Server ohne separaten DB-Service auf
BTP Cloud Foundry deploybar ist (nur `cf push`). Die Vektorsuche übernimmt die
sqlite-vec-Extension (Cosine-Distanz).
"""

from __future__ import annotations

import math
import sqlite3
import struct
from collections.abc import Sequence

import sqlite_vec


class Database:
    """Kapselt Verbindung, Schema und Schreib-/Suchoperationen auf sqlite-vec."""

    def __init__(self, db_path: str, embedding_dim: int = 384):
        self.embedding_dim = embedding_dim
        self._conn = sqlite3.connect(db_path)
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)

    @property
    def backend_name(self) -> str:
        return "sqlite"

    def acquire_run_lock(self) -> bool:
        """SQLite is retained as a local fallback."""
        return True

    def init_schema(self) -> None:
        """Legt Tabellen und die Vektor-Tabelle an (idempotent)."""
        cur = self._conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sharepoint_path TEXT UNIQUE NOT NULL,
                filename TEXT NOT NULL,
                file_type TEXT NOT NULL,
                size_bytes INTEGER,
                modified_at TEXT,
                document_author TEXT,
                document_created_at TEXT,
                document_last_modified_by TEXT,
                document_modified_at TEXT,
                metadata_scanned_at TEXT,
                web_url TEXT,
                indexed_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER REFERENCES files(id) ON DELETE CASCADE,
                chunk_index INTEGER NOT NULL,
                page_or_slide INTEGER,
                text TEXT NOT NULL
            )
            """
        )
        # Vektor-Tabelle (sqlite-vec). chunk_id verknüpft 1:1 mit chunks.id.
        cur.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
                chunk_id INTEGER PRIMARY KEY,
                embedding float[{self.embedding_dim}] distance_metric=cosine
            )
            """
        )
        cur.execute("PRAGMA table_info(files)")
        existing_columns = {str(row[1]) for row in cur.fetchall()}
        for column in (
            "document_author",
            "document_created_at",
            "document_last_modified_by",
            "document_modified_at",
            "metadata_scanned_at",
        ):
            if column not in existing_columns:
                cur.execute(f"ALTER TABLE files ADD COLUMN {column} TEXT")
        self._conn.commit()

    def verify_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute("SELECT 1 FROM files LIMIT 0")
        cur.execute("SELECT 1 FROM chunks LIMIT 0")

    def get_indexed_versions(self, sharepoint_prefix: str) -> dict[str, str | None]:
        prefix = sharepoint_prefix.rstrip("/")
        folder_prefix = f"{prefix}/"
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT sharepoint_path, modified_at FROM files
            WHERE sharepoint_path = ?
               OR substr(sharepoint_path, 1, length(?)) = ?
            """,
            (prefix, folder_prefix, folder_prefix),
        )
        return {str(path): modified for path, modified in cur.fetchall()}

    def upsert_file(
        self, sharepoint_path: str, filename: str, file_type: str,
        size_bytes: int, modified_at: str, web_url: str = "",
        document_author: str | None = None,
        document_created_at: str | None = None,
        document_last_modified_by: str | None = None,
        document_modified_at: str | None = None,
        metadata_scanned_at: str | None = None,
    ) -> int:
        """Fügt eine Datei ein oder aktualisiert sie; gibt die file id zurück."""
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO files (
                sharepoint_path, filename, file_type, size_bytes, modified_at, web_url,
                document_author, document_created_at, document_last_modified_by,
                document_modified_at, metadata_scanned_at, indexed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT (sharepoint_path) DO UPDATE
            SET filename = excluded.filename,
                file_type = excluded.file_type,
                size_bytes = excluded.size_bytes,
                modified_at = excluded.modified_at,
                web_url = excluded.web_url,
                document_author = CASE WHEN excluded.metadata_scanned_at IS NOT NULL
                    THEN excluded.document_author ELSE files.document_author END,
                document_created_at = CASE WHEN excluded.metadata_scanned_at IS NOT NULL
                    THEN excluded.document_created_at ELSE files.document_created_at END,
                document_last_modified_by = CASE WHEN excluded.metadata_scanned_at IS NOT NULL
                    THEN excluded.document_last_modified_by ELSE files.document_last_modified_by END,
                document_modified_at = CASE WHEN excluded.metadata_scanned_at IS NOT NULL
                    THEN excluded.document_modified_at ELSE files.document_modified_at END,
                metadata_scanned_at = COALESCE(
                    excluded.metadata_scanned_at, files.metadata_scanned_at
                ),
                indexed_at = datetime('now')
            RETURNING id
            """,
            (
                sharepoint_path, filename, file_type, size_bytes, modified_at, web_url,
                document_author, document_created_at, document_last_modified_by,
                document_modified_at, metadata_scanned_at,
            ),
        )
        file_id = cur.fetchone()[0]
        return file_id

    def get_metadata_pending_paths(self, sharepoint_prefix: str) -> set[str]:
        prefix = sharepoint_prefix.rstrip("/")
        folder_prefix = f"{prefix}/"
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT sharepoint_path FROM files
            WHERE file_type IN ('pptx', 'docx')
              AND metadata_scanned_at IS NULL
              AND (
                  sharepoint_path = ?
                  OR substr(sharepoint_path, 1, length(?)) = ?
              )
            """,
            (prefix, folder_prefix, folder_prefix),
        )
        return {str(row[0]) for row in cur.fetchall()}

    def update_file_metadata(
        self,
        updates: Sequence[
            tuple[str | None, str | None, str | None, str | None, str, str]
        ],
    ) -> int:
        if not updates:
            return 0
        cur = self._conn.cursor()
        cur.executemany(
            """
            UPDATE files SET
                document_author = ?,
                document_created_at = ?,
                document_last_modified_by = ?,
                document_modified_at = ?,
                metadata_scanned_at = ?
            WHERE sharepoint_path = ?
            """,
            updates,
        )
        return int(cur.rowcount)

    def is_indexed(self, sharepoint_path: str, modified_at: str) -> bool:
        """True, wenn die Datei mit exakt diesem modified_at bereits indexiert ist."""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT modified_at FROM files WHERE sharepoint_path = ?",
            (sharepoint_path,),
        )
        row = cur.fetchone()
        if row is None:
            return False
        return row[0] == modified_at

    def delete_file_chunks(self, file_id: int) -> None:
        """Löscht alle Chunks einer Datei (für Re-Indexierung)."""
        cur = self._conn.cursor()
        # Erst die zugehörigen Vektoren löschen (kein CASCADE über die Virtual Table).
        cur.execute(
            "DELETE FROM vec_chunks WHERE chunk_id IN (SELECT id FROM chunks WHERE file_id = ?)",
            (file_id,),
        )
        cur.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))

    def insert_chunk(
        self, file_id: int, chunk_index: int, page_or_slide: int | None,
        text: str, embedding: list[float],
    ) -> None:
        """Fügt einen einzelnen Chunk ein (Text + Vektor)."""
        if len(embedding) != self.embedding_dim:
            raise ValueError(
                f"Embedding has dimension {len(embedding)}; "
                f"expected {self.embedding_dim}"
            )
        if not all(math.isfinite(value) for value in embedding):
            raise ValueError("Embedding contains a non-finite value")
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO chunks (file_id, chunk_index, page_or_slide, text)
            VALUES (?, ?, ?, ?)
            """,
            (file_id, chunk_index, page_or_slide, text),
        )
        chunk_id = cur.lastrowid
        cur.execute(
            "INSERT INTO vec_chunks (chunk_id, embedding) VALUES (?, ?)",
            (chunk_id, _to_blob(embedding)),
        )

    def delete_files(self, sharepoint_paths: list[str]) -> int:
        """Delete explicitly selected files and their sqlite-vec rows."""
        if not sharepoint_paths:
            return 0
        placeholders = ",".join("?" for _ in sharepoint_paths)
        cur = self._conn.cursor()
        cur.execute(
            f"SELECT id FROM files WHERE sharepoint_path IN ({placeholders})",
            sharepoint_paths,
        )
        file_ids = [row[0] for row in cur.fetchall()]
        for file_id in file_ids:
            self.delete_file_chunks(file_id)
        cur.execute(
            f"DELETE FROM files WHERE sharepoint_path IN ({placeholders})",
            sharepoint_paths,
        )
        return int(cur.rowcount)

    def commit(self) -> None:
        self._conn.commit()

    def publish_revision(self) -> None:
        """The deployed SQLite fallback cannot hot-reload its index."""
        self._conn.commit()
        return None

    def search(self, query_embedding: list[float], top_k: int = 5) -> list[dict]:
        """Cosine-Similarity-Suche; gibt Chunks mit Datei-Metadaten zurück."""
        if len(query_embedding) != self.embedding_dim:
            raise ValueError(
                f"Query has dimension {len(query_embedding)}; "
                f"expected {self.embedding_dim}"
            )
        if not all(math.isfinite(value) for value in query_embedding):
            raise ValueError("Query embedding contains a non-finite value")
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT ch.text, ch.page_or_slide, f.filename, f.sharepoint_path, f.web_url,
                   1 - v.distance AS score
            FROM (
                SELECT chunk_id, distance FROM vec_chunks
                WHERE embedding MATCH ? ORDER BY distance LIMIT ?
            ) v
            JOIN chunks ch ON ch.id = v.chunk_id
            JOIN files f ON f.id = ch.file_id
            ORDER BY v.distance
            """,
            (_to_blob(query_embedding), top_k),
        )
        rows = cur.fetchall()
        return [
            {
                "text": r[0],
                "page_or_slide": r[1],
                "filename": r[2],
                "sharepoint_path": r[3],
                "web_url": r[4],
                "score": float(r[5]),
            }
            for r in rows
        ]

    def rollback(self) -> None:
        """Setzt eine fehlgeschlagene Transaktion zurück, damit die Verbindung
        weiter nutzbar bleibt."""
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


def _to_blob(vec: list[float]) -> bytes:
    """Wandelt eine Float-Liste in das sqlite-vec Blob-Format (little-endian float32)."""
    return struct.pack(f"{len(vec)}f", *vec)
