"""Rigorous unit coverage for ``ChatSession.spawn_subagent``.

Drives a real child turn via a scripted ``FakeAdapter`` (no subprocess), so every
invariant is pinned deterministically: the structured ``SubagentRunResult``
(summary + usage stats + child_session_id), the parent↔child linkage,
the NON-raising error path, no fan-out cap, concurrent
spawns surviving a sibling's crash, interrupt-then-force-summary,
the agent-prompt injection, recursion-off, permission-mode inheritance, and child
cleanup. The live cross-process proof (model calls the spawn tool → real child
subprocess) is the opencode_e2e.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.adapter import HarnessCrashError, PromptInput
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.runtime import (
    ENV_SUBAGENT_TURN_BUDGET,
    SUBAGENT_REPORT_CEILING_DEFAULT,
    SUBAGENT_TURN_BUDGET_SECONDS,
    AdapterFactory,
    HarnessRuntime,
    SubagentError,
    _child_permission_mode,
    subagent_turn_budget,
)
from alkera_cli.harness.system_prompt import KNOWLEDGE_BLOCK
from alkera_cli.host.limits import env_seconds
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import (
    Event,
    PartCreated,
    SubagentCompleted,
    SubagentStarted,
    TextPart,
    ToolCall,
    ToolCallUpdate,
)


class _SpawnFactory(AdapterFactory):
    """Builds FakeAdapters that auto-reply with ``reply_text`` (so a spawned
    child runs a real turn), or crash on prompt (the failure path). Reports every
    harness as available so the test doesn't depend on installed binaries."""

    def __init__(self, *, reply_text: str = "subagent done", crash: bool = False) -> None:
        super().__init__(binary=None)
        self._reply = reply_text
        self._crash = crash
        self.adapters: list[FakeAdapter] = []

    def __call__(  # type: ignore[override]
        self, config: object, *, bus: EventBus, harness_type: str = "agent"
    ) -> FakeAdapter:
        adapter = FakeAdapter(
            reply_text=None if self._crash else self._reply,
            crash_after_prompt=self._crash,
        )
        adapter._bus = bus  # the runtime's bus — what the session subscribes to
        self.adapters.append(adapter)
        return adapter

    def is_available(self, harness_type: str = "agent") -> bool:
        return True


class _AdapterFactory(AdapterFactory):
    """Base for the specialized fakes: every harness available, records created
    adapters, builds via ``_make`` and wires the runtime's bus."""

    def __init__(self) -> None:
        super().__init__(binary=None)
        self.adapters: list[FakeAdapter] = []

    def __call__(  # type: ignore[override]
        self, config: object, *, bus: EventBus, harness_type: str = "agent"
    ) -> FakeAdapter:
        adapter = self._make()
        adapter._bus = bus
        self.adapters.append(adapter)
        return adapter

    def is_available(self, harness_type: str = "agent") -> bool:
        return True

    def _make(self) -> FakeAdapter:
        raise NotImplementedError


class _SlowFakeAdapter(FakeAdapter):
    """Turn 1 (exploration): ``running`` + a partial, then HANG so the exploration
    budget fires; ``cancel()`` synthesizes the flushed partial + a terminal. Turn 2
    (force-summary): the interrupted turn's own late terminal first (both
    transports deliver it after the interrupt is acked, so it lands behind the
    summary prompt), then a normal turn whose text is the returned summary."""

    #: How the cancel's terminal comes back: a stamped abort, or a crash's
    #: unstamped error (the adapter collapsed and lost track of the attempt).
    cancel_stamped = True

    def __init__(self) -> None:
        super().__init__()
        self._turn = 0

    async def send_prompt(self, prompt: PromptInput) -> None:
        self._sent_prompts.append(prompt)
        self._turn += 1
        if self._turn == 1:
            await self._bus.publish(self._status("r1", "running", prompt.turn_id))
            await self._bus.publish(
                PartCreated(
                    event_id="pp",
                    time=datetime.now(UTC),
                    session_id="fake",
                    part=TextPart(part_id="pp", message_id="m1", text="...partial so far..."),
                )
            )
            # No idle: the parent's exploration budget fires and it cancels us.
        else:
            # The summary turn is under way (a real adapter publishes `running`
            # before the write) when the interrupted turn's terminal finally lands.
            await self._bus.publish(self._status("r2", "running", prompt.turn_id))
            await self._bus.publish(self._status("late", "error", self._sent_prompts[0].turn_id))
            await self._emit_scripted_turn(
                "FINAL SUMMARY: found X at foo.py:1; no Y.", prompt.turn_id
            )

    async def cancel(self) -> None:
        self._cancels += 1
        await self._bus.publish(
            PartCreated(
                event_id="pf",
                time=datetime.now(UTC),
                session_id="fake",
                part=TextPart(part_id="pf", message_id="m1", text="<<flushed partial on cancel>>"),
            )
        )
        if self.cancel_stamped:
            await self._bus.publish(self._status("ab", "aborted", self._last_turn_id()))
        else:
            await self._bus.publish(self._status("ab", "error", None))


class _SlowFactory(_AdapterFactory):
    def _make(self) -> FakeAdapter:
        return _SlowFakeAdapter()


class _SilentThenReportAdapter(FakeAdapter):
    """A child whose exploration ends on its own with nothing said, so the parent
    asks for a write-up without any budget having fired. Turn 2 is the report."""

    async def send_prompt(self, prompt: PromptInput) -> None:
        self._sent_prompts.append(prompt)
        if len(self._sent_prompts) == 1:
            await self._bus.publish(self._status("s1", "running", prompt.turn_id))
            await self._bus.publish(self._status("s1-end", "idle", prompt.turn_id))
            return
        await self._emit_scripted_turn("FINAL SUMMARY: nothing to report.", prompt.turn_id)


class _SilentThenReportFactory(_AdapterFactory):
    def _make(self) -> FakeAdapter:
        return _SilentThenReportAdapter()


class _StatsAdapter(FakeAdapter):
    """Emits a turn with several tool calls so the stats tally can be pinned —
    including the streaming case (``ToolCall.input`` empty, path in the update)."""

    async def send_prompt(self, prompt: PromptInput) -> None:
        self._sent_prompts.append(prompt)
        now = datetime.now(UTC)
        sid = "fake"

        async def _tc(cid: str, name: str, inp: dict[str, Any]) -> None:
            await self._bus.publish(
                ToolCall(
                    event_id=f"tc-{cid}",
                    time=now,
                    session_id=sid,
                    tool_call_id=cid,
                    message_id="m1",
                    tool_name=name,
                    input=inp,
                )
            )

        await self._bus.publish(self._status("r", "running", prompt.turn_id))
        # read a.py — streamed: the ToolCall has empty input; the path arrives later.
        await _tc("c1", "read", {})
        await self._bus.publish(
            ToolCallUpdate(
                event_id="u1",
                time=now,
                session_id=sid,
                tool_call_id="c1",
                input={"file_path": "a.py"},
            )
        )
        await _tc("c2", "read", {"file_path": "a.py"})  # dup path → deduped
        await _tc("c3", "read", {"file_path": "b.py"})
        await _tc("c4", "glob", {"pattern": "**/*.py"})
        await self._bus.publish(
            PartCreated(
                event_id="p",
                time=now,
                session_id=sid,
                part=TextPart(part_id="pp", message_id="m1", text="report"),
            )
        )
        await self._bus.publish(self._status("i", "idle", prompt.turn_id))


class _StatsFactory(_AdapterFactory):
    def _make(self) -> FakeAdapter:
        return _StatsAdapter()


class _MixedAdapter(FakeAdapter):
    """Crashes when the prompt text is ``__crash__``; otherwise replies ``ok``.
    Lets one spawn among concurrent siblings fail in isolation."""

    async def send_prompt(self, prompt: PromptInput) -> None:
        self._sent_prompts.append(prompt)
        if prompt.text == "__crash__":
            raise HarnessCrashError("simulated child crash")
        await self._emit_scripted_turn("ok", prompt.turn_id)


class _MixedFactory(_AdapterFactory):
    def _make(self) -> FakeAdapter:
        return _MixedAdapter()


def _runtime(tmp_path: Path, **kw: object) -> HarnessRuntime:
    return HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"),
        adapter_factory=_SpawnFactory(**kw),  # type: ignore[arg-type]
    )


async def test_spawn_runs_child_returns_summary_and_child_id(tmp_path: Path) -> None:
    rt = _runtime(tmp_path, reply_text="the answer is 42")
    parent = await rt.open_chat(create=True, harness_type="agent")
    try:
        result = await parent.spawn_subagent("compute", agent="explore", description="math task")
        assert result.summary == "the answer is 42"
        assert result.error is None
        # The parent↔child link is the child_session_id on the RESULT (no separate
        # SubagentStarted/Completed events are emitted — the spawn is one tool card).
        assert result.child_session_id

        # The child is a real, parent-linked chat...
        child = rt._chats_store.open(result.child_session_id)
        try:
            assert child.manifest.parent_session_id == parent.session_id
        finally:
            child.close()
    finally:
        await rt.close_chat(parent.session_id)


async def test_spawn_emits_early_subagent_started_on_parent_bus(tmp_path: Path) -> None:
    """The spawn publishes a DATA-ONLY ``SubagentStarted`` on the PARENT bus while the
    child runs — carrying ``child_session_id`` plus the ``(agent_name, prompt)`` the UI
    binds to this spawn's tool card to stream the running child + power the live
    drill-in. We do NOT emit a ``SubagentCompleted`` (the tool result already carries
    the final ``{summary, stats, child_session_id}``)."""
    rt = _runtime(tmp_path, reply_text="done")
    parent = await rt.open_chat(create=True, harness_type="agent")
    sub = parent.subscribe()
    collected: list[Event] = []

    async def _pump() -> None:
        async for ev in sub:
            collected.append(ev)

    pump = asyncio.create_task(_pump())
    try:
        result = await parent.spawn_subagent(
            "map the auth flow", agent="explore", description="auth map"
        )
        await asyncio.sleep(0.05)  # let the pump drain the buffered parent-bus publishes
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump
        await rt.close_chat(parent.session_id)

    started = [e for e in collected if isinstance(e, SubagentStarted)]
    assert len(started) == 1, "exactly one early SubagentStarted on the parent bus"
    ev = started[0]
    # The pointer the UI correlates on: child id + (agent, prompt), and it precedes
    # the result (published mid-spawn) — the whole point of the early signal.
    assert ev.child_session_id == result.child_session_id
    assert ev.agent_name == "explore"
    assert ev.prompt == "map the auth flow"
    assert ev.description == "auth map"
    assert ev.session_id == parent.session_id
    # DATA-ONLY single signal — no SubagentCompleted twin (the result carries it).
    assert not any(isinstance(e, SubagentCompleted) for e in collected)


async def test_subagent_started_fires_only_once_child_is_observable(tmp_path: Path) -> None:
    """Regression (the 'works if you wait a few seconds' bug): the early pointer must
    fire only AFTER the child is OPEN in the runtime, so a drill-in click can observe
    it IMMEDIATELY. Emitting it at bare creation (before ``open_chat``) left a window
    where a fast click hit the held lock before the child was registered as observable,
    so the read-only observe fell back to a non-live replay and the open surfaced
    "already used by another session". We pin — synchronously, AT publish time — that
    ``open_session(child)`` is already live when ``SubagentStarted`` is published."""
    rt = _runtime(tmp_path, reply_text="done")
    parent = await rt.open_chat(create=True, harness_type="agent")
    observable_at_publish: list[bool] = []
    original_publish = parent._bus.publish

    async def _spy_publish(event: Event) -> None:
        # Checked INSIDE the publish (in the spawn coroutine) — so it reflects the
        # runtime exactly when the UI first learns the child id, with no scheduling race.
        if isinstance(event, SubagentStarted):
            observable_at_publish.append(rt.open_session(event.child_session_id) is not None)
        await original_publish(event)

    parent._bus.publish = _spy_publish  # type: ignore[method-assign]
    try:
        await parent.spawn_subagent("go", agent="explore")
    finally:
        await rt.close_chat(parent.session_id)

    assert observable_at_publish == [True], "child must be observable when SubagentStarted fires"


async def test_spawn_child_is_closed_after_completion(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    parent = await rt.open_chat(create=True, harness_type="agent")
    try:
        result = await parent.spawn_subagent("do it")
        # The child session was closed (removed from the live set) — re-opening
        # it must succeed (its lock was released).
        assert result.child_session_id not in rt.open_session_ids
        reopened = await rt.open_chat(result.child_session_id, harness_type="agent")
        await rt.close_chat(reopened.session_id)
    finally:
        await rt.close_chat(parent.session_id)


async def test_spawn_inherits_parent_permission_mode(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    parent = await rt.open_chat(create=True, harness_type="agent")
    parent.set_permission_mode("read_only")
    try:
        result = await parent.spawn_subagent("explore the schema", agent="explore")
        child = rt._chats_store.open(result.child_session_id)
        try:
            assert child.manifest.permission_mode == "read_only"  # ceiling propagated
        finally:
            child.close()
    finally:
        await rt.close_chat(parent.session_id)


async def _spawn_child_mode(tmp_path: Path, *, parent_mode: str, agent: str) -> str:
    rt = _runtime(tmp_path)
    parent = await rt.open_chat(create=True, harness_type="agent")
    parent.set_permission_mode(parent_mode)  # type: ignore[arg-type]
    try:
        result = await parent.spawn_subagent("go", agent=agent)
        child = rt._chats_store.open(result.child_session_id)
        try:
            return str(child.manifest.permission_mode)
        finally:
            child.close()
    finally:
        await rt.close_chat(parent.session_id)


@pytest.mark.parametrize("agent", ["explore", "review"])
@pytest.mark.parametrize("parent_mode", ["default", "auto", "bypass", "read_only", "plan"])
async def test_readonly_agents_are_always_read_only(
    tmp_path: Path, parent_mode: str, agent: str
) -> None:
    # Both built-in subagents (`explore` and `review`, tool_scope="read_only") are
    # ALWAYS read_only — they must never mutate, regardless of the parent's mode. Under
    # a mutable parent (default/auto/bypass) this clamps writes off; under a `plan`
    # parent it also drops the plan/ask steering (a subagent never plans).
    assert await _spawn_child_mode(tmp_path, parent_mode=parent_mode, agent=agent) == "read_only"


async def test_unknown_agent_inherits_parent_mode(tmp_path: Path) -> None:
    # An unknown agent name (`worker` is not a built-in — only `explore` and `review`
    # are) resolves to no AgentDefinition → no read-only pin → inherits the parent's
    # mode unchanged. Graceful unknown-agent fallback, not a crash.
    assert await _spawn_child_mode(tmp_path, parent_mode="default", agent="worker") == "default"


@pytest.mark.parametrize("parent_mode", ["default", "auto", "bypass", "read_only", "plan"])
def test_child_permission_mode_pins_read_only_agents(parent_mode: str) -> None:
    # The pure rule, exhaustively. A read-only-scoped (or explore-mode) agent def is
    # read_only under EVERY parent mode; an unknown/None or write-capable agent inherits
    # the parent's mode — except a `plan` ceiling, which a subagent never keeps.
    from types import SimpleNamespace

    for read_only_def in (
        SimpleNamespace(tool_scope="read_only", mode=None),
        SimpleNamespace(tool_scope=None, mode="explore"),
    ):
        assert _child_permission_mode(parent_mode, read_only_def) == "read_only"
    expected = "read_only" if parent_mode == "plan" else parent_mode
    assert _child_permission_mode(parent_mode, None) == expected
    assert (
        _child_permission_mode(parent_mode, SimpleNamespace(tool_scope=None, mode=None)) == expected
    )


async def test_open_chat_threads_broker_and_tool_scope(tmp_path: Path) -> None:
    # The plumbing the subagent spine rides: a session opened with a broker +
    # tool_scope carries both on its dispatch binding (shared gate + restriction).
    from alkera_cli.harness.permission_broker import PermissionBroker

    async def _resolver(req: object) -> str:
        return "allow_once"

    broker = PermissionBroker(_resolver, default_timeout_seconds=None)
    rt = _runtime(tmp_path)
    session = await rt.open_chat(
        create=True, harness_type="agent", permission_broker=broker, tool_scope="read_only"
    )
    try:
        assert session.tool_binding is not None
        assert session.tool_binding.broker is broker
        assert session.tool_binding.tool_scope == "read_only"
    finally:
        await rt.close_chat(session.session_id)


async def test_spawn_shares_parent_broker_and_passes_agent_scope(tmp_path: Path) -> None:
    # A subagent SHARES the parent's broker (so its prompts reach the same human)
    # and gets its agent definition's tool_scope. We spy on open_chat to capture
    # the child's kwargs since the child session is torn down after its turn.
    from alkera_cli.harness.permission_broker import PermissionBroker

    async def _resolver(req: object) -> str:
        return "allow_once"

    broker = PermissionBroker(_resolver, default_timeout_seconds=None)
    rt = _runtime(tmp_path)
    parent = await rt.open_chat(create=True, harness_type="agent", permission_broker=broker)
    captured: list[dict[str, object]] = []
    original = rt.open_chat

    async def _spy(session_id: object = None, **kw: object) -> object:
        if session_id is not None:  # the child open (resume by id)
            captured.append(kw)
        return await original(session_id, **kw)  # type: ignore[arg-type]

    rt.open_chat = _spy  # type: ignore[method-assign]
    try:
        await parent.spawn_subagent("explore the schema", agent="explore")
        assert captured, "child open_chat was not observed"
        child_kw = captured[0]
        assert child_kw["permission_broker"] is broker  # shared gate
        assert child_kw["tool_scope"] == "read_only"  # explore = read tools only
    finally:
        rt.open_chat = original  # type: ignore[method-assign]
        await rt.close_chat(parent.session_id)


async def test_spawn_failure_returns_error_result_not_raises(tmp_path: Path) -> None:
    # A child crash does NOT raise to the parent. It returns a
    # SubagentRunResult with `error` set (the spawn tool turns it into {error, tool}),
    # so concurrent siblings + the parent turn survive.
    rt = _runtime(tmp_path, crash=True)  # the child adapter crashes on prompt
    parent = await rt.open_chat(create=True, harness_type="agent")
    try:
        result = await parent.spawn_subagent("doomed")  # does NOT raise
        assert result.error is not None
        assert result.summary == ""
    finally:
        await rt.close_chat(parent.session_id)


async def test_recursion_is_off_a_subagent_cannot_spawn(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    # A chat WITH a parent is a subagent.
    child_manifest = await rt.new_chat(harness_type="agent", parent_session_id="parent-sid")
    child = await rt.open_chat(child_manifest.session_id, harness_type="agent")
    try:
        assert child.is_subagent is True
        with pytest.raises(SubagentError, match="cannot spawn"):
            await child.spawn_subagent("recurse")
    finally:
        await rt.close_chat(child.session_id)


async def test_no_fanout_cap_many_concurrent_spawns_all_succeed(tmp_path: Path) -> None:
    # There is no fan-out cap. A parent may spawn arbitrarily many agents in
    # one turn; concurrent spawns are safe (each child has its own chat/lock/bus,
    # and removing the cap removed the only shared mutable parent state).
    rt = _runtime(tmp_path, reply_text="done")
    parent = await rt.open_chat(create=True, harness_type="agent")
    try:
        results = await asyncio.gather(
            *(parent.spawn_subagent(f"task {i}", agent="explore") for i in range(12))
        )
        assert len(results) == 12
        assert all(r.error is None and r.summary == "done" for r in results)
    finally:
        await rt.close_chat(parent.session_id)


async def test_crashing_sibling_does_not_fail_healthy_concurrent_spawns(tmp_path: Path) -> None:
    # One failing spawn among N concurrent ones must not fail its siblings or the
    # parent. The crasher returns an error result; the healthy ones return
    # their summaries — the gather never raises.
    rt = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"),
        adapter_factory=_MixedFactory(),  # type: ignore[arg-type]
    )
    parent = await rt.open_chat(create=True, harness_type="agent")
    try:
        results = await asyncio.gather(
            parent.spawn_subagent("healthy-a", agent="explore"),
            parent.spawn_subagent("__crash__", agent="explore"),
            parent.spawn_subagent("healthy-b", agent="explore"),
        )
        crashed = [r for r in results if r.error is not None]
        healthy = [r for r in results if r.error is None]
        assert len(crashed) == 1
        assert len(healthy) == 2
        assert all(r.summary == "ok" for r in healthy)
    finally:
        await rt.close_chat(parent.session_id)


async def test_under_budget_turn_is_not_truncated(tmp_path: Path) -> None:
    # A turn that finishes naturally under the exploration budget is NOT truncated
    # and never interrupts the child.
    factory = _SpawnFactory(reply_text="quick answer")
    rt = HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory)  # type: ignore[arg-type]
    parent = await rt.open_chat(create=True, harness_type="agent")
    try:
        result = await parent.spawn_subagent("go", agent="explore")
        assert result.summary == "quick answer"
        assert result.stats.truncated is False
        # The child adapter (last created) was never cancelled.
        assert factory.adapters[-1].cancel_count == 0
    finally:
        await rt.close_chat(parent.session_id)


@pytest.mark.parametrize("cancel_stamped", [True, False], ids=["stamped abort", "unstamped crash"])
async def test_budget_hit_interrupts_then_force_summarizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_stamped: bool
) -> None:
    # On the exploration budget, the child is INTERRUPTED (not killed) and
    # forced to return a final summary. The returned summary is the POST-interrupt
    # report (not the pre-cancel partial), truncated=True, exactly one cancel, and a
    # second prompt (the force-summary) was sent. The drain ceiling is set an hour out,
    # so the spawn can only return by the drain releasing on the cancelled attempt's
    # terminal — stamped, or lost to a collapse and carried by the unstamped one.
    monkeypatch.setattr("alkera_cli.harness.runtime.SUBAGENT_TURN_BUDGET_SECONDS", 0.05)
    monkeypatch.setattr("alkera_cli.harness.runtime._DRAIN_CEILING_SECONDS", 3600.0)
    _SlowFakeAdapter.cancel_stamped = cancel_stamped
    factory = _SlowFactory()
    rt = HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory)  # type: ignore[arg-type]
    parent = await rt.open_chat(create=True, harness_type="agent")
    try:
        try:
            async with asyncio.timeout(30):
                result = await parent.spawn_subagent("explore forever", agent="explore")
        except TimeoutError:
            pytest.fail("the drain never released on the cancelled attempt's terminal")
        assert result.summary.startswith("FINAL SUMMARY: found X at foo.py:1; no Y.")
        # The report itself says it was cut short — the parent reads the summary,
        # not the stats, and a partial report read as a complete one is a wrong
        # answer nobody can see is wrong.
        assert "research budget" in result.summary
        assert result.stats.truncated is True
        child_adapter = factory.adapters[-1]
        assert child_adapter.cancel_count == 1
        assert len(child_adapter.sent_prompts) == 2  # initial + force-summary
        # The force-summary prompt carried the time-budget instruction.
        assert "time budget" in child_adapter.sent_prompts[1].text.lower()
    finally:
        _SlowFakeAdapter.cancel_stamped = True
        await rt.close_chat(parent.session_id)


async def test_the_shipped_default_leaves_a_worker_exploring_past_the_old_hour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No budget ships, and no budget means no interrupt — not just no clock.

    The same child that a 0.05s budget cuts is left exploring here with the
    module constant untouched, while the loop's clock is walked two hours past
    the hour that used to cut it: no cancel, no force-summary turn. Driven
    against the runtime rather than the parser, because the bug it guards was
    the parser answering ``None`` and the turn still being cut.
    """
    assert SUBAGENT_TURN_BUDGET_SECONDS is None, "the shipped default is no research budget"
    loop = asyncio.get_running_loop()
    base = loop.time
    skew = 0.0
    monkeypatch.setattr(loop, "time", lambda: base() + skew)
    _SlowFakeAdapter.cancel_stamped = True
    factory = _SlowFactory()
    rt = HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory)  # type: ignore[arg-type]
    parent = await rt.open_chat(create=True, harness_type="agent")
    spawn = asyncio.ensure_future(parent.spawn_subagent("explore forever", agent="explore"))
    try:
        await asyncio.sleep(0.05)  # the child is exploring
        skew = 7200.0  # two hours past the budget that used to fire
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(spawn), timeout=1.0)
        child = factory.adapters[-1]
        assert child.cancel_count == 0, "an unbounded turn must never be interrupted"
        assert len(child.sent_prompts) == 1, "no force-summary turn was asked for"
    finally:
        spawn.cancel()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(spawn, timeout=5.0)
        await rt.close_chat(parent.session_id)


async def test_a_report_never_claims_a_budget_when_none_was_set(tmp_path: Path) -> None:
    """The note the parent reads is the only place a partial report says it is
    partial — so it must appear exactly when a budget cut the work, and never on
    a report written with no budget in force. A child that ends its exploration
    with nothing to report takes the forced write-up path here (the same code
    that appends the note), and comes back clean."""
    assert SUBAGENT_TURN_BUDGET_SECONDS is None
    factory = _SilentThenReportFactory()
    rt = HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory)  # type: ignore[arg-type]
    parent = await rt.open_chat(create=True, harness_type="agent")
    try:
        async with asyncio.timeout(30):
            result = await parent.spawn_subagent("investigate", agent="explore")
        child = factory.adapters[-1]
        assert len(child.sent_prompts) == 2, "the silent exploration was asked to write up"
        assert child.cancel_count == 0, "an unbounded exploration is never interrupted"
        assert result.summary == "FINAL SUMMARY: nothing to report."
        assert "budget" not in result.summary.lower()
    finally:
        await rt.close_chat(parent.session_id)


async def test_stats_count_tools_and_dedupe_files(tmp_path: Path) -> None:
    # Stats are tallied from the child's tool events: per-tool counts from ToolCall;
    # file paths merged from ToolCall + the populated ToolCallUpdate (a streamed
    # ToolCall.input is empty until the update), deduped.
    factory = _StatsFactory()
    rt = HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory)  # type: ignore[arg-type]
    parent = await rt.open_chat(create=True, harness_type="agent")
    try:
        result = await parent.spawn_subagent("investigate", agent="explore")
        assert result.summary == "report"
        assert result.stats.tool_calls == 4
        assert result.stats.by_tool == {"read": 3, "glob": 1}
        # a.py (deduped from two reads) + b.py + the glob pattern → 3 distinct.
        assert result.stats.files_read == 3
        assert result.stats.truncated is False
    finally:
        await rt.close_chat(parent.session_id)


async def test_agent_prompt_injected_as_system_addendum(tmp_path: Path) -> None:
    # The agent definition's `prompt` is injected into the child's turn as the
    # per-turn system addendum (the gap: it was never wired). A custom agent file
    # gives us a known prompt to assert on.
    agents_dir = tmp_path / ".alkera" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "scout.md").write_text(
        "---\ntool_scope: read_only\n---\nYOU-ARE-SCOUT marker prompt.\n"
    )
    factory = _SpawnFactory(reply_text="scouted")
    rt = HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory)  # type: ignore[arg-type]
    parent = await rt.open_chat(create=True, harness_type="agent")
    try:
        result = await parent.spawn_subagent("look", agent="scout")
        assert result.summary == "scouted"
        child_adapter = factory.adapters[-1]
        assert child_adapter.sent_prompts, "child never received a prompt"
        system = child_adapter.sent_prompts[0].system or ""
        assert "YOU-ARE-SCOUT marker prompt." in system
    finally:
        await rt.close_chat(parent.session_id)


async def test_subagent_turn_omits_main_agent_guidance(tmp_path: Path) -> None:
    # A subagent can't spawn, so it must NOT be told it has a spawn_agent tool — it
    # gets its OWN agent prompt, never the main-agent guidance.
    factory = _SpawnFactory(reply_text="done")
    project = ProjectDirectory(tmp_path / ".alkera")
    rt = HarnessRuntime(project, adapter_factory=factory)  # type: ignore[arg-type]
    parent = await rt.open_chat(create=True, harness_type="agent")
    try:
        await parent.spawn_subagent("investigate", agent="explore")
        child_system = factory.adapters[-1].sent_prompts[0].system or ""
        # The child carries its own agent prompt; a broken lookup would leave the
        # system empty and let every "not in" below pass for the wrong reason.
        assert child_system.strip()
        # Main-agent guidance absent (a child can't spawn, so it's never told it has
        # the agent tools nor to discover them via list_agent_types).
        assert "ALWAYS have a `spawn_agent` tool" not in child_system
        assert "list_agent_types" not in child_system
        assert KNOWLEDGE_BLOCK not in child_system
    finally:
        await rt.close_chat(parent.session_id)


async def test_subagent_turn_does_not_build_a_task_reminder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The live TODO reminder is ROOT-only. Spy on render_task_reminder: a root turn
    # builds it, but a subagent turn must short-circuit BEFORE the call (the
    # `not self.is_subagent` guard) — even though the child has its own task_store.
    # A dropped guard would call render_task_reminder on the child turn → caught here.
    from alkera_cli.harness import runtime as runtime_mod

    calls = 0
    original = runtime_mod.render_task_reminder

    def _spy(tasks: object) -> object:
        nonlocal calls
        calls += 1
        return original(tasks)  # type: ignore[arg-type]

    monkeypatch.setattr(runtime_mod, "render_task_reminder", _spy)

    factory = _SpawnFactory(reply_text="done")
    rt = HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory)  # type: ignore[arg-type]
    parent = await rt.open_chat(create=True, harness_type="agent")
    try:
        await parent._tool_binding.task_store.apply(upsert=[{"id": "build", "title": "Build it"}])
        await parent.send_prompt("go")
        root_calls = calls
        assert root_calls >= 1  # the root turn DOES build the reminder
        await parent.spawn_subagent("investigate", agent="explore")
        assert calls == root_calls  # the subagent turn built NONE
    finally:
        await rt.close_chat(parent.session_id)


# --- tier-aware model routing ----------------------------------------------

_CHEAP_MODEL = {"provider_id": "alkera-anthropic", "model_id": "lil-haiku", "efforts": []}
_FRONTIER_MODEL = {"provider_id": "alkera-anthropic", "model_id": "big-opus", "efforts": []}


async def _routing_resolver(agent_def: object, parent_model: object) -> dict[str, object] | None:
    """Route a cheap-tier agent to the cheap model; everything else inherits."""
    if getattr(agent_def, "model_tier", None) == "cheap":
        return dict(_CHEAP_MODEL)
    return None


@pytest.mark.parametrize(
    ("agent", "model_id"),
    [("explore", "lil-haiku"), ("worker", "big-opus")],
    ids=["routed cheap", "inherits frontier"],
)
async def test_a_child_takes_the_model_its_tier_routes_to(
    tmp_path: Path, agent: str, model_id: str
) -> None:
    """A child with a tier takes that tier's model; one without inherits the
    parent's."""
    rt = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"),
        adapter_factory=_SpawnFactory(),  # type: ignore[arg-type]
        subagent_model_resolver=_routing_resolver,
    )
    parent = await rt.open_chat(create=True, harness_type="agent", model=dict(_FRONTIER_MODEL))
    try:
        result = await parent.spawn_subagent("go", agent=agent)
        child = rt._chats_store.open(result.child_session_id)
        try:
            assert child.manifest.model["model_id"] == model_id
        finally:
            child.close()
    finally:
        await rt.close_chat(parent.session_id)


# ---------------------------------------------------------------------------
# Early-exit force-summary guard: a child that ends WITHOUT a usable report
# (nothing, or right after a denied tool) must force a summary — never hand the
# parent an empty string or a mid-thought partial.
# ---------------------------------------------------------------------------


class _EmptyThenReportAdapter(FakeAdapter):
    """Turn 1: running → idle with NO text (the child produced nothing). Turn 2: the
    forced summary."""

    def __init__(self) -> None:
        super().__init__()
        self._turn = 0

    async def send_prompt(self, prompt: PromptInput) -> None:
        self._sent_prompts.append(prompt)
        self._turn += 1
        if self._turn == 1:
            await self._bus.publish(self._status("r", "running", prompt.turn_id))
            await self._bus.publish(self._status("i", "idle", prompt.turn_id))
        else:
            await self._emit_scripted_turn(
                "FORCED REPORT: nothing found, ruled out X.", prompt.turn_id
            )


class _EmptyFactory(_AdapterFactory):
    def _make(self) -> FakeAdapter:
        return _EmptyThenReportAdapter()


class _DenialStubAdapter(FakeAdapter):
    """Turn 1: running → a mid-thought partial → a write ToolCall → its ERROR update
    (the write was denied) → idle. The child ended on a denied tool, never a report.
    Turn 2: the forced summary."""

    def __init__(self) -> None:
        super().__init__()
        self._turn = 0

    async def send_prompt(self, prompt: PromptInput) -> None:
        self._sent_prompts.append(prompt)
        self._turn += 1
        now = datetime.now(UTC)
        if self._turn == 1:
            await self._bus.publish(self._status("r", "running", prompt.turn_id))
            await self._bus.publish(
                PartCreated(
                    event_id="pp",
                    time=now,
                    session_id="fake",
                    part=TextPart(part_id="pp", message_id="m1", text="...let me save the list..."),
                )
            )
            await self._bus.publish(
                ToolCall(
                    event_id="tc",
                    time=now,
                    session_id="fake",
                    tool_call_id="c1",
                    message_id="m1",
                    tool_name="bash",
                    input={"command": "cat > scratch.txt"},
                )
            )
            await self._bus.publish(
                ToolCallUpdate(
                    event_id="tu",
                    time=now,
                    session_id="fake",
                    tool_call_id="c1",
                    status="error",
                    error_text="permission denied",
                )
            )
            await self._bus.publish(self._status("i", "idle", prompt.turn_id))
        else:
            await self._emit_scripted_turn(
                "FORCED REPORT: found X at a.py:1; no Y.", prompt.turn_id
            )


class _DenialStubFactory(_AdapterFactory):
    def _make(self) -> FakeAdapter:
        return _DenialStubAdapter()


async def test_subagent_empty_turn_forces_a_summary(tmp_path: Path) -> None:
    factory = _EmptyFactory()
    rt = HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory)  # type: ignore[arg-type]
    parent = await rt.open_chat(create=True, harness_type="agent")
    try:
        result = await parent.spawn_subagent("investigate", agent="explore")
        # The parent never receives "" — a summary turn is forced.
        assert result.summary == "FORCED REPORT: nothing found, ruled out X."
        assert result.error is None
        child = factory.adapters[-1]
        assert len(child.sent_prompts) == 2  # initial + forced summary
        # The child already reached idle, so we did NOT interrupt it.
        assert child.cancel_count == 0
    finally:
        await rt.close_chat(parent.session_id)


async def test_subagent_denial_stub_forces_a_summary(tmp_path: Path) -> None:
    factory = _DenialStubFactory()
    rt = HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory)  # type: ignore[arg-type]
    parent = await rt.open_chat(create=True, harness_type="agent")
    try:
        result = await parent.spawn_subagent("investigate", agent="explore")
        # The parent gets the real forced report, NOT the mid-thought partial.
        assert result.summary == "FORCED REPORT: found X at a.py:1; no Y."
        assert "let me save the list" not in result.summary
        child = factory.adapters[-1]
        assert len(child.sent_prompts) == 2
        assert child.cancel_count == 0
    finally:
        await rt.close_chat(parent.session_id)


async def test_subagent_clean_report_does_not_force_a_summary(tmp_path: Path) -> None:
    # A child that ends ON a text report returns it directly — NO extra force-summary
    # turn (no double spend).
    factory = _StatsFactory()  # ends on a text "report"
    rt = HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory)  # type: ignore[arg-type]
    parent = await rt.open_chat(create=True, harness_type="agent")
    try:
        result = await parent.spawn_subagent("investigate", agent="explore")
        assert result.summary == "report"
        assert result.stats.truncated is False
        child = factory.adapters[-1]
        assert len(child.sent_prompts) == 1  # NO force-summary turn
        assert child.cancel_count == 0
    finally:
        await rt.close_chat(parent.session_id)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(None, None, id="unset-is-no-budget"),
        pytest.param("", None, id="empty"),
        pytest.param("   ", None, id="blank"),
        pytest.param("nope", None, id="not-a-number"),
        pytest.param("0", None, id="zero-is-no-budget-too"),
        pytest.param("-60", None, id="negative-is-no-budget-too"),
        pytest.param("120", 120.0, id="a-short-leash"),
        pytest.param("7200.5", 7200.5, id="a-long-one"),
    ],
)
def test_the_subagent_research_budget_is_unbounded_and_settable(
    raw: str | None, expected: float | None
) -> None:
    """A worker reading a large repository, profiling a warehouse or running a
    build is doing the work it was spawned for, and however long that takes is
    how long it takes — so nothing cuts it by default. An operator who wants a
    leash sets one; every unreadable answer lands on the unbounded default
    rather than inventing a clock nobody asked for."""
    assert subagent_turn_budget(raw) == expected


def test_the_module_budget_is_what_the_environment_asked_for() -> None:
    assert SUBAGENT_TURN_BUDGET_SECONDS == subagent_turn_budget(
        os.environ.get(ENV_SUBAGENT_TURN_BUDGET)
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(None, None, id="unset-is-no-ceiling"),
        pytest.param("junk", None, id="junk-keeps-the-unbounded-default"),
        pytest.param("120", 120.0, id="an-operator-ceiling"),
        pytest.param("0", None, id="zero-is-no-ceiling-too"),
    ],
)
def test_the_subagent_report_ceiling_is_unbounded_and_settable(
    raw: str | None, expected: float | None
) -> None:
    """A write-up cut by a clock reaches the parent mid-sentence and reads as
    findings, so by default the parent waits for the whole report; the child
    being gone still releases it. An operator who wants a ceiling sets one."""
    assert env_seconds(raw, default=SUBAGENT_REPORT_CEILING_DEFAULT) == expected
    assert SUBAGENT_REPORT_CEILING_DEFAULT is None


class _SlowReportAdapter(_SlowFakeAdapter):
    """A child whose WRITE-UP takes a while: the report turn publishes its text a
    third of a second after the prompt, the way a thinking model does."""

    report_delay = 0.3

    async def send_prompt(self, prompt: PromptInput) -> None:
        if len(self._sent_prompts) == 0:
            await super().send_prompt(prompt)
            return
        self._sent_prompts.append(prompt)
        await self._bus.publish(self._status("r2", "running", prompt.turn_id))
        await self._bus.publish(self._status("late", "error", self._sent_prompts[0].turn_id))

        async def _write_up() -> None:
            await asyncio.sleep(self.report_delay)
            await self._emit_scripted_turn(
                "FINAL SUMMARY: found X at foo.py:1; no Y.", prompt.turn_id
            )

        self._writing = asyncio.create_task(_write_up())


class _SlowReportFactory(_SlowFactory):
    def _make(self) -> FakeAdapter:
        return _SlowReportAdapter()


@pytest.mark.parametrize(
    ("ceiling", "reports_whole"),
    [
        pytest.param(0.05, False, id="a-ceiling-under-the-write-up-cuts-it"),
        pytest.param(None, True, id="no-ceiling-waits-for-the-whole-report"),
    ],
)
async def test_the_report_ceiling_decides_whether_a_slow_write_up_is_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ceiling: float | None, reports_whole: bool
) -> None:
    """A write-up slower than the ceiling is abandoned and the parent falls back to
    the pre-cancel partial — which is why the ceiling has to be generous, and
    removable for a deployment that would rather always wait for the real report."""
    monkeypatch.setattr("alkera_cli.harness.runtime.SUBAGENT_TURN_BUDGET_SECONDS", 0.05)
    monkeypatch.setattr("alkera_cli.harness.runtime._DRAIN_CEILING_SECONDS", 3600.0)
    monkeypatch.setattr("alkera_cli.harness.runtime.SUBAGENT_REPORT_CEILING_SECONDS", ceiling)
    _SlowReportAdapter.cancel_stamped = True
    rt = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"),
        adapter_factory=_SlowReportFactory(),  # type: ignore[arg-type]
    )
    parent = await rt.open_chat(create=True, harness_type="agent")
    try:
        async with asyncio.timeout(30):
            result = await parent.spawn_subagent("explore forever", agent="explore")
        assert result.summary.startswith("FINAL SUMMARY: found X") is reports_whole
    finally:
        await rt.close_chat(parent.session_id)
