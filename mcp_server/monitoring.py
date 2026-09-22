"""Privacy-preserving PostgreSQL usage and health monitoring."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from typing import Any

import psycopg
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from psycopg import sql

from mcp_server.postgres_config import validate_schema_name

logger = logging.getLogger(__name__)

_IDENTITY_CLAIMS = ("sub", "user_uuid")
_MONITORED_AUTH_PATHS = {
    "/authorize",
    "/auth/callback",
    "/register",
    "/revoke",
    "/token",
}
_METRIC_WRITE_TIMEOUT_SECONDS = 2.0
_DEFAULT_RETENTION_DAYS = 400


def pseudonymous_identity(access_token: Any | None) -> tuple[str, str]:
    """Return a stable one-way identifier and a non-sensitive source label."""
    identity_kind = "anonymous"
    identity = "anonymous"
    if access_token is not None:
        claims = getattr(access_token, "claims", None) or {}
        for claim in _IDENTITY_CLAIMS:
            value = claims.get(claim)
            if value:
                identity_kind = "subject"
                identity = f"{claims.get('iss', '')}|{value}"
                break
        else:
            resource_owner = getattr(access_token, "resource_owner", None)
            client_id = getattr(access_token, "client_id", None)
            if resource_owner:
                identity_kind = "resource_owner"
                identity = str(resource_owner)
            elif client_id:
                identity_kind = "client"
                identity = str(client_id)

    salt = (
        os.environ.get("MCP_USAGE_HASH_SALT")
        or os.environ.get("OIDC_CLIENT_ID")
        or "sharepoint-search-mcp"
    )
    digest = hashlib.sha256(
        f"{salt}\0{identity_kind}\0{identity}".encode()
    ).hexdigest()
    return digest, identity_kind


def _path_group(path: str) -> str:
    if path.startswith("/mcp"):
        return "/mcp"
    if path.startswith("/.well-known/"):
        return "/.well-known/*"
    if path in _MONITORED_AUTH_PATHS:
        return path
    return "other"


class NullUsageMonitor:
    """No-op implementation for local SQLite use and tests."""

    enabled = False

    async def record_tool_call(
        self,
        *,
        user_hash: str,
        identity_kind: str,
        tool_name: str,
        success: bool,
        duration_ms: int,
    ) -> None:
        return None

    async def record_http_issue(self, *, path: str, status_code: int) -> None:
        return None

    async def get_summary(self, days: int = 7) -> dict[str, Any]:
        return {"enabled": False, "days": days}


class PostgresUsageMonitor:
    """Store daily aggregates without retaining user or query content."""

    enabled = True

    def __init__(
        self,
        database_url: str,
        *,
        schema: str = "sharepoint_mcp",
        retention_days: int | None = None,
    ) -> None:
        self._database_url = database_url
        self._schema = validate_schema_name(schema)
        configured_retention = (
            retention_days
            if retention_days is not None
            else int(
                os.environ.get(
                    "MCP_USAGE_RETENTION_DAYS", str(_DEFAULT_RETENTION_DAYS)
                )
            )
        )
        if not 30 <= configured_retention <= 3_650:
            raise ValueError("MCP usage retention must be between 30 and 3650 days")
        self._retention_days = configured_retention
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()

    async def _connect(self):
        return await psycopg.AsyncConnection.connect(
            self._database_url,
            autocommit=True,
            connect_timeout=5,
            application_name="sharepoint-search-mcp-monitoring",
            options="-c statement_timeout=5000",
        )

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            namespace = sql.Identifier(self._schema)
            async with await self._connect() as connection:
                await connection.execute(
                    sql.SQL(
                        """
                        CREATE TABLE IF NOT EXISTS {}.mcp_tool_usage_daily (
                            usage_date DATE NOT NULL,
                            user_hash CHAR(64) NOT NULL,
                            identity_kind TEXT NOT NULL,
                            tool_name TEXT NOT NULL,
                            successful_calls BIGINT NOT NULL DEFAULT 0,
                            failed_calls BIGINT NOT NULL DEFAULT 0,
                            total_duration_ms BIGINT NOT NULL DEFAULT 0,
                            max_duration_ms BIGINT NOT NULL DEFAULT 0,
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            PRIMARY KEY (usage_date, user_hash, tool_name)
                        )
                        """
                    ).format(namespace)
                )
                await connection.execute(
                    sql.SQL(
                        """
                        CREATE TABLE IF NOT EXISTS {}.mcp_http_issues_daily (
                            usage_date DATE NOT NULL,
                            path_group TEXT NOT NULL,
                            status_code INTEGER NOT NULL,
                            occurrence_count BIGINT NOT NULL DEFAULT 0,
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            PRIMARY KEY (usage_date, path_group, status_code)
                        )
                        """
                    ).format(namespace)
                )
                for table in ("mcp_tool_usage_daily", "mcp_http_issues_daily"):
                    await connection.execute(
                        sql.SQL(
                            "DELETE FROM {}.{} WHERE usage_date < "
                            "(CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date - %s"
                        ).format(namespace, sql.Identifier(table)),
                        (self._retention_days,),
                    )
            self._schema_ready = True

    async def record_tool_call(
        self,
        *,
        user_hash: str,
        identity_kind: str,
        tool_name: str,
        success: bool,
        duration_ms: int,
    ) -> None:
        await self._ensure_schema()
        namespace = sql.Identifier(self._schema)
        successful_calls = 1 if success else 0
        failed_calls = 0 if success else 1
        async with await self._connect() as connection:
            await connection.execute(
                sql.SQL(
                    """
                    INSERT INTO {}.mcp_tool_usage_daily (
                        usage_date, user_hash, identity_kind, tool_name,
                        successful_calls, failed_calls, total_duration_ms,
                        max_duration_ms, updated_at
                    ) VALUES (
                        (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date,
                        %s, %s, %s, %s, %s, %s, %s, NOW()
                    )
                    ON CONFLICT (usage_date, user_hash, tool_name) DO UPDATE SET
                        identity_kind = EXCLUDED.identity_kind,
                        successful_calls = mcp_tool_usage_daily.successful_calls
                            + EXCLUDED.successful_calls,
                        failed_calls = mcp_tool_usage_daily.failed_calls
                            + EXCLUDED.failed_calls,
                        total_duration_ms = mcp_tool_usage_daily.total_duration_ms
                            + EXCLUDED.total_duration_ms,
                        max_duration_ms = GREATEST(
                            mcp_tool_usage_daily.max_duration_ms,
                            EXCLUDED.max_duration_ms
                        ),
                        updated_at = NOW()
                    """
                ).format(namespace),
                (
                    user_hash,
                    identity_kind,
                    tool_name[:128],
                    successful_calls,
                    failed_calls,
                    max(0, duration_ms),
                    max(0, duration_ms),
                ),
            )

    async def record_http_issue(self, *, path: str, status_code: int) -> None:
        if status_code < 400:
            return
        path_group = _path_group(path)
        if path_group == "other":
            return
        await self._ensure_schema()
        namespace = sql.Identifier(self._schema)
        async with await self._connect() as connection:
            await connection.execute(
                sql.SQL(
                    """
                    INSERT INTO {}.mcp_http_issues_daily (
                        usage_date, path_group, status_code, occurrence_count, updated_at
                    ) VALUES (
                        (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date, %s, %s, 1, NOW()
                    )
                    ON CONFLICT (usage_date, path_group, status_code) DO UPDATE SET
                        occurrence_count = mcp_http_issues_daily.occurrence_count + 1,
                        updated_at = NOW()
                    """
                ).format(namespace),
                (path_group, status_code),
            )

    async def get_summary(self, days: int = 7) -> dict[str, Any]:
        days = max(1, min(int(days), 90))
        await self._ensure_schema()
        namespace = sql.Identifier(self._schema)
        since_days = days - 1
        async with await self._connect() as connection:
            daily_cursor = await connection.execute(
                sql.SQL(
                    """
                    SELECT usage_date,
                           COUNT(DISTINCT user_hash) FILTER (
                               WHERE identity_kind = 'subject'
                           ) AS unique_users,
                           COUNT(DISTINCT user_hash) FILTER (
                               WHERE identity_kind IN ('resource_owner', 'client')
                           ) AS fallback_identities,
                           SUM(successful_calls + failed_calls) AS tool_calls,
                           SUM(failed_calls) AS failed_calls,
                           ROUND(
                               SUM(total_duration_ms)::numeric
                               / NULLIF(SUM(successful_calls + failed_calls), 0), 1
                           ) AS average_duration_ms,
                           MAX(max_duration_ms) AS max_duration_ms
                    FROM {}.mcp_tool_usage_daily
                    WHERE usage_date >=
                        (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date - %s
                    GROUP BY usage_date
                    ORDER BY usage_date
                    """
                ).format(namespace),
                (since_days,),
            )
            daily_rows = await daily_cursor.fetchall()

            tool_cursor = await connection.execute(
                sql.SQL(
                    """
                    SELECT tool_name, SUM(successful_calls + failed_calls),
                           SUM(failed_calls),
                           ROUND(
                               SUM(total_duration_ms)::numeric
                               / NULLIF(SUM(successful_calls + failed_calls), 0), 1
                           ),
                           MAX(max_duration_ms)
                    FROM {}.mcp_tool_usage_daily
                    WHERE usage_date >=
                        (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date - %s
                    GROUP BY tool_name
                    ORDER BY SUM(successful_calls + failed_calls) DESC, tool_name
                    """
                ).format(namespace),
                (since_days,),
            )
            tool_rows = await tool_cursor.fetchall()

            total_cursor = await connection.execute(
                sql.SQL(
                    """
                    SELECT COUNT(DISTINCT user_hash) FILTER (
                               WHERE identity_kind = 'subject'
                           ),
                           COUNT(DISTINCT user_hash) FILTER (
                               WHERE identity_kind IN ('resource_owner', 'client')
                           )
                    FROM {}.mcp_tool_usage_daily
                    WHERE usage_date >=
                        (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date - %s
                    """
                ).format(namespace),
                (since_days,),
            )
            total_row = await total_cursor.fetchone()

            issue_cursor = await connection.execute(
                sql.SQL(
                    """
                    SELECT usage_date, path_group, status_code, occurrence_count
                    FROM {}.mcp_http_issues_daily
                    WHERE usage_date >=
                        (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date - %s
                    ORDER BY usage_date, path_group, status_code
                    """
                ).format(namespace),
                (since_days,),
            )
            issue_rows = await issue_cursor.fetchall()

        return {
            "enabled": True,
            "days": days,
            "unique_users": int(total_row[0] or 0),
            "fallback_identities": int(total_row[1] or 0),
            "daily": [
                {
                    "date": str(row[0]),
                    "unique_users": int(row[1] or 0),
                    "fallback_identities": int(row[2] or 0),
                    "tool_calls": int(row[3] or 0),
                    "failed_calls": int(row[4] or 0),
                    "average_duration_ms": float(row[5] or 0),
                    "max_duration_ms": int(row[6] or 0),
                }
                for row in daily_rows
            ],
            "tools": [
                {
                    "tool_name": str(row[0]),
                    "tool_calls": int(row[1] or 0),
                    "failed_calls": int(row[2] or 0),
                    "average_duration_ms": float(row[3] or 0),
                    "max_duration_ms": int(row[4] or 0),
                }
                for row in tool_rows
            ],
            "http_issues": [
                {
                    "date": str(row[0]),
                    "path": str(row[1]),
                    "status": int(row[2]),
                    "count": int(row[3]),
                }
                for row in issue_rows
            ],
        }


class ToolUsageMiddleware(Middleware):
    """Count MCP tool calls and duration without retaining arguments."""

    def __init__(self, monitor: NullUsageMonitor | PostgresUsageMonitor) -> None:
        self._monitor = monitor

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> Any:
        started = time.perf_counter()
        success = False
        try:
            result = await call_next(context)
            success = not bool(getattr(result, "is_error", False))
            return result
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000)
            try:
                user_hash, identity_kind = pseudonymous_identity(get_access_token())
                await asyncio.wait_for(
                    self._monitor.record_tool_call(
                        user_hash=user_hash,
                        identity_kind=identity_kind,
                        tool_name=str(getattr(context.message, "name", "unknown")),
                        success=success,
                        duration_ms=duration_ms,
                    ),
                    timeout=_METRIC_WRITE_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                logger.warning(
                    "Could not persist MCP tool usage metric (%s)",
                    type(exc).__name__,
                )


class HttpIssueMonitoringMiddleware:
    """Count HTTP errors outside the auth middleware, including 401/403."""

    def __init__(self, app: Any, monitor: NullUsageMonitor | PostgresUsageMonitor):
        self.app = app
        self._monitor = monitor

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        status_code: int | None = None

        async def monitored_send(message):
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, monitored_send)
        except Exception:
            status_code = 500
            raise
        finally:
            if status_code is not None and status_code >= 400:
                try:
                    await asyncio.wait_for(
                        self._monitor.record_http_issue(
                            path=str(scope.get("path", "")),
                            status_code=status_code,
                        ),
                        timeout=_METRIC_WRITE_TIMEOUT_SECONDS,
                    )
                except Exception as exc:
                    logger.warning(
                        "Could not persist MCP HTTP issue metric (%s)",
                        type(exc).__name__,
                    )


def format_usage_summary(summary: dict[str, Any]) -> str:
    if not summary.get("enabled"):
        return "Usage monitoring is available only with the PostgreSQL backend."

    daily = summary.get("daily", [])
    tools = summary.get("tools", [])
    issues = summary.get("http_issues", [])
    total_calls = sum(row["tool_calls"] for row in daily)
    failed_calls = sum(row["failed_calls"] for row in daily)
    auth_rejections = sum(
        row["count"] for row in issues if row["status"] in {401, 403}
    )
    server_errors = sum(row["count"] for row in issues if row["status"] >= 500)

    lines = [
        f"Usage monitoring (UTC, last {summary['days']} days)",
        (
            f"Unique users: {summary['unique_users']}; tool calls: {total_calls}; "
            f"failed tool calls: {failed_calls}; authentication rejections: "
            f"{auth_rejections}; HTTP 5xx: {server_errors}."
        ),
    ]
    fallback = int(summary.get("fallback_identities", 0))
    if fallback:
        lines.append(
            f"Additionally observed fallback client identities: {fallback} "
            "(the token did not contain an IAS subject claim)."
        )

    if daily:
        lines.append("Daily:")
        lines.extend(
            f"- {row['date']}: {row['unique_users']} users, "
            f"{row['tool_calls']} calls, {row['failed_calls']} failed, "
            f"avg {row['average_duration_ms']:.1f} ms, max {row['max_duration_ms']} ms"
            for row in daily
        )
    if tools:
        lines.append("By tool:")
        lines.extend(
            f"- {row['tool_name']}: {row['tool_calls']} calls, "
            f"{row['failed_calls']} failed, avg {row['average_duration_ms']:.1f} ms"
            for row in tools
        )
    if issues:
        lines.append("HTTP issues:")
        lines.extend(
            f"- {row['date']} {row['path']} HTTP {row['status']}: {row['count']}"
            for row in issues
        )
        lines.append(
            "Note: 401/403 counts include normal OAuth challenges and are not proof "
            "that a person failed to sign in."
        )
    return "\n".join(lines)
