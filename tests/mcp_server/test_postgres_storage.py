import asyncio

import pytest

from mcp_server.postgres_storage import PostgresKVStorage


class FakeAsyncCursor:
    def __init__(self, row=None):
        self._row = row

    async def fetchone(self):
        return self._row


class FakeAsyncConnection:
    def __init__(self, values, statements):
        self.values = values
        self.statements = statements

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def execute(self, query, params=None):
        statement = query.as_string()
        self.statements.append(statement)
        if statement.lstrip().startswith("SELECT"):
            value = self.values.get(params[0])
            return FakeAsyncCursor((value,) if value is not None else None)
        if statement.lstrip().startswith("INSERT"):
            self.values[params[0]] = params[1].obj
        elif statement.lstrip().startswith("DELETE"):
            self.values.pop(params[0], None)
        return FakeAsyncCursor()


def test_postgres_storage_round_trip(monkeypatch):
    values = {}
    statements = []
    storage = PostgresKVStorage("postgresql://unused", schema="oauth_test")

    async def connect():
        return FakeAsyncConnection(values, statements)

    monkeypatch.setattr(storage, "_connect", connect)

    async def scenario():
        assert await storage.get("missing") is None
        await storage.set("client-id", {"redirect_uris": ["https://example.test"]})
        assert await storage.get("client-id") == {
            "redirect_uris": ["https://example.test"]
        }
        await storage.delete("client-id")
        assert await storage.get("client-id") is None

    asyncio.run(scenario())

    assert any('CREATE TABLE IF NOT EXISTS "oauth_test".oauth_clients' in sql for sql in statements)
    assert any(sql.lstrip().startswith("INSERT") for sql in statements)
    assert any(sql.lstrip().startswith("DELETE") for sql in statements)
    assert sum("CREATE TABLE IF NOT EXISTS" in sql for sql in statements) == 1


def test_postgres_storage_rejects_unbounded_keys_and_values():
    storage = PostgresKVStorage("postgresql://unused")

    async def scenario():
        with pytest.raises(ValueError, match="storage key"):
            await storage.get("")
        with pytest.raises(ValueError, match="storage key"):
            await storage.delete("x" * 513)
        with pytest.raises(ValueError, match="256 KiB"):
            await storage.set("key", {"payload": "x" * 262_145})

    asyncio.run(scenario())
