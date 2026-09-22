import pytest

from indexer.db import Database
from mcp_server.db import QueryDatabase


@pytest.fixture
def seeded_db(tmp_index_db):
    # Mit dem Schreib-Layer seeden, dann read-only über QueryDatabase lesen.
    w = Database(tmp_index_db, embedding_dim=384)
    w.init_schema()
    fid = w.upsert_file("/f/analytics.pptx", "analytics.pptx", "pptx", 100,
                        "2026-01-01T00:00:00Z",
                        web_url="https://contoso.sharepoint.com/x?id=/f/analytics.pptx",
                        document_author="Ada Lovelace",
                        document_created_at="2024-01-02T03:04:05",
                        document_last_modified_by="Grace Hopper",
                        document_modified_at="2025-02-03T04:05:06",
                        metadata_scanned_at="2026-09-21T10:00:00+00:00")
    vec = [0.1] * 384
    w.insert_chunk(fid, 0, 1, "Analytics Architektur Übersicht", vec)
    w.insert_chunk(fid, 1, 2, "Data Quality Framework", vec)
    w.commit()
    w.close()

    db = QueryDatabase(tmp_index_db)
    yield db
    db.close()


def test_search_returns_ranked_results(seeded_db):
    results = seeded_db.search([0.1] * 384, top_k=5)
    assert len(results) == 2
    assert results[0]["filename"] == "analytics.pptx"
    assert "text" in results[0]
    assert results[0]["web_url"] == "https://contoso.sharepoint.com/x?id=/f/analytics.pptx"
    assert results[0]["document_author"] == "Ada Lovelace"
    assert results[0]["document_last_modified_by"] == "Grace Hopper"


def test_get_document_content_returns_ordered_chunks(seeded_db):
    chunks = seeded_db.get_document_content("/f/analytics.pptx")
    assert len(chunks) == 2
    assert chunks[0]["chunk_index"] == 0
    assert chunks[1]["chunk_index"] == 1


def test_list_files_returns_metadata(seeded_db):
    files = seeded_db.list_files()
    assert len(files) == 1
    assert files[0]["filename"] == "analytics.pptx"
    assert files[0]["file_type"] == "pptx"
    assert files[0]["document_created_at"] == "2024-01-02T03:04:05"
    assert files[0]["document_modified_at"] == "2025-02-03T04:05:06"


def test_list_files_filters_by_type(seeded_db):
    assert len(seeded_db.list_files(file_type="pdf")) == 0
    assert len(seeded_db.list_files(file_type="pptx")) == 1


def test_list_files_treats_sql_wildcards_as_literal_text(tmp_index_db):
    writer = Database(tmp_index_db, embedding_dim=384)
    writer.init_schema()
    first_id = writer.upsert_file(
        "/scope_1/a.pptx", "a.pptx", "pptx", 1, "v1"
    )
    second_id = writer.upsert_file(
        "/scopeX1/b.pptx", "b.pptx", "pptx", 1, "v1"
    )
    writer.insert_chunk(first_id, 0, 1, "first", [0.1] * 384)
    writer.insert_chunk(second_id, 0, 1, "second", [0.1] * 384)
    writer.commit()
    writer.close()

    reader = QueryDatabase(tmp_index_db)
    try:
        files = reader.list_files(folder_path="/scope_1", limit=10)
    finally:
        reader.close()

    assert [item["filename"] for item in files] == ["a.pptx"]


def test_list_files_hides_zero_chunk_source_markers(tmp_index_db):
    writer = Database(tmp_index_db, embedding_dim=384)
    writer.init_schema()
    writer.upsert_file("/empty.docx", "empty.docx", "docx", 0, "v1")
    writer.commit()
    writer.close()

    reader = QueryDatabase(tmp_index_db)
    try:
        assert reader.list_files() == []
    finally:
        reader.close()


def test_list_files_applies_limit(seeded_db):
    assert len(seeded_db.list_files(limit=1)) == 1
    assert seeded_db.list_files(limit=0) == []


def test_search_rejects_non_finite_query(seeded_db):
    with pytest.raises(ValueError, match="non-finite"):
        seeded_db.search([float("nan")] * 384)


def test_get_file_metadata(seeded_db):
    metadata = seeded_db.get_file_metadata("/f/analytics.pptx")

    assert metadata["filename"] == "analytics.pptx"
    assert metadata["document_author"] == "Ada Lovelace"
    assert metadata["document_last_modified_by"] == "Grace Hopper"
    assert seeded_db.get_file_metadata("/f/missing.pptx") is None


def test_old_sqlite_schema_returns_empty_embedded_metadata(seeded_db):
    # Exercise the projection used when opening the pre-migration SQLite index.
    seeded_db._has_document_metadata = False

    result = seeded_db.search([0.1] * 384, top_k=1)[0]
    listed = seeded_db.list_files()[0]
    metadata = seeded_db.get_file_metadata("/f/analytics.pptx")

    assert result["document_author"] is None
    assert listed["document_created_at"] is None
    assert metadata["document_last_modified_by"] is None
