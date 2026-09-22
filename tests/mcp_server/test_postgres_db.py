import numpy as np
import pytest

from mcp_server.postgres_db import (
    PostgresQueryDatabase,
    build_normalized_matrix,
    cosine_top_k,
)


def _blob(values):
    return np.asarray(values, dtype="<f4").tobytes()


class FakeCursor:
    def __init__(self, result):
        self.result = result
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def execute(self, query, params=None):
        self.executions.append((query.as_string(), params))

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.result


class FakeConnection:
    def __init__(self, *results):
        self.results = list(results)
        self.cursors = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def cursor(self):
        cursor = FakeCursor(self.results.pop(0))
        self.cursors.append(cursor)
        return cursor


def test_refresh_reloads_only_when_revision_changes(monkeypatch):
    connections = iter(
        [
            FakeConnection((1,), [(10, _blob([1, 0])), (20, _blob([0, 1]))]),
            FakeConnection((1,)),
            FakeConnection((2,), [(20, _blob([0, 1])), (30, _blob([1, 1]))]),
        ]
    )
    database = PostgresQueryDatabase(
        "postgresql://unused", embedding_dim=2, preload=False
    )
    monkeypatch.setattr(database, "_connect", lambda: next(connections))

    assert database.refresh() is True
    assert database.cached_vector_count == 2
    assert database._ids.tolist() == [10, 20]

    assert database.refresh() is False
    assert database._ids.tolist() == [10, 20]

    assert database.refresh() is True
    assert database._ids.tolist() == [20, 30]
    assert database._revision == 2


def test_search_preserves_similarity_order_when_database_rows_are_unordered(
    monkeypatch,
):
    metadata_rows = [
        (20, "second", 2, "b.pptx", "/folder/b.pptx", "https://b",
         "Author B", "2024-01-01", "Editor B", "2025-01-01"),
        (10, "first", 1, "a.pptx", "/folder/a.pptx", "https://a",
         "Author A", "2023-01-01", "Editor A", "2026-01-01"),
    ]
    database = PostgresQueryDatabase(
        "postgresql://unused", embedding_dim=2, preload=False
    )
    database._ids = np.asarray([10, 20], dtype=np.int64)
    database._matrix = np.asarray([[1, 0], [0, 1]], dtype=np.float32)
    monkeypatch.setattr(database, "refresh", lambda **kwargs: False)
    monkeypatch.setattr(
        database, "_connect", lambda: FakeConnection(metadata_rows)
    )

    results = database.search([1, 0], top_k=2)

    assert [result["filename"] for result in results] == ["a.pptx", "b.pptx"]
    assert results[0]["score"] == pytest.approx(1.0)
    assert results[1]["score"] == pytest.approx(0.0)
    assert results[0]["document_author"] == "Author A"
    assert results[1]["document_last_modified_by"] == "Editor B"


def test_list_files_returns_embedded_metadata(monkeypatch):
    rows = [
        ("a.docx", "/folder/a.docx", "docx", 123, "source-time",
         "https://a", "Author A", "created-time", "Editor A",
         "modified-time")
    ]
    database = PostgresQueryDatabase(
        "postgresql://unused", embedding_dim=2, preload=False
    )
    monkeypatch.setattr(database, "_connect", lambda: FakeConnection(rows))

    files = database.list_files(file_type="docx")

    assert files[0]["document_author"] == "Author A"
    assert files[0]["document_created_at"] == "created-time"
    assert files[0]["document_last_modified_by"] == "Editor A"
    assert files[0]["document_modified_at"] == "modified-time"


def test_get_file_metadata(monkeypatch):
    row = (
        "a.pptx", "/folder/a.pptx", "pptx", 456, "source-time",
        "https://a", "Author A", "created-time", "Editor A",
        "modified-time"
    )
    database = PostgresQueryDatabase(
        "postgresql://unused", embedding_dim=2, preload=False
    )
    monkeypatch.setattr(database, "_connect", lambda: FakeConnection(row))

    metadata = database.get_file_metadata("/folder/a.pptx")

    assert metadata["filename"] == "a.pptx"
    assert metadata["document_author"] == "Author A"
    assert metadata["document_modified_at"] == "modified-time"


def test_rejects_non_finite_stored_and_query_vectors():
    with pytest.raises(ValueError, match="non-finite"):
        build_normalized_matrix([(1, _blob([float("nan"), 1.0]))], embedding_dim=2)

    ids = np.asarray([1], dtype=np.int64)
    matrix = np.asarray([[1.0, 0.0]], dtype=np.float32)
    with pytest.raises(ValueError, match="non-finite"):
        cosine_top_k(ids, matrix, [float("inf"), 0.0], top_k=1)


def test_list_files_uses_literal_substring_and_limit(monkeypatch):
    connection = FakeConnection([])
    database = PostgresQueryDatabase(
        "postgresql://unused", embedding_dim=2, preload=False
    )
    monkeypatch.setattr(database, "_connect", lambda: connection)

    assert database.list_files(folder_path="/scope_1", limit=25) == []

    statement, params = connection.cursors[0].executions[0]
    assert "EXISTS (SELECT 1 FROM" in statement
    assert "strpos(sharepoint_path, %s) > 0" in statement
    assert "LIMIT %s" in statement
    assert params == ["/scope_1", 25]
    database.close()


def test_connection_pool_is_lazy_when_preload_is_disabled():
    database = PostgresQueryDatabase("postgresql://unused", preload=False)
    try:
        assert database._pool_started is False
    finally:
        database.close()
