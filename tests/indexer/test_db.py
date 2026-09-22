import pytest

from indexer.db import Database


@pytest.fixture
def db(tmp_index_db):
    d = Database(tmp_index_db, embedding_dim=384)
    d.init_schema()
    yield d
    d.close()


def test_init_schema_creates_tables(db):
    cur = db._conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    names = {r[0] for r in cur.fetchall()}
    assert "files" in names
    assert "chunks" in names
    assert "vec_chunks" in names
    cur.execute("PRAGMA table_info(files)")
    columns = {row[1] for row in cur.fetchall()}
    assert {
        "document_author",
        "document_created_at",
        "document_last_modified_by",
        "document_modified_at",
        "metadata_scanned_at",
    } <= columns


def test_init_schema_migrates_existing_files_table(tmp_index_db):
    legacy = Database(tmp_index_db, embedding_dim=384)
    legacy._conn.execute(
        """
        CREATE TABLE files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sharepoint_path TEXT UNIQUE NOT NULL,
            filename TEXT NOT NULL,
            file_type TEXT NOT NULL,
            size_bytes INTEGER,
            modified_at TEXT,
            web_url TEXT,
            indexed_at TEXT
        )
        """
    )
    legacy._conn.execute(
        """
        INSERT INTO files (sharepoint_path, filename, file_type)
        VALUES ('/legacy/a.pptx', 'a.pptx', 'pptx')
        """
    )
    legacy._conn.commit()

    legacy.init_schema()

    columns = {
        row[1] for row in legacy._conn.execute("PRAGMA table_info(files)").fetchall()
    }
    assert "document_author" in columns
    assert "metadata_scanned_at" in columns
    assert legacy._conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
    legacy.close()


def test_upsert_file_returns_id(db):
    fid = db.upsert_file("/path/deck.pptx", "deck.pptx", "pptx", 12345, "2026-01-01T00:00:00Z")
    assert isinstance(fid, int)


def test_upsert_file_is_idempotent(db):
    fid1 = db.upsert_file("/path/a.pptx", "a.pptx", "pptx", 100, "2026-01-01T00:00:00Z")
    fid2 = db.upsert_file("/path/a.pptx", "a.pptx", "pptx", 200, "2026-02-01T00:00:00Z")
    assert fid1 == fid2  # gleicher Pfad -> Update, nicht neue Zeile


def test_upsert_and_backfill_embedded_office_metadata(db):
    db.upsert_file(
        "/scope/a.pptx",
        "a.pptx",
        "pptx",
        100,
        "v1",
        document_author="Ada",
        document_created_at="2024-01-02T03:04:05",
        document_last_modified_by="Grace",
        document_modified_at="2025-02-03T04:05:06",
        metadata_scanned_at="2026-09-21T10:00:00+00:00",
    )
    db.upsert_file("/scope/b.docx", "b.docx", "docx", 200, "v1")
    db.upsert_file("/scope/c.pdf", "c.pdf", "pdf", 300, "v1")

    assert db.get_metadata_pending_paths("/scope") == {"/scope/b.docx"}

    updated = db.update_file_metadata(
        [("Lin", None, "Margaret", None, "scan-2", "/scope/b.docx")]
    )
    assert updated == 1
    assert db.get_metadata_pending_paths("/scope") == set()

    row = db._conn.execute(
        """
        SELECT document_author, document_created_at, document_last_modified_by,
               document_modified_at, metadata_scanned_at
        FROM files WHERE sharepoint_path = '/scope/b.docx'
        """
    ).fetchone()
    assert row == ("Lin", None, "Margaret", None, "scan-2")


def test_is_indexed_reflects_modified_at(db):
    db.upsert_file("/path/a.pptx", "a.pptx", "pptx", 100, "2026-01-01T00:00:00Z")
    assert db.is_indexed("/path/a.pptx", "2026-01-01T00:00:00Z") is True
    # neuere Version -> nicht mehr aktuell indexiert
    assert db.is_indexed("/path/a.pptx", "2026-02-01T00:00:00Z") is False
    assert db.is_indexed("/path/unknown.pptx", "2026-01-01T00:00:00Z") is False


def test_insert_and_search_chunks(db):
    fid = db.upsert_file("/path/a.pptx", "a.pptx", "pptx", 100, "2026-01-01T00:00:00Z",
                         web_url="https://contoso.sharepoint.com/x?id=/path/a.pptx")
    vec = [0.1] * 384
    db.insert_chunk(fid, chunk_index=0, page_or_slide=1, text="Analytics Architektur", embedding=vec)
    results = db.search(query_embedding=vec, top_k=5)
    assert len(results) == 1
    assert results[0]["text"] == "Analytics Architektur"
    assert results[0]["filename"] == "a.pptx"
    assert results[0]["page_or_slide"] == 1
    assert results[0]["web_url"] == "https://contoso.sharepoint.com/x?id=/path/a.pptx"


def test_rejects_incompatible_embedding_dimensions(db):
    fid = db.upsert_file("/path/a.pptx", "a.pptx", "pptx", 100, "v1")
    with pytest.raises(ValueError, match="expected 384"):
        db.insert_chunk(fid, 0, 1, "text", [0.1] * 383)
    with pytest.raises(ValueError, match="expected 384"):
        db.search([0.1] * 383)
    with pytest.raises(ValueError, match="non-finite"):
        db.insert_chunk(fid, 0, 1, "text", [float("nan")] * 384)
    with pytest.raises(ValueError, match="non-finite"):
        db.search([float("inf")] * 384)


def test_delete_file_chunks_removes_old_data(db):
    fid = db.upsert_file("/path/a.pptx", "a.pptx", "pptx", 100, "2026-01-01T00:00:00Z")
    db.insert_chunk(fid, 0, 1, "alt", [0.1] * 384)
    db.delete_file_chunks(fid)
    results = db.search([0.1] * 384, top_k=5)
    assert len(results) == 0


def test_file_replacement_rolls_back_atomically(db):
    old_vector = [0.1] * 384
    file_id = db.upsert_file("/path/a.pptx", "a.pptx", "pptx", 100, "v1")
    db.insert_chunk(file_id, 0, 1, "old", old_vector)
    db.commit()

    db.upsert_file("/path/a.pptx", "a.pptx", "pptx", 200, "v2")
    db.delete_file_chunks(file_id)
    db.insert_chunk(file_id, 0, 1, "new", [0.2] * 384)
    db.rollback()

    assert db.is_indexed("/path/a.pptx", "v1") is True
    assert db.search(old_vector, top_k=1)[0]["text"] == "old"


def test_delete_files_removes_metadata_and_vectors(db):
    file_id = db.upsert_file("/scope/a.pptx", "a.pptx", "pptx", 100, "v1")
    db.insert_chunk(file_id, 0, 1, "text", [0.1] * 384)
    db.commit()

    assert db.delete_files(["/scope/a.pptx"]) == 1
    db.commit()

    assert db.get_indexed_versions("/scope") == {}
    assert db.search([0.1] * 384, top_k=1) == []


def test_get_indexed_versions_treats_like_wildcards_literally(db):
    matching_id = db.upsert_file(
        "/scope_1/a.pptx", "a.pptx", "pptx", 100, "v1"
    )
    db.insert_chunk(matching_id, 0, 1, "matching", [0.1] * 384)
    other_id = db.upsert_file(
        "/scopeX1/b.pptx", "b.pptx", "pptx", 100, "v1"
    )
    db.insert_chunk(other_id, 0, 1, "other", [0.1] * 384)
    db.commit()

    assert db.get_indexed_versions("/scope_1") == {"/scope_1/a.pptx": "v1"}
