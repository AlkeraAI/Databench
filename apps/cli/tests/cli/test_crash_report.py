"""Crash-report library + daemon report.submitCrash."""

from __future__ import annotations

import pytest
from alkera_cli.observability import crash_report
from alkera_cli.observability.crash_report import (
    CrashReportInput,
    build_payload,
    exception_to_fields,
)


def test_build_payload_scrubs_and_adds_context() -> None:
    payload = build_payload(
        CrashReportInput(
            component="cli",
            message="failed at /Users/bob/x with Bearer abcdef0123456789",
            comment="key sk-proj-ABCDEFGHIJKLMNOP1234",
        )
    )
    assert payload.component.value == "cli"
    assert "/Users/bob" not in payload.message
    assert "~/x" in payload.message
    assert "abcdef0123456789" not in payload.message
    assert "sk-proj-ABCDEFGHIJKLMNOP1234" not in payload.comment
    ctx = payload.context.to_dict()
    assert ctx["os"] and ctx["arch"] and ctx["python"]
    assert payload.app_version
    assert "/" in payload.platform


def test_exception_to_fields_scrubs() -> None:
    try:
        raise ValueError("oops at /Users/bob/secret.py")
    except ValueError as exc:
        error_type, message, stack = exception_to_fields(exc)
    assert error_type == "ValueError"
    assert "/Users/bob" not in message
    assert "/Users/bob" not in stack
    assert "ValueError" in stack


async def test_daemon_report_method_submits(monkeypatch: pytest.MonkeyPatch) -> None:
    from alkera_cli.daemon.methods.report import ReportCrashRequest, report_submit_crash

    captured: dict[str, object] = {}

    def _fake_submit(report: CrashReportInput) -> str:
        captured["component"] = report.component
        captured["message"] = report.message
        return "rid-9"

    monkeypatch.setattr(crash_report, "submit_with_stored_auth", _fake_submit)
    resp = await report_submit_crash(
        None,  # type: ignore[arg-type]
        ReportCrashRequest(component="extension", message="daemon crashed"),
    )
    assert resp.submitted is True
    assert resp.report_id == "rid-9"
    assert captured["component"] == "extension"


async def test_daemon_report_method_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from alkera_cli.daemon.methods.report import ReportCrashRequest, report_submit_crash

    monkeypatch.setattr(crash_report, "submit_with_stored_auth", lambda _r: None)
    resp = await report_submit_crash(
        None,  # type: ignore[arg-type]
        ReportCrashRequest(message="no auth"),
    )
    assert resp.submitted is False
    assert resp.report_id is None
