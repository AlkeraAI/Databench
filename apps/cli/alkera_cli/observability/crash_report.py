"""Crash-report collection + submission.

Shared by the CLI's crash reporting and the daemon's `report.submitCrash` RPC.
Collects analytics-safe context (version, OS/arch — never file
contents/prompts/secrets), scrubs every free-text field client-side, and POSTs
to /api/v1/errors/reports via the typed SDK.
"""

from __future__ import annotations

import platform
import sys
import traceback
from dataclasses import dataclass
from typing import Any

from alkera_core.observability.redaction import scrub_path, scrub_text
from alkera_sdk import AlkeraClient
from alkera_sdk._generated.models.crash_report_create import CrashReportCreate
from alkera_sdk._generated.models.crash_report_create_component import CrashReportCreateComponent
from alkera_sdk._generated.models.crash_report_create_context_type_0 import (
    CrashReportCreateContextType0,
)
from alkera_sdk._generated.types import UNSET, Unset

from alkera_cli.account.binding import resolve_profile_or_none


def _scrub(text: str | None) -> str | None:
    if not text:
        return text
    return scrub_path(scrub_text(text))


def _to_optional(value: str | None) -> str | Unset:
    return value if value else UNSET


def collect_context() -> dict[str, Any]:
    """Analytics-safe environment context — ids/versions/counts only."""
    return {
        "python": sys.version.split()[0],
        "os": platform.system(),
        "arch": platform.machine(),
    }


def platform_string() -> str:
    return f"{platform.system().lower()}/{platform.machine()}"


def app_version() -> str:
    from alkera_cli import __version__

    return __version__


def exception_to_fields(exc: BaseException) -> tuple[str, str, str]:
    """(error_type, scrubbed message, scrubbed stack trace) from an exception."""
    error_type = type(exc).__name__
    message = _scrub(str(exc)) or error_type
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return error_type, message, _scrub(tb) or ""


@dataclass(frozen=True, slots=True)
class CrashReportInput:
    component: str
    message: str
    error_type: str | None = None
    stacktrace: str | None = None
    comment: str | None = None
    logs: str | None = None
    context: dict[str, Any] | None = None


def build_payload(report: CrashReportInput) -> CrashReportCreate:
    ctx = {**collect_context(), **(report.context or {})}
    return CrashReportCreate(
        component=CrashReportCreateComponent(report.component),
        message=_scrub(report.message) or report.message,
        error_type=_to_optional(report.error_type),
        stacktrace=_to_optional(_scrub(report.stacktrace)),
        comment=_to_optional(_scrub(report.comment)),
        logs=_to_optional(_scrub(report.logs)),
        app_version=app_version(),
        platform=platform_string(),
        context=CrashReportCreateContextType0.from_dict(ctx),
    )


def submit(
    report: CrashReportInput, *, api_url: str, token: str, timeout: float = 10.0
) -> str | None:
    """Submit a crash report; return its id, or None on any failure."""
    payload = build_payload(report)
    try:
        with AlkeraClient(base_url=api_url, token=token, timeout=timeout) as api:
            result = api.errors.submit_crash_report(payload)
    except Exception:  # reporting must never raise into the caller
        return None
    return str(result.id)


def submit_with_stored_auth(report: CrashReportInput) -> str | None:
    """Submit as the sign-in profile resolved for this process (``--org`` /
    ``ALKERA_ORG``, else the current one). None if the user isn't logged in or
    the submission fails."""
    stored = resolve_profile_or_none()
    if stored is None:
        return None
    return submit(report, api_url=stored.api_url, token=stored.token)
