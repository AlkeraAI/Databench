"""Tests for `HarnessRuntime` + `ChatSession` using `FakeAdapter`."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.harness import (
    EventBus,
    HarnessNotReadyError,
    HarnessRuntime,
    HarnessUnavailableError,
    PermissionBroker,
    SessionConfig,
)
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.question_broker import QuestionBroker
from alkera_cli.harness.runtime import AdapterFactory
from alkera_cli.host import paths
from alkera_cli.preferences.instructions import MAX_GLOBAL_INSTRUCTIONS_BYTES
from alkera_core.project.directory import ProjectDirectory
from alkera_core.project.locking import LockHeldError
from alkera_core.schemas.chat import (
    AgentMessageChunk,
    CommandResult,
    Heartbeat,
    PartCreated,
    PermissionOption,
    PermissionRequest,
    QuestionPrompt,
    QuestionRequest,
    SessionStatusChanged,
    TextPart,
    ToolCall,
    TurnStarted,
)

_T = datetime(2026, 5, 26, tzinfo=UTC)


def _runtime(tmp_path: Path) -> tuple[HarnessRuntime, FakeAdapterFactory]:
    project = ProjectDirectory(tmp_path / ".alkera")
    factory = FakeAdapterFactory()
    return HarnessRuntime(project, adapter_factory=factory), factory


def _idle_for(turn_id: str, event_id: str) -> SessionStatusChanged:
    """The terminal an adapter stamps with the attempt that produced it."""
    return SessionStatusChanged(
        event_id=event_id, time=_T, session_id="s", status="idle", turn_id=turn_id
    )


async def _wait_for_settlements(session: Any, count: int, budget: float = 5.0) -> None:
    """Wait out the settlement edge on a deadline rather than a fixed pause."""
    deadline = asyncio.get_running_loop().time() + budget
    while session.turn_settlements < count:
        assert asyncio.get_running_loop().time() < deadline, (
            f"only {session.turn_settlements} of {count} turns settled in {budget}s"
        )
        await asyncio.sleep(0.01)


def _settling_runtime(tmp_path: Path) -> tuple[HarnessRuntime, FakeAdapterFactory]:
    """A runtime whose fake ends every turn, for tests that send more than one root
    prompt without leaving the previous one live."""
    project = ProjectDirectory(tmp_path / ".alkera")
    factory = FakeAdapterFactory(lambda: FakeAdapter(reply_text="ok"))
    return HarnessRuntime(project, adapter_factory=factory), factory


async def test_active_task_list_rides_each_root_turn(tmp_path: Path) -> None:
    """The live TODO list is re-injected into the system at the START of every root
    turn that has active tasks (and omitted when empty), so the model never loses
    the DAG between turns."""
    rt, factory = _settling_runtime(tmp_path)
    chat = rt._chats_store.create(title="c", harness_type="agent")
    sid = chat.session_id
    chat.close()
    session = await rt.open_chat(sid)
    try:
        await session.send_prompt("first")  # no tasks yet
        # Add an active task to the SAME store the session reads each turn.
        assert session._tool_binding is not None
        await session._tool_binding.task_store.apply(upsert=[{"id": "build", "title": "Build it"}])
        await session.send_prompt("second")
        sent = factory.adapters[-1].sent_prompts
        first = sent[0].system or ""
        second = sent[1].system or ""
        # Empty list → no dynamic reminder; active list → reminder + live checklist.
        assert "Your active task list" not in first
        assert "Your active task list" in second
        assert "[ ] build — Build it" in second
    finally:
        await rt.close_chat(sid)


async def test_disabled_plugin_absent_from_the_grounding_brief(tmp_path: Path) -> None:
    """A user-disabled plugin's tools drop off the surface, and its connection must
    ALSO vanish from the agent's upfront grounding brief, or the agent is told about a
    datasource it can't actually use."""
    import duckdb

    con = duckdb.connect(str(tmp_path / "shopdb.duckdb"))
    con.execute("CREATE TABLE t (id INTEGER)")
    con.close()
    rt, _ = _runtime(tmp_path)
    await rt.plugin_registry()  # auto-activates duckdb_local + auto-adds the connection
    chat = rt._chats_store.create(title="c", harness_type="agent")
    sid = chat.session_id
    chat.close()
    session = await rt.open_chat(sid)
    try:
        assert "shopdb" in await session._plugins_brief()
        await rt.disable_plugin("duckdb_local")
        assert "shopdb" not in await session._plugins_brief()
    finally:
        await rt.close_chat(sid)


async def test_plugins_brief_rides_the_first_root_turn(tmp_path: Path) -> None:
    """The integrations brief (active plugins + live connections + what's available to
    enable) is wired into the FIRST root turn's system — so the agent reliably sees the
    integration picture without calling list_plugins — and is NOT re-injected after."""
    import duckdb

    con = duckdb.connect(str(tmp_path / "shopdb.duckdb"))
    con.execute("CREATE TABLE t (id INTEGER)")
    con.close()
    rt, factory = _settling_runtime(tmp_path)
    await rt.plugin_registry()  # auto-activates duckdb_local + auto-adds the connection
    chat = rt._chats_store.create(title="c", harness_type="agent")
    sid = chat.session_id
    chat.close()
    session = await rt.open_chat(sid)
    try:
        await session.send_prompt("first")
        await session.send_prompt("second")
        sent = factory.adapters[-1].sent_prompts
        first = sent[0].system or ""
        second = sent[1].system or ""
        # Turn 1: the integrations brief names the live connection.
        assert "ACTIVE DATA INTEGRATIONS" in first
        assert "shopdb" in first
        # Turn 2: the one-shot brief is NOT re-injected.
        assert "shopdb" not in second
    finally:
        await rt.close_chat(sid)


# ---------------------------------------------------------------------------
# Harness type — pinned on create, persisted, restored + dispatched on resume
# ---------------------------------------------------------------------------


async def test_harness_type_persists_and_restores(tmp_path: Path) -> None:
    """The chosen harness is saved in the manifest and read back on resume
    (store-level round-trip — independent of harness availability)."""
    rt, _ = _runtime(tmp_path)
    chat = rt._chats_store.create(title="cc", harness_type="claude-agent")
    sid = chat.session_id
    chat.close()
    reopened = rt._chats_store.open(sid)
    try:
        assert reopened.manifest.harness_type == "claude-agent"
    finally:
        reopened.close()


async def test_resume_dispatches_pinned_harness(tmp_path: Path) -> None:
    """Resume reads harness_type from the manifest and builds THAT adapter — not
    the default — and never re-checks availability."""
    rt, factory = _runtime(tmp_path)
    chat = rt._chats_store.create(title="cc", harness_type="claude-agent")
    sid = chat.session_id
    chat.close()
    session = await rt.open_chat(sid)
    try:
        assert session.manifest.harness_type == "claude-agent"
        assert factory.adapters[-1]._harness_type == "claude-agent"
    finally:
        await rt.close_chat(sid)


async def test_resume_ignores_conflicting_harness_arg(tmp_path: Path) -> None:
    """Resuming with a DIFFERENT harness than the chat was created with uses the
    chat's PINNED harness (manifest), not the passed one — i.e. resuming a claude
    chat stays on claude even if the alkera harness is asked for."""
    rt, factory = _runtime(tmp_path)
    chat = rt._chats_store.create(title="cc", harness_type="claude-agent")
    sid = chat.session_id
    chat.close()
    # Explicitly pass the OPENCODE harness on resume (simulating `--harness alkera`).
    session = await rt.open_chat(sid, harness_type="agent")
    try:
        assert session.manifest.harness_type == "claude-agent"  # pinned wins
        assert factory.adapters[-1]._harness_type == "claude-agent"
    finally:
        await rt.close_chat(sid)


async def test_claude_env_injected_for_claude_chat(tmp_path: Path) -> None:
    """At open, a claude-agent chat gets a fresh `claude_env` from the builder —
    and NOT opencode's `agent_config`. (Resume path → no availability gate, so
    this runs whether or not the claude binary is installed.)"""
    project = ProjectDirectory(tmp_path / ".alkera")
    factory = FakeAdapterFactory()
    rt = HarnessRuntime(
        project,
        adapter_factory=factory,
        claude_env_builder=lambda _m: {"ANTHROPIC_API_KEY": "sentinel-jwt"},
        gateway_config_builder=lambda _m: {"agent_config_sentinel": True},
    )
    chat = rt._chats_store.create(title="cc", harness_type="claude-agent")
    sid = chat.session_id
    chat.close()
    await rt.open_chat(sid)
    try:
        native = factory.configs[-1].harness_native
        assert native["claude_env"] == {"ANTHROPIC_API_KEY": "sentinel-jwt"}
        assert "agent_config" not in native  # opencode's builder must not fire here
    finally:
        await rt.close_chat(sid)


async def test_opencode_chat_gets_agent_config_not_claude_env(tmp_path: Path) -> None:
    """The mirror: an `agent` (opencode) chat gets `agent_config`, never `claude_env`."""
    project = ProjectDirectory(tmp_path / ".alkera")
    factory = FakeAdapterFactory()
    rt = HarnessRuntime(
        project,
        adapter_factory=factory,
        claude_env_builder=lambda _m: {"ANTHROPIC_API_KEY": "sentinel-jwt"},
        gateway_config_builder=lambda _m: {"agent_config_sentinel": True},
    )
    chat = rt._chats_store.create(title="oc", harness_type="agent")
    sid = chat.session_id
    chat.close()
    await rt.open_chat(sid)
    try:
        native = factory.configs[-1].harness_native
        assert native["agent_config"] == {"agent_config_sentinel": True}
        assert "claude_env" not in native
    finally:
        await rt.close_chat(sid)


# ---------------------------------------------------------------------------
# Global instructions (~/.alkera/instructions.md) — read + cap + no-clobber at open
# ---------------------------------------------------------------------------


def _runtime_with_builders(tmp_path: Path) -> tuple[HarnessRuntime, FakeAdapterFactory]:
    project = ProjectDirectory(tmp_path / ".alkera")
    factory = FakeAdapterFactory()
    rt = HarnessRuntime(
        project,
        adapter_factory=factory,
        claude_env_builder=lambda _m: {"ANTHROPIC_API_KEY": "sentinel-jwt"},
        gateway_config_builder=lambda _m: {"agent_config_sentinel": True},
    )
    return rt, factory


async def _open_harness(rt: HarnessRuntime, *, harness_type: str) -> str:
    chat = rt._chats_store.create(title="t", harness_type=harness_type)
    sid = chat.session_id
    chat.close()
    await rt.open_chat(sid)
    return sid


async def test_global_instructions_injected_without_clobbering_agent_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An opencode chat gets the global instructions AND keeps its gateway
    `agent_config` — the global injection is a separate key that must not disturb
    the `if 'agent_config' not in harness_native` builder branch."""
    instr = tmp_path / "instructions.md"
    instr.write_text("Always prefer DuckDB.", encoding="utf-8")
    monkeypatch.setattr(paths, "INSTRUCTIONS_FILE_PATH", instr)
    rt, factory = _runtime_with_builders(tmp_path)
    sid = await _open_harness(rt, harness_type="agent")
    try:
        native = factory.configs[-1].harness_native
        assert native["global_instructions"] == "Always prefer DuckDB."
        assert native["agent_config"] == {"agent_config_sentinel": True}  # not clobbered
    finally:
        await rt.close_chat(sid)


async def test_global_instructions_injected_for_claude_without_clobbering_claude_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Claude mirror — global instructions ride alongside `claude_env`."""
    instr = tmp_path / "instructions.md"
    instr.write_text("Be concise.", encoding="utf-8")
    monkeypatch.setattr(paths, "INSTRUCTIONS_FILE_PATH", instr)
    rt, factory = _runtime_with_builders(tmp_path)
    sid = await _open_harness(rt, harness_type="claude-agent")
    try:
        native = factory.configs[-1].harness_native
        assert native["global_instructions"] == "Be concise."
        assert native["claude_env"] == {"ANTHROPIC_API_KEY": "sentinel-jwt"}  # not clobbered
    finally:
        await rt.close_chat(sid)


async def test_blank_global_instructions_not_injected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A whitespace-only file injects nothing (the `.strip()` guard)."""
    instr = tmp_path / "instructions.md"
    instr.write_text("   \n\t  \n", encoding="utf-8")
    monkeypatch.setattr(paths, "INSTRUCTIONS_FILE_PATH", instr)
    rt, factory = _runtime_with_builders(tmp_path)
    sid = await _open_harness(rt, harness_type="agent")
    try:
        assert "global_instructions" not in factory.configs[-1].harness_native
    finally:
        await rt.close_chat(sid)


async def test_missing_global_instructions_not_injected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No file → no key (chat start never blocks on it)."""
    monkeypatch.setattr(paths, "INSTRUCTIONS_FILE_PATH", tmp_path / "absent.md")
    rt, factory = _runtime_with_builders(tmp_path)
    sid = await _open_harness(rt, harness_type="agent")
    try:
        assert "global_instructions" not in factory.configs[-1].harness_native
    finally:
        await rt.close_chat(sid)


async def test_pinned_global_instructions_win_over_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller (e.g. a test) that pinned `global_instructions` in the manifest is
    preserved — the `not in harness_native` guard means the file is not re-read."""
    instr = tmp_path / "instructions.md"
    instr.write_text("from file", encoding="utf-8")
    monkeypatch.setattr(paths, "INSTRUCTIONS_FILE_PATH", instr)
    rt, factory = _runtime_with_builders(tmp_path)
    chat = rt._chats_store.create(title="oc", harness_type="agent")
    chat.manifest.harness = {**chat.manifest.harness, "global_instructions": "pinned"}
    chat.flush_manifest()
    sid = chat.session_id
    chat.close()
    await rt.open_chat(sid)
    try:
        assert factory.configs[-1].harness_native["global_instructions"] == "pinned"
    finally:
        await rt.close_chat(sid)


async def test_oversized_global_instructions_are_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An oversized file is head-truncated before it reaches the adapter."""
    instr = tmp_path / "instructions.md"
    instr.write_text("Z" * (40 * 1024), encoding="utf-8")
    monkeypatch.setattr(paths, "INSTRUCTIONS_FILE_PATH", instr)
    rt, factory = _runtime_with_builders(tmp_path)
    sid = await _open_harness(rt, harness_type="agent")
    try:
        gi = factory.configs[-1].harness_native["global_instructions"]
        assert len(gi.encode("utf-8")) < MAX_GLOBAL_INSTRUCTIONS_BYTES + 200
        assert "truncated" in gi
    finally:
        await rt.close_chat(sid)


async def test_new_chat_pins_harness_type(tmp_path: Path) -> None:
    rt, _ = _runtime(tmp_path)
    manifest = await rt.new_chat(harness_type="agent", title="oc")
    assert manifest.harness_type == "agent"


async def test_new_chat_rejects_unavailable_harness(tmp_path: Path) -> None:
    rt, _ = _runtime(tmp_path)
    with pytest.raises(HarnessUnavailableError):
        await rt.new_chat(harness_type="no-such-harness")


async def test_open_chat_create_rejects_unavailable_harness(tmp_path: Path) -> None:
    rt, _ = _runtime(tmp_path)
    with pytest.raises(HarnessUnavailableError):
        await rt.open_chat(create=True, harness_type="no-such-harness")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_new_chat_creates_manifest_without_spawning(tmp_path: Path) -> None:
    rt, factory = _runtime(tmp_path)
    manifest = await rt.new_chat(title="Test")
    assert manifest.title == "Test"
    assert factory.adapters == []  # nothing spawned


async def test_open_chat_creates_when_session_id_none(tmp_path: Path) -> None:
    rt, factory = _runtime(tmp_path)
    session = await rt.open_chat(create=True, title="fresh")
    try:
        assert session.session_id != ""
        assert session.manifest.title == "fresh"
        assert len(factory.adapters) == 1
        assert factory.adapters[0].started is True
    finally:
        await rt.close_chat(session.session_id)


async def test_turn_abort_event_wired_reset_per_turn_and_set_on_cancel(tmp_path: Path) -> None:
    """The per-turn abort Event (the foreground-bash reap signal) is wired onto the
    tool binding at open, re-pointed at a FRESH clear Event each turn (so a prior
    cancel never leaks), and SET synchronously by cancel()."""
    rt, _ = _runtime(tmp_path)
    session = await rt.open_chat(create=True, title="abort")
    try:
        binding = session._tool_binding
        assert binding is not None
        # Wired at open: the binding's abort IS this session's live turn-abort Event.
        assert binding.abort is session._turn_abort
        first = session._turn_abort
        assert not first.is_set()

        await session.send_prompt("hello")
        # A new turn re-points the binding at a fresh, clear Event.
        assert session._turn_abort is not first
        assert binding.abort is session._turn_abort
        assert not session._turn_abort.is_set()

        # Cancel SETS the live Event so a still-running foreground bash reaps.
        await session.cancel()
        assert session._turn_abort.is_set()
    finally:
        await rt.close_chat(session.session_id)


async def test_a_second_prompt_supersedes_the_live_turn(tmp_path: Path) -> None:
    """A caller prompt over a live turn fires at once and supersedes it: both reach
    the adapter in order, and the withdrawn attempt's OWN terminal settles nothing —
    only the live attempt's does."""
    rt, factory = _runtime(tmp_path)
    session = await rt.open_chat(create=True, title="one-at-a-time")
    try:
        first = await session.send_prompt("first")
        # It fires rather than waiting out the live turn; a queueing send_prompt
        # would block here until something settled "first", which nothing does.
        second = await asyncio.wait_for(session.send_prompt("second"), 5.0)
        adapter = factory.adapters[0]

        assert [prompt.text for prompt in adapter.sent_prompts] == ["first", "second"]
        assert first != second
        assert session.turn_active

        # Published in order, so the withdrawn attempt's terminal gets its chance
        # first. Honoring it would make settlement 0 the withdrawn attempt's.
        await adapter.feed(_idle_for(first, "idle-first"))
        await adapter.feed(_idle_for(second, "idle-second"))
        await _wait_for_settlements(session, 1)

        assert session.settlement(0).turn_id == second
        assert session.turn_settlements == 1
        assert not session.turn_active
    finally:
        await rt.close_chat(session.session_id)


async def test_list_chats_overlays_live_background_count(tmp_path: Path) -> None:
    """A chat OPEN with running background jobs lists with the live count; the
    on-disk manifest is never mutated, so a fresh load + a chat with no jobs
    both read 0."""
    rt, _ = _runtime(tmp_path)
    session = await rt.open_chat(create=True, title="bg")
    sid = session.session_id
    try:
        # No jobs yet → the listed manifest reads 0.
        before = next(m for m in rt.list_chats() if m.session_id == sid)
        assert before.background_jobs_running == 0

        gate = asyncio.Event()

        async def _hang() -> str:
            await gate.wait()
            return "done"

        session._background.submit(_hang, kind="bash", title="dev server")
        # The live overlay surfaces the running job on the SAME chat...
        listed = next(m for m in rt.list_chats() if m.session_id == sid)
        assert listed.background_jobs_running == 1
        # ...but the persisted (on-disk) manifest is untouched — the overlay is a copy.
        assert session.manifest.background_jobs_running == 0

        # Finish the job → back to 0 in the listing.
        gate.set()
        await session._background.drain()
        after = next(m for m in rt.list_chats() if m.session_id == sid)
        assert after.background_jobs_running == 0
    finally:
        await rt.close_chat(sid)


async def test_send_prompt_validates_variant_against_chat_efforts(tmp_path: Path) -> None:
    """A per-turn `variant` must be one of the chat's allowed efforts. A valid one
    is forwarded; an unknown one fails UP FRONT (before composing an invalid
    `<model>::<variant>` the gateway would 400 mid-turn)."""
    rt, factory = _settling_runtime(tmp_path)
    model = {
        "provider_id": "alkera-openai",
        "model_id": "m1",
        "efforts": ["low", "high"],
        "effort": "low",
    }
    session = await rt.open_chat(create=True, model=model)
    try:
        await session.send_prompt("hi", variant="high")  # valid → forwarded
        assert factory.adapters[0]._sent_prompts[-1].variant == "high"
        with pytest.raises(ValueError, match="unknown reasoning effort"):
            await session.send_prompt("hi", variant="ludicrous")
    finally:
        await rt.close_chat(session.session_id)


async def test_send_prompt_skips_variant_check_when_efforts_unknown(tmp_path: Path) -> None:
    """A non-gateway chat (no efforts in the manifest model) doesn't gate the
    variant — the check only fires when the allowed set is known."""
    rt, factory = _runtime(tmp_path)
    session = await rt.open_chat(create=True)  # no model → no efforts
    try:
        await session.send_prompt("hi", variant="whatever")
        assert factory.adapters[0]._sent_prompts[-1].variant == "whatever"
    finally:
        await rt.close_chat(session.session_id)


async def test_set_model_effort_rejects_unknown_but_persists_known(tmp_path: Path) -> None:
    """When the chat's model advertises efforts, an unknown one is rejected (a
    typo can't silently downgrade later turns) and a known one is persisted into
    the manifest so it survives a resume."""
    rt, _factory = _runtime(tmp_path)
    model = {"provider_id": "alkera-openai", "model_id": "m1", "efforts": ["low", "high"]}
    session = await rt.open_chat(create=True, model=model)
    try:
        with pytest.raises(ValueError, match="not offered by this chat's model"):
            await session.set_model_effort("ludicrous")
        await session.set_model_effort("high")
        assert session.manifest.model["effort"] == "high"
    finally:
        await rt.close_chat(session.session_id)


async def test_set_model_effort_accepts_when_efforts_unknown(tmp_path: Path) -> None:
    """Fail-OPEN parity with send_prompt's variant check: a chat whose manifest
    model carries no `efforts` list accepts any effort (the gateway is the
    backstop) instead of rejecting EVERY effort."""
    rt, _factory = _runtime(tmp_path)
    session = await rt.open_chat(create=True)  # no model → no efforts
    try:
        await session.set_model_effort("high")
        assert session.manifest.model["effort"] == "high"
    finally:
        await rt.close_chat(session.session_id)


async def test_set_model_effort_raises_on_closed_session(tmp_path: Path) -> None:
    """A closed session must RAISE, not silently no-op — a no-op would let the
    daemon report success with the unchanged effort."""
    rt, _factory = _runtime(tmp_path)
    session = await rt.open_chat(create=True)
    sid = session.session_id
    await rt.close_chat(sid)
    with pytest.raises(HarnessNotReadyError):
        await session.set_model_effort("high")


async def test_events_accessor_reads_persisted_log(tmp_path: Path) -> None:
    """The public `events()` accessor surfaces the chat's persisted event log
    (the daemon's open_chat replay reads through it instead of the private
    `_chat` handle)."""
    rt, _factory = _runtime(tmp_path)
    session = await rt.open_chat(create=True)
    try:
        await session.publish_event(
            CommandResult(
                event_id="cmd-title",
                time=datetime.now(UTC),
                session_id=session.session_id,
                command="title",
                outcome_kind="ok",
                payload={"title": "X"},
                message=None,
            )
        )
        # The persist pump writes asynchronously; poll the accessor (bounded).
        found = False
        for _ in range(100):
            if any(isinstance(e, CommandResult) for e in session.events()):
                found = True
                break
            await asyncio.sleep(0.02)
        assert found, "events() did not surface the persisted CommandResult"
    finally:
        await rt.close_chat(session.session_id)


async def test_open_chat_resumes_by_session_id(tmp_path: Path) -> None:
    rt, factory = _runtime(tmp_path)
    s1 = await rt.open_chat(create=True, title="first")
    sid = s1.session_id
    await rt.close_chat(sid)

    s2 = await rt.open_chat(sid)
    try:
        assert s2.session_id == sid
        assert s2.manifest.title == "first"
        # Two distinct adapter instances spawned (open → close → open)
        assert len(factory.adapters) == 2
    finally:
        await rt.close_chat(sid)


async def test_set_title_updates_manifest_and_survives_resume(
    tmp_path: Path,
) -> None:
    """`session.set_title` updates the in-memory manifest immediately AND
    persists, so a close + reopen (resume) reads the title back. Chats
    start untitled."""
    rt, _ = _runtime(tmp_path)
    s1 = await rt.open_chat(create=True)  # no title → untitled
    sid = s1.session_id
    try:
        assert s1.manifest.title is None
        await s1.set_title("Investigate slow query")
        assert s1.manifest.title == "Investigate slow query"
    finally:
        await rt.close_chat(sid)

    s2 = await rt.open_chat(sid)
    try:
        assert s2.manifest.title == "Investigate slow query"
    finally:
        await rt.close_chat(sid)


async def test_set_permission_mode_persists_and_survives_resume(tmp_path: Path) -> None:
    """`session.set_permission_mode` persists to the manifest, so a close +
    reopen (resume) restores the mode instead of silently reverting to
    'default'. Fresh chats start in 'default'."""
    rt, _ = _runtime(tmp_path)
    s1 = await rt.open_chat(create=True)
    sid = s1.session_id
    try:
        assert s1.permission_mode == "default"  # fresh chats start default
        s1.set_permission_mode("plan")
        assert s1.permission_mode == "plan"
        assert s1.manifest.permission_mode == "plan"  # mirrored onto the manifest
    finally:
        await rt.close_chat(sid)

    s2 = await rt.open_chat(sid)
    try:
        assert s2.permission_mode == "plan"  # restored on resume, not "default"
    finally:
        await rt.close_chat(sid)


async def test_set_title_persists_event_to_chat_jsonl(tmp_path: Path) -> None:
    """The rename is also recorded on the durable log (a `SessionUpdated`
    with the new title) — the firehose/audit record + the resume-fold
    fallback when manifest.json is absent."""
    from alkera_core.schemas.chat import SessionUpdated

    rt, _ = _runtime(tmp_path)
    s1 = await rt.open_chat(create=True)
    sid = s1.session_id
    await s1.set_title("Renamed via slash")
    await rt.close_chat(sid)  # drains persist pump

    chat = rt.project.chats().open(sid)
    try:
        events = list(chat.events())
    finally:
        chat.close()
    renames = [
        e for e in events if isinstance(e, SessionUpdated) and e.title == "Renamed via slash"
    ]
    assert renames, "set_title did not persist a SessionUpdated to chat.jsonl"


async def test_open_chat_in_runtime_twice_raises(tmp_path: Path) -> None:
    rt, _ = _runtime(tmp_path)
    s1 = await rt.open_chat(create=True)
    try:
        with pytest.raises(RuntimeError, match="already open"):
            await rt.open_chat(s1.session_id)
    finally:
        await rt.close_chat(s1.session_id)


async def test_close_releases_lock(tmp_path: Path) -> None:
    rt, _ = _runtime(tmp_path)
    s1 = await rt.open_chat(create=True)
    sid = s1.session_id
    await rt.close_chat(sid)

    # Lock file should be gone (cleanup happens in chat.close()).
    project = rt.project
    assert not (project.chats_path / sid / ".lock").exists()

    # A second runtime can open the same chat (proves the lock released).
    rt2 = HarnessRuntime(project, adapter_factory=FakeAdapterFactory())
    s2 = await rt2.open_chat(sid)
    try:
        assert s2.session_id == sid
    finally:
        await rt2.close_chat(sid)


async def test_close_all_drains_every_session(tmp_path: Path) -> None:
    rt, factory = _runtime(tmp_path)
    s1 = await rt.open_chat(create=True)
    s2 = await rt.open_chat(create=True)
    assert sorted(rt.open_session_ids) == sorted([s1.session_id, s2.session_id])
    await rt.close_all()
    assert rt.open_session_ids == []
    assert all(a.stopped for a in factory.adapters)


# ---------------------------------------------------------------------------
# Lock conflict
# ---------------------------------------------------------------------------


async def test_lock_conflict_when_another_runtime_holds(tmp_path: Path) -> None:
    """Open in runtime A; runtime B's open_chat raises LockHeldError."""
    rt_a, _ = _runtime(tmp_path)
    s = await rt_a.open_chat(create=True)
    sid = s.session_id

    rt_b = HarnessRuntime(rt_a.project, adapter_factory=FakeAdapterFactory())
    try:
        with pytest.raises(LockHeldError):
            await rt_b.open_chat(sid)
    finally:
        await rt_a.close_chat(sid)


# ---------------------------------------------------------------------------
# Delete (incl. recursive)
# ---------------------------------------------------------------------------


async def test_delete_refuses_open_chat(tmp_path: Path) -> None:
    rt, _ = _runtime(tmp_path)
    s = await rt.open_chat(create=True)
    try:
        with pytest.raises(RuntimeError, match="refuse to delete open chat"):
            await rt.delete_chat(s.session_id)
    finally:
        await rt.close_chat(s.session_id)


async def test_delete_after_close(tmp_path: Path) -> None:
    rt, _ = _runtime(tmp_path)
    s = await rt.open_chat(create=True)
    sid = s.session_id
    await rt.close_chat(sid)
    await rt.delete_chat(sid)
    assert sid not in [m.session_id for m in rt.list_chats()]


async def test_delete_recursive_handles_subagent_chain(tmp_path: Path) -> None:
    rt, _ = _runtime(tmp_path)
    parent = await rt.new_chat(title="parent")
    # Hand-build a subagent manifest under the parent.
    child = await rt.new_chat(title="child", parent_session_id=parent.session_id)
    grandchild = await rt.new_chat(title="grandchild", parent_session_id=child.session_id)

    await rt.delete_chat(parent.session_id, recursive=True)
    remaining = {m.session_id for m in rt.list_chats()}
    for sid in (parent.session_id, child.session_id, grandchild.session_id):
        assert sid not in remaining


# ---------------------------------------------------------------------------
# Event firehose + persistence
# ---------------------------------------------------------------------------


async def test_events_flow_through_session_subscribe(tmp_path: Path) -> None:
    rt, factory = _runtime(tmp_path)
    session = await rt.open_chat(create=True)
    sid = session.session_id
    adapter = factory.adapters[0]

    received: list = []
    # Subscribe BEFORE feeding so the queue is registered when publish() fires.
    sub = session.subscribe()

    async def consume() -> None:
        async for ev in sub:
            received.append(ev)
            if len(received) == 2:
                return

    consumer = asyncio.create_task(consume())
    await adapter.feed(
        AgentMessageChunk(
            event_id="ev1",
            time=_T,
            session_id=sid,
            message_id="m1",
            part_id="p1",
            sequence=0,
            text="hello",
        )
    )
    await adapter.feed(
        PartCreated(
            event_id="ev2",
            time=_T,
            session_id=sid,
            part=TextPart(part_id="p1", message_id="m1", text="hello"),
        )
    )
    await asyncio.wait_for(consumer, timeout=1.0)
    assert isinstance(received[0], AgentMessageChunk)
    assert isinstance(received[1], PartCreated)
    await rt.close_chat(sid)


async def test_persistence_skips_non_persisted_events(tmp_path: Path) -> None:
    """Heartbeats + chunks must NEVER land in chat.jsonl. The persist
    pump drains them; the storage filter inside `Chat.append_event`
    drops them."""
    rt, factory = _runtime(tmp_path)
    session = await rt.open_chat(create=True)
    sid = session.session_id
    adapter = factory.adapters[0]

    # Hammer the bus with non-persisted + persisted events.
    await adapter.feed(Heartbeat(event_id="hb1", time=_T, session_id=sid, last_activity_ms=1))
    await adapter.feed(
        AgentMessageChunk(
            event_id="c1",
            time=_T,
            session_id=sid,
            message_id="m1",
            part_id="p1",
            sequence=0,
            text="x",
        )
    )
    await adapter.feed(
        PartCreated(
            event_id="pc1",
            time=_T,
            session_id=sid,
            part=TextPart(part_id="p1", message_id="m1", text="x"),
        )
    )

    # Give the persist pump a tick to flush.
    await asyncio.sleep(0.05)
    await rt.close_chat(sid)

    # Re-open and confirm only the persisted events are in chat.jsonl.
    rt2 = HarnessRuntime(rt.project, adapter_factory=FakeAdapterFactory())
    reopened = await rt2.open_chat(sid)
    try:
        events = list(reopened._chat.events())
    finally:
        await rt2.close_chat(sid)
    event_types = [type(e).__name__ for e in events]
    assert "Heartbeat" not in event_types
    assert "AgentMessageChunk" not in event_types
    assert "PartCreated" in event_types
    # SessionCreated is always the first persisted event.
    assert event_types[0] == "SessionCreated"


# ---------------------------------------------------------------------------
# Permission broker
# ---------------------------------------------------------------------------


async def test_permission_broker_routes_request(tmp_path: Path) -> None:
    """When the adapter emits a PermissionRequest, the broker is asked,
    and the adapter's `resolve_permission` gets the chosen option."""
    chosen_options: list = []

    async def resolver(req):
        return "allow_once"

    broker = PermissionBroker(resolver, default_timeout_seconds=1.0)
    rt, factory = _runtime(tmp_path)
    session = await rt.open_chat(create=True, permission_broker=broker)
    sid = session.session_id
    adapter = factory.adapters[0]

    await adapter.feed(
        PermissionRequest(
            event_id="ev",
            time=_T,
            session_id=sid,
            request_id="r1",
            permission_kind="run",
            options=[
                PermissionOption(option_id="allow_once", name="Allow once"),
                PermissionOption(option_id="reject_once", name="Reject once"),
            ],
        )
    )
    # Block until the permission pump has replied (deterministic — no sleep).
    await adapter.wait_for_permission_reply("r1", "allow_once")
    assert adapter.permission_replies == [("r1", "allow_once")]
    await rt.close_chat(sid)
    _ = chosen_options


async def test_plan_mode_injects_agent_and_system_prompt(tmp_path: Path) -> None:
    """In plan mode, send_prompt routes through the `plan` agent and
    appends the plan-mode system directive. Other modes do neither."""
    from alkera_cli.harness.permission_mode import PLAN_MODE_SYSTEM_PROMPT

    rt, factory = _settling_runtime(tmp_path)
    session = await rt.open_chat(create=True)
    adapter = factory.adapters[0]
    try:
        # default mode → no plan agent, no PLAN steering (the always-on main-agent
        # guidance is injected on every root turn, but that's orthogonal to mode).
        await session.send_prompt("hello")
        first = adapter.sent_prompts[-1]
        assert first.agent is None
        assert PLAN_MODE_SYSTEM_PROMPT not in (first.system or "")

        # plan mode → plan agent + plan system prompt (composed with the guidance).
        session.set_permission_mode("plan")
        await session.send_prompt("design something")
        planned = adapter.sent_prompts[-1]
        assert planned.agent == "plan"
        assert PLAN_MODE_SYSTEM_PROMPT in (planned.system or "")

        # back to default → no plan agent, no plan steering.
        session.set_permission_mode("default")
        await session.send_prompt("go")
        third = adapter.sent_prompts[-1]
        assert third.agent is None
        assert PLAN_MODE_SYSTEM_PROMPT not in (third.system or "")
    finally:
        await rt.close_chat(session.session_id)


async def test_permission_mode_drives_auto_approval(tmp_path: Path) -> None:
    """A mode-aware broker (like the CLI's) auto-approves edits in
    auto/bypass and prompts nothing — the adapter's resolve_permission
    gets allow_once without any human in the loop. Switching the
    session's mode changes the outcome on the next request."""
    from alkera_cli.harness.permission_mode import mode_auto_decision

    rt, factory = _runtime(tmp_path)

    async def resolver(req):
        decision = mode_auto_decision(session.permission_mode, req.canonical_kind)
        # No prompt path in the test — default would block; we only
        # exercise allow/reject here.
        return "allow_once" if decision == "allow" else "reject_once"

    broker = PermissionBroker(resolver, default_timeout_seconds=1.0)
    session = await rt.open_chat(create=True, permission_broker=broker, question_broker=None)
    sid = session.session_id
    adapter = factory.adapters[0]

    def _edit_request(rid: str) -> PermissionRequest:
        return PermissionRequest(
            event_id=rid,
            time=_T,
            session_id=sid,
            request_id=rid,
            permission_kind="edit",
            canonical_kind="edit",
            options=[PermissionOption(option_id="allow_once", name="Allow once")],
        )

    # auto → edit auto-approved (the kind-only fallback allows the middle).
    session.set_permission_mode("auto")
    await adapter.feed(_edit_request("r1"))
    await adapter.wait_for_permission_reply("r1", "allow_once")
    assert ("r1", "allow_once") in adapter.permission_replies

    # plan → edit auto-rejected.
    session.set_permission_mode("plan")
    await adapter.feed(_edit_request("r2"))
    await adapter.wait_for_permission_reply("r2", "reject_once")
    assert ("r2", "reject_once") in adapter.permission_replies

    await rt.close_chat(sid)


# ---------------------------------------------------------------------------
# Subagent stub
# ---------------------------------------------------------------------------


# spawn_subagent is now implemented — see test_subagent_spawn.py for its full
# (unit) coverage + test_alkera_mcp_e2e.py for the live spawn-tool e2e.


# ---------------------------------------------------------------------------
# Persistence back-pressure: the persist queue MUST be unbounded
# ---------------------------------------------------------------------------


def test_persist_queue_constant_is_unbounded() -> None:
    """The audit log can NEVER drop. asyncio.Queue(maxsize<=0) is the
    documented unbounded form."""
    from alkera_cli.harness.runtime import PERSIST_QUEUE_MAXSIZE

    assert PERSIST_QUEUE_MAXSIZE == 0


async def test_persist_pump_does_not_drop_under_burst(tmp_path: Path) -> None:
    """Feeding >> DEFAULT_QUEUE_MAXSIZE persisted events in a tight loop
    must result in every event reaching `chat.append_event`. With the
    old bounded persist queue (4096), publish() would silently drop
    excess events — losing audit history. Unbounded persist guarantees
    no drops."""
    from alkera_cli.harness.event_bus import DEFAULT_QUEUE_MAXSIZE

    rt, factory = _runtime(tmp_path)
    session = await rt.open_chat(create=True)
    sid = session.session_id
    adapter = factory.adapters[0]

    # Replace `append_event` with a counter — we're testing the bus
    # → persist-pump path, not the actual JSONL write.
    call_count = 0

    def _counting_append(event):
        nonlocal call_count
        call_count += 1

    session._chat.append_event = _counting_append

    n = DEFAULT_QUEUE_MAXSIZE + 1000  # well past the bounded ceiling
    for i in range(n):
        await adapter.feed(
            PartCreated(
                event_id=f"e{i}",
                time=_T,
                session_id=sid,
                part=TextPart(part_id=f"p{i}", message_id="m", text="x"),
            )
        )

    # Let the persist pump drain whatever's left in the queue.
    for _ in range(50):
        if call_count >= n:
            break
        await asyncio.sleep(0.02)

    assert call_count == n, (
        f"expected {n} events persisted, got {call_count} — "
        f"persist sub dropped {n - call_count} events"
    )
    await rt.close_chat(sid)


# ---------------------------------------------------------------------------
# C2: native_state() pinning across opens
# ---------------------------------------------------------------------------


def _pinning_factory(native: dict[str, Any] | None = None) -> FakeAdapterFactory:
    """A factory whose fakes report ``native`` from native_state()."""
    return FakeAdapterFactory(lambda: FakeAdapter(native_state_value=native))


async def test_native_state_pinned_into_manifest_after_first_start(
    tmp_path: Path,
) -> None:
    """On first open of a chat, after `adapter.start()` returns the
    runtime merges `adapter.native_state()` into `manifest.harness` and
    flushes immediately. The pin survives a close/re-open cycle."""
    project = ProjectDirectory(tmp_path / ".alkera")
    factory = _pinning_factory(native={"opencode_session_id": "oc-pinned-1"})
    rt = HarnessRuntime(project, adapter_factory=factory)

    s1 = await rt.open_chat(create=True, title="pin me")
    sid = s1.session_id
    # The manifest mirrors the adapter's pin immediately (not on close).
    assert s1.manifest.harness == {"opencode_session_id": "oc-pinned-1"}
    await rt.close_chat(sid)

    # New open — the runtime should hand the persisted pin to the
    # next adapter via SessionConfig.harness_native.
    await rt.open_chat(sid)
    try:
        cfg = factory.configs[-1]
        # The persisted pin round-trips into the next session's harness_native…
        assert cfg.harness_native["opencode_session_id"] == "oc-pinned-1"
        # …alongside the always-on Alkera tool surface the runtime now injects
        # (the OpenCode local-MCP block).
        assert "alkera_mcp" in cfg.harness_native
    finally:
        await rt.close_chat(sid)


async def test_native_state_empty_does_not_clobber_existing_harness_fields(
    tmp_path: Path,
) -> None:
    """An adapter that returns an empty native_state() must NOT empty
    out the manifest's harness dict (which may carry other identity
    fields written elsewhere)."""
    project = ProjectDirectory(tmp_path / ".alkera")
    factory = _pinning_factory(native=None)  # adapter returns {}
    rt = HarnessRuntime(project, adapter_factory=factory)

    s = await rt.open_chat(create=True, title="no pin")
    try:
        assert s.manifest.harness == {}
    finally:
        await rt.close_chat(s.session_id)


async def test_native_state_merges_with_existing_harness_dict(
    tmp_path: Path,
) -> None:
    """Pinning is a merge, not a replace — pre-existing entries in
    `manifest.harness` survive."""
    project = ProjectDirectory(tmp_path / ".alkera")

    # First run pins {"sid": "A"} via the factory.
    factory_a = _pinning_factory(native={"sid": "A"})
    rt_a = HarnessRuntime(project, adapter_factory=factory_a)
    s1 = await rt_a.open_chat(create=True, title="merge")
    sid = s1.session_id
    await rt_a.close_chat(sid)

    # Second run uses a different adapter that pins {"writer": "fake-1.2"}.
    # The manifest should end with BOTH keys.
    factory_b = _pinning_factory(native={"writer": "fake-1.2"})
    rt_b = HarnessRuntime(project, adapter_factory=factory_b)
    s2 = await rt_b.open_chat(sid)
    try:
        assert s2.manifest.harness == {"sid": "A", "writer": "fake-1.2"}
    finally:
        await rt_b.close_chat(sid)


class _RejectLostAfterStopAdapter(FakeAdapter):
    """Models opencode: a `reject_question` POST after `stop()` hits a dead HTTP
    client and is LOST. So a pending question's reject MUST be delivered while the
    adapter is still alive — which is the whole point of cancelling the question
    pump before `adapter.stop()` in `close()`."""

    async def reject_question(self, request_id: str, reason: str | None = None) -> None:
        if self._stopped:
            raise RuntimeError("adapter stopped — a reject here is lost (the re-ask bug)")
        await super().reject_question(request_id, reason)


class _RejectLostFactory(AdapterFactory):
    def __init__(self) -> None:
        super().__init__(binary=None)
        self.adapters: list[_RejectLostAfterStopAdapter] = []

    def __call__(  # type: ignore[override]
        self, config: SessionConfig, *, bus: EventBus, harness_type: str = "oc"
    ) -> _RejectLostAfterStopAdapter:
        adapter = _RejectLostAfterStopAdapter()
        adapter._bus = bus
        self.adapters.append(adapter)
        return adapter


async def test_close_rejects_a_pending_question_before_stopping_the_adapter(
    tmp_path: Path,
) -> None:
    """Closing a chat with an UNANSWERED question delivers the reject to the harness
    WHILE IT IS STILL ALIVE — so opencode resolves the pending tool and won't
    re-ask it on resume (the double-question-card bug). The adapter here loses any
    reject sent after `stop()`, so this fails unless `close()` cancels the question
    pump (firing its shielded reject) BEFORE `adapter.stop()`."""
    parked = asyncio.Event()

    async def _never_answers(_req: QuestionRequest) -> Any:
        parked.set()  # the resolver is now holding the turn open…
        await asyncio.sleep(3600)  # …and the human walked away (never resolves)

    project = ProjectDirectory(tmp_path / ".alkera")
    factory = _RejectLostFactory()
    rt = HarnessRuntime(project, adapter_factory=factory)
    chat = await rt.open_chat(
        create=True, title="t", question_broker=QuestionBroker(_never_answers)
    )
    adapter = factory.adapters[0]
    await adapter.feed(
        QuestionRequest(
            event_id="q",
            time=_T,
            session_id=chat.session_id,
            request_id="rq1",
            questions=[QuestionPrompt(question="Pick a warehouse?")],
        )
    )
    await asyncio.wait_for(parked.wait(), timeout=2)  # the question pump is parked on the broker

    await chat.close()

    # The reject landed before stop() — recorded, not lost to a dead client.
    assert ("rq1", "client-disconnected") in adapter._question_rejects
    assert adapter.stopped  # …and the adapter did go on to stop


async def test_open_chat_does_not_seed_inline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seeding is owned by the scheduler beat (+ the file watcher), NOT chat-open — so a huge
    project's first chat is instant and open_chat never kicks the offline seed itself."""
    seeded = asyncio.Event()

    async def _seed(*_a: object, **_k: object) -> None:
        seeded.set()

    rt, _factory = _runtime(tmp_path)
    monkeypatch.setattr(rt, "refresh_lineage", _seed)
    chat = rt._chats_store.create(title="c", harness_type="agent")
    sid = chat.session_id
    chat.close()
    try:
        await rt.open_chat(sid)
        assert not seeded.is_set()  # open_chat did NOT seed inline — the beat/watcher own it
    finally:
        await rt.close_all()


# ---------------------------------------------------------------------------
# Denial handling: a MANUAL reject ends the turn; AUTOMATIC policy denials
# continue (guarded only by a high stuck-loop failsafe).
# ---------------------------------------------------------------------------


def _perm_req(*, request_id: str = "r", raw: str | None = "echo x > f") -> PermissionRequest:
    subject = {"capability": "bash", "raw": raw} if raw is not None else None
    return PermissionRequest(
        event_id="e",
        time=_T,
        session_id="s",
        request_id=request_id,
        tool_call_id="tc",
        permission_kind="bash",
        canonical_kind="shell",
        subject=subject,
    )


async def _open_session(rt: HarnessRuntime):
    chat = rt._chats_store.create(title="c", harness_type="agent")
    sid = chat.session_id
    chat.close()
    return await rt.open_chat(sid), sid


async def test_manual_reject_interrupts_only_on_claude(tmp_path: Path) -> None:
    from alkera_cli.harness.registry import CLAUDE_HARNESS

    rt, _ = _runtime(tmp_path)
    session, sid = await _open_session(rt)  # opencode-like harness ("agent")
    try:
        cancels: list[bool] = []
        stops: list[str] = []

        async def fake_cancel() -> None:
            cancels.append(True)

        async def fake_stop(detail: str) -> None:
            stops.append(detail)

        session.cancel = fake_cancel  # type: ignore[method-assign]
        session._stop_turn = fake_stop  # type: ignore[method-assign]

        ev = _perm_req()
        # On opencode a human reject sends NO reason → opencode ends the turn itself
        # (RejectedError); the runtime must NOT also interrupt (a redundant interrupt
        # would synthesize a spurious 'aborted' after the clean stop).
        await session._after_reject(ev, "human")
        assert cancels == []

        # On Claude the SDK continues on any deny, so a human reject MUST interrupt.
        session._chat.manifest.harness_type = CLAUDE_HARNESS
        await session._after_reject(ev, "human")
        assert cancels == [True]

        # Every automatic policy denial keeps the turn going — never interrupts (on
        # either harness), no stop (far below the failsafe threshold).
        cancels.clear()
        for decided_by in ("mode", "rule", "floor", "judge", "fail_closed"):
            await session._after_reject(ev, decided_by)
        assert cancels == []
        assert stops == []
    finally:
        await rt.close_chat(sid)


async def test_repeating_one_refused_action_never_stops_the_turn(tmp_path: Path) -> None:
    """A model that keeps asking for the same refused thing is spending its own
    round trips, and a turn ended on its behalf takes the rest of the work with
    it. Only the person, the gateway or the harness ends a turn."""
    rt, _ = _runtime(tmp_path)
    session, sid = await _open_session(rt)
    try:
        stops: list[str] = []

        async def fake_stop(detail: str) -> None:
            stops.append(detail)

        session._stop_turn = fake_stop  # type: ignore[method-assign]

        ev = _perm_req(raw="echo x > f")
        for _ in range(100):
            await session._after_reject(ev, "mode")
        assert stops == []
    finally:
        await rt.close_chat(sid)


async def test_every_repeat_of_a_refused_action_is_still_recorded(tmp_path: Path) -> None:
    """The turn survives the repetition; the ledger still carries one row per
    ask, so a model wedged on a refused action is visible to whoever reads the
    decisions afterwards."""
    import json

    rt, _ = _runtime(tmp_path)
    session, sid = await _open_session(rt)
    try:

        async def _resolver(req: PermissionRequest) -> str:
            raise AssertionError("a refused write never reaches the person")

        session._broker = PermissionBroker(_resolver)
        session.set_permission_mode("read_only")
        for n in range(5):
            option, _reason, decided_by = await session._decide_permission(
                _perm_req(request_id=f"r{n}", raw=None)
            )
            assert option == "reject_once"
            await session._after_reject(_perm_req(request_id=f"r{n}"), decided_by)
        rows = [
            json.loads(line)
            for line in (session.decision_sink.directory / "decisions.jsonl")
            .read_text()
            .splitlines()
            if line.strip()
        ]
        assert [row["decision"] for row in rows] == ["reject"] * 5
        assert {row["request_id"] for row in rows} == {f"r{n}" for n in range(5)}
    finally:
        await rt.close_chat(sid)


async def test_coarse_reject_is_not_silent_and_continues(tmp_path: Path) -> None:
    # A subject-less ask (no typed descriptor) auto-rejected by the MODE must still
    # hand the model a provenance-bearing reason (it used to return None) — and it's
    # automatic, so the turn continues.
    rt, _ = _runtime(tmp_path)
    session, sid = await _open_session(rt)
    try:

        async def _resolver(req: PermissionRequest) -> str:
            return "reject_once"  # not called on an auto-reject

        session._broker = PermissionBroker(_resolver)
        session.set_permission_mode("read_only")
        option, reason, decided_by = await session._decide_permission(_perm_req(raw=None))
        assert str(option).startswith("reject")
        assert reason is not None and "read-only mode" in reason
        assert decided_by != "human"  # automatic → continue
    finally:
        await rt.close_chat(sid)


async def test_coarse_human_reject_is_marked_human_with_no_reason(tmp_path: Path) -> None:
    # A human-prompted reject (default mode) is marked ``human`` AND carries NO reason,
    # so opencode turns it into a turn-ending RejectedError (control back to the user)
    # rather than a recoverable CorrectedError that would keep the turn going.
    rt, _ = _runtime(tmp_path)
    session, sid = await _open_session(rt)
    try:

        async def _resolver(req: PermissionRequest) -> str:
            return "reject_once"

        session._broker = PermissionBroker(_resolver)
        session.set_permission_mode("default")
        option, reason, decided_by = await session._decide_permission(_perm_req(raw=None))
        assert str(option).startswith("reject")
        assert decided_by == "human"
        assert reason is None
    finally:
        await rt.close_chat(sid)


async def test_mode_switch_emits_one_shot_reminder_on_the_next_turn(tmp_path: Path) -> None:
    rt, factory = _settling_runtime(tmp_path)
    session, sid = await _open_session(rt)
    try:
        await session.send_prompt("one")  # default, first turn → seeds the baseline silently
        session.set_permission_mode("read_only")
        await session.send_prompt("two")  # the switch is announced exactly here
        await session.send_prompt("three")  # same mode → not repeated
        sent = factory.adapters[-1].sent_prompts
        s1, s2, s3 = (sent[0].system or "", sent[1].system or "", sent[2].system or "")
        # First turn never fires a spurious "switched" notice (resume safety).
        assert "just switched the permission mode" not in s1
        # The switch is announced once, on the first turn after it.
        assert "from default to read-only" in s2
        # ...and not repeated on a same-mode turn.
        assert "just switched the permission mode" not in s3
        # The always-on read-only steer rides EVERY read-only turn.
        assert "read-only mode, set by the user" in s2
        assert "read-only mode, set by the user" in s3
    finally:
        await rt.close_chat(sid)


async def test_open_chat_closes_a_turn_interrupted_by_a_crash(tmp_path: Path) -> None:
    """A chat the daemon left mid-turn (open tool call, `running` status, no
    turn.finished — e.g. a crash/restart killed the adapter) is reconciled on the
    next open: the persisted log gains the close events, so a resumed UI shows the
    turn ended instead of spinning "working…" forever. Idempotent — reopening an
    already-closed chat adds nothing."""
    rt, _ = _runtime(tmp_path)
    chat = rt._chats_store.create(title="c", harness_type="agent")
    sid = chat.session_id
    chat.append_event(
        TurnStarted(event_id="e1", time=_T, session_id=sid, turn_id="t1", user_message_id="m1")
    )
    chat.append_event(
        SessionStatusChanged(event_id="e2", time=_T, session_id=sid, status="running")
    )
    chat.append_event(
        ToolCall(
            event_id="e3",
            time=_T,
            session_id=sid,
            tool_call_id="tc1",
            message_id="m1",
            tool_name="sql.query",
            status="pending",
        )
    )
    chat.close()

    await rt.open_chat(sid)
    try:
        # The reconciliation is appended SYNCHRONOUSLY during open_chat.
        events = list(rt.read_chat_events(sid))
        finished = [e for e in events if e.event_type == "turn.finished"]
        assert len(finished) == 1
        assert finished[0].stop_reason == "cancelled"  # type: ignore[attr-defined]
        assert "interrupted" in (finished[0].error_detail or "")  # type: ignore[attr-defined]
        # The pending tool call was failed, and the session ends idle.
        assert any(e.event_type == "tool.call_update" for e in events)
        assert any(
            e.event_type == "session.status_changed" and e.status == "idle"  # type: ignore[attr-defined]
            for e in events
        )
    finally:
        await rt.close_chat(sid)

    # Reopening a now-closed chat must NOT close it a second time.
    await rt.open_chat(sid)
    try:
        finished2 = [e for e in rt.read_chat_events(sid) if e.event_type == "turn.finished"]
        assert len(finished2) == 1  # still exactly one — idempotent
    finally:
        await rt.close_chat(sid)


async def test_reconcile_write_failure_does_not_block_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Best-effort contract: if appending a reconciliation closure fails (disk full /
    bad fd), the chat must STILL open — a user recovering from a crash can't be locked
    out. A partial write self-heals on the next clean open (reconcile is idempotent)."""
    from alkera_core.project.chats.chat import Chat

    rt, _ = _runtime(tmp_path)
    chat = rt._chats_store.create(title="c", harness_type="agent")
    sid = chat.session_id
    chat.append_event(
        TurnStarted(event_id="e1", time=_T, session_id=sid, turn_id="t1", user_message_id="m1")
    )
    chat.append_event(
        ToolCall(
            event_id="e2",
            time=_T,
            session_id=sid,
            tool_call_id="tc1",
            message_id="m1",
            status="pending",
        )
    )
    chat.close()

    # Make the closing turn.finished append blow up mid-reconcile (the tool-update
    # closure is emitted first and lands; the turn closure then raises).
    real_append = Chat.append_event

    def _boom(self: Chat, event: object) -> None:
        if getattr(event, "event_type", "") == "turn.finished":
            raise OSError("simulated append failure")
        real_append(self, event)

    monkeypatch.setattr(Chat, "append_event", _boom)
    await rt.open_chat(sid)  # must NOT raise despite the append failure
    await rt.close_chat(sid)

    # A clean reopen self-heals: the still-open turn finally gets its turn.finished.
    monkeypatch.undo()
    await rt.open_chat(sid)
    try:
        assert any(e.event_type == "turn.finished" for e in rt.read_chat_events(sid))
    finally:
        await rt.close_chat(sid)


async def test_seed_worker_prune_keeps_the_blob_gc_job(tmp_path: Path) -> None:
    """A seed/context worker is a fresh HarnessRuntime that never registers the standing-job
    runners; its plugin_registry() runs prune_orphans, which MUST NOT delete the daemon's standing
    blob_gc job. blob_gc is in the armed standing set but was missing from the prune-protected set,
    so every worker deleted it → blob GC never ran under the daemon (unbounded .alkera/ growth)."""
    from alkera_cli.plugins.plugin_base.blob_gc_job import BLOB_GC_KIND, schedule_blob_gc
    from alkera_cli.plugins.plugin_base.scheduler import IntervalTrigger, new_job

    daemon, _ = _runtime(tmp_path)
    schedule_blob_gc(daemon.scheduler(), daemon.project, now=_T)
    # Control: an unprotected, runnerless job that the SAME prune pass must delete — proving the
    # prune actually ran (so blob_gc's survival is protection, not a skipped/suppressed prune).
    daemon.scheduler().register(
        new_job("orphan_kind", IntervalTrigger(seconds=99.0), now=_T, kind="orphan_kind")
    )

    # A fresh worker runtime on the SAME project (file-backed scheduler store) sees both armed jobs
    # but has no runner for either.
    worker = HarnessRuntime(daemon.project, adapter_factory=FakeAdapterFactory())
    kinds_before = {j.kind for j in worker.scheduler().list_jobs()}
    assert {BLOB_GC_KIND, "orphan_kind"} <= kinds_before  # shared store

    await worker.plugin_registry()  # runs prune_orphans

    kinds_after = {j.kind for j in worker.scheduler().list_jobs()}
    assert "orphan_kind" not in kinds_after, "prune did not run (control job survived)"
    assert BLOB_GC_KIND in kinds_after, "the seed worker's prune deleted the daemon's blob_gc job"


# ---------------------------------------------------------------------------
# Every turn carries the chat's pin as the manifest holds it when it is sent
# ---------------------------------------------------------------------------

_PINNED = {
    "provider_id": "alkera-anthropic",
    "model_id": "claude-opus-4.8",
    "efforts": ["low", "high"],
    "effort": "high",
}


@pytest.mark.parametrize(
    ("pin", "kwargs", "model", "variant"),
    [
        pytest.param(
            _PINNED,
            {},
            {"provider_id": "alkera-anthropic", "model_id": "claude-opus-4.8"},
            "high",
            id="no-model-named-carries-the-pin-and-its-effort",
        ),
        pytest.param(
            _PINNED,
            {"variant": "low"},
            {"provider_id": "alkera-anthropic", "model_id": "claude-opus-4.8"},
            "low",
            id="a-callers-effort-rides-on-the-pinned-model",
        ),
        pytest.param(
            _PINNED,
            {"model": {"provider_id": "alkera-openai", "model_id": "gpt-5.5"}},
            {"provider_id": "alkera-openai", "model_id": "gpt-5.5"},
            None,
            id="a-callers-model-is-obeyed",
        ),
        pytest.param(
            {**_PINNED, "effort": "max"},
            {},
            {"provider_id": "alkera-anthropic", "model_id": "claude-opus-4.8"},
            None,
            id="an-effort-the-model-does-not-offer-is-not-sent",
        ),
        pytest.param(
            {**_PINNED, "efforts": [], "effort": "high"},
            {},
            {"provider_id": "alkera-anthropic", "model_id": "claude-opus-4.8"},
            None,
            id="a-model-with-no-efforts-sends-none",
        ),
        pytest.param({}, {}, None, None, id="no-pin-leaves-the-harness-default"),
    ],
)
async def test_a_turn_is_sent_with_the_chats_pin(
    tmp_path: Path,
    pin: dict[str, Any],
    kwargs: dict[str, Any],
    model: dict[str, str] | None,
    variant: str | None,
) -> None:
    rt, factory = _settling_runtime(tmp_path)
    session = await rt.open_chat(create=True, model=dict(pin) if pin else None)
    try:
        await session.send_prompt("hi", **kwargs)
        sent = factory.adapters[0]._sent_prompts[-1]
        assert (sent.model, sent.variant) == (model, variant)
    finally:
        await rt.close_chat(session.session_id)


async def test_a_switch_while_the_session_runs_reaches_the_next_turn(tmp_path: Path) -> None:
    """The pin moves while the agent keeps running (a relay, a picker); the
    next turn carries the new model and effort, not the spawn-time ones."""
    rt, factory = _settling_runtime(tmp_path)
    session = await rt.open_chat(create=True, model=dict(_PINNED))
    try:
        await session.send_prompt("before")
        await session.set_model(
            {
                "provider_id": "alkera-anthropic",
                "model_id": "claude-sonnet-4.6",
                "efforts": ["low", "high"],
                "effort": "low",
            }
        )
        await session.send_prompt("after")
        sent = factory.adapters[0]._sent_prompts
        assert (sent[0].model, sent[0].variant) == (
            {"provider_id": "alkera-anthropic", "model_id": "claude-opus-4.8"},
            "high",
        )
        assert (sent[1].model, sent[1].variant) == (
            {"provider_id": "alkera-anthropic", "model_id": "claude-sonnet-4.6"},
            "low",
        )
    finally:
        await rt.close_chat(session.session_id)


class _ServingAdapter(FakeAdapter):
    """A fake that carries only the models it was told it was spawned with."""

    carried: frozenset[str] = frozenset()

    def serves_model(self, model: Any) -> bool:
        return f"{model['provider_id']}/{model['model_id']}" in self.carried


async def test_serves_pinned_model_asks_for_the_model_and_effort_the_turn_would_send(
    tmp_path: Path,
) -> None:
    _ServingAdapter.carried = frozenset({"alkera-anthropic/claude-opus-4.8::high"})
    project = ProjectDirectory(tmp_path / ".alkera")
    factory = FakeAdapterFactory(_ServingAdapter)
    rt = HarnessRuntime(project, adapter_factory=factory)
    session = await rt.open_chat(create=True, model=dict(_PINNED))
    try:
        assert session.serves_pinned_model() is True
        await session.set_model({**_PINNED, "effort": "low"})
        assert session.serves_pinned_model() is False
        await session.set_model({})
        assert session.serves_pinned_model() is True
    finally:
        await rt.close_chat(session.session_id)
