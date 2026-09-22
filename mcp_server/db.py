"""Read-only Query-Layer für den MCP-Server gegen sqlite-vec."""

from __future__ import annotations

import math
import os
import struct
from threading import RLock
from urllib.parse import quote

import sqlite_vec

from mcp_server.sqlite_compat import sqlite3

_SP_TYPE_CHAR = {".pptx": "p", ".docx": "w", ".pdf": "b"}


def _fix_web_url(web_url: str | None, sharepoint_path: str | None) -> str | None:
    """Konvertiert alte AllItems.aspx-URLs ins direkte /:X:/r/-Format.

    Alte URLs (aus älteren Re-Indizes) öffneten nur den Ordner, nicht die Datei.
    Das neue Format öffnet die Datei direkt, ohne GUID.
    """
    if not web_url or not sharepoint_path:
        return web_url
    if "AllItems.aspx" not in web_url:
        return web_url  # bereits neues Format, nichts tun
    # Host aus der alten URL extrahieren (alles vor /teams/)
    host = web_url.split("/teams/")[0] if "/teams/" in web_url else None
    if not host:
        return web_url
    ext = os.path.splitext(sharepoint_path)[1].lower()
    type_char = _SP_TYPE_CHAR.get(ext, "r")
    return f"{host}/:{type_char}:/r{quote(sharepoint_path)}?csf=1&web=1"


class QueryDatabase:
    """Führt Such- und Metadaten-Queries gegen die Index-Datei aus."""

    def __init__(self, db_path: str):
        # Read-only öffnen (die Datei wird nur gelesen, nie geschrieben).
        self._conn = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True, check_same_thread=False
        )
        self._lock = RLock()
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)
        cursor = self._conn.cursor()
        cursor.execute("PRAGMA table_info(files)")
        available = {str(row[1]) for row in cursor.fetchall()}
        self._has_document_metadata = {
            "document_author",
            "document_created_at",
            "document_last_modified_by",
            "document_modified_at",
        }.issubset(available)

    def _metadata_projection(self, alias: str = "") -> str:
        prefix = f"{alias}." if alias else ""
        if self._has_document_metadata:
            return ", ".join(
                f"{prefix}{column}"
                for column in (
                    "document_author",
                    "document_created_at",
                    "document_last_modified_by",
                    "document_modified_at",
                )
            )
        return ", ".join(
            f"NULL AS {column}"
            for column in (
                "document_author",
                "document_created_at",
                "document_last_modified_by",
                "document_modified_at",
            )
        )

    def search(self, query_embedding: list[float], top_k: int = 5) -> list[dict]:
        """Cosine-Similarity-Suche über alle Chunks."""
        if len(query_embedding) != 384:
            raise ValueError(
                f"Query has dimension {len(query_embedding)}; expected 384"
            )
        if not all(math.isfinite(value) for value in query_embedding):
            raise ValueError("Query embedding contains a non-finite value")
        if top_k <= 0:
            return []
        metadata_projection = self._metadata_projection("f")
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                f"""
                SELECT ch.text, ch.page_or_slide, f.filename, f.sharepoint_path, f.web_url,
                       1 - v.distance AS score, {metadata_projection}
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
            {"text": r[0], "page_or_slide": r[1], "filename": r[2],
             "sharepoint_path": r[3],
             "web_url": _fix_web_url(r[4], r[3]),
             "score": float(r[5]), "document_author": r[6],
             "document_created_at": r[7], "document_last_modified_by": r[8],
             "document_modified_at": r[9]}
            for r in rows
        ]

    def get_document_content(self, sharepoint_path: str) -> list[dict]:
        """Gibt alle Chunks einer Datei geordnet nach Position zurück."""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT ch.chunk_index, ch.page_or_slide, ch.text
                FROM chunks ch JOIN files f ON f.id = ch.file_id
                WHERE f.sharepoint_path = ?
                ORDER BY ch.chunk_index
                """,
                (sharepoint_path,),
            )
            rows = cur.fetchall()
        return [{"chunk_index": r[0], "page_or_slide": r[1], "text": r[2]} for r in rows]

    def list_files(
        self,
        folder_path: str | None = None,
        file_type: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Listet Datei-Metadaten, optional gefiltert nach Ordner und Typ."""
        if limit is not None and limit <= 0:
            return []
        # Files intentionally recorded with zero chunks (for example empty Office
        # documents) are source-state markers, not searchable MCP results.
        clauses = [
            "EXISTS (SELECT 1 FROM chunks indexed_chunk "
            "WHERE indexed_chunk.file_id = files.id)"
        ]
        params: list = []
        if folder_path:
            clauses.append("instr(sharepoint_path, ?) > 0")
            params.append(folder_path)
        if file_type:
            clauses.append("file_type = ?")
            params.append(file_type)
        where = " WHERE " + " AND ".join(clauses)
        metadata_projection = self._metadata_projection()
        limit_clause = " LIMIT ?" if limit is not None else ""
        if limit is not None:
            params.append(limit)
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                f"""
                SELECT filename, sharepoint_path, file_type, size_bytes, modified_at, web_url,
                       {metadata_projection}
                FROM files{where}
                ORDER BY filename{limit_clause}
                """,
                params,
            )
            rows = cur.fetchall()
        return [
            {"filename": r[0], "sharepoint_path": r[1], "file_type": r[2],
             "size_bytes": r[3], "modified_at": str(r[4]) if r[4] else None,
             "web_url": _fix_web_url(r[5], r[1]), "document_author": r[6],
             "document_created_at": r[7], "document_last_modified_by": r[8],
             "document_modified_at": r[9]}
            for r in rows
        ]

    def get_file_metadata(self, sharepoint_path: str) -> dict | None:
        metadata_projection = self._metadata_projection()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                f"""
                SELECT filename, sharepoint_path, file_type, size_bytes, modified_at, web_url,
                       {metadata_projection}
                FROM files WHERE sharepoint_path = ?
                """,
                (sharepoint_path,),
            )
            row = cur.fetchone()
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
        with self._lock:
            self._conn.close()


def _to_blob(vec: list[float]) -> bytes:
    """Float-Liste -> sqlite-vec Blob (little-endian float32)."""
    return struct.pack(f"{len(vec)}f", *vec)
