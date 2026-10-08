"""Pure-logic tests for resume reconciliation of an interrupted turn.

A daemon crash/restart kills the adapter mid-turn, so the persisted log is left
open (pending tool call, dangling subagent, no turn.finished, `running` status).
``reconcile_interrupted_turn`` closes it on the next open. These exercise the
observable contract: a clean log is untouched (idempotent), and every open kind
gets exactly the right closer.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.resume_reconcile import reconcile_interrupted_turn
from alkera_cli.harness.turn_restart import (
    TURN_RESTART_FAILED,
    TURN_RESTART_NOTE,
    RestartLedger,
    TurnRestart,
    plan_turn_restart,
)
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import (
    Event,
    MessageCompleted,
    MessageCreated,
    PartCreated,
    PartStarted,
    PermissionOption,
    PermissionRequest,
    PermissionResolved,
    PromptCancelled,
    QuestionPrompt,
    QuestionRejected,
    QuestionRequest,
    SessionStatusChanged,
    SubagentCompleted,
    SubagentStarted,
    TextPart,
    ToolCall,
    ToolCallUpdate,
    TurnFinished,
    TurnStarted,
)

T = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)
SID = "s1"


def _ids() -> Callable[[], str]:
    counter = iter(range(10_000))
    return lambda: f"r{next(counter)}"


def _reconcile(events: list[Event]) -> list[Event]:
    return reconcile_interrupted_turn(events, session_id=SID, now=T, next_id=_ids())


def _types(events: list[Event]) -> list[str]:
    return [e.event_type for e in events]  # type: ignore[attr-defined]


# --- envelopes (event_id/time/session_id are required on every event) --------


def _base(n: int) -> dict[str, object]:
    return {"event_id": f"e{n}", "time": T, "session_id": SID}


# --------------------------------------------------------------------------
# no-op cases (a healthy log must be left alone)
# --------------------------------------------------------------------------


def test_empty_log_is_noop() -> None:
    assert _reconcile([]) == []


def test_cleanly_closed_turn_is_noop() -> None:
    events: list[Event] = [
        TurnStarted(**_base(1), turn_id="t1", user_message_id="m1"),
        ToolCall(
            **_base(2), tool_call_id="tc1", message_id="m1", tool_name="read", status="pending"
        ),
        ToolCallUpdate(**_base(3), tool_call_id="tc1", status="completed"),
        TurnFinished(**_base(4), turn_id="t1", stop_reason="end_turn"),
        SessionStatusChanged(**_base(5), status="idle"),
    ]
    assert _reconcile(events) == []


# --------------------------------------------------------------------------
# interrupted cases — each open kind gets the right closer
# --------------------------------------------------------------------------


def test_interrupted_tool_and_turn_are_closed() -> None:
    events: list[Event] = [
        TurnStarted(**_base(1), turn_id="t1", user_message_id="m1"),
        SessionStatusChanged(**_base(2), status="running", phase="running_tool"),
        ToolCall(
            **_base(3), tool_call_id="tc1", message_id="m1", tool_name="sql.query", status="pending"
        ),
    ]
    out = _reconcile(events)
    kinds = _types(out)
    assert "tool.call_update" in kinds
    assert "turn.finished" in kinds
    assert kinds[-1] == "session.status_changed"  # session marked idle LAST

    tool_close = next(e for e in out if isinstance(e, ToolCallUpdate))
    assert tool_close.tool_call_id == "tc1" and tool_close.status == "error"
    turn_close = next(e for e in out if isinstance(e, TurnFinished))
    assert turn_close.turn_id == "t1" and turn_close.stop_reason == "cancelled"
    status = out[-1]
    assert isinstance(status, SessionStatusChanged) and status.status == "idle"


def test_latest_tool_status_wins_completed_not_reclosed() -> None:
    # pending → running → completed: the tool is DONE, so no tool closer (only the
    # turn, which never finished, is closed).
    events: list[Event] = [
        TurnStarted(**_base(1), turn_id="t1", user_message_id="m1"),
        ToolCall(**_base(2), tool_call_id="tc1", message_id="m1", status="pending"),
        ToolCallUpdate(**_base(3), tool_call_id="tc1", status="running"),
        ToolCallUpdate(**_base(4), tool_call_id="tc1", status="completed"),
    ]
    out = _reconcile(events)
    assert not [e for e in out if isinstance(e, ToolCallUpdate)]  # tool already done
    assert any(isinstance(e, TurnFinished) for e in out)  # turn still open → closed


def test_dangling_subagent_is_closed() -> None:
    events: list[Event] = [
        TurnStarted(**_base(1), turn_id="t1", user_message_id="m1"),
        SubagentStarted(**_base(2), child_session_id="child", agent_name="review"),
    ]
    out = _reconcile(events)
    sub = next(e for e in out if isinstance(e, SubagentCompleted))
    assert sub.child_session_id == "child" and sub.error


def test_open_text_part_is_dropped() -> None:
    # An interrupted text part can't be recovered (its token deltas aren't persisted),
    # and persisting an empty finalized part renders as a blank guardrail line in the
    # transcript — so it's DROPPED, not closed. The turn still closes cleanly.
    events: list[Event] = [
        TurnStarted(**_base(1), turn_id="t1", user_message_id="m1"),
        PartStarted(**_base(2), message_id="m1", part_id="p1", part_type="text"),
    ]
    out = _reconcile(events)
    assert not [e for e in out if isinstance(e, PartCreated)]  # no blank part persisted
    assert any(isinstance(e, TurnFinished) for e in out)  # the open turn still closes
    assert isinstance(out[-1], SessionStatusChanged) and out[-1].status == "idle"


def test_multiple_open_text_and_reasoning_parts_are_all_dropped() -> None:
    # The real-world bug: an interrupted turn left several open text parts, so resume
    # reconciliation persisted a stack of empty parts that rendered as blank rails in
    # the TUI. None of them should produce a PartCreated.
    events: list[Event] = [
        TurnStarted(**_base(1), turn_id="t1", user_message_id="m1"),
        SessionStatusChanged(**_base(2), status="running"),
        *[
            PartStarted(**_base(10 + i), message_id="m1", part_id=f"p{i}", part_type="text")
            for i in range(5)
        ],
        PartStarted(**_base(20), message_id="m1", part_id="pr", part_type="reasoning"),
    ]
    out = _reconcile(events)
    assert not [e for e in out if isinstance(e, PartCreated)]  # zero blank parts persisted
    assert any(isinstance(e, TurnFinished) for e in out)
    assert isinstance(out[-1], SessionStatusChanged) and out[-1].status == "idle"


def test_open_permission_request_is_cancelled() -> None:
    events: list[Event] = [
        TurnStarted(**_base(1), turn_id="t1", user_message_id="m1"),
        PermissionRequest(
            **_base(2),
            request_id="rq1",
            permission_kind="edit",
            options=[PermissionOption(option_id="allow_once", name="Allow once")],
        ),
    ]
    out = _reconcile(events)
    res = next(e for e in out if isinstance(e, PermissionResolved))
    assert res.request_id == "rq1" and res.option_id == "cancelled"


def test_open_question_is_rejected() -> None:
    events: list[Event] = [
        TurnStarted(**_base(1), turn_id="t1", user_message_id="m1"),
        QuestionRequest(
            **_base(2), request_id="qr1", questions=[QuestionPrompt(question="Which?")]
        ),
    ]
    out = _reconcile(events)
    assert any(isinstance(e, QuestionRejected) and e.request_id == "qr1" for e in out)


def test_lone_running_status_closes_to_idle() -> None:
    # Even with no other open state, a left-`running` status must flip to idle.
    out = _reconcile([SessionStatusChanged(**_base(1), status="running")])
    assert len(out) == 1
    assert isinstance(out[0], SessionStatusChanged) and out[0].status == "idle"


def test_a_part_whose_message_completed_is_not_an_interruption() -> None:
    """The log a healthy turn leaves on the box: a text part the adapter never
    finalized, its message completed, the session idle, a user message nobody
    has answered yet. Nothing here was interrupted — but the open part tripped
    the detection, and every restart of the daemon wrote one more
    "interrupted" closure into a chat with nothing running (thirteen in one)."""
    events: list[Event] = [
        PartStarted(**_base(1), message_id="m1", part_id="p1", part_type="text"),
        MessageCompleted(**_base(2), message_id="m1", finish_reason="stop"),
        SessionStatusChanged(**_base(3), status="running", phase="awaiting_llm"),
        SessionStatusChanged(**_base(4), status="idle", phase="idle"),
        MessageCreated(**_base(5), message_id="m2", role="user"),
    ]
    assert _reconcile(events) == []


def test_a_part_of_another_message_is_still_open_when_one_completes() -> None:
    events: list[Event] = [
        PartStarted(**_base(1), message_id="m1", part_id="p1", part_type="text"),
        PartStarted(**_base(2), message_id="m2", part_id="p2", part_type="text"),
        MessageCompleted(**_base(3), message_id="m1"),
        SessionStatusChanged(**_base(4), status="running"),
    ]
    out = _reconcile(events)
    assert out and isinstance(out[-1], SessionStatusChanged) and out[-1].status == "idle"


def test_a_dropped_part_does_not_reopen_the_chat_on_the_next_open() -> None:
    """An interrupted text part is dropped, never finalized — so the closure
    leaves the log with that part still unmatched. The next open must read the
    turn's own end as closing it, or every open writes the closure again."""
    events: list[Event] = [
        TurnStarted(**_base(1), turn_id="t1", user_message_id="m1"),
        PartStarted(**_base(2), message_id="m1", part_id="p1", part_type="text"),
        SessionStatusChanged(**_base(3), status="running"),
    ]
    first = _reconcile(events)
    assert _types(first) == ["turn.finished", "session.status_changed"]
    assert _reconcile([*events, *first]) == []
    assert _reconcile([*events, *first, *_reconcile([*events, *first])]) == []


def test_reconciliation_is_idempotent() -> None:
    events: list[Event] = [
        TurnStarted(**_base(1), turn_id="t1", user_message_id="m1"),
        SessionStatusChanged(**_base(2), status="running"),
        ToolCall(**_base(3), tool_call_id="tc1", message_id="m1", status="running"),
        SubagentStarted(**_base(4), child_session_id="child", agent_name="review"),
    ]
    first = _reconcile(events)
    assert first  # something was closed
    # Applying the closures and re-running yields nothing (fixed point).
    assert _reconcile([*events, *first]) == []


# --------------------------------------------------------------------------
# restarting the interrupted turn (the next open runs it again)
# --------------------------------------------------------------------------

QUESTION = "count the rows in orders"


def _asked(n: int = 1, text: str = QUESTION) -> list[Event]:
    return [
        MessageCreated(**_base(n), message_id="u1", role="user"),
        PartCreated(**_base(n + 1), part=TextPart(part_id="up1", message_id="u1", text=text)),
    ]


def _working(n: int) -> list[Event]:
    """A turn mid-flight: the agent is running a tool when the process dies."""
    return [
        SessionStatusChanged(**_base(n), status="running", phase="running_tool"),
        MessageCreated(**_base(n + 1), message_id="a1", role="assistant"),
        ToolCall(
            **_base(n + 2),
            tool_call_id="tc1",
            message_id="a1",
            tool_name="sql.query",
            status="running",
        ),
    ]


def _answered(n: int) -> list[Event]:
    return [
        SessionStatusChanged(**_base(n), status="running"),
        MessageCreated(**_base(n + 1), message_id="a1", role="assistant"),
        PartCreated(**_base(n + 2), part=TextPart(part_id="ap1", message_id="a1", text="42")),
        MessageCompleted(**_base(n + 3), message_id="a1", finish_reason="stop"),
    ]


@pytest.mark.parametrize(
    ("events", "restarted", "restarted_text", "expected"),
    [
        pytest.param([*_asked(), *_working(3)], 0, "", TurnRestart(QUESTION, 1), id="interrupted"),
        pytest.param(
            [*_asked(), *_working(3)], 2, QUESTION, TurnRestart(QUESTION, 3), id="third-restart"
        ),
        pytest.param(
            [*_asked(), *_working(3)],
            3,
            QUESTION,
            TurnRestart(QUESTION, 4, give_up=True),
            id="fourth-interruption-gives-up",
        ),
        pytest.param(
            [*_asked(), *_working(3)],
            3,
            "an older question",
            TurnRestart(QUESTION, 1),
            id="another-message-starts-the-count-again",
        ),
        pytest.param([*_asked(), *_answered(3)], 0, "", None, id="answer-completed"),
        pytest.param(
            [
                *_asked(),
                *_working(3),
                PromptCancelled(**_base(9), message_id="u1", client_id="c1"),
            ],
            0,
            "",
            None,
            id="cancelled",
        ),
        pytest.param(_working(1), 0, "", None, id="no-message-to-restart-from"),
    ],
)
def test_what_the_next_open_does_about_the_last_turn(
    events: list[Event], restarted: int, restarted_text: str, expected: TurnRestart | None
) -> None:
    plan = plan_turn_restart(
        events,
        interrupted=bool(_reconcile(events)),
        restarted=restarted,
        restarted_text=restarted_text,
    )
    assert plan == expected


def test_a_cleanly_closed_turn_is_never_restarted() -> None:
    """A turn the person stopped closes itself: the next open finds nothing
    open, so nothing restarts it however the log reads."""
    events = [*_asked(), *_working(3), SessionStatusChanged(**_base(9), status="idle")]
    events[-2] = ToolCall(
        **_base(5), tool_call_id="tc1", message_id="a1", tool_name="sql.query", status="error"
    )
    assert _reconcile(events) == []
    assert plan_turn_restart(events, interrupted=False) is None


def _interrupted_chat(
    tmp_path: Path, *, ledger: tuple[int, str] | None = None
) -> tuple[HarnessRuntime, FakeAdapterFactory, str]:
    project = ProjectDirectory(tmp_path / ".alkera")
    factory = FakeAdapterFactory(lambda: FakeAdapter(reply_text="the answer"))
    runtime = HarnessRuntime(project, adapter_factory=factory)
    chat = runtime._chats_store.create(title="c", harness_type="agent")
    for event in [*_asked(), *_working(3)]:
        chat.append_event(event)
    if ledger is not None:
        attempts, text = ledger
        RestartLedger(chat.path).record(TurnRestart(text, attempts))
    sid = chat.session_id
    chat.close()
    return runtime, factory, sid


async def test_an_interrupted_turn_restarts_from_the_same_message(tmp_path: Path) -> None:
    runtime, factory, sid = _interrupted_chat(tmp_path)
    session = await runtime.open_chat(sid)
    try:
        assert session.interrupted_turn == TurnRestart(QUESTION, 1)
        assert await session.restart_interrupted_turn() is True
        sent = factory.adapters[-1].sent_prompts
        assert [prompt.text for prompt in sent] == [QUESTION]
        assert TURN_RESTART_NOTE in (sent[0].system or "")
        assert session.interrupted_turn is None
        assert await session.restart_interrupted_turn() is False
    finally:
        await runtime.close_chat(sid)

    chat = runtime._chats_store.open(sid)
    try:
        events = list(chat.events())
    finally:
        chat.close()
    # The interrupted attempt stays, closed as interrupted, and the new attempt
    # follows it with its own turn events and answer.
    tool_ids = [e.tool_call_id for e in events if isinstance(e, ToolCall)]
    assert tool_ids == ["tc1"]
    closed = [e for e in events if isinstance(e, ToolCallUpdate) and e.tool_call_id == "tc1"]
    assert closed and closed[0].status == "error"
    answers = [
        e.part.text
        for e in events
        if isinstance(e, PartCreated) and isinstance(e.part, TextPart) and e.part.text
    ]
    assert answers[-1] == "the answer"
    statuses = [e.status for e in events if isinstance(e, SessionStatusChanged)]
    assert statuses[-2:] == ["running", "idle"]


async def test_a_restart_is_counted_before_it_runs(tmp_path: Path) -> None:
    """A process that dies again during the restart must find it counted, or
    a turn that always kills its box would restart for ever."""
    runtime, _factory, sid = _interrupted_chat(tmp_path, ledger=(1, QUESTION))
    session = await runtime.open_chat(sid)
    try:
        await session.restart_interrupted_turn()
        chat_dir = runtime._chats_store.path / sid
        assert RestartLedger(chat_dir).read() == (2, QUESTION)
    finally:
        await runtime.close_chat(sid)


async def test_the_fourth_interruption_closes_the_turn_as_failed(tmp_path: Path) -> None:
    runtime, factory, sid = _interrupted_chat(tmp_path, ledger=(3, QUESTION))
    session = await runtime.open_chat(sid)
    try:
        assert session.interrupted_turn == TurnRestart(QUESTION, 4, give_up=True)
        assert await session.restart_interrupted_turn() is False
        assert await session.restart_interrupted_turn(QUESTION) is False
        assert factory.adapters[-1].sent_prompts == []
    finally:
        await runtime.close_chat(sid)
    chat = runtime._chats_store.open(sid)
    try:
        events = list(chat.events())
    finally:
        chat.close()
    last = events[-1]
    assert isinstance(last, SessionStatusChanged)
    assert (last.status, last.detail) == ("error", TURN_RESTART_FAILED)


async def test_a_caller_that_names_the_message_restarts_it_and_is_counted(
    tmp_path: Path,
) -> None:
    """A box that did not run the turn restarts it from the chat's record;
    the count is still per message, and the fourth try is refused."""
    project = ProjectDirectory(tmp_path / ".alkera")
    factory = FakeAdapterFactory(lambda: FakeAdapter())
    runtime = HarnessRuntime(project, adapter_factory=factory)
    chat = runtime._chats_store.create(title="c", harness_type="agent")
    sid = chat.session_id
    chat.close()
    sent: list[str] = []
    for n in range(4):
        # Each restart dies mid-turn: the log is left running, so the next
        # open finds it interrupted rather than finished.
        chat = runtime._chats_store.open(sid)
        chat.append_event(SessionStatusChanged(**_base(100 + n), status="running"))
        chat.close()
        session = await runtime.open_chat(sid)
        try:
            if await session.restart_interrupted_turn(QUESTION):
                sent.append(factory.adapters[-1].sent_prompts[-1].text)
        finally:
            await runtime.close_chat(sid)
    assert sent == [QUESTION] * 3


async def test_a_restart_that_finished_starts_the_count_again(tmp_path: Path) -> None:
    runtime, _factory, sid = _interrupted_chat(tmp_path, ledger=(3, QUESTION))
    chat = runtime._chats_store.open(sid)
    chat.append_event(ToolCallUpdate(**_base(50), tool_call_id="tc1", status="completed"))
    chat.append_event(SessionStatusChanged(**_base(51), status="idle"))
    chat.close()
    session = await runtime.open_chat(sid)
    try:
        assert session.interrupted_turn is None
    finally:
        await runtime.close_chat(sid)
    assert RestartLedger(runtime._chats_store.path / sid).read() == (0, "")


async def test_can_restart_says_what_restart_would_do(tmp_path: Path) -> None:
    runtime, _factory, sid = _interrupted_chat(tmp_path, ledger=(3, "another message"))
    session = await runtime.open_chat(sid)
    try:
        assert session.can_restart(QUESTION) is True
        assert session.can_restart("another message") is False
    finally:
        await runtime.close_chat(sid)
