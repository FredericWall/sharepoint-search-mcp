"""Orchestriert die komplette Index-Pipeline. Als Script ausführbar.

Quelle ist ein lokaler Ordner (z.B. der synchronisierte OneDrive-/SharePoint-
Ordner). Der Microsoft-Graph-Weg (indexer/graph_client.py) bleibt als
Alternative erhalten, wird hier aber nicht verwendet.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from dataclasses import dataclass

from dotenv import dotenv_values, load_dotenv

from indexer.chunker import chunk_text
from indexer.db import Database
from indexer.document_metadata import DocumentMetadata, extract_document_metadata
from indexer.embeddings import Embedder
from indexer.extractor import extract_text
from indexer.graph_client import GraphFile
from indexer.local_source import list_local_files


@dataclass(frozen=True)
class IndexPlan:
    new: tuple[str, ...]
    changed: tuple[str, ...]
    unchanged: tuple[str, ...]
    missing: tuple[str, ...]


_SOURCE_MAPPING_KEYS = (
    "LOCAL_ROOT",
    "LOCAL_PREFIX",
    "SHAREPOINT_PREFIX",
    "SITE_URL",
)


def load_indexer_environment() -> None:
    """Load indexer/.env and reject ambiguous source mappings.

    Existing process variables normally win over python-dotenv values. For source
    paths that is dangerous because a stale shell can silently write incorrect
    SharePoint links. Equal duplicate values are accepted; differing values fail.
    An empty INDEXER_ENV_FILE explicitly disables dotenv loading for isolated tests.
    """
    env = os.environ
    configured_path = env.get("INDEXER_ENV_FILE")
    if configured_path == "":
        return
    env_path = configured_path or os.path.join(os.path.dirname(__file__), ".env")
    values = dotenv_values(env_path)
    conflicts = [
        key
        for key in _SOURCE_MAPPING_KEYS
        if env.get(key) is not None
        and values.get(key) is not None
        and env[key] != values[key]
    ]
    if conflicts:
        raise RuntimeError(
            "Conflicting source mapping variables are already set in the process: "
            + ", ".join(conflicts)
            + ". Start from a clean shell or make them match indexer/.env."
        )
    load_dotenv(dotenv_path=env_path)


def build_index_plan(
    files: list[GraphFile], indexed_versions: dict[str, str | None]
) -> IndexPlan:
    source = {item.sharepoint_path: item.modified_at for item in files}
    if len(source) != len(files):
        raise ValueError(
            "The local inventory maps multiple files to the same SharePoint path"
        )
    source_paths = set(source)
    indexed_paths = set(indexed_versions)
    shared = source_paths & indexed_paths
    return IndexPlan(
        new=tuple(sorted(source_paths - indexed_paths)),
        changed=tuple(
            sorted(path for path in shared if source[path] != indexed_versions[path])
        ),
        unchanged=tuple(
            sorted(path for path in shared if source[path] == indexed_versions[path])
        ),
        missing=tuple(sorted(indexed_paths - source_paths)),
    )


def create_database(environment: dict[str, str]):
    backend = environment.get("INDEX_BACKEND", "sqlite").strip().lower()
    embedding_dim = int(environment.get("EMBEDDING_DIM", "384"))
    if backend == "sqlite":
        return Database(
            environment.get("INDEX_DB_PATH", "mcp_server/index.db"),
            embedding_dim=embedding_dim,
        )
    if backend == "postgres":
        from indexer.postgres_db import PostgresDatabase

        use_libpq_environment = environment.get("INDEX_USE_LIBPQ_ENV") == "1"
        database_url = "" if use_libpq_environment else (
            environment.get("INDEX_DATABASE_URL") or environment.get("DATABASE_URL", "")
        )
        if not database_url and not environment.get("PGHOST"):
            raise RuntimeError(
                "PostgreSQL indexing requires INDEX_DATABASE_URL, DATABASE_URL, "
                "or the standard PGHOST/PGUSER/PGPASSWORD/PGDATABASE variables"
            )
        return PostgresDatabase(
            database_url,
            schema=environment.get("POSTGRES_SCHEMA", "sharepoint_mcp"),
            embedding_dim=embedding_dim,
        )
    raise ValueError(f"Unsupported INDEX_BACKEND: {backend!r}")


def index_file(
    gf: GraphFile,
    db: Database,
    embedder: Embedder,
    chunk_size: int,
    overlap: int,
    min_text_characters: int = 20,
) -> str:
    """Indexiert eine einzelne lokale Datei.

    ``gf.item_id`` enthält den lokalen Dateipfad. Gibt 'skipped', 'indexed',
    'empty' oder 'error' zurück.
    """
    if min_text_characters < 1:
        raise ValueError("min_text_characters must be at least 1")
    if db.is_indexed(gf.sharepoint_path, gf.modified_at):
        return "skipped"

    try:
        # Avoid asking Office/PDF parsers to open a known zero-byte placeholder.
        pages = [] if gf.size_bytes == 0 else extract_text(gf.item_id)
        meaningful_characters = sum(
            character.isalnum()
            for _, page_text in pages
            for character in page_text
        )
        if meaningful_characters < min_text_characters:
            metadata_scanned_at = (
                dt.datetime.now(dt.UTC).isoformat()
                if gf.file_type in {"pptx", "docx"}
                else None
            )
            try:
                file_id = db.upsert_file(
                    gf.sharepoint_path,
                    gf.name,
                    gf.file_type,
                    gf.size_bytes,
                    gf.modified_at,
                    gf.web_url,
                    document_author=None,
                    document_created_at=None,
                    document_last_modified_by=None,
                    document_modified_at=None,
                    metadata_scanned_at=metadata_scanned_at,
                )
                # A changed file may previously have had searchable content.
                db.delete_file_chunks(file_id)
                db.commit()
            except Exception as exc:  # noqa: BLE001 - continue with later files
                db.rollback()
                print(f"  FEHLER (DB) bei {gf.name}: {exc}")
                return "error"
            print(
                f"  ÜBERSPRUNGEN (zu wenig Text) bei {gf.name}: "
                f"{meaningful_characters}/{min_text_characters} Zeichen"
            )
            return "empty"

        prepared_chunks: list[tuple[int, int, str, list[float]]] = []
        chunk_index = 0
        for page_or_slide, page_text in pages:
            text_chunks = chunk_text(
                page_text, chunk_size_tokens=chunk_size, overlap_tokens=overlap
            )
            if not text_chunks:
                continue
            embeddings = embedder.embed_batch(text_chunks)
            if len(embeddings) != len(text_chunks):
                raise ValueError(
                    "Embedding provider returned a different number of vectors "
                    "than input chunks"
                )
            for text, embedding in zip(text_chunks, embeddings, strict=True):
                prepared_chunks.append(
                    (chunk_index, page_or_slide, text, embedding)
                )
                chunk_index += 1
    except Exception as exc:  # noqa: BLE001 - wir loggen und fahren fort
        print(f"  FEHLER (Extraktion/Embedding) bei {gf.name}: {exc}")
        return "error"

    metadata, metadata_scanned_at = _read_document_metadata(gf)

    try:
        file_id = db.upsert_file(
            gf.sharepoint_path,
            gf.name,
            gf.file_type,
            gf.size_bytes,
            gf.modified_at,
            gf.web_url,
            document_author=metadata.author,
            document_created_at=metadata.created_at,
            document_last_modified_by=metadata.last_modified_by,
            document_modified_at=metadata.modified_at,
            metadata_scanned_at=metadata_scanned_at,
        )
        db.delete_file_chunks(file_id)  # bei Re-Index alte Chunks entfernen
        for chunk_index, page_or_slide, text, embedding in prepared_chunks:
            db.insert_chunk(
                file_id, chunk_index, page_or_slide, text, embedding
            )
        db.commit()
    except Exception as exc:  # noqa: BLE001 - eine fehlerhafte Datei darf den Lauf nicht killen
        # Transaktion zuruecksetzen, sonst werden alle folgenden commits zu
        # Rollbacks (aborted-transaction-Kaskade) und nichts wird gespeichert.
        db.rollback()
        print(f"  FEHLER (DB) bei {gf.name}: {exc}")
        return "error"

    return "indexed"


def _read_document_metadata(
    gf: GraphFile,
) -> tuple[DocumentMetadata, str | None]:
    if gf.file_type not in {"pptx", "docx"}:
        return DocumentMetadata(), None
    try:
        metadata = extract_document_metadata(gf.item_id)
    except Exception as exc:  # noqa: BLE001 - metadata is optional
        print(f"  WARNUNG (Metadaten) bei {gf.name}: {exc}")
        return DocumentMetadata(), None
    scanned_at = dt.datetime.now(dt.UTC).isoformat()
    return metadata, scanned_at


def backfill_document_metadata(
    files: list[GraphFile], db, sharepoint_prefix: str
) -> tuple[int, int]:
    """Populate missing Office metadata without rebuilding chunks or embeddings."""
    pending = db.get_metadata_pending_paths(sharepoint_prefix)
    if not pending:
        return 0, 0

    by_path = {item.sharepoint_path: item for item in files}
    updates = []
    errors = 0
    for path in sorted(pending):
        gf = by_path.get(path)
        if gf is None:
            continue
        metadata, scanned_at = _read_document_metadata(gf)
        if scanned_at is None:
            errors += 1
            continue
        updates.append(
            (
                metadata.author,
                metadata.created_at,
                metadata.last_modified_by,
                metadata.modified_at,
                scanned_at,
                path,
            )
        )

    if not updates:
        return 0, errors
    try:
        db.update_file_metadata(updates)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - content updates must remain committed
        db.rollback()
        print(f"  FEHLER beim Metadaten-Backfill: {exc}")
        return 0, errors + len(updates)
    return len(updates), errors


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Index a locally synced SharePoint folder")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write changes; without this flag the command only prints a plan",
    )
    parser.add_argument(
        "--reconcile-deletions",
        action="store_true",
        help="delete database files that are absent from the local source",
    )
    parser.add_argument(
        "--allow-large-deletion",
        action="store_true",
        help="allow deletion of more than 10 percent (or 20) of the scoped index",
    )
    args = parser.parse_args(argv)
    if args.reconcile_deletions and not args.apply:
        parser.error("--reconcile-deletions requires --apply")
    return args


def _print_plan(plan: IndexPlan) -> None:
    print(
        "Plan: "
        f"new={len(plan.new)}, changed={len(plan.changed)}, "
        f"unchanged={len(plan.unchanged)}, missing={len(plan.missing)}"
    )
    groups = (("new", plan.new), ("changed", plan.changed), ("missing", plan.missing))
    for label, paths in groups:
        for path in paths[:10]:
            print(f"  {label}: {path}")
        if len(paths) > 10:
            print(f"  {label}: ... and {len(paths) - 10} more")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_indexer_environment()

    local_root = os.environ["LOCAL_ROOT"]
    local_prefix = os.environ.get("LOCAL_PREFIX", local_root)
    sharepoint_prefix = os.environ["SHAREPOINT_PREFIX"]
    site_url = os.environ["SITE_URL"]
    embedding_provider = os.environ.get("EMBEDDING_PROVIDER", "local")
    embedding_model = os.environ.get("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    chunk_size = int(os.environ.get("CHUNK_SIZE_TOKENS", "500"))
    overlap = int(os.environ.get("CHUNK_OVERLAP_TOKENS", "50"))
    min_text_characters = int(os.environ.get("MIN_TEXT_CHARACTERS", "20"))
    embedding_dim = int(os.environ.get("EMBEDDING_DIM", "384"))
    if embedding_dim != 384:
        raise RuntimeError(
            f"EMBEDDING_DIM must remain 384 for the configured model; got {embedding_dim}"
        )
    if chunk_size <= 0 or overlap < 0 or overlap >= chunk_size:
        raise RuntimeError(
            "CHUNK_SIZE_TOKENS must be positive and CHUNK_OVERLAP_TOKENS must "
            "be between 0 and chunk size - 1"
        )
    if not 1 <= min_text_characters <= 100_000:
        raise RuntimeError("MIN_TEXT_CHARACTERS must be between 1 and 100000")
    exclude_dirs = [
        d.strip()
        for d in os.environ.get("EXCLUDE_DIRS", "99_Archive_Jun2nd").split(",")
        if d.strip()
    ]

    print(f"Liste Dateien im lokalen Ordner auf: {local_root}")
    if not os.path.isdir(local_root):
        raise RuntimeError(f"LOCAL_ROOT does not exist or is not a directory: {local_root}")
    files = list_local_files(
        local_root, local_prefix, sharepoint_prefix, site_url, exclude_dirs=exclude_dirs
    )
    print(f"Gefunden: {len(files)} unterstützte Dateien (ausgeschlossen: {', '.join(exclude_dirs) or '—'})")
    if not files:
        raise RuntimeError("Local inventory is empty; refusing to continue")

    print("Verbinde mit Datenbank...")
    db = create_database(os.environ)

    try:
        if args.apply:
            if not db.acquire_run_lock():
                print("Ein anderer Index-Lauf ist bereits aktiv.")
                return 2
            db.init_schema()
        else:
            db.verify_schema()

        indexed_versions = db.get_indexed_versions(sharepoint_prefix)
        plan = build_index_plan(files, indexed_versions)
        _print_plan(plan)
        if not args.apply:
            print("Nur Planung; es wurden keine Daten geändert. Für Änderungen --apply verwenden.")
            return 0

        if args.reconcile_deletions and plan.missing and not args.allow_large_deletion:
            deletion_limit = max(20, int(len(indexed_versions) * 0.10))
            if len(plan.missing) > deletion_limit:
                print(
                    f"Abbruch: {len(plan.missing)} Löschungen überschreiten das "
                    f"Sicherheitslimit von {deletion_limit}."
                )
                print("Lokale Synchronisierung prüfen oder bewusst --allow-large-deletion setzen.")
                return 2

        candidates = set(plan.new) | set(plan.changed)
        counts = {
            "skipped": len(plan.unchanged),
            "indexed": 0,
            "empty": 0,
            "error": 0,
        }
        if candidates:
            print("Lade Embedding-Modell...")
            embedder = Embedder(provider=embedding_provider, model=embedding_model)
            changed_files = [item for item in files if item.sharepoint_path in candidates]
            for i, gf in enumerate(changed_files, start=1):
                print(f"[{i}/{len(changed_files)}] {gf.name}")
                result = index_file(
                    gf,
                    db,
                    embedder,
                    chunk_size,
                    overlap,
                    min_text_characters,
                )
                counts[result] += 1

        deleted = 0
        if args.reconcile_deletions and plan.missing:
            try:
                deleted = db.delete_files(list(plan.missing))
                db.commit()
            except Exception:
                db.rollback()
                raise

        metadata_updated, metadata_errors = backfill_document_metadata(
            files, db, sharepoint_prefix
        )

        # Publish even for a no-op apply. If a previous run committed file changes but
        # failed while publishing its revision, the next apply must heal the stale MCP
        # cache even though all source timestamps now look unchanged.
        revision = db.publish_revision()
        revision_label = revision if revision is not None else "n/a"
        print(
            f"Fertig. Indexiert: {counts['indexed']}, "
            f"Übersprungen: {counts['skipped']}, "
            f"Ohne ausreichenden Text: {counts['empty']}, "
            f"Fehler: {counts['error']}, "
            f"Gelöscht: {deleted}, Metadaten aktualisiert: {metadata_updated}, "
            f"Metadatenfehler: {metadata_errors}, Revision: {revision_label}"
        )
        return 1 if counts["error"] else 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
