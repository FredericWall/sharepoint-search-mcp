"""Liefert ein sqlite3-Modul MIT Loadable-Extension-Support.

Hintergrund: Der Python-Build des CF-Buildpacks ist ohne
``--enable-loadable-sqlite-extensions`` kompiliert — dort fehlt
``Connection.enable_load_extension``, das sqlite-vec zwingend braucht.
``pysqlite3-binary`` bringt ein eigenes, vollständiges SQLite mit und wird
bevorzugt. Lokal (Windows/normaler Python) hat das eingebaute ``sqlite3``
den Support bereits, daher der Fallback.
"""

from __future__ import annotations

try:  # pragma: no cover - hängt von der Umgebung ab
    import pysqlite3 as sqlite3  # type: ignore
except ImportError:  # pragma: no cover
    import sqlite3  # type: ignore

__all__ = ["sqlite3"]
