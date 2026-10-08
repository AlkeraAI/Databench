"""Crash-report daemon method.

The VS Code extension routes user-consented crash reports through the daemon —
the daemon owns the authed token (~/.alkera/auth.yml), so the extension never
handles it. Submission is the same shared `crash_report` library the CLI uses.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Literal

from alkera_cli.daemon.protocol import _DaemonModel, method

if TYPE_CHECKING:
    from alkera_cli.daemon.server import JsonRpcServer

Component = Literal["cli", "daemon", "extension", "web", "backend", "gateway"]


class ReportCrashRequest(_DaemonModel):
    """A user-consented crash report to submit on their behalf."""

    component: Component = "extension"
    message: str
    error_type: str | None = None
    stacktrace: str | None = None
    comment: str | None = None
    logs: str | None = None
    context: dict[str, Any] | None = None


class ReportCrashResponse(_DaemonModel):
    submitted: bool
    report_id: str | None = None


@method("report.submitCrash")
async def report_submit_crash(
    server: JsonRpcServer, params: ReportCrashRequest
) -> ReportCrashResponse:
    """Submit a crash report using the daemon's stored auth. Returns the report
    id (None if the user isn't logged in or the submit failed)."""
    from alkera_cli.observability.crash_report import CrashReportInput, submit_with_stored_auth

    report = CrashReportInput(
        component=params.component,
        message=params.message,
        error_type=params.error_type,
        stacktrace=params.stacktrace,
        comment=params.comment,
        logs=params.logs,
        context=params.context,
    )
    # The SDK is sync — submit off the event loop.
    loop = asyncio.get_running_loop()
    report_id = await loop.run_in_executor(None, submit_with_stored_auth, report)
    return ReportCrashResponse(submitted=report_id is not None, report_id=report_id)


__all__ = ["ReportCrashRequest", "ReportCrashResponse"]
