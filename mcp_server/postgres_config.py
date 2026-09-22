"""PostgreSQL configuration helpers for local and Cloud Foundry runtimes."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping

_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def resolve_database_url(
    environment: Mapping[str, str] | None = None, *, required: bool = False
) -> str | None:
    """Resolve PostgreSQL credentials from DATABASE_URL or VCAP_SERVICES."""
    env = os.environ if environment is None else environment
    if explicit := env.get("DATABASE_URL"):
        return explicit

    raw_services = env.get("VCAP_SERVICES")
    if not raw_services:
        if required:
            raise RuntimeError("DATABASE_URL or a PostgreSQL service binding is required")
        return None

    services = json.loads(raw_services)
    candidates = []
    for label, bindings in services.items():
        for binding in bindings:
            tags = [str(tag).lower() for tag in binding.get("tags", [])]
            if "postgres" in label.lower() or any("postgres" in tag for tag in tags):
                candidates.append(binding)

    if not candidates:
        if required:
            raise RuntimeError("No PostgreSQL service binding found in VCAP_SERVICES")
        return None
    if len(candidates) != 1:
        raise RuntimeError(
            f"Expected one PostgreSQL service binding, found {len(candidates)}"
        )

    credentials = candidates[0].get("credentials", {})
    uri = credentials.get("uri") or credentials.get("url")
    if not uri:
        raise RuntimeError("PostgreSQL service binding contains no uri/url")
    return str(uri)


def validate_schema_name(schema: str) -> str:
    """Reject unsafe schema names before they are passed to SQL identifiers."""
    if not _SCHEMA_RE.fullmatch(schema):
        raise ValueError(f"Invalid PostgreSQL schema name: {schema!r}")
    return schema
