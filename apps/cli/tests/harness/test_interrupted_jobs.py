"""A background job a restart cut off is reported, never silently lost.

The job died with the process that ran it. The next open finds its START card
with no FINISH card, and the session tells the model and the reader that it
was interrupted by a machine restart, once."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.interrupted_jobs import (
    InterruptedJob,
    interrupted_background_jobs,
    interruption_notice,
)
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import Event, SessionStatusChanged, ToolCall, ToolCallUpdate

T0 = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)


def _card(prefix: str, job: str, *, at: datetime, tool: str = "bash") -> ToolCall:
    return ToolCall(
        event_id=f"{prefix}{job}-{at.timestamp()}",
        time=at,
        session_id="chat-1",
        tool_call_id=f"{prefix}{job}",
        message_id="",
        tool_name=tool,
        input={"command": "sleep 300 && echo done > a.txt"},
        status="running" if prefix == "bgjob:" else "completed",
    )


def _status(at: datetime) -> SessionStatusChanged:
    return SessionStatusChanged(
        event_id=f"s-{at.timestamp()}", time=at, session_id="chat-1", status="idle"
    )


def test_a_started_job_with_no_finish_card_was_interrupted() -> None:
    events: list[Event] = [
        _card("bgjob:", "job_a", at=T0),
        _status(T0 + timedelta(seconds=5)),
        _card("bgjob:", "job_b", at=T0 + timedelta(seconds=10)),
        _card("bgdone:", "job_b", at=T0 + timedelta(seconds=40)),
        _status(T0 + timedelta(minutes=7, seconds=10)),
    ]
    (job,) = interrupted_background_jobs(events)
    assert (job.job_id, job.kind, job.started) == ("job_a", "bash", T0)
    assert job.ended == T0 + timedelta(minutes=7, seconds=10)
    assert job.minutes == 8


@pytest.mark.parametrize(
    ("tool", "kind"),
    [
        pytest.param("bash", "bash", id="bash"),
        pytest.param("sql.query", "sql", id="sql"),
        pytest.param("call_integration_sdk", "integration_sdk", id="integration"),
    ],
)
def test_every_card_kind_of_background_job_is_found(tool: str, kind: str) -> None:
    (job,) = interrupted_background_jobs([_card("bgjob:", "j", at=T0, tool=tool)])
    assert job.kind == kind


def test_a_card_that_is_no_background_job_is_left_alone() -> None:
    assert interrupted_background_jobs([_card("bgjob:", "j", at=T0, tool="read")]) == []
    assert interrupted_background_jobs([]) == []


def test_the_notice_names_the_job_the_restart_and_how_long_it_ran() -> None:
    job = InterruptedJob(
        job_id="job_a",
        kind="bash",
        started=T0,
        ended=T0 + timedelta(seconds=30),
        input={"command": "sleep 300"},
    )
    assert interruption_notice(job) == (
        "The background job job_a (sleep 300) was interrupted by a machine restart "
        "after 1 minute; it did not finish."
    )


async def test_the_reopened_session_tells_the_model_and_the_reader_once(tmp_path: Path) -> None:
    project = ProjectDirectory(tmp_path / ".alkera")
    factory = FakeAdapterFactory(lambda: FakeAdapter(reply_text="it was interrupted"))
    runtime = HarnessRuntime(project, adapter_factory=factory)
    chat = runtime._chats_store.create(title="c", harness_type="agent")
    for event in (_card("bgjob:", "job_a", at=T0), _status(T0 + timedelta(minutes=3))):
        chat.append_event(event)
    sid = chat.session_id
    chat.close()

    session = await runtime.open_chat(sid)
    try:
        assert await session.report_interrupted_jobs() == 1
        assert await session.report_interrupted_jobs() == 0
        prompts = [prompt.text for prompt in factory.adapters[-1].sent_prompts]
        assert len(prompts) == 1
        assert "job_a" in prompts[0] and "interrupted by a machine restart" in prompts[0]
    finally:
        await runtime.close_chat(sid)

    chat = runtime._chats_store.open(sid)
    try:
        events = list(chat.events())
    finally:
        chat.close()
    finish = [
        e for e in events if isinstance(e, ToolCallUpdate) and e.tool_call_id == "bgdone:job_a"
    ]
    assert finish and finish[-1].status == "error"
    assert "interrupted by a machine restart after 3 minutes" in (finish[-1].error_text or "")

    # Reported once: the next open finds the job finished.
    again = await runtime.open_chat(sid)
    try:
        assert await again.report_interrupted_jobs() == 0
    finally:
        await runtime.close_chat(sid)
