"""Globale pytest-Konfiguration.

Der Index liegt jetzt als sqlite-vec-Datei vor. Tests nutzen pro Test eine
eigene temporäre Datei (Fixture ``tmp_index_db``) — dadurch gibt es keine
gemeinsame DB mehr, die versehentlich der Produktions-Index sein könnte.
Der frühere Postgres-TRUNCATE-Schutz ist damit gegenstandslos.
"""

from __future__ import annotations

import os

import pytest

# Remove ambient service/indexer settings before test-module collection. This
# prevents accidental access to a real CF binding, production index, or the
# operator's ignored indexer/.env file.
_EXTERNAL_DATABASE_VARIABLES = (
    "DATABASE_URL",
    "VCAP_SERVICES",
    "INDEX_DATABASE_URL",
    "INDEX_USE_LIBPQ_ENV",
    "INDEX_BACKEND",
    "DATABASE_SCHEMA",
    "POSTGRES_SCHEMA",
    "PGHOST",
    "PGPORT",
    "PGUSER",
    "PGPASSWORD",
    "PGDATABASE",
    "PGSSLMODE",
)
for _name in _EXTERNAL_DATABASE_VARIABLES:
    os.environ.pop(_name, None)

# WICHTIG: conftest.py wird von pytest VOR dem Sammeln (collection) der
# Testmodule importiert. mcp_server.main ruft beim Import _build_auth() auf —
# das passiert schon während der Collection, also bevor irgendeine Fixture
# (auch session-scoped autouse) läuft. Daher das break-glass-Flag hier auf
# Modulebene setzen, nicht erst in der Fixture, sonst wirft der Import einen
# RuntimeError (fehlende OIDC-Env) und bricht die Collection ab.
os.environ["MCP_AUTH_DISABLED"] = "1"
os.environ["INDEXER_ENV_FILE"] = ""
os.environ["INDEX_DB_PATH"] = "__pytest_requires_tmp_index_fixture__.db"
os.environ["SHAREPOINT_LIBRARY_VIEW_URL"] = (
    "https://contoso.sharepoint.com/sites/knowledge-base/"
    "Shared%20Documents/Forms/AllItems.aspx"
)
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"


@pytest.fixture(scope="session", autouse=True)
def _disable_mcp_auth():
    """MCP-Server importiert main.py, das beim Import _build_auth() aufruft.
    Ohne OIDC-Env würde das ein RuntimeError werfen und auch das ~20s-Modell laden.
    Für Tests das break-glass-Flag setzen: _build_auth()→None, Eager-Init übersprungen.
    """
    os.environ["MCP_AUTH_DISABLED"] = "1"
    yield


@pytest.fixture
def tmp_index_db(tmp_path):
    """Pfad zu einer frischen, isolierten sqlite-vec Index-Datei pro Test."""
    return str(tmp_path / "index_test.db")


@pytest.fixture
def fake_model_path(monkeypatch) -> str:
    """Replace sentence-transformers with a deterministic offline test double."""
    import numpy as np

    import indexer.embeddings
    import mcp_server.embeddings

    class FakeSentenceTransformer:
        def encode(self, texts, normalize_embeddings=False):
            vectors = []
            for value in texts:
                vector = np.zeros(384, dtype=np.float32)
                vector[1 if any(term in value.lower() for term in ("weather", "wetter")) else 0] = 1.0
                vectors.append(vector)
            return np.asarray(vectors)

    model = FakeSentenceTransformer()
    monkeypatch.setattr(indexer.embeddings, "_load_local_model", lambda _: model)
    monkeypatch.setattr(mcp_server.embeddings, "_load_local_model", lambda _: model)
    return "offline-test-model"
