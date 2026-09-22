"""End-to-End: indexer schreibt, mcp_server liest — echte lokale Datei, echte Embeddings."""

import pytest

import mcp_server.main as server
from indexer.db import Database
from indexer.embeddings import Embedder as IndexerEmbedder
from mcp_server.db import QueryDatabase


@pytest.fixture
def clean_db(tmp_index_db):
    d = Database(tmp_index_db, embedding_dim=384)
    d.init_schema()
    yield d, tmp_index_db
    d.close()


def test_index_then_search_end_to_end(clean_db, fake_model_path):
    db, db_path = clean_db
    embedder = IndexerEmbedder(provider="local", model=fake_model_path)
    # Zwei "Dokumente" indexieren
    fid1 = db.upsert_file("/f/analytics.pptx", "analytics.pptx", "pptx", 100, "2026-01-01T00:00:00Z")
    for i, text in enumerate(["Analytics Architektur und Datenmodell", "BW Bridge Integration"]):
        db.insert_chunk(fid1, i, i + 1, text, embedder.embed(text))
    fid2 = db.upsert_file("/f/weather.pptx", "weather.pptx", "pptx", 100, "2026-01-01T00:00:00Z")
    db.insert_chunk(fid2, 0, 1, "Das Wetter ist heute sonnig", embedder.embed("Das Wetter ist heute sonnig"))
    db.commit()

    # Über den MCP-Server-Layer suchen
    previous_db, previous_embedder = server._db, server._embedder
    server._db = QueryDatabase(db_path)
    server._embedder = __import__(
        "mcp_server.embeddings", fromlist=["Embedder"]
    ).Embedder(provider="local", model=fake_model_path)
    try:
        result = server._search_documents(
            "Wie ist die Analytics Architektur aufgebaut?", top_k=3
        )
        assert "analytics.pptx" in result
        # Das relevanteste Ergebnis sollte NICHT das Wetter-Dokument sein
        first_line = result.split("\n")[0]
        assert "weather" not in first_line.lower()
    finally:
        server._db.close()
        server._db = previous_db
        server._embedder = previous_embedder
