import json

import pytest

from mcp_server.postgres_config import resolve_database_url, validate_schema_name


def test_resolve_database_url_prefers_explicit_value():
    environment = {
        "DATABASE_URL": "postgresql://explicit",
        "VCAP_SERVICES": "not parsed when an explicit URL exists",
    }

    assert resolve_database_url(environment) == "postgresql://explicit"


def test_resolve_database_url_finds_tagged_binding():
    environment = {
        "VCAP_SERVICES": json.dumps(
            {
                "managed-service": [
                    {
                        "tags": ["database", "postgresql"],
                        "credentials": {"uri": "postgresql://bound"},
                    }
                ]
            }
        )
    }

    assert resolve_database_url(environment) == "postgresql://bound"


def test_resolve_database_url_is_optional_without_binding():
    assert resolve_database_url({}) is None

    with pytest.raises(RuntimeError, match="is required"):
        resolve_database_url({}, required=True)


def test_resolve_database_url_rejects_ambiguous_bindings():
    environment = {
        "VCAP_SERVICES": json.dumps(
            {
                "postgresql-db": [
                    {"credentials": {"uri": "postgresql://one"}},
                    {"credentials": {"uri": "postgresql://two"}},
                ]
            }
        )
    }

    with pytest.raises(RuntimeError, match="found 2"):
        resolve_database_url(environment)


@pytest.mark.parametrize("schema", ["sharepoint_mcp", "Index2", "_private"])
def test_validate_schema_name_accepts_identifiers(schema):
    assert validate_schema_name(schema) == schema


@pytest.mark.parametrize("schema", ["", "two words", "x; DROP SCHEMA public"])
def test_validate_schema_name_rejects_unsafe_values(schema):
    with pytest.raises(ValueError):
        validate_schema_name(schema)
