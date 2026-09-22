import json

import numpy as np
import pytest

from mcp_server.postgres_config import resolve_database_url, validate_schema_name
from mcp_server.postgres_db import EMBEDDING_DIM, build_normalized_matrix, cosine_top_k


def _blob(values: list[float]) -> bytes:
    return np.asarray(values, dtype="<f4").tobytes()


def test_resolve_database_url_prefers_explicit_value():
    assert resolve_database_url({"DATABASE_URL": "postgresql://explicit"}) == (
        "postgresql://explicit"
    )


def test_resolve_database_url_finds_postgres_binding():
    environment = {
        "VCAP_SERVICES": json.dumps(
            {
                "other-service": [{"credentials": {"uri": "other://ignored"}}],
                "postgresql-db": [
                    {"credentials": {"uri": "postgresql://bound"}}
                ],
            }
        )
    }
    assert resolve_database_url(environment) == "postgresql://bound"


def test_schema_name_rejects_sql_fragments():
    with pytest.raises(ValueError):
        validate_schema_name("mvp; DROP SCHEMA public")


def test_cosine_search_uses_existing_float32_blobs():
    x = [1.0, 0.0] + [0.0] * (EMBEDDING_DIM - 2)
    y = [0.0, 1.0] + [0.0] * (EMBEDDING_DIM - 2)
    diagonal = [1.0, 1.0] + [0.0] * (EMBEDDING_DIM - 2)
    ids, matrix = build_normalized_matrix(
        [(10, _blob(x)), (20, _blob(y)), (30, _blob(diagonal))]
    )

    matches = cosine_top_k(ids, matrix, np.asarray(x), top_k=3)

    assert [chunk_id for chunk_id, _ in matches] == [10, 30, 20]
    assert matches[0][1] == pytest.approx(1.0)
    assert matches[1][1] == pytest.approx(2**-0.5)
    assert matches[2][1] == pytest.approx(0.0)


def test_embedding_dimension_is_validated():
    with pytest.raises(ValueError, match="expected 384"):
        build_normalized_matrix([(1, _blob([1.0, 2.0]))])
