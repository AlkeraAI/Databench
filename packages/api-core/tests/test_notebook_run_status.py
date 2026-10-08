"""A notebook run's status only moves forward, except the platform's own
guess that the box was silent, which yields to what the engine says."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from alkera_core.notebooks.models import NotebookRun
from alkera_core.notebooks.runs import MACHINE_SILENT, advance

AT = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def _run(status: str, reason: str | None = None) -> NotebookRun:
    return NotebookRun(
        run_id=uuid.uuid4(),
        status=status,
        reason=reason,
        finished_at=AT if status in {"refused", "ok"} else None,
    )


@pytest.mark.parametrize(
    ("status", "reason", "moved", "after"),
    [
        pytest.param("running", None, True, ("running", None), id="started"),
        pytest.param("ok", None, True, ("ok", None), id="finished"),
        pytest.param("queued", None, True, ("queued", None), id="answered-queued"),
        pytest.param("refused", "kernel_busy", True, ("refused", "kernel_busy"), id="refused"),
        pytest.param(
            "refused", MACHINE_SILENT, False, ("refused", MACHINE_SILENT), id="silent-again"
        ),
    ],
)
def test_a_silenced_run_takes_the_engine_s_word(
    status: str, reason: str | None, moved: bool, after: tuple[str, str | None]
) -> None:
    run = _run("refused", MACHINE_SILENT)
    assert advance(run, status, reason) is moved
    assert (run.status, run.reason) == after
    if after[0] in {"queued", "running"}:
        assert run.finished_at is None


@pytest.mark.parametrize(
    ("ended", "reason"),
    [
        pytest.param("refused", "upstream_being_edited", id="refused-by-the-engine"),
        pytest.param("refused", "not_sent", id="never-sent"),
        pytest.param("ok", None, id="finished"),
    ],
)
def test_any_other_ending_is_final(ended: str, reason: str | None) -> None:
    run = _run(ended, reason)
    assert advance(run, "running") is False
    assert (run.status, run.reason, run.finished_at) == (ended, reason, AT)


def test_a_late_queued_never_undoes_running() -> None:
    run = _run("running")
    assert advance(run, "queued") is False
    assert run.status == "running"
