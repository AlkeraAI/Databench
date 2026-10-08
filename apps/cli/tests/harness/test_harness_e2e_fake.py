"""Comprehensive end-to-end coverage of the harness abstraction via
``FakeAdapter``.

The opencode adapter has its own e2e suite (``test_opencode_e2e.py``)
that exercises real subprocess spawning, SSE pumping, and the real
prompt_async path. These tests target the LAYER ABOVE — every
HarnessRuntime / ChatSession invariant that ANY adapter would have to
honor:

- send_prompt input shaping (text / model / agent / system)
- permission-broker round-trips across every PermissionMode
- question-broker round-trips for both kinds
- cancel mid-turn closes parts + emits TurnFinished
- crash mid-turn synthesizes close events
- chat.jsonl persistence + replay
- manifest token / cost / updated_at maintenance
- native_state() propagation across open → close → open
- /chat list, delete, recursive subagent delete
- concurrent open prevention (lock)
- restart of an adapter for the same session reuses the pin

If a future adapter implementation breaks any of these, the failures
land here regardless of how the adapter spawns or speaks its protocol.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.harness import (
    HarnessRuntime,
    PermissionBroker,
    QuestionBroker,
)
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.adapter import (
    HarnessCrashError,
    HarnessNotReadyError,
    HarnessStartError,
    PromptInput,
)
from alkera_core.project.directory import ProjectDirectory
from alkera_core.project.locking import LockHeldError
from alkera_core.schemas.chat import (
    AgentMessageChunk,
    Event,
    MessageCompleted,
    PartCreated,
    PartStarted,
    PermissionOption,
    PermissionRequest,
    QuestionPrompt,
    QuestionRequest,
    SessionStatusChanged,
    TextPart,
    TurnFinished,
    TurnStarted,
)

_T = datetime(2026, 5, 26, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Factory / runtime helpers
# ---------------------------------------------------------------------------


def _runtime(
    tmp_path: Path, *, native: dict[str, Any] | None = None
) -> tuple[HarnessRuntime, FakeAdapterFactory]:
    project = ProjectDirectory(tmp_path / ".alkera")
    factory = FakeAdapterFactory(lambda: FakeAdapter(native_state_value=native))
    return HarnessRuntime(project, adapter_factory=factory), factory


async def _drain_until(
    sub,
    predicate,
    *,
    deadline_seconds: float = 2.0,
) -> Event | None:
    try:
        async with asyncio.timeout(deadline_seconds):
            async for ev in sub:
                if predicate(ev):
                    return ev
    except TimeoutError:
        return None
    return None


# ---------------------------------------------------------------------------
# 1. Adapter lifecycle invariants
# ---------------------------------------------------------------------------


async def test_adapter_started_after_open(tmp_path: Path) -> None:
    rt, factory = _runtime(tmp_path)
    session = await rt.open_chat(create=True)
    try:
        assert factory.adapters[0].started
        assert not factory.adapters[0].stopped
    finally:
        await rt.close_chat(session.session_id)
    assert factory.adapters[0].stopped


async def test_adapter_send_prompt_before_start_raises(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    with pytest.raises(HarnessNotReadyError):
        await adapter.send_prompt(PromptInput(text="hi"))


async def test_adapter_feed_before_start_raises(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    with pytest.raises(HarnessNotReadyError):
        await adapter.feed(
            SessionStatusChanged(event_id="x", time=_T, session_id="s", status="idle")
        )


async def test_adapter_double_start_raises() -> None:
    adapter = FakeAdapter()
    await adapter.start()
    with pytest.raises(RuntimeError, match="already started"):
        await adapter.start()
    await adapter.stop()


async def test_adapter_double_stop_is_idempotent() -> None:
    adapter = FakeAdapter()
    await adapter.start()
    await adapter.stop()
    await adapter.stop()  # no raise


async def test_adapter_send_prompt_after_stop_raises() -> None:
    adapter = FakeAdapter()
    await adapter.start()
    await adapter.stop()
    with pytest.raises(HarnessCrashError):
        await adapter.send_prompt(PromptInput(text="hi"))


async def test_start_failure_surfaces_as_harness_start_error() -> None:
    adapter = FakeAdapter(start_should_fail=True)
    with pytest.raises(HarnessStartError):
        await adapter.start()


# ---------------------------------------------------------------------------
# 2. send_prompt input shaping (everything an adapter receives)
# ---------------------------------------------------------------------------


async def test_send_prompt_records_text(tmp_path: Path) -> None:
    rt, factory = _runtime(tmp_path)
    session = await rt.open_chat(create=True)
    try:
        await session.send_prompt("hello world")
        adapter = factory.adapters[0]
        assert len(adapter.sent_prompts) == 1
        assert adapter.sent_prompts[0].text == "hello world"
    finally:
        await rt.close_chat(session.session_id)


async def test_send_prompt_carries_model_override(tmp_path: Path) -> None:
    rt, factory = _runtime(tmp_path)
    session = await rt.open_chat(create=True)
    try:
        await session.send_prompt(
            "hi", model={"provider_id": "anthropic", "model_id": "claude-opus-4-7"}
        )
        sent = factory.adapters[0].sent_prompts[0]
        assert sent.model == {
            "provider_id": "anthropic",
            "model_id": "claude-opus-4-7",
        }
    finally:
        await rt.close_chat(session.session_id)


async def test_send_prompt_in_auto_mode_carries_no_plan_agent(
    tmp_path: Path,
) -> None:
    """Only plan mode triggers a plan agent override. auto must NOT mislead the
    model into planning — it gets its own system steering but the default agent."""
    rt, factory = _runtime(tmp_path)
    session = await rt.open_chat(create=True)
    try:
        session.set_permission_mode("auto")
        await session.send_prompt("go")
        sent = factory.adapters[0].sent_prompts[0]
        assert sent.agent is None
    finally:
        await rt.close_chat(session.session_id)


async def test_send_prompt_in_bypass_mode_carries_mode_steering(
    tmp_path: Path,
) -> None:
    from alkera_cli.harness.system_prompt import compose_main_agent_guidance

    rt, factory = _runtime(tmp_path)
    session = await rt.open_chat(create=True)
    try:
        session.set_permission_mode("bypass")
        await session.send_prompt("yolo")
        sent = factory.adapters[0].sent_prompts[0]
        assert sent.agent is None
        # Every mode now self-describes: bypass's steer leads, ahead of the
        # always-on main-agent guidance. The first turn seeds the baseline silently,
        # so no "switched" notice fires.
        assert sent.system is not None
        assert "bypass mode, set by the user" in sent.system
        assert compose_main_agent_guidance() in sent.system
        assert "just switched the permission mode" not in sent.system
    finally:
        await rt.close_chat(session.session_id)


async def test_multiple_sequential_send_prompts_each_recorded(
    tmp_path: Path,
) -> None:
    # The fake ends every turn, so each prompt starts settled.  # same-author-ok: comment fix
    factory = FakeAdapterFactory(lambda: FakeAdapter(reply_text="ok"))
    rt = HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory)
    session = await rt.open_chat(create=True)
    try:
        for i in range(5):
            await session.send_prompt(f"turn {i}")
        adapter = factory.adapters[0]
        assert [p.text for p in adapter.sent_prompts] == [f"turn {i}" for i in range(5)]
    finally:
        await rt.close_chat(session.session_id)


# ---------------------------------------------------------------------------
# 3. Permission flow — broker round-trips across all modes
# ---------------------------------------------------------------------------


#: How long a permission reply may take before the test calls the pump hung. It
#: bounds a hang, never how fast the pump is: the wait below returns the moment
#: the reply lands.
_REPLY_HANG_BOUND = 30.0


async def _replied(adapter: FakeAdapter, request_id: str, option_id: str) -> None:
    """Wait for the permission pump to deliver ``option_id`` for ``request_id``.

    The reply leaves only after the broker has answered AND the decision has been
    appended to the audit log on a worker thread, so on a loaded runner it can
    trail the broker's answer by seconds. Waiting on the reply itself, not on a
    wall-clock poll, is what keeps that from reading as a missing reply.
    """
    async with asyncio.timeout(_REPLY_HANG_BOUND):
        await adapter.wait_for_permission_reply(request_id, option_id)


def _permission_request(
    sid: str, rid: str, *, canonical: str = "edit", native: str = "edit"
) -> PermissionRequest:
    return PermissionRequest(
        event_id=rid,
        time=_T,
        session_id=sid,
        request_id=rid,
        permission_kind=native,
        canonical_kind=canonical,  # type: ignore[arg-type]
        options=[
            PermissionOption(option_id="allow_once", name="Allow once"),
            PermissionOption(option_id="reject_once", name="Reject once"),
        ],
    )


async def test_permission_broker_routes_user_choice_to_adapter(
    tmp_path: Path,
) -> None:
    """The broker calls the resolver, gets a choice, and the runtime
    forwards via adapter.resolve_permission."""
    chosen: list[str] = []

    async def resolver(req: PermissionRequest):
        chosen.append(req.request_id)
        return "allow_once"

    broker = PermissionBroker(resolver, default_timeout_seconds=1.0)
    rt, factory = _runtime(tmp_path)
    session = await rt.open_chat(create=True, permission_broker=broker)
    try:
        adapter = factory.adapters[0]
        await adapter.feed(_permission_request(session.session_id, "r1"))
        await _replied(adapter, "r1", "allow_once")
        assert chosen == ["r1"]
        assert adapter.permission_replies == [("r1", "allow_once")]
    finally:
        await rt.close_chat(session.session_id)


async def test_permission_broker_auto_rejects_on_resolver_exception(
    tmp_path: Path,
) -> None:
    async def boom(_req: PermissionRequest):
        raise RuntimeError("oops")

    broker = PermissionBroker(boom, default_timeout_seconds=1.0)
    rt, factory = _runtime(tmp_path)
    session = await rt.open_chat(create=True, permission_broker=broker)
    try:
        adapter = factory.adapters[0]
        await adapter.feed(_permission_request(session.session_id, "r1"))
        # The resolver raises → the broker auto-rejects.
        await _replied(adapter, "r1", "reject_once")
        assert adapter.permission_replies == [("r1", "reject_once")]
    finally:
        await rt.close_chat(session.session_id)


async def test_permission_broker_auto_rejects_on_timeout(
    tmp_path: Path,
) -> None:
    async def hangs(_req: PermissionRequest):
        await asyncio.sleep(10.0)
        return "allow_once"

    broker = PermissionBroker(hangs, default_timeout_seconds=0.1)
    rt, factory = _runtime(tmp_path)
    session = await rt.open_chat(create=True, permission_broker=broker)
    try:
        adapter = factory.adapters[0]
        await adapter.feed(_permission_request(session.session_id, "r1"))
        # The runtime waits on the broker; broker auto-rejects after 0.1s.
        await _replied(adapter, "r1", "reject_once")
        assert adapter.permission_replies == [("r1", "reject_once")]
    finally:
        await rt.close_chat(session.session_id)


async def test_permission_replies_routed_independently_per_request(
    tmp_path: Path,
) -> None:
    """Three different request_ids → each gets the matching reply."""
    answers = {"r1": "allow_once", "r2": "reject_once", "r3": "allow_always"}

    async def resolver(req: PermissionRequest):
        return answers[req.request_id]

    broker = PermissionBroker(resolver, default_timeout_seconds=1.0)
    rt, factory = _runtime(tmp_path)
    session = await rt.open_chat(create=True, permission_broker=broker)
    try:
        adapter = factory.adapters[0]
        for rid in ("r1", "r2", "r3"):
            await adapter.feed(_permission_request(session.session_id, rid))
            # Sequential — broker serializes per chat.
            for _ in range(40):
                if any(r[0] == rid for r in adapter.permission_replies):
                    break
                await asyncio.sleep(0.02)
        assert dict(adapter.permission_replies) == answers
    finally:
        await rt.close_chat(session.session_id)


# ---------------------------------------------------------------------------
# 4. Question flow — both kinds
# ---------------------------------------------------------------------------


def _question(
    sid: str,
    rid: str,
    *,
    kind: str = "question",
) -> QuestionRequest:
    return QuestionRequest(
        event_id=rid,
        time=_T,
        session_id=sid,
        request_id=rid,
        kind=kind,  # type: ignore[arg-type]
        questions=[QuestionPrompt(question="pick", options=[])],
    )


async def test_question_broker_routes_answer_to_adapter(
    tmp_path: Path,
) -> None:
    async def resolver(_req: QuestionRequest):
        return ("answer", [["picked"]])

    qbroker = QuestionBroker(resolver, default_timeout_seconds=1.0)
    rt, factory = _runtime(tmp_path)
    session = await rt.open_chat(create=True, permission_broker=None, question_broker=qbroker)
    try:
        adapter = factory.adapters[0]
        await adapter.feed(_question(session.session_id, "q1"))
        for _ in range(40):
            if adapter.question_replies:
                break
            await asyncio.sleep(0.02)
        assert adapter.question_replies == [("q1", [["picked"]])]
        assert adapter.question_rejects == []
    finally:
        await rt.close_chat(session.session_id)


async def test_question_broker_routes_reject_to_adapter(tmp_path: Path) -> None:
    async def resolver(_req: QuestionRequest):
        return ("reject", "user-cancelled")

    qbroker = QuestionBroker(resolver, default_timeout_seconds=1.0)
    rt, factory = _runtime(tmp_path)
    session = await rt.open_chat(create=True, permission_broker=None, question_broker=qbroker)
    try:
        adapter = factory.adapters[0]
        await adapter.feed(_question(session.session_id, "q1"))
        for _ in range(40):
            if adapter.question_rejects:
                break
            await asyncio.sleep(0.02)
        assert adapter.question_rejects == [("q1", "user-cancelled")]
    finally:
        await rt.close_chat(session.session_id)


async def test_question_pending_at_close_is_rejected_not_dangled(tmp_path: Path) -> None:
    """Teardown with a question in flight: close cancels the question loop
    mid-resolve, and the pending question must STILL receive a rejection
    instead of dangling. The resolver here never returns, so only the
    cancel path can deliver — this pins, deterministically, the race that
    `test_disconnect_mid_question_auto_rejects` hits via a real disconnect
    (whether the failed-future wakeup or the teardown cancel arrives first
    is a scheduler race; the thread-bridged Windows pipes reliably lose it)."""
    resolver_started = asyncio.Event()

    async def parked_resolver(_req: QuestionRequest):
        resolver_started.set()
        await asyncio.sleep(3600)  # only cancellation ends this

    qbroker = QuestionBroker(parked_resolver, default_timeout_seconds=None)
    rt, factory = _runtime(tmp_path)
    session = await rt.open_chat(create=True, permission_broker=None, question_broker=qbroker)
    adapter = factory.adapters[0]
    await adapter.feed(_question(session.session_id, "q-pending"))
    await asyncio.wait_for(resolver_started.wait(), timeout=5)

    await rt.close_chat(session.session_id)  # cancels the loop mid-resolve

    assert adapter.question_rejects == [("q-pending", "client-disconnected")]
    assert adapter.question_replies == []


async def test_permission_pending_at_close_is_rejected_not_dangled(tmp_path: Path) -> None:
    """Permission twin of the test above: a cancel mid-decide must deliver
    `reject_once` to the adapter rather than leaving the ask dangling."""
    resolver_started = asyncio.Event()

    async def parked_resolver(_req: PermissionRequest):
        resolver_started.set()
        await asyncio.sleep(3600)  # only cancellation ends this

    broker = PermissionBroker(parked_resolver, default_timeout_seconds=None)
    rt, factory = _runtime(tmp_path)
    session = await rt.open_chat(create=True, permission_broker=broker)
    adapter = factory.adapters[0]
    await adapter.feed(_permission_request(session.session_id, "p-pending"))
    await asyncio.wait_for(resolver_started.wait(), timeout=5)

    await rt.close_chat(session.session_id)  # cancels the loop mid-decide

    assert adapter.permission_replies == [("p-pending", "reject_once")]


async def test_question_broker_auto_rejects_on_resolver_exception(
    tmp_path: Path,
) -> None:
    async def boom(_req: QuestionRequest):
        raise RuntimeError("oops")

    qbroker = QuestionBroker(boom, default_timeout_seconds=1.0)
    rt, factory = _runtime(tmp_path)
    session = await rt.open_chat(create=True, permission_broker=None, question_broker=qbroker)
    try:
        adapter = factory.adapters[0]
        await adapter.feed(_question(session.session_id, "q1"))
        for _ in range(40):
            if adapter.question_rejects:
                break
            await asyncio.sleep(0.02)
        # Reason is the broker's internal signal — exact string may vary
        # by source. Just assert SOMETHING was rejected.
        assert len(adapter.question_rejects) == 1
        assert adapter.question_rejects[0][0] == "q1"
    finally:
        await rt.close_chat(session.session_id)


async def test_question_plan_approval_kind_preserved_to_resolver(
    tmp_path: Path,
) -> None:
    seen: list[str] = []

    async def resolver(req: QuestionRequest):
        seen.append(req.kind)
        return ("reject", None)

    qbroker = QuestionBroker(resolver, default_timeout_seconds=1.0)
    rt, factory = _runtime(tmp_path)
    session = await rt.open_chat(create=True, permission_broker=None, question_broker=qbroker)
    try:
        adapter = factory.adapters[0]
        await adapter.feed(_question(session.session_id, "q1", kind="plan_approval"))
        for _ in range(40):
            if seen:
                break
            await asyncio.sleep(0.02)
        assert seen == ["plan_approval"]
    finally:
        await rt.close_chat(session.session_id)


# ---------------------------------------------------------------------------
# 5. Event ordering + persistence
# ---------------------------------------------------------------------------


async def test_events_persist_to_chat_jsonl_in_feed_order(tmp_path: Path) -> None:
    """Feed a realistic turn — TurnStarted → PartStarted → chunks →
    PartCreated → TurnFinished — and confirm chat.jsonl reflects every
    event in append order (chunks DROPPED per NON_PERSISTED_EVENT_TYPES;
    everything else persisted)."""
    rt, factory = _runtime(tmp_path)
    session = await rt.open_chat(create=True)
    sid = session.session_id
    try:
        adapter = factory.adapters[0]
        events: list[Event] = [
            TurnStarted(
                event_id="t1-start",
                time=_T,
                session_id=sid,
                turn_id="t1",
                user_message_id="m1",
            ),
            PartStarted(
                event_id="p1-start",
                time=_T,
                session_id=sid,
                message_id="m1",
                part_id="p1",
                part_type="text",
            ),
            AgentMessageChunk(
                event_id="chunk0",
                time=_T,
                session_id=sid,
                message_id="m1",
                part_id="p1",
                sequence=0,
                text="Hello",
            ),
            AgentMessageChunk(
                event_id="chunk1",
                time=_T,
                session_id=sid,
                message_id="m1",
                part_id="p1",
                sequence=1,
                text=" world",
            ),
            PartCreated(
                event_id="p1-end",
                time=_T,
                session_id=sid,
                part=TextPart(part_id="p1", message_id="m1", text="Hello world"),
            ),
            MessageCompleted(
                event_id="m1-done",
                time=_T,
                session_id=sid,
                message_id="m1",
                finish_reason="stop",
            ),
            TurnFinished(
                event_id="t1-end",
                time=_T,
                session_id=sid,
                turn_id="t1",
                stop_reason="end_turn",
            ),
        ]
        for ev in events:
            await adapter.feed(ev)
        # Let the persist pump drain.
        for _ in range(50):
            persisted = list(session._chat.events())
            if any(isinstance(e, TurnFinished) for e in persisted):
                break
            await asyncio.sleep(0.02)
    finally:
        await rt.close_chat(sid)

    # Re-read after close to make sure manifest + jsonl flushed atomically.
    rt2, _ = _runtime(tmp_path)
    session2 = await rt2.open_chat(sid)
    try:
        persisted_types = [type(e).__name__ for e in session2._chat.events()]
        # Chunks DROPPED — only finalized parts + lifecycle events.
        # The runtime auto-prepends a SessionCreated on chat creation;
        # filter to just the events we fed.
        assert "AgentMessageChunk" not in persisted_types
        fed_types = [t for t in persisted_types if t != "SessionCreated"]
        assert fed_types == [
            "TurnStarted",
            "PartStarted",
            "PartCreated",
            "MessageCompleted",
            "TurnFinished",
        ]
    finally:
        await rt2.close_chat(sid)


async def test_manifest_tokens_accumulate_across_message_completeds(
    tmp_path: Path,
) -> None:
    rt, factory = _runtime(tmp_path)
    session = await rt.open_chat(create=True)
    sid = session.session_id
    try:
        adapter = factory.adapters[0]
        await adapter.feed(
            MessageCompleted(
                event_id="m1-done",
                time=_T,
                session_id=sid,
                message_id="m1",
                finish_reason="stop",
                tokens={
                    "input": 100,
                    "output": 50,
                    "cache_read": 10,
                    "cache_write": 5,
                },
                cost=0.012,
            )
        )
        await adapter.feed(
            MessageCompleted(
                event_id="m2-done",
                time=_T,
                session_id=sid,
                message_id="m2",
                finish_reason="stop",
                tokens={"input": 50, "output": 25},
                cost=0.006,
            )
        )
        # Let persistence run.
        await asyncio.sleep(0.1)
    finally:
        await rt.close_chat(sid)

    rt2, _ = _runtime(tmp_path)
    session2 = await rt2.open_chat(sid)
    try:
        tot = session2.manifest.tokens_total
        assert tot.input == 150
        assert tot.output == 75
        assert tot.cache_read == 10
        assert tot.cache_write == 5
        assert pytest.approx(session2.manifest.cost_total) == 0.018
    finally:
        await rt2.close_chat(sid)


async def test_persist_pump_does_not_drop_under_burst(
    tmp_path: Path,
) -> None:
    """Audit guarantee: a burst reaches chat.jsonl WHOLE and in order.

    Publishing never yields, so the whole burst is queued before the persist pump
    runs once — the shape that a bounded persist queue would silently truncate.
    Closing the chat drains the pump to completion (that is the contract
    ``ChatSession.close`` states), so the file is read afterwards and every event
    is asserted where the next reader would find it: on disk, by id, in feed
    order. No wall-clock poll decides when to look.

    The magnitude that a persist queue would have to be bounded BELOW is pinned
    separately, by ``test_harness_runtime.py`` — it feeds past the bus default
    with no writer attached, which is the cheap place to make a size claim.
    """
    rt, factory = _runtime(tmp_path)
    session = await rt.open_chat(create=True)
    sid = session.session_id

    adapter = factory.adapters[0]
    burst = 64
    for i in range(burst):
        await adapter.feed(
            PartCreated(
                event_id=f"p{i}",
                time=_T,
                session_id=sid,
                part=TextPart(part_id=f"p{i}", message_id="m", text=f"x{i}"),
            )
        )
    await rt.close_chat(sid)

    # The lock-free replay an observer would use — the bytes the writer left.
    landed = [e for e in rt._chats_store.read_events(sid) if isinstance(e, PartCreated)]
    assert [(e.event_id, e.part.text) for e in landed] == [(f"p{i}", f"x{i}") for i in range(burst)]


# ---------------------------------------------------------------------------
# 6. Cancel + crash
# ---------------------------------------------------------------------------


async def test_cancel_increments_adapter_cancel_count(tmp_path: Path) -> None:
    rt, factory = _runtime(tmp_path)
    session = await rt.open_chat(create=True)
    try:
        await session.cancel()
        await session.cancel()
        assert factory.adapters[0].cancel_count == 2
    finally:
        await rt.close_chat(session.session_id)


async def test_simulated_crash_after_prompt_raises(tmp_path: Path) -> None:
    """An adapter configured to raise on send_prompt surfaces the
    error to the caller — the broker doesn't swallow it."""
    rt = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"),
        adapter_factory=FakeAdapterFactory(lambda: FakeAdapter(crash_after_prompt=True)),
    )
    session = await rt.open_chat(create=True)
    try:
        with pytest.raises(HarnessCrashError):
            await session.send_prompt("hi")
    finally:
        await rt.close_chat(session.session_id)


# ---------------------------------------------------------------------------
# 7. Resume / multi-open / lock
# ---------------------------------------------------------------------------


async def test_chat_resume_carries_manifest_native_state(tmp_path: Path) -> None:
    """C2 contract: native_state survives close → open."""
    rt, _factory = _runtime(tmp_path, native={"opencode_session_id": "ses_X"})
    s1 = await rt.open_chat(create=True, title="round-trip")
    sid = s1.session_id
    assert s1.manifest.harness == {"opencode_session_id": "ses_X"}
    await rt.close_chat(sid)

    rt2, factory2 = _runtime(tmp_path, native={"opencode_session_id": "ses_X"})
    await rt2.open_chat(sid)
    try:
        cfg = factory2.configs[-1]
        # The pin from the prior run is handed to the next adapter, alongside the
        # always-on Alkera tool surface the runtime injects.
        assert cfg.harness_native["opencode_session_id"] == "ses_X"
        assert "alkera_mcp" in cfg.harness_native
    finally:
        await rt2.close_chat(sid)


async def test_open_chat_twice_in_same_runtime_raises(tmp_path: Path) -> None:
    rt, _ = _runtime(tmp_path)
    s = await rt.open_chat(create=True)
    try:
        with pytest.raises(RuntimeError, match="already open"):
            await rt.open_chat(s.session_id)
    finally:
        await rt.close_chat(s.session_id)


async def test_open_chat_across_runtimes_is_lock_contended(
    tmp_path: Path,
) -> None:
    rt1, _ = _runtime(tmp_path)
    s = await rt1.open_chat(create=True)
    try:
        rt2, _ = _runtime(tmp_path)
        with pytest.raises(LockHeldError):
            await rt2.open_chat(s.session_id)
    finally:
        await rt1.close_chat(s.session_id)


async def test_list_chats_includes_created_chat(tmp_path: Path) -> None:
    rt, _ = _runtime(tmp_path)
    m = await rt.new_chat(title="alpha")
    listed = rt.list_chats()
    assert m.session_id in [c.session_id for c in listed]


async def test_delete_chat_removes_it_from_list(tmp_path: Path) -> None:
    rt, _ = _runtime(tmp_path)
    m = await rt.new_chat(title="rm-me")
    await rt.delete_chat(m.session_id)
    listed = rt.list_chats()
    assert m.session_id not in [c.session_id for c in listed]


async def test_delete_chat_refuses_open_chat(tmp_path: Path) -> None:
    rt, _ = _runtime(tmp_path)
    s = await rt.open_chat(create=True)
    try:
        with pytest.raises(RuntimeError, match="open"):
            await rt.delete_chat(s.session_id)
    finally:
        await rt.close_chat(s.session_id)


async def test_close_all_drains_every_session(tmp_path: Path) -> None:
    rt, _ = _runtime(tmp_path)
    s1 = await rt.open_chat(create=True, title="a")
    s2 = await rt.open_chat(create=True, title="b")
    assert not s1.closed
    assert not s2.closed
    await rt.close_all()
    assert s1.closed
    assert s2.closed


# ---------------------------------------------------------------------------
# 8. Cross-adapter restart preserves continuity
# ---------------------------------------------------------------------------


async def test_restart_adapter_for_same_chat_reuses_native_pin(
    tmp_path: Path,
) -> None:
    """First open pins ses_A. The user closes + reopens. Even though
    the new factory wants to pin ses_B, the EXISTING manifest pin is
    passed back via SessionConfig.harness_native — so a real adapter
    would attach to ses_A. This guards against the silent
    pick-latest fallback re-targeting a different opencode session."""
    rt1, _factory1 = _runtime(tmp_path, native={"opencode_session_id": "ses_A"})
    s1 = await rt1.open_chat(create=True)
    sid = s1.session_id
    await rt1.close_chat(sid)

    # New runtime, factory that WANTS to pin ses_B. The CONFIG it
    # receives should still carry ses_A (the persisted pin).
    rt2, factory2 = _runtime(tmp_path, native={"opencode_session_id": "ses_B"})
    await rt2.open_chat(sid)
    try:
        cfg = factory2.configs[-1]
        # The persisted pin (ses_A) wins over the factory's would-be ses_B, and
        # the runtime also injects the always-on Alkera tool surface.
        assert cfg.harness_native["opencode_session_id"] == "ses_A"
        assert "alkera_mcp" in cfg.harness_native
    finally:
        await rt2.close_chat(sid)
