"""A reader's note on a plan answer reaches the model.

The browser sends the note beside the plan card's answer; the box's relay
path carries it to the harness with the chosen label, and the plan tool's
outcome text hands it to the model on an approval and on a rejection alike.
Driven through the real mirror relay path and the real Claude adapter's plan
tool, with no model and no server.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import CloudRestClient, CloudSocket
from alkera_cli.cloud.mirror import ChatMirror, _relay_of_recorded
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.adapter import SessionConfig
from alkera_cli.harness.adapters.claude_agent import ClaudeAgentAdapter
from alkera_cli.harness.claude_binary import ResolvedClaudeBinary
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.permission_mode import PLAN_ACCEPT_OPTIONS
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import QuestionOption, QuestionPrompt, QuestionRequest

CHAT_ID = "11111111-2222-4333-8444-555555555555"
OWNER = "99999999-8888-4777-8666-555555555555"
APPROVE = PLAN_ACCEPT_OPTIONS[0][0]
REJECT = "The user rejected this plan. Revise it and present an updated plan."
NOTE = "Skip the migration for now."


def _mirror(tmp_path: Path) -> ChatMirror:
    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"), adapter_factory=FakeAdapterFactory(FakeAdapter)
    )
    rest = CloudRestClient(
        api_url="http://objects.test",
        token="device-jwt",
        agent_id=CHAT_ID,
        transport=httpx.MockTransport(lambda _: httpx.Response(404)),
    )
    mirror = ChatMirror(
        chat_id=CHAT_ID,
        runtime=runtime,
        socket=cast(CloudSocket, object()),
        rest=rest,
        user_id=OWNER,
        owner_user_id=OWNER,
    )
    mirror._ready.set()
    return mirror


def _question(kind: str) -> QuestionRequest:
    return QuestionRequest(
        event_id="ev-q",
        time=datetime(2026, 9, 24, tzinfo=UTC),
        session_id=CHAT_ID,
        request_id="q-1",
        kind=kind,  # type: ignore[arg-type]
        questions=[
            QuestionPrompt(
                question="Approve this plan?",
                header="plan",
                options=[QuestionOption(label=label) for label, _ in PLAN_ACCEPT_OPTIONS],
                custom=True,
            )
        ],
    )


async def _relayed(tmp_path: Path, kind: str, relay: dict[str, Any]) -> Any:
    """What the harness receives when ``relay`` answers a parked ask of ``kind``."""
    mirror = _mirror(tmp_path)
    task = asyncio.get_running_loop().create_task(mirror._resolve_question(_question(kind)))
    await asyncio.sleep(0)
    assert mirror.pending_interrupts == ["q-1"]
    mirror._answer_interrupt("q-1", relay)
    return await asyncio.wait_for(task, 1.0)


async def _plan_tool_text(tmp_path: Path, answers: list[list[str]]) -> str:
    """The text the model gets back from the plan tool for ``answers``."""
    config = SessionConfig(session_id="our-sid", project_dir=tmp_path, chat_dir=tmp_path / "chat")
    adapter = ClaudeAgentAdapter(
        config,
        binary=ResolvedClaudeBinary(path=Path("/usr/bin/true"), source="path"),
        event_bus=EventBus(),
    )
    sandbox = config.chat_dir / "sandbox"
    sandbox.mkdir(parents=True, exist_ok=True)
    (sandbox / "plan.md").write_text("# Plan\n\nstep 1")
    sub = adapter._bus.subscribe()
    task = asyncio.create_task(adapter._handle_present_plan("plan.md"))

    async def _ask() -> QuestionRequest:
        async for event in sub:
            if isinstance(event, QuestionRequest):
                return event
        raise AssertionError("no plan question")

    request = await asyncio.wait_for(_ask(), 1.0)
    await adapter.answer_question(request.request_id, answers)
    result: dict[str, Any] = await asyncio.wait_for(task, 1.0)
    return str(result["content"][0]["text"])


@pytest.mark.parametrize(
    ("label", "outcome"),
    [
        pytest.param(APPROVE, "approved", id="an-approval"),
        pytest.param(REJECT, "rejected", id="a-rejection"),
    ],
)
async def test_a_relayed_plan_note_reaches_the_model_with_either_answer(
    tmp_path: Path, label: str, outcome: str
) -> None:
    resolution = await _relayed(
        tmp_path, "plan_approval", {"answers": [[label]], "note": f"  {NOTE}\n"}
    )
    assert resolution == ("answer", [[label, NOTE]])
    text = await _plan_tool_text(tmp_path, resolution[1])
    assert outcome in text.lower()
    assert text.endswith(f"Note from the user: {NOTE}")


async def test_a_note_does_not_change_which_answer_approves(tmp_path: Path) -> None:
    """The label still decides the outcome: a note never turns an approval into
    the free-form rejection a second answer entry could be read as."""
    resolution = await _relayed(tmp_path, "plan_approval", {"answers": [[APPROVE]], "note": NOTE})
    text = await _plan_tool_text(tmp_path, resolution[1])
    assert "rejected" not in text.lower()


@pytest.mark.parametrize(
    "note",
    [pytest.param(None, id="no-note"), pytest.param("   ", id="a-blank-note")],
)
async def test_a_plan_answer_without_a_note_reads_as_before(
    tmp_path: Path, note: str | None
) -> None:
    resolution = await _relayed(tmp_path, "plan_approval", {"answers": [[APPROVE]], "note": note})
    assert resolution == ("answer", [[APPROVE]])
    text = await _plan_tool_text(tmp_path, resolution[1])
    assert "Note from the user" not in text


async def test_a_note_beside_an_ordinary_question_leaves_its_answers_alone(
    tmp_path: Path,
) -> None:
    """Only a plan answer carries a note to the harness; a question's answers
    are the reader's choices and nothing is added to them."""
    resolution = await _relayed(tmp_path, "question", {"answers": [["Postgres"]], "note": NOTE})
    assert resolution == ("answer", [["Postgres"]])


def test_a_recorded_answer_carries_its_note_into_the_relay_it_stands_for() -> None:
    """An answer given while no box held the ask is applied from the row; the
    note on that row goes through the same path a live relay's does."""
    relay = _relay_of_recorded(
        {"event_type": "question.answered", "answers": [[APPROVE]], "note": NOTE}
    )
    assert relay == {"answers": [[APPROVE]], "note": NOTE}
