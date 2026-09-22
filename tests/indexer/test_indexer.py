from unittest.mock import MagicMock

import pytest

from indexer.document_metadata import DocumentMetadata
from indexer.graph_client import GraphFile
from indexer.indexer import (
    backfill_document_metadata,
    build_index_plan,
    index_file,
    load_indexer_environment,
)


def test_index_file_skips_already_indexed():
    gf = GraphFile("/local/a.pptx", "a.pptx", "pptx", 100, "2026-01-01T00:00:00Z",
                   "/f/a.pptx", "https://sap/x?id=/f/a.pptx")
    db = MagicMock()
    db.is_indexed.return_value = True
    embedder = MagicMock()

    result = index_file(
        gf, db, embedder, chunk_size=500, overlap=50, min_text_characters=1
    )

    assert result == "skipped"
    db.upsert_file.assert_not_called()


def test_index_file_processes_new_file(monkeypatch):
    gf = GraphFile("/local/a.pptx", "a.pptx", "pptx", 100, "2026-01-01T00:00:00Z",
                   "/f/a.pptx", "https://sap/x?id=/f/a.pptx")
    db = MagicMock()
    db.is_indexed.return_value = False
    db.upsert_file.return_value = 42
    embedder = MagicMock()
    embedder.embed_batch.side_effect = [
        [[0.1] * 384],
        [[0.2] * 384],
    ]

    # extract_text/chunk_text patchen wir über das Modul
    import indexer.indexer as mod
    extract = MagicMock(return_value=[(1, "text slide eins"), (2, "text slide zwei")])
    metadata = DocumentMetadata(
        author="Ada",
        created_at="2024-01-02T03:04:05",
        last_modified_by="Grace",
        modified_at="2025-02-03T04:05:06",
    )
    monkeypatch.setattr(mod, "extract_text", extract)
    monkeypatch.setattr(mod, "chunk_text", MagicMock(side_effect=lambda t, **kw: [t]))
    monkeypatch.setattr(mod, "extract_document_metadata", MagicMock(return_value=metadata))

    result = index_file(
        gf, db, embedder, chunk_size=500, overlap=50, min_text_characters=1
    )

    assert result == "indexed"
    # extract_text wird mit dem lokalen Pfad (item_id) aufgerufen
    extract.assert_called_once_with("/local/a.pptx")
    # web_url wird an upsert_file durchgereicht
    _, kwargs = db.upsert_file.call_args
    args = db.upsert_file.call_args.args
    assert "https://sap/x?id=/f/a.pptx" in list(args) + list(kwargs.values())
    assert kwargs["document_author"] == "Ada"
    assert kwargs["document_created_at"] == "2024-01-02T03:04:05"
    assert kwargs["document_last_modified_by"] == "Grace"
    assert kwargs["document_modified_at"] == "2025-02-03T04:05:06"
    assert kwargs["metadata_scanned_at"].endswith("+00:00")
    db.delete_file_chunks.assert_called_once_with(42)
    assert db.insert_chunk.call_count == 2
    db.commit.assert_called_once()


def test_index_file_recovers_from_db_error(monkeypatch):
    # Ein fehlgeschlagener insert_chunk darf den Lauf NICHT killen: die Datei
    # wird als 'error' markiert und die Transaktion zurückgesetzt, damit die
    # Verbindung fuer die naechste Datei nutzbar bleibt.
    gf = GraphFile("/local/a.pptx", "a.pptx", "pptx", 100, "2026-01-01T00:00:00Z",
                   "/f/a.pptx", "https://sap/x?id=/f/a.pptx")
    db = MagicMock()
    db.is_indexed.return_value = False
    db.upsert_file.return_value = 42
    db.insert_chunk.side_effect = Exception("insert boom")
    embedder = MagicMock()
    embedder.embed_batch.return_value = [[0.1] * 384]

    import indexer.indexer as mod
    monkeypatch.setattr(mod, "extract_text", MagicMock(return_value=[(1, "text")]))
    monkeypatch.setattr(
        mod, "chunk_text", MagicMock(side_effect=lambda t, **kw: [t])
    )

    result = index_file(
        gf, db, embedder, chunk_size=500, overlap=50, min_text_characters=1
    )

    assert result == "error"
    db.rollback.assert_called_once()
    db.commit.assert_not_called()


def test_index_file_recovers_from_upsert_error(monkeypatch):
    gf = GraphFile("/local/a.pptx", "a.pptx", "pptx", 100, "2026-01-01T00:00:00Z",
                   "/f/a.pptx", "https://sap/x?id=/f/a.pptx")
    db = MagicMock()
    db.is_indexed.return_value = False
    db.upsert_file.side_effect = Exception("upsert boom")
    embedder = MagicMock()
    embedder.embed_batch.return_value = [[0.1] * 384]

    import indexer.indexer as mod
    monkeypatch.setattr(mod, "extract_text", MagicMock(return_value=[(1, "text")]))
    monkeypatch.setattr(
        mod, "chunk_text", MagicMock(side_effect=lambda t, **kw: [t])
    )

    result = index_file(
        gf, db, embedder, chunk_size=500, overlap=50, min_text_characters=1
    )

    assert result == "error"
    db.rollback.assert_called_once()
    db.commit.assert_not_called()


def test_build_index_plan_classifies_all_states():
    files = [
        GraphFile("/local/a.pptx", "a.pptx", "pptx", 1, "v2", "/f/a.pptx", "https://a"),
        GraphFile("/local/b.pdf", "b.pdf", "pdf", 1, "v1", "/f/b.pdf", "https://b"),
        GraphFile("/local/c.docx", "c.docx", "docx", 1, "v1", "/f/c.docx", "https://c"),
    ]
    plan = build_index_plan(
        files,
        {"/f/a.pptx": "v1", "/f/b.pdf": "v1", "/f/deleted.pdf": "v1"},
    )

    assert plan.new == ("/f/c.docx",)
    assert plan.changed == ("/f/a.pptx",)
    assert plan.unchanged == ("/f/b.pdf",)
    assert plan.missing == ("/f/deleted.pdf",)


def test_build_index_plan_rejects_duplicate_sharepoint_paths():
    files = [
        GraphFile("/one/a.pptx", "a.pptx", "pptx", 1, "v1", "/f/a.pptx"),
        GraphFile("/two/a.pptx", "a.pptx", "pptx", 1, "v1", "/f/a.pptx"),
    ]

    with pytest.raises(ValueError, match="same SharePoint path"):
        build_index_plan(files, {})


def test_index_file_rolls_back_if_embedding_count_is_wrong(monkeypatch):
    gf = GraphFile(
        "/local/a.pptx", "a.pptx", "pptx", 100, "v1", "/f/a.pptx"
    )
    db = MagicMock()
    db.is_indexed.return_value = False
    db.upsert_file.return_value = 42
    embedder = MagicMock()
    embedder.embed_batch.return_value = []

    import indexer.indexer as mod

    monkeypatch.setattr(mod, "extract_text", MagicMock(return_value=[(1, "text")]))
    monkeypatch.setattr(mod, "chunk_text", MagicMock(return_value=["text"]))

    assert (
        index_file(
            gf,
            db,
            embedder,
            chunk_size=500,
            overlap=50,
            min_text_characters=1,
        )
        == "error"
    )
    db.rollback.assert_not_called()
    db.upsert_file.assert_not_called()
    db.insert_chunk.assert_not_called()


def test_load_environment_rejects_conflicting_source_mapping(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("LOCAL_ROOT=from-file\n", encoding="utf-8")
    monkeypatch.setenv("INDEXER_ENV_FILE", str(env_file))
    monkeypatch.setenv("LOCAL_ROOT", "from-shell")

    with pytest.raises(RuntimeError, match="LOCAL_ROOT"):
        load_indexer_environment()


def test_metadata_failure_does_not_block_content_indexing(monkeypatch):
    gf = GraphFile(
        "/local/a.docx", "a.docx", "docx", 100, "v1",
        "/scope/a.docx", "https://sap/x"
    )
    db = MagicMock()
    db.is_indexed.return_value = False
    db.upsert_file.return_value = 42
    embedder = MagicMock()
    embedder.embed_batch.return_value = [[0.1] * 384]

    import indexer.indexer as mod
    monkeypatch.setattr(mod, "extract_text", MagicMock(return_value=[(1, "text")]))
    monkeypatch.setattr(mod, "chunk_text", MagicMock(return_value=["text"]))
    monkeypatch.setattr(
        mod, "extract_document_metadata", MagicMock(side_effect=ValueError("bad core.xml"))
    )

    assert (
        index_file(
            gf,
            db,
            embedder,
            chunk_size=500,
            overlap=50,
            min_text_characters=1,
        )
        == "indexed"
    )
    kwargs = db.upsert_file.call_args.kwargs
    assert kwargs["document_author"] is None
    assert kwargs["metadata_scanned_at"] is None
    db.commit.assert_called_once()
    db.rollback.assert_not_called()


def test_zero_byte_file_is_recorded_without_opening_or_embedding(monkeypatch):
    gf = GraphFile(
        "/local/empty.docx",
        "empty.docx",
        "docx",
        0,
        "v1",
        "/scope/empty.docx",
        "https://sap.example/empty.docx",
    )
    db = MagicMock()
    db.is_indexed.return_value = False
    db.upsert_file.return_value = 42
    embedder = MagicMock()

    import indexer.indexer as mod

    extract = MagicMock()
    monkeypatch.setattr(mod, "extract_text", extract)

    result = index_file(gf, db, embedder, 500, 50, min_text_characters=20)

    assert result == "empty"
    extract.assert_not_called()
    embedder.embed_batch.assert_not_called()
    db.delete_file_chunks.assert_called_once_with(42)
    assert db.upsert_file.call_args.kwargs["metadata_scanned_at"] is not None
    db.commit.assert_called_once_with()


def test_short_extracted_text_replaces_stale_chunks_without_embedding(monkeypatch):
    gf = GraphFile(
        "/local/short.pdf",
        "short.pdf",
        "pdf",
        100,
        "v2",
        "/scope/short.pdf",
        "https://sap.example/short.pdf",
    )
    db = MagicMock()
    db.is_indexed.return_value = False
    db.upsert_file.return_value = 7
    embedder = MagicMock()

    import indexer.indexer as mod

    monkeypatch.setattr(mod, "extract_text", MagicMock(return_value=[(1, "A - B")]))

    result = index_file(gf, db, embedder, 500, 50, min_text_characters=3)

    assert result == "empty"
    embedder.embed_batch.assert_not_called()
    db.delete_file_chunks.assert_called_once_with(7)
    db.commit.assert_called_once_with()


def test_index_file_rejects_invalid_minimum_text_configuration():
    gf = GraphFile("/local/a.pdf", "a.pdf", "pdf", 1, "v1", "/scope/a.pdf")

    with pytest.raises(ValueError, match="at least 1"):
        index_file(gf, MagicMock(), MagicMock(), 500, 50, min_text_characters=0)


def test_backfill_metadata_does_not_rebuild_embeddings(monkeypatch):
    files = [
        GraphFile(
            "/local/a.pptx", "a.pptx", "pptx", 100, "v1",
            "/scope/a.pptx", "https://sap/a"
        ),
        GraphFile(
            "/local/b.docx", "b.docx", "docx", 100, "v1",
            "/scope/b.docx", "https://sap/b"
        ),
    ]
    db = MagicMock()
    db.get_metadata_pending_paths.return_value = {
        "/scope/a.pptx", "/scope/b.docx"
    }

    import indexer.indexer as mod

    def extract(path):
        if path.endswith("b.docx"):
            raise ValueError("corrupt metadata")
        return DocumentMetadata(author="Ada")

    monkeypatch.setattr(mod, "extract_document_metadata", extract)

    updated, errors = backfill_document_metadata(files, db, "/scope")

    assert (updated, errors) == (1, 1)
    updates = db.update_file_metadata.call_args.args[0]
    assert len(updates) == 1
    assert updates[0][0] == "Ada"
    assert updates[0][-1] == "/scope/a.pptx"
    db.commit.assert_called_once()
    db.rollback.assert_not_called()


def test_noop_apply_still_publishes_revision_for_recovery(monkeypatch, tmp_path):
    gf = GraphFile(
        str(tmp_path / "a.pdf"),
        "a.pdf",
        "pdf",
        100,
        "v1",
        "/scope/a.pdf",
        "https://sap.example/a.pdf",
    )
    db = MagicMock()
    db.acquire_run_lock.return_value = True
    db.get_indexed_versions.return_value = {"/scope/a.pdf": "v1"}
    db.get_metadata_pending_paths.return_value = set()
    db.publish_revision.return_value = 7

    import indexer.indexer as mod

    monkeypatch.setattr(mod, "load_indexer_environment", MagicMock())
    monkeypatch.setattr(mod.os.path, "isdir", MagicMock(return_value=True))
    monkeypatch.setattr(mod, "list_local_files", MagicMock(return_value=[gf]))
    monkeypatch.setattr(mod, "create_database", MagicMock(return_value=db))
    monkeypatch.setenv("LOCAL_ROOT", str(tmp_path))
    monkeypatch.setenv("LOCAL_PREFIX", str(tmp_path))
    monkeypatch.setenv("SHAREPOINT_PREFIX", "/scope")
    monkeypatch.setenv("SITE_URL", "https://sap.example/site")

    assert mod.main(["--apply"]) == 0
    db.publish_revision.assert_called_once_with()
    db.close.assert_called_once_with()
