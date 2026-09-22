import json

import pytest

import mcp_server.usage_report as usage_report


def test_text_report_uses_backend_summary(monkeypatch, capsys):
    summary = {
        "enabled": True,
        "days": 3,
        "unique_users": 2,
        "fallback_identities": 0,
        "daily": [],
        "tools": [],
        "http_issues": [],
    }

    async def load(days):
        assert days == 3
        return summary

    monkeypatch.setattr(usage_report, "load_usage_summary", load)

    assert usage_report.main(["--days", "3"]) == 0
    assert "Unique users: 2" in capsys.readouterr().out


def test_json_report_is_machine_readable(monkeypatch, capsys):
    summary = {"enabled": True, "days": 7, "unique_users": 4}

    async def load(days):
        assert days == 7
        return summary

    monkeypatch.setattr(usage_report, "load_usage_summary", load)

    assert usage_report.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["unique_users"] == 4


def test_report_days_rejects_out_of_range_value():
    with pytest.raises(SystemExit):
        usage_report.main(["--days", "91"])
