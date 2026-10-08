"""Validation + round-trip for the observability HTTP schemas."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from alkera_core.schemas.observability import (
    ClientErrorEvent,
    CrashReportCreate,
    CrashReportRead,
)
from pydantic import ValidationError


def test_crash_report_create_minimal() -> None:
    model = CrashReportCreate(component="cli", message="boom")
    assert model.component == "cli"
    assert model.comment is None


def test_crash_report_create_rejects_unknown_component() -> None:
    with pytest.raises(ValidationError):
        CrashReportCreate(component="mainframe", message="boom")


def test_crash_report_create_requires_nonempty_message() -> None:
    with pytest.raises(ValidationError):
        CrashReportCreate(component="cli", message="")


def test_crash_report_read_from_attributes() -> None:
    @dataclass
    class _Row:
        id: UUID
        user_id: UUID
        org_team_id: UUID
        component: str
        error_type: str | None
        message: str
        stacktrace: str | None
        context: dict[str, int] | None
        comment: str | None
        app_version: str | None
        platform: str | None
        occurred_at: datetime | None
        created_at: datetime

    row = _Row(
        id=uuid4(),
        user_id=uuid4(),
        org_team_id=uuid4(),
        component="daemon",
        error_type="HarnessCrashError",
        message="boom",
        stacktrace=None,
        context={"input_tokens": 3},
        comment=None,
        app_version="1.0.0",
        platform="darwin/arm64",
        occurred_at=None,
        created_at=datetime.now(tz=UTC),
    )
    read = CrashReportRead.model_validate(row)
    assert read.component == "daemon"
    assert read.context == {"input_tokens": 3}


def test_client_error_event_defaults_component_web() -> None:
    event = ClientErrorEvent(message="oops")
    assert event.component == "web"
