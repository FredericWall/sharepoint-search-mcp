import struct

import pytest

from indexer.postgres_db import embedding_to_bytes, validate_schema_name


def test_embedding_to_bytes_uses_little_endian_float32():
    blob = embedding_to_bytes([1.0, -2.5], 2)

    assert len(blob) == 8
    assert struct.unpack("<2f", blob) == pytest.approx((1.0, -2.5))


def test_embedding_to_bytes_rejects_wrong_dimension():
    with pytest.raises(ValueError, match="expected 2"):
        embedding_to_bytes([1.0], 2)


def test_embedding_to_bytes_rejects_non_finite_values():
    with pytest.raises(ValueError, match="non-finite"):
        embedding_to_bytes([float("nan"), 1.0], 2)


@pytest.mark.parametrize("schema", ["sharepoint_mcp", "Index2", "_private"])
def test_validate_schema_name_accepts_identifiers(schema):
    assert validate_schema_name(schema) == schema


@pytest.mark.parametrize("schema", ["", "two words", "x; DROP SCHEMA public"])
def test_validate_schema_name_rejects_unsafe_values(schema):
    with pytest.raises(ValueError):
        validate_schema_name(schema)
