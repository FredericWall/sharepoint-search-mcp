import pytest

import mcp_server.main as main


def test_init_uses_index_db_path(monkeypatch, tmp_index_db):
    """_init() öffnet die per INDEX_DB_PATH konfigurierte Index-Datei."""
    # Erst eine gültige (leere, aber initialisierte) Index-Datei anlegen.
    from indexer.db import Database
    w = Database(tmp_index_db, embedding_dim=384)
    w.init_schema()
    w.close()

    monkeypatch.setenv("INDEX_DB_PATH", tmp_index_db)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("VCAP_SERVICES", raising=False)
    # Embedder nicht laden müssen: als bereits gesetzt vortäuschen.
    main._db = None
    main._embedder = object()
    try:
        main._init()
        assert main._db is not None
        # Read-only nutzbar (list_files gegen leere DB -> [])
        assert main._db.list_files() == []
    finally:
        if main._db is not None:
            main._db.close()
        main._db = None
        main._embedder = None


def test_init_defaults_to_mcp_server_index_db(monkeypatch):
    """Ohne INDEX_DB_PATH wird der Default mcp_server/index.db verwendet."""
    monkeypatch.delenv("INDEX_DB_PATH", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("VCAP_SERVICES", raising=False)
    captured = {}

    class FakeQuery:
        def __init__(self, path):
            captured["path"] = path

    monkeypatch.setattr(main, "QueryDatabase", FakeQuery)
    main._db = None
    main._embedder = object()
    try:
        main._init()
        assert captured["path"] == "mcp_server/index.db"
    finally:
        main._db = None
        main._embedder = None


def test_init_uses_postgres_when_database_url_is_available(monkeypatch):
    captured = {}

    class FakePostgresQuery:
        def __init__(self, database_url, *, schema):
            captured.update(database_url=database_url, schema=schema)

    monkeypatch.setenv("DATABASE_URL", "postgresql://bound")
    monkeypatch.setenv("INDEX_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_SCHEMA", "custom_index")
    monkeypatch.setattr(main, "PostgresQueryDatabase", FakePostgresQuery)
    main._db = None
    main._embedder = object()
    try:
        main._init()
        assert captured == {
            "database_url": "postgresql://bound",
            "schema": "custom_index",
        }
    finally:
        main._db = None
        main._embedder = None


def test_postgres_backend_fails_closed_without_binding(monkeypatch):
    monkeypatch.setenv("INDEX_BACKEND", "postgres")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("VCAP_SERVICES", raising=False)

    with pytest.raises(RuntimeError, match="is required"):
        main._database_url_for_backend()


def test_sqlite_backend_ignores_ambient_database_url(monkeypatch):
    monkeypatch.setenv("INDEX_BACKEND", "sqlite")
    monkeypatch.setenv("DATABASE_URL", "postgresql://must-not-be-used")

    assert main._database_url_for_backend() is None
