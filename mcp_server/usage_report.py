"""CF-operator-only CLI for privacy-preserving MCP usage reports."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import warnings
from collections.abc import Sequence
from typing import Any

from authlib.deprecate import AuthlibDeprecationWarning

# Authlib deliberately enables its deprecation category globally on import. The
# warning is unrelated to this report and would otherwise pollute CLI/JSON output.
warnings.filterwarnings("ignore", category=AuthlibDeprecationWarning)

from mcp_server.monitoring import (  # noqa: E402 - filter before Authlib import chain
    PostgresUsageMonitor,
    format_usage_summary,
)
from mcp_server.postgres_config import resolve_database_url  # noqa: E402


def _report_days(value: str) -> int:
    days = int(value)
    if not 1 <= days <= 90:
        raise argparse.ArgumentTypeError("days must be between 1 and 90")
    return days


async def load_usage_summary(days: int) -> dict[str, Any]:
    database_url = resolve_database_url(required=True)
    schema = os.environ.get("DATABASE_SCHEMA", "sharepoint_mcp")
    monitor = PostgresUsageMonitor(database_url, schema=schema)
    return await monitor.get_summary(days)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read aggregate MCP usage metrics from PostgreSQL."
    )
    parser.add_argument(
        "--days", type=_report_days, default=7, help="UTC reporting window (1-90)"
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )
    return parser


def _run(coroutine):
    """Run psycopg async code with a Windows-compatible selector loop."""
    if sys.platform == "win32":
        loop = asyncio.SelectorEventLoop()
        try:
            return loop.run_until_complete(coroutine)
        finally:
            loop.close()
    return asyncio.run(coroutine)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = _run(load_usage_summary(args.days))
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(format_usage_summary(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
