"""Integration coverage for backgrounded ``spawn_agent`` (the runtime wiring).

Drives a real child turn via a scripted ``FakeAdapter`` (no subprocess) and pins
the backgrounding contract end to end: ``background=True`` returns a "started"
stub immediately, the child runs detached, a ``SubagentCompleted`` lands on the
PARENT bus carrying the native report, and the parent is WOKEN with a
"Backgrounded Tool Finished" turn whose text wraps that same native result. Also
pins the read-only clamp, child reaping, and that the foreground path is
unchanged. The wake renderer is unit-tested in isolation (pure function).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.background import BackgroundJob
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.permission_broker import PermissionBroker
from alkera_cli.harness.runtime import AdapterFactory, HarnessRuntime, _render_background_wake
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import (
    Event,
    PermissionOption,
    PermissionRequest,
    SubagentCompleted,
    SubagentStarted,
)


def _factory(reply_text: str = "done") -> FakeAdapterFactory:
    """Auto-replying fakes, recorded in creation order (``adapters[0]`` is the
    parent, ``adapters[1]`` the first child)."""
    return FakeAdapterFactory(lambda: FakeAdapter(reply_text=reply_text), available=True)


async def _wait_for(predicate: Callable[[], bool], *, tries: int = 150) -> bool:
    """Poll (~3s total) until ``predicate()`` is true or the tries run out."""
    for _ in range(tries):
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


# --------------------------------------------------------------------------- #
# End-to-end: stub return + native-result notification.
# --------------------------------------------------------------------------- #


async def test_background_spawn_returns_stub_immediately_then_notifies(tmp_path: Path) -> None:
    factory = _factory(reply_text="REPORT: found the bug at x.py:1")
    rt = HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory)  # type: ignore[arg-type]
    parent = await rt.open_chat(create=True, harness_type="agent")
    parent_adapter = factory.adapters[0]

    collected: list[Event] = []
    sub = parent.subscribe()

    async def _pump() -> None:
        async for ev in sub:
            collected.append(ev)

    pump = asyncio.create_task(_pump())
    try:
        result = await parent.spawn_subagent(
            "find the bug", agent="explore", description="bug hunt", background=True
        )
        # Immediate stub: a "started" note + the child id, no error, NO report yet.
        assert result.error is None
        assert result.child_session_id
        assert "background" in result.summary.lower()
        assert "REPORT:" not in result.summary  # the real report comes via the wake

        # The child ran detached → SubagentCompleted lands on the PARENT bus...
        assert await _wait_for(lambda: any(isinstance(e, SubagentCompleted) for e in collected)), (
            "no SubagentCompleted on the parent bus"
        )
        completed = [e for e in collected if isinstance(e, SubagentCompleted)]
        assert len(completed) == 1
        assert completed[0].child_session_id == result.child_session_id
        assert completed[0].summary == "REPORT: found the bug at x.py:1"
        # ...and exactly one early SubagentStarted preceded it (the live card binding).
        assert sum(isinstance(e, SubagentStarted) for e in collected) == 1

        # The parent was WOKEN with a "Backgrounded Tool Finished" turn carrying the
        # SAME native report — not a flattened dump.
        assert await _wait_for(
            lambda: any(
                "<backgrounded_tool_finished" in p.text for p in parent_adapter.sent_prompts
            )
        ), "parent was not woken with the native-result envelope"
        wake = next(
            p for p in parent_adapter.sent_prompts if "<backgrounded_tool_finished" in p.text
        )
        assert 'tool="spawn_agent"' in wake.text
        assert 'status="completed"' in wake.text
        assert "REPORT: found the bug at x.py:1" in wake.text
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump
        await rt.close_chat(parent.session_id)


async def test_background_spawn_clamps_child_to_read_only(tmp_path: Path) -> None:
    # An unknown agent under a `default` parent would normally inherit `default`
    # (mutable) — but a BACKGROUND spawn is clamped to read_only so a detached child
    # can never stall on a write prompt.
    factory = _factory()
    rt = HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory)  # type: ignore[arg-type]
    parent = await rt.open_chat(create=True, harness_type="agent")  # default mode
    try:
        result = await parent.spawn_subagent("go", agent="worker", background=True)
        assert await _wait_for(lambda: not parent._background.has_running())
        child = rt._chats_store.open(result.child_session_id)
        try:
            assert child.manifest.permission_mode == "read_only"
        finally:
            child.close()
    finally:
        await rt.close_chat(parent.session_id)


async def test_background_subagent_child_gets_a_non_interactive_auto_reject_broker(
    tmp_path: Path,
) -> None:
    """A detached subagent must never block on a permission prompt. The read-only MODE
    clamp alone does NOT guarantee that — a project ``.alkera/permissions.yml`` ``ask``
    rule overrides the mode base to PROMPT — so the child is given a NON-interactive
    auto-rejecting broker (not the parent's shared interactive one) and no question
    broker. Here the parent's resolver would HANG forever if consulted; we prove the
    child got its own auto-rejecting broker and the parent's resolver is never reached."""
    parent_consulted: list[object] = []

    async def _hang_forever(req: PermissionRequest) -> str:
        parent_consulted.append(req)
        await asyncio.Event().wait()  # a human-in-the-loop broker with no human watching
        return "allow_once"  # unreachable

    parent_broker = PermissionBroker(_hang_forever, default_timeout_seconds=None)

    factory = _factory()
    rt = HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory)  # type: ignore[arg-type]
    parent = await rt.open_chat(create=True, harness_type="agent", permission_broker=parent_broker)

    # Capture the brokers passed when the CHILD is opened (patched AFTER the parent).
    captured: list[tuple[Any, Any]] = []
    orig_open = rt.open_chat

    async def _spy_open(*args: Any, **kw: Any) -> Any:
        if "permission_broker" in kw:
            captured.append((kw.get("permission_broker"), kw.get("question_broker")))
        return await orig_open(*args, **kw)

    rt.open_chat = _spy_open  # type: ignore[method-assign]
    try:
        result = await parent.spawn_subagent("go", agent="worker", background=True)
        assert await _wait_for(lambda: not parent._background.has_running())
        assert result.error is None

        assert captured, "the child was never opened with a permission broker"
        child_broker, child_qbroker = captured[0]
        # A DIFFERENT broker than the parent's interactive one, and NO question broker.
        assert child_broker is not parent_broker
        assert child_qbroker is None
        # It auto-rejects ANY prompt — so a write the mode clamp let through can't stall.
        req = PermissionRequest(
            event_id="ev",
            time=datetime.now(UTC),
            session_id=result.child_session_id,
            request_id="r1",
            permission_kind="edit",
            options=[
                PermissionOption(option_id="allow_once", name="Allow once"),
                PermissionOption(option_id="reject_once", name="Reject once"),
            ],
        )
        assert await child_broker.resolve(req) == "reject_once"
        # The parent's hang-forever resolver was NEVER consulted by the detached child.
        assert parent_consulted == []
    finally:
        rt.open_chat = orig_open  # type: ignore[method-assign]
        await rt.close_chat(parent.session_id)


async def test_background_spawn_reaps_child_after_completion(tmp_path: Path) -> None:
    factory = _factory()
    rt = HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory)  # type: ignore[arg-type]
    parent = await rt.open_chat(create=True, harness_type="agent")
    try:
        result = await parent.spawn_subagent("do it", agent="explore", background=True)
        assert await _wait_for(lambda: not parent._background.has_running())
        # The child's lock was released → re-opening it succeeds.
        assert result.child_session_id not in rt.open_session_ids
        reopened = await rt.open_chat(result.child_session_id, harness_type="agent")
        await rt.close_chat(reopened.session_id)
    finally:
        await rt.close_chat(parent.session_id)


async def test_foreground_spawn_still_blocks_and_returns_report(tmp_path: Path) -> None:
    # Regression guard: background=False is unchanged — it blocks and returns the
    # report directly (no job, no wake).
    factory = _factory(reply_text="inline answer")
    rt = HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory)  # type: ignore[arg-type]
    parent = await rt.open_chat(create=True, harness_type="agent")
    try:
        result = await parent.spawn_subagent("compute", agent="explore")
        assert result.summary == "inline answer"
        assert not parent._background.has_jobs()  # nothing was backgrounded
    finally:
        await rt.close_chat(parent.session_id)


async def test_turn_owner_unwinds_when_send_prompt_raises(tmp_path: Path) -> None:
    # If the adapter fire RAISES, turn ownership must unwind — otherwise
    # every later background completion would queue forever (the pump never sees a
    # closing idle to clear it).
    factory = _factory()
    rt = HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory)  # type: ignore[arg-type]
    parent = await rt.open_chat(create=True, harness_type="agent")
    try:
        factory.adapters[0]._crash_after_prompt = True  # the next send_prompt raises
        with pytest.raises(Exception):  # noqa: B017 - any harness send failure
            await parent.send_prompt("hi")
        assert parent.turn_active is False  # unwound, not stuck True
    finally:
        await rt.close_chat(parent.session_id)


class _ChildCrashFactory(AdapterFactory):
    """The parent adapter (first built) replies normally; child adapters (built
    during a spawn) crash on prompt — so a backgrounded subagent's turn fails."""

    def __init__(self) -> None:
        super().__init__(binary=None)
        self.adapters: list[FakeAdapter] = []

    def __call__(  # type: ignore[override]
        self, config: object, *, bus: EventBus, harness_type: str = "agent"
    ) -> FakeAdapter:
        crash = len(self.adapters) >= 1
        adapter = FakeAdapter(reply_text=None if crash else "ok", crash_after_prompt=crash)
        adapter._bus = bus
        self.adapters.append(adapter)
        return adapter

    def is_available(self, harness_type: str = "agent") -> bool:
        return True


async def test_failed_background_subagent_reports_error_not_no_report(tmp_path: Path) -> None:
    factory = _ChildCrashFactory()
    rt = HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory)  # type: ignore[arg-type]
    parent = await rt.open_chat(create=True, harness_type="agent")
    parent_adapter = factory.adapters[0]
    collected: list[Event] = []
    sub = parent.subscribe()

    async def _pump() -> None:
        async for ev in sub:
            collected.append(ev)

    pump = asyncio.create_task(_pump())
    try:
        # The stub returns OK immediately; the child crashes later inside the job.
        result = await parent.spawn_subagent("doomed", agent="explore", background=True)
        assert result.error is None

        # The job errors → SubagentCompleted carries the ERROR, not a phantom summary.
        assert await _wait_for(lambda: any(isinstance(e, SubagentCompleted) for e in collected))
        completed = [e for e in collected if isinstance(e, SubagentCompleted)]
        assert len(completed) == 1
        assert completed[0].error
        assert not completed[0].summary
        # It carries the REAL child id even though the errored job's result is None
        # (the id is stashed on job.input at submit, not read off the absent result).
        # The TUI folds the error onto the card by this id; resume_reconcile clears the
        # dangling-subagent record by it — an empty id would leave both wrong.
        assert completed[0].child_session_id == result.child_session_id
        assert completed[0].child_session_id  # not the ""-regression

        # ...and the model wake reports status="error" + the real error, NOT "(no report)".
        assert await _wait_for(
            lambda: any(
                "<backgrounded_tool_finished" in p.text for p in parent_adapter.sent_prompts
            )
        )
        wake = next(
            p for p in parent_adapter.sent_prompts if "<backgrounded_tool_finished" in p.text
        )
        assert 'status="error"' in wake.text
        assert "(no report)" not in wake.text
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump
        await rt.close_chat(parent.session_id)


# --------------------------------------------------------------------------- #
# The wake renderer (pure function) — single + coalesced.
# --------------------------------------------------------------------------- #


def _job(job_id: str, kind: str, title: str, state: str, result: object) -> BackgroundJob:
    return BackgroundJob(job_id=job_id, kind=kind, title=title, state=state, result=result)


def test_render_background_wake_single_wraps_native_result() -> None:
    job = _job("job_1", "agent", "bug hunt", "completed", "the report body")
    text = _render_background_wake([(job, "")], lambda j, d: str(j.result))
    assert text.startswith('<backgrounded_tool_finished job_id="job_1"')
    assert 'tool="spawn_agent"' in text
    assert 'status="completed"' in text
    assert "<summary>Background spawn_agent completed: bug hunt</summary>" in text
    assert "the report body" in text
    # A single job is NOT wrapped in the plural envelope.
    assert "<backgrounded_tools_finished>" not in text


def test_render_background_wake_coalesces_with_per_job_state() -> None:
    jobs = [
        _job("j1", "sql", "q1", "completed", "rows..."),
        _job("j2", "bash", "build", "error", None),
    ]
    text = _render_background_wake([(j, "") for j in jobs], lambda j, d: j.error or str(j.result))
    assert text.startswith("<backgrounded_tools_finished>")
    assert text.rstrip().endswith("</backgrounded_tools_finished>")
    # Each job keeps its OWN tool name + state (a failure isn't hidden in an
    # "all finished" summary).
    assert 'tool="sql.query"' in text and 'status="completed"' in text
    assert 'tool="bash"' in text and 'status="error"' in text
    assert "Background bash failed: build" in text
