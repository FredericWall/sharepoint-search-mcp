import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import mcp_server.monitoring as monitoring
from mcp_server.monitoring import (
    HttpIssueMonitoringMiddleware,
    PostgresUsageMonitor,
    ToolUsageMiddleware,
    _path_group,
    format_usage_summary,
    pseudonymous_identity,
)


class FakeCursor:
    def __init__(self, result=None):
        self.result = result

    async def fetchall(self):
        return self.result

    async def fetchone(self):
        return self.result


class FakeConnection:
    def __init__(self, results):
        self.results = list(results)
        self.executions = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def execute(self, query, params=None):
        self.executions.append((query.as_string(), params))
        result = self.results.pop(0) if self.results else None
        return FakeCursor(result)


def test_pseudonymous_identity_uses_subject_without_exposing_it(monkeypatch):
    monkeypatch.setenv("MCP_USAGE_HASH_SALT", "test-salt")
    token = SimpleNamespace(
        claims={"iss": "https://tenant.example", "sub": "person-123"},
        client_id="client-id",
        resource_owner=None,
    )

    first, kind = pseudonymous_identity(token)
    second, _ = pseudonymous_identity(token)

    assert kind == "subject"
    assert first == second
    assert len(first) == 64
    assert "person-123" not in first


def test_path_group_avoids_unbounded_url_cardinality():
    assert _path_group("/mcp/anything") == "/mcp"
    assert _path_group("/.well-known/oauth-authorization-server") == "/.well-known/*"
    assert _path_group("/token") == "/token"
    assert _path_group("/unexpected/identifier") == "other"


def test_tool_middleware_records_success_and_failure(monkeypatch):
    monitor = SimpleNamespace(record_tool_call=AsyncMock())
    middleware = ToolUsageMiddleware(monitor)
    context = SimpleNamespace(message=SimpleNamespace(name="search_documents"))
    token = SimpleNamespace(claims={"sub": "user-1"})
    monkeypatch.setattr(monitoring, "get_access_token", lambda: token)

    async def scenario():
        async def successful(_context):
            return SimpleNamespace(is_error=False)

        result = await middleware.on_call_tool(context, successful)
        assert result.is_error is False

        async def failing(_context):
            raise RuntimeError("tool failed")

        with pytest.raises(RuntimeError, match="tool failed"):
            await middleware.on_call_tool(context, failing)

    asyncio.run(scenario())

    assert monitor.record_tool_call.await_count == 2
    assert monitor.record_tool_call.await_args_list[0].kwargs["success"] is True
    assert monitor.record_tool_call.await_args_list[1].kwargs["success"] is False


def test_http_middleware_records_unauthorized_response():
    monitor = SimpleNamespace(record_http_issue=AsyncMock())

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 401, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = HttpIssueMonitoringMiddleware(downstream, monitor)

    async def scenario():
        messages = []

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)

        await middleware(
            {"type": "http", "path": "/mcp"}, receive, send
        )
        assert messages[0]["status"] == 401

    asyncio.run(scenario())
    monitor.record_http_issue.assert_awaited_once_with(
        path="/mcp", status_code=401
    )


def test_postgres_summary_returns_only_aggregates(monkeypatch):
    connection = FakeConnection(
        [
            [(date(2026, 9, 21), 3, 0, 12, 1, 25.5, 80)],
            [("search_documents", 10, 1, 30.0, 80)],
            (3, 0),
            [(date(2026, 9, 21), "/mcp", 401, 2)],
        ]
    )
    monitor = PostgresUsageMonitor("postgresql://unused")
    monitor._schema_ready = True

    async def connect():
        return connection

    monkeypatch.setattr(monitor, "_connect", connect)

    summary = asyncio.run(monitor.get_summary(7))

    assert summary["unique_users"] == 3
    assert summary["daily"][0]["tool_calls"] == 12
    assert summary["tools"][0]["failed_calls"] == 1
    assert summary["http_issues"][0]["status"] == 401
    assert "user_hash" not in repr(summary)


def test_postgres_monitor_creates_tables_and_records_aggregate(monkeypatch):
    schema_connection = FakeConnection([None, None])
    write_connection = FakeConnection([None])
    connections = iter([schema_connection, write_connection])
    monitor = PostgresUsageMonitor("postgresql://unused", schema="usage_test")

    async def connect():
        return next(connections)

    monkeypatch.setattr(monitor, "_connect", connect)

    asyncio.run(
        monitor.record_tool_call(
            user_hash="a" * 64,
            identity_kind="subject",
            tool_name="search_documents",
            success=True,
            duration_ms=42,
        )
    )

    schema_sql = "\n".join(statement for statement, _ in schema_connection.executions)
    assert 'CREATE TABLE IF NOT EXISTS "usage_test".mcp_tool_usage_daily' in schema_sql
    assert 'CREATE TABLE IF NOT EXISTS "usage_test".mcp_http_issues_daily' in schema_sql
    insert_sql, params = write_connection.executions[0]
    assert 'INSERT INTO "usage_test".mcp_tool_usage_daily' in insert_sql
    assert params == ("a" * 64, "subject", "search_documents", 1, 0, 42, 42)
    assert schema_sql.count("DELETE FROM") == 2


@pytest.mark.parametrize("retention_days", [0, 29, 3651])
def test_monitor_rejects_unsafe_retention(retention_days):
    with pytest.raises(ValueError, match="retention"):
        PostgresUsageMonitor(
            "postgresql://unused", retention_days=retention_days
        )


def test_monitor_ignores_unrelated_http_paths(monkeypatch):
    monitor = PostgresUsageMonitor("postgresql://unused")
    monitor._schema_ready = True
    connect = AsyncMock()
    monkeypatch.setattr(monitor, "_connect", connect)

    asyncio.run(monitor.record_http_issue(path="/random-probe", status_code=404))

    connect.assert_not_awaited()


def test_format_usage_summary_explains_auth_rejections():
    summary = {
        "enabled": True,
        "days": 7,
        "unique_users": 3,
        "fallback_identities": 0,
        "daily": [
            {
                "date": "2026-09-21",
                "unique_users": 3,
                "tool_calls": 12,
                "failed_calls": 1,
                "average_duration_ms": 25.5,
                "max_duration_ms": 80,
            }
        ],
        "tools": [
            {
                "tool_name": "search_documents",
                "tool_calls": 12,
                "failed_calls": 1,
                "average_duration_ms": 25.5,
                "max_duration_ms": 80,
            }
        ],
        "http_issues": [
            {"date": "2026-09-21", "path": "/mcp", "status": 401, "count": 2}
        ],
    }

    result = format_usage_summary(summary)

    assert "Unique users: 3" in result
    assert "tool calls: 12" in result
    assert "authentication rejections: 2" in result
    assert "normal OAuth challenges" in result
