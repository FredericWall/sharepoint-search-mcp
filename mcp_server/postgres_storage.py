"""PostgreSQL-backed FastMCP dynamic-client registration storage."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from mcp_server.postgres_config import validate_schema_name


class PostgresKVStorage:
    """Implement FastMCP's async KVStorage protocol using a JSONB table."""

    def __init__(self, database_url: str, *, schema: str = "sharepoint_mcp"):
        self._database_url = database_url
        self._schema = validate_schema_name(schema)
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()

    @staticmethod
    def _validate_key(key: str) -> str:
        if not isinstance(key, str) or not key or len(key) > 512:
            raise ValueError("OAuth storage key must contain 1 to 512 characters")
        return key

    @staticmethod
    def _validate_value(value: dict[str, Any]) -> None:
        encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        if len(encoded.encode("utf-8")) > 262_144:
            raise ValueError("OAuth storage value exceeds 256 KiB")

    async def _connect(self):
        return await psycopg.AsyncConnection.connect(
            self._database_url,
            autocommit=True,
            connect_timeout=10,
            application_name="sharepoint-search-oauth-storage",
            options="-c statement_timeout=10000",
        )

    async def _ensure_table(self, connection) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            await self._create_table(connection)
            self._schema_ready = True

    async def _create_table(self, connection) -> None:
        namespace = sql.Identifier(self._schema)
        await connection.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {}.oauth_clients (
                    storage_key TEXT PRIMARY KEY,
                    value JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            ).format(namespace)
        )

    async def get(self, key: str) -> dict[str, Any] | None:
        key = self._validate_key(key)
        namespace = sql.Identifier(self._schema)
        async with await self._connect() as connection:
            await self._ensure_table(connection)
            cursor = await connection.execute(
                sql.SQL(
                    "SELECT value FROM {}.oauth_clients WHERE storage_key = %s"
                ).format(namespace),
                (key,),
            )
            row = await cursor.fetchone()
        return row[0] if row else None

    async def set(self, key: str, value: dict[str, Any]) -> None:
        key = self._validate_key(key)
        self._validate_value(value)
        namespace = sql.Identifier(self._schema)
        async with await self._connect() as connection:
            await self._ensure_table(connection)
            await connection.execute(
                sql.SQL(
                    """
                    INSERT INTO {}.oauth_clients (storage_key, value, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (storage_key) DO UPDATE
                    SET value = EXCLUDED.value, updated_at = NOW()
                    """
                ).format(namespace),
                (key, Jsonb(value)),
            )

    async def delete(self, key: str) -> None:
        key = self._validate_key(key)
        namespace = sql.Identifier(self._schema)
        async with await self._connect() as connection:
            await self._ensure_table(connection)
            await connection.execute(
                sql.SQL(
                    "DELETE FROM {}.oauth_clients WHERE storage_key = %s"
                ).format(namespace),
                (key,),
            )
