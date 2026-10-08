"""Adapter configuration: env (airgap + gateway seam), options (permission
settings, isolation, resume/session pinning), native-state, info, plan steering.
All exercised on a bare adapter — no CLI spawn.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alkera_cli.harness.adapter import HarnessModelError, PromptInput, SessionConfig
from alkera_cli.harness.adapters.claude_agent import (
    _DISALLOWED_TOOLS,
    _LOCKDOWN_ENV,
    _PLAN_TOOL_FQN,
    ClaudeAgentAdapter,
    _detach_claude_from_controlling_terminal,
)
from alkera_cli.harness.claude_binary import ResolvedClaudeBinary
from alkera_cli.harness.event_bus import EventBus
from alkera_core.schemas.chat import (
    MessageCreated,
    PartCreated,
    SessionStatusChanged,
    TextPart,
    TurnStarted,
)


def _adapter(
    tmp_path: Path,
    *,
    harness_native: dict | None = None,
    model: dict | None = None,
    binary: ResolvedClaudeBinary | None = None,
    parent_session_id: str | None = None,
) -> ClaudeAgentAdapter:
    config = SessionConfig(
        session_id="our-sid",
        project_dir=tmp_path,
        chat_dir=tmp_path / "chat",
        harness_native=harness_native or {},
        model=model,
        parent_session_id=parent_session_id,
    )
    return ClaudeAgentAdapter(
        config,
        binary=binary
        or ResolvedClaudeBinary(path=Path("/usr/bin/true"), source="path", version="2.1.0"),
        event_bus=EventBus(),
    )


# ---------------------------------------------------------------------------
# Env
# ---------------------------------------------------------------------------


def test_build_env_has_lockdown_and_config_dir(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    env = adapter._build_env()
    for key, val in _LOCKDOWN_ENV.items():
        assert env[key] == val
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "chat" / ".runtime")


def test_build_env_merges_claude_env_override_and_override_wins(tmp_path: Path) -> None:
    adapter = _adapter(
        tmp_path,
        harness_native={
            "claude_env": {
                "ANTHROPIC_BASE_URL": "http://gw/anthropic",
                "ANTHROPIC_API_KEY": "jwt",
                # override a lockdown default to prove the seam wins
                "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "0",
            }
        },
    )
    env = adapter._build_env()
    assert env["ANTHROPIC_BASE_URL"] == "http://gw/anthropic"
    assert env["ANTHROPIC_API_KEY"] == "jwt"
    assert env["CLAUDE_CODE_DISABLE_TERMINAL_TITLE"] == "0"


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


def test_build_options_default_mode_and_permission_settings(tmp_path: Path) -> None:
    opts = _adapter(tmp_path)._build_options(resume=False)
    assert opts.permission_mode == "default"
    assert opts.can_use_tool is not None
    assert opts.include_partial_messages is True
    assert opts.setting_sources == []
    assert set(_DISALLOWED_TOOLS).issubset(set(opts.disallowed_tools))
    settings = json.loads(opts.settings)
    perms = settings["permissions"]
    # Full passthrough: every permission-relevant tool asks (incl. read tools);
    # only our own MCP tools auto-allow. Sandbox pinned off.
    assert "Bash" in perms["ask"] and "Edit" in perms["ask"]
    for read_tool in ("Read", "Glob", "Grep", "LS"):
        assert read_tool in perms["ask"], read_tool
        assert read_tool not in perms["allow"], read_tool
    assert _PLAN_TOOL_FQN in perms["allow"]
    assert settings["sandbox"]["enabled"] is False
    assert "plan" in opts.mcp_servers
    # Vendor-specific tools are disabled outright (Alkera owns these features;
    # AskUserQuestion never prompts; native plan mode is off). They are NOT gated
    # via the broker — they're removed from the tool set entirely.
    disallowed = set(opts.disallowed_tools)
    for vendor in (
        "AskUserQuestion",
        "Task",
        "NotebookEdit",
        "Skill",
        "Workflow",
        "EnterPlanMode",
        "ExitPlanMode",
        "EnterWorktree",
        "ExitWorktree",
        "ScheduleWakeup",
        "CronCreate",
        "TaskCreate",
        "TaskStop",
        "TodoWrite",
    ):
        assert vendor in disallowed, vendor
    # Native Bash is disabled in favor of our own bash loopback-MCP tool ONLY on
    # POSIX (that parent-hosted tool is POSIX-only); Windows keeps native Bash.
    if os.name == "posix":
        assert "Bash" in disallowed
    else:
        assert "Bash" not in disallowed
    # ...and therefore are NOT in the broker-gated "ask" or auto-"allow" sets.
    assert "NotebookEdit" not in perms["ask"]
    assert "Task" not in perms["ask"]
    assert "AskUserQuestion" not in perms["ask"]
    assert "TodoWrite" not in perms["allow"]
    # WebFetch/WebSearch are ENABLED (web access), gated through our broker — NOT
    # disallowed. (Trades the strict airgap for web reach.)
    assert "WebFetch" not in disallowed and "WebSearch" not in disallowed
    assert "WebFetch" in perms["ask"] and "WebSearch" in perms["ask"]


def test_web_fetch_is_reachable_by_default(tmp_path: Path) -> None:
    """The switch is subtractive, so an adapter told nothing keeps Claude's own
    broker-gated web reach — the behaviour every session had before it existed."""
    opts = _adapter(tmp_path)._build_options(resume=False)
    assert "WebFetch" not in set(opts.disallowed_tools)
    assert "WebFetch" in json.loads(opts.settings)["permissions"]["ask"]


@pytest.mark.parametrize(
    ("native", "reachable"),
    [
        pytest.param({"web_fetch_enabled": True}, True, id="switch-on"),
        pytest.param({"web_fetch_enabled": False}, False, id="switch-off"),
    ],
)
def test_the_web_fetch_switch_removes_claudes_native_fetch(
    tmp_path: Path, native: dict, reachable: bool
) -> None:
    """`AGENT_WEB_FETCH_ENABLED=false` has to hold on THIS harness too. Claude
    never gets the `web` MCP mount — its web reach is the vendor's own WebFetch —
    so leaving that tool merely broker-gated would give an install that believes
    its agents are off the public internet a session that still reaches it.

    Disallowed, not denied: the tool has to leave the model's tool set entirely,
    or it is still advertised and the turn is spent on a refusal. WebSearch is
    untouched — it queries a fixed provider, and the switch is about fetching an
    arbitrary URL."""
    opts = _adapter(tmp_path, harness_native=native)._build_options(resume=False)
    disallowed = set(opts.disallowed_tools)
    asked = json.loads(opts.settings)["permissions"]["ask"]
    assert ("WebFetch" in disallowed) is not reachable
    assert ("WebFetch" in asked) is reachable
    # The metasearch the org is still paying for is never collateral.
    assert "WebSearch" not in disallowed
    assert "WebSearch" in asked
    # Nothing else moved out of the tool set.
    assert set(_DISALLOWED_TOOLS) <= disallowed


def test_a_subagent_inherits_the_web_fetch_switch(tmp_path: Path) -> None:
    """A subagent runs headless on the same deployment. If the switch only held
    for the main session, the easiest way past it would be to spawn one."""
    adapter = _adapter(
        tmp_path, harness_native={"web_fetch_enabled": False}, parent_session_id="parent-sid"
    )
    assert adapter._is_subagent
    opts = adapter._build_options(resume=False)
    assert "WebFetch" in set(opts.disallowed_tools)
    assert "WebFetch" not in json.loads(opts.settings)["permissions"]["ask"]


def test_build_options_cli_path_and_model(tmp_path: Path) -> None:
    adapter = _adapter(
        tmp_path,
        model={"provider_id": "alkera-anthropic", "model_id": "claude-opus-4.5"},
    )
    opts = adapter._build_options(resume=False)
    assert opts.cli_path == str(Path("/usr/bin/true"))  # OS-native separators
    assert opts.model == "claude-opus-4.5"


def test_build_options_always_pins_cli_path(tmp_path: Path) -> None:
    # The binary path is always pinned — never None, never a $PATH fallback.
    opts = _adapter(tmp_path)._build_options(resume=False)
    assert opts.cli_path == str(Path("/usr/bin/true"))  # OS-native separators


def test_build_options_session_id_vs_resume(tmp_path: Path) -> None:
    fresh = _adapter(tmp_path)
    opts_fresh = fresh._build_options(resume=False)
    assert opts_fresh.session_id == fresh.session_id
    assert opts_fresh.resume is None

    opts_resume = fresh._build_options(resume=True)
    assert opts_resume.resume == fresh.session_id
    assert opts_resume.session_id is None


# ---------------------------------------------------------------------------
# Resume detection + native state
# ---------------------------------------------------------------------------


def test_fresh_chat_mints_uuid_and_no_resume(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    assert adapter._resume is False
    # a uuid4 string
    assert len(adapter.session_id) == 36 and adapter.session_id.count("-") == 4


def test_pinned_session_resumes(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path, harness_native={"agent_session_id": "pinned-uuid-123"})
    assert adapter._resume is True
    assert adapter.session_id == "pinned-uuid-123"


def test_native_state_pins_session(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    assert adapter.native_state() == {"agent_session_id": adapter.session_id}


def test_info_reports_binary_source_and_version(tmp_path: Path) -> None:
    info = _adapter(tmp_path).info()
    assert info.name == "claude-agent"
    assert info.source == "path"
    assert info.version == "2.1.0"


def test_is_available_reflects_local_claude(monkeypatch: object) -> None:
    import alkera_cli.harness.adapters.claude_agent as mod

    monkeypatch.setattr(mod, "claude_is_available", lambda: True)  # type: ignore[attr-defined]
    assert ClaudeAgentAdapter.is_available() is True
    monkeypatch.setattr(mod, "claude_is_available", lambda: False)  # type: ignore[attr-defined]
    assert ClaudeAgentAdapter.is_available() is False


# ---------------------------------------------------------------------------
# Plan steering selection
# ---------------------------------------------------------------------------


def test_plan_steering_for_plan_turns(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    steer = adapter._plan_steering(PromptInput(text="go", agent="plan"))
    assert steer is not None
    assert _PLAN_TOOL_FQN in steer


def test_plan_steering_honors_explicit_system(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    steer = adapter._plan_steering(PromptInput(text="go", system="be terse"))
    assert steer == "be terse"


def test_plan_steering_none_for_plain_turn(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    assert adapter._plan_steering(PromptInput(text="go")) is None


def test_plan_steering_appends_global_instructions(tmp_path: Path) -> None:
    """The user's global instructions ride the same per-turn <system-reminder> as the
    rest of the steering, appended AFTER an explicit system addendum."""
    adapter = _adapter(tmp_path, harness_native={"global_instructions": "Always use type hints."})
    steer = adapter._plan_steering(PromptInput(text="go", system="be terse"))
    assert steer is not None
    assert "be terse" in steer
    assert "Always use type hints." in steer
    assert steer.index("be terse") < steer.index("Always use type hints.")


def test_plan_steering_global_instructions_on_plain_turn(tmp_path: Path) -> None:
    """Even a plain turn (no plan, no system addendum) carries global instructions."""
    adapter = _adapter(tmp_path, harness_native={"global_instructions": "Prefer DuckDB."})
    steer = adapter._plan_steering(PromptInput(text="go"))
    assert steer is not None
    assert "Prefer DuckDB." in steer


def test_plan_steering_ignores_blank_global_instructions(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path, harness_native={"global_instructions": "   \n  "})
    assert adapter._plan_steering(PromptInput(text="go")) is None


# ---------------------------------------------------------------------------
# send_prompt records the user's message (so resume-replay isn't blank)
# ---------------------------------------------------------------------------


class _FakeClient:
    """Stands in for the SDK client so send_prompt runs without a real spawn."""

    async def query(self, _text: str) -> None: ...
    async def set_model(self, _m: str) -> None: ...


class _IdleClient(_FakeClient):
    """A spawned client whose message stream stays open and silent, so the
    pump a swap installs neither ends nor reads as a crash."""

    def __init__(self) -> None:
        self.disconnected = False
        self.set_models: list[str] = []

    async def set_model(self, model: str) -> None:
        self.set_models.append(model)

    async def receive_messages(self) -> AsyncIterator[object]:
        await asyncio.Event().wait()
        yield None  # pragma: no cover - never reached

    async def disconnect(self) -> None:
        self.disconnected = True


class _Spawns:
    """Stands in for ``_spawn_client``: records the model string and the resume
    flag each spawn was asked for, and hands back an idle client (or raises
    what the test scripted)."""

    def __init__(self, adapter: ClaudeAgentAdapter, *, fail: BaseException | None = None) -> None:
        self._adapter = adapter
        self.fail = fail
        self.calls: list[tuple[str | None, bool]] = []
        self.clients: list[_IdleClient] = []

    async def __call__(self, *, resume: bool) -> _IdleClient:
        self.calls.append((self._adapter._model_id, resume))
        if self.fail is not None:
            raise self.fail
        client = _IdleClient()
        self.clients.append(client)
        return client

    @property
    def models(self) -> list[str | None]:
        return [model for model, _resume in self.calls]


def _respawning(adapter: ClaudeAgentAdapter, **kw: object) -> _Spawns:
    spawns = _Spawns(adapter, **kw)  # type: ignore[arg-type]
    adapter._spawn_client = spawns  # type: ignore[method-assign]
    adapter._state.started = True
    first = _IdleClient()
    adapter._state.client = first  # type: ignore[assignment]
    return spawns


def test_prompt_input_rejects_split_show_thinking_boolean() -> None:
    """Catch a partial cleanup that leaves the independent boolean on the public
    prompt interface after reasoning effort became the sole thinking choice."""
    with pytest.raises(TypeError, match="show_thinking"):
        PromptInput(text="hello", show_thinking=True)  # type: ignore[call-arg]


async def test_send_prompt_emits_user_message_then_turn(tmp_path: Path) -> None:
    """The SDK never echoes the human prompt, so send_prompt must synthesize a
    user-role MessageCreated + its text part BEFORE TurnStarted, or resume-replay
    (which anchors on user-role MessageCreated) shows nothing and the chat looks
    brand new. Everything the send publishes carries the prompt's own attempt id:
    that stamp is what lets the runtime tell this turn's terminal from the one
    still arriving for the turn before it."""
    adapter = _adapter(tmp_path)
    adapter._state.started = True  # send_prompt requires a started client
    adapter._state.client = _FakeClient()  # type: ignore[assignment]
    sub = adapter._bus.subscribe()
    prompt = PromptInput(text="hello world")
    await adapter.send_prompt(prompt)

    collected: list[object] = []
    while not any(isinstance(e, SessionStatusChanged) for e in collected):
        collected.append(await asyncio.wait_for(anext(sub), timeout=1.0))  # type: ignore[arg-type]

    mc = next(e for e in collected if isinstance(e, MessageCreated))
    assert mc.role == "user"
    pc = next(e for e in collected if isinstance(e, PartCreated) and isinstance(e.part, TextPart))
    assert pc.part.text == "hello world"
    assert pc.part.message_id == mc.message_id  # text belongs to the user message
    ts = next(e for e in collected if isinstance(e, TurnStarted))
    assert ts.user_message_id == mc.message_id  # turn references the same user message
    assert ts.turn_id == prompt.turn_id
    running = next(e for e in collected if isinstance(e, SessionStatusChanged))
    assert (running.status, running.turn_id) == ("running", prompt.turn_id)
    # ordering: the user message precedes the turn (so replay/live read in order)
    assert collected.index(mc) < collected.index(ts)


async def test_send_prompt_skips_empty_user_text_part(tmp_path: Path) -> None:
    """An empty prompt still records the user MessageCreated (turn anchor) but no
    empty text part."""
    adapter = _adapter(tmp_path)
    adapter._state.started = True
    adapter._state.client = _FakeClient()  # type: ignore[assignment]
    sub = adapter._bus.subscribe()
    await adapter.send_prompt(PromptInput(text=""))

    collected: list[object] = []
    while not any(isinstance(e, TurnStarted) for e in collected):
        collected.append(await asyncio.wait_for(anext(sub), timeout=1.0))  # type: ignore[arg-type]
    assert any(isinstance(e, MessageCreated) for e in collected)
    assert not any(isinstance(e, PartCreated) for e in collected)


async def test_claude_all_enabled_efforts_summarize_null_inherits_and_none_disables(
    tmp_path: Path,
) -> None:
    """Catch high-only display routing or treating Python ``None`` as the
    user-facing "none": low/medium/high summarize, omitted/null inherit high,
    and literal ``"none"`` remains routed without the display component. Each
    change is applied by spawning the client again on the new model string."""
    adapter = _adapter(
        tmp_path,
        model={
            "provider_id": "alkera-anthropic",
            "model_id": "claude-opus-4.8::high",
        },
    )
    spawns = _respawning(adapter)

    async def send(prompt: PromptInput) -> None:
        await adapter.send_prompt(prompt)
        adapter._translator_ctx.open_queries.clear()  # the turn ended

    # The first client already runs the spawn-time effort with its display.
    assert adapter._model_id == "claude-opus-4.8::high::summarized"
    await send(PromptInput(text="omitted-inherits-high"))
    await send(PromptInput(text="null-also-inherits-high", variant=None))
    assert spawns.calls == []

    for effort in ("low", "medium", "high"):
        await send(PromptInput(text=f"explicit-{effort}", variant=effort))
        assert spawns.models[-1] == (f"claude-opus-4.8::{effort}::summarized")

    await send(PromptInput(text="literal-none-disables-display", variant="none"))
    assert spawns.models[-1] == "claude-opus-4.8::none"
    # set_model is never the channel: through the gateway its validation probe
    # fails after being billed.
    assert all(c.set_models == [] for c in spawns.clients)


@pytest.mark.parametrize(
    ("model_id", "spawned_on"),
    [
        pytest.param("claude-opus-4.8::high", "claude-opus-4.8::high::summarized", id="an-effort"),
        pytest.param("claude-opus-4.8::none", "claude-opus-4.8::none", id="thinking-off"),
        pytest.param("claude-opus-4.8", "claude-opus-4.8", id="no-effort"),
    ],
)
async def test_a_sessions_first_turn_runs_on_the_client_it_was_spawned_with(
    tmp_path: Path, model_id: str, spawned_on: str
) -> None:
    """The first client is spawned on the string the first turn asks for, so
    that turn writes to it: a second spawn there doubled every chat's start
    and dropped the client the session had just opened."""
    adapter = _adapter(tmp_path, model={"provider_id": "alkera-anthropic", "model_id": model_id})
    spawns = _respawning(adapter)
    first = adapter._state.client

    await adapter.send_prompt(PromptInput(text="first"))

    assert adapter._build_options(resume=False).model == spawned_on
    assert spawns.calls == []
    assert adapter._state.client is first and not first.disconnected  # type: ignore[union-attr]


async def test_a_model_change_respawns_on_the_new_model_and_retires_the_old_client(
    tmp_path: Path,
) -> None:
    adapter = _adapter(
        tmp_path, model={"provider_id": "alkera-anthropic", "model_id": "claude-opus-4.8"}
    )
    spawns = _respawning(adapter)
    first = adapter._state.client

    await adapter.send_prompt(
        PromptInput(
            text="switch",
            model={"provider_id": "alkera-anthropic", "model_id": "claude-sonnet-4.6"},
            variant="low",
        )
    )

    assert spawns.models == ["claude-sonnet-4.6::low::summarized"]
    assert adapter._state.client is spawns.clients[0]
    assert first.disconnected  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("resumed_chat", "turns_before", "resumes"),
    [
        pytest.param(False, 0, False, id="a-new-chat-before-its-first-turn-opens-afresh"),
        pytest.param(False, 1, True, id="a-new-chat-after-a-turn-resumes-it"),
        pytest.param(True, 0, True, id="a-resumed-chat-resumes"),
    ],
)
async def test_a_respawn_resumes_the_conversation_only_when_there_is_one(
    tmp_path: Path, resumed_chat: bool, turns_before: int, resumes: bool
) -> None:
    """A conversation the CLI never wrote cannot be resumed (it answers "no
    conversation found"); one that exists must be, or the switch would drop
    the chat's history."""
    native = {"agent_session_id": "11111111-1111-4111-8111-111111111111"} if resumed_chat else {}
    adapter = _adapter(
        tmp_path,
        harness_native=native,
        model={"provider_id": "alkera-anthropic", "model_id": "claude-opus-4.8::high"},
    )
    spawns = _respawning(adapter)
    for n in range(turns_before):
        await adapter.send_prompt(PromptInput(text=f"turn {n}"))
        adapter._translator_ctx.open_queries.clear()
    await adapter.send_prompt(PromptInput(text="move", variant="low"))

    assert spawns.calls[-1] == ("claude-opus-4.8::low::summarized", resumes)


async def test_a_spawn_that_fails_is_the_turns_error_and_moves_nothing(tmp_path: Path) -> None:
    adapter = _adapter(
        tmp_path, model={"provider_id": "alkera-anthropic", "model_id": "claude-opus-4.8"}
    )
    spawns = _respawning(adapter, fail=RuntimeError("no such model"))
    first = adapter._state.client
    sub = adapter._bus.subscribe()

    with pytest.raises(HarnessModelError, match=r"claude-opus-4\.8::low::summarized"):
        await adapter.send_prompt(PromptInput(text="go", variant="low", turn_id="T1"))

    status = await asyncio.wait_for(anext(sub), timeout=1.0)  # type: ignore[arg-type]
    assert isinstance(status, SessionStatusChanged)
    assert (status.status, status.turn_id) == ("error", "T1")
    assert adapter._state.client is first and not first.disconnected  # type: ignore[union-attr]
    assert adapter._model_id == "claude-opus-4.8"
    assert spawns.calls == [("claude-opus-4.8::low::summarized", False)]


async def test_a_model_off_the_anthropic_wire_is_refused(tmp_path: Path) -> None:
    adapter = _adapter(
        tmp_path, model={"provider_id": "alkera-anthropic", "model_id": "claude-opus-4.8"}
    )
    spawns = _respawning(adapter)

    with pytest.raises(HarnessModelError, match=r"alkera-openai/gpt-5\.5"):
        await adapter.send_prompt(
            PromptInput(text="go", model={"provider_id": "alkera-openai", "model_id": "gpt-5.5"})
        )
    assert spawns.calls == []


async def test_a_turn_over_a_running_one_keeps_the_model_and_says_so(tmp_path: Path) -> None:
    """Swapping the client would cut the turn still running; the new turn
    stays on the model in use, and its stamp names that model, not the one
    it asked for."""
    adapter = _adapter(
        tmp_path, model={"provider_id": "alkera-anthropic", "model_id": "claude-opus-4.8::high"}
    )
    spawns = _respawning(adapter)
    sub = adapter._bus.subscribe()
    await adapter.send_prompt(PromptInput(text="long running"))
    assert adapter._translator_ctx.open_queries, "the first turn is still open"
    spawned = len(spawns.calls)

    await adapter.send_prompt(
        PromptInput(
            text="over it",
            model={"provider_id": "alkera-anthropic", "model_id": "claude-sonnet-4.6"},
        )
    )

    assert len(spawns.calls) == spawned
    stamps = []
    while True:
        try:
            ev = await asyncio.wait_for(anext(sub), timeout=0.2)  # type: ignore[arg-type]
        except TimeoutError:
            break
        if isinstance(ev, TurnStarted):
            stamps.append(ev.model)
    assert stamps[-1] == {
        "provider_id": "alkera-anthropic",
        "model_id": "claude-opus-4.8",
        "effort": "high",
    }


# ---------------------------------------------------------------------------
# Detach the spawned `claude` from the controlling terminal (Ctrl+C = interrupt
# the TURN, not kill the child) — mirrors opencode's start_new_session=True.
# ---------------------------------------------------------------------------


class _FakeAnyio:
    """Stand-in for the SDK transport's `anyio` module, recording open_process."""

    sentinel = "real-anyio"  # a non-open_process attr to prove delegation

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    async def open_process(self, *args: object, **kwargs: object) -> str:
        self.calls.append((args, kwargs))
        return "PROC"


async def test_detach_injects_start_new_session_and_delegates(
    monkeypatch: object,
) -> None:
    """The proxy injects start_new_session=True into the SDK's spawn (so the
    child gets its own session/process group) and delegates every other attr."""
    from claude_agent_sdk._internal.transport import subprocess_cli

    fake = _FakeAnyio()
    monkeypatch.setattr(subprocess_cli, "anyio", fake)  # type: ignore[attr-defined]

    _detach_claude_from_controlling_terminal()
    proxy = subprocess_cli.anyio
    assert proxy is not fake  # rebound to the proxy

    result = await proxy.open_process(["claude"], stdin=-1, env={"X": "1"})
    assert result == "PROC"
    (args, kwargs) = fake.calls[-1]
    assert args == (["claude"],)
    # The whole point — the child is detached from the console's Ctrl-C, via
    # the platform's mechanism: setsid on POSIX, a new process group on Windows.
    if sys.platform == "win32":
        assert kwargs["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert kwargs["start_new_session"] is True
    assert kwargs["env"] == {"X": "1"}  # caller args preserved
    assert proxy.sentinel == "real-anyio"  # non-open_process attrs delegate


async def test_detach_is_idempotent(monkeypatch: object) -> None:
    from claude_agent_sdk._internal.transport import subprocess_cli

    monkeypatch.setattr(subprocess_cli, "anyio", _FakeAnyio())  # type: ignore[attr-defined]
    _detach_claude_from_controlling_terminal()
    once = subprocess_cli.anyio
    _detach_claude_from_controlling_terminal()  # second call must not re-wrap
    assert subprocess_cli.anyio is once


async def test_detach_does_not_override_explicit_start_new_session(
    monkeypatch: object,
) -> None:
    """setdefault semantics: an explicit value (should one ever be passed) wins."""
    from claude_agent_sdk._internal.transport import subprocess_cli

    fake = _FakeAnyio()
    monkeypatch.setattr(subprocess_cli, "anyio", fake)  # type: ignore[attr-defined]
    _detach_claude_from_controlling_terminal()
    await subprocess_cli.anyio.open_process(["claude"], start_new_session=False)
    assert fake.calls[-1][1]["start_new_session"] is False


class _StrictAnyio:
    """Fake anyio whose open_process mirrors the REAL anyio signature: keyword-
    only params, NO **kwargs, and crucially NO `preexec_fn`. Regression net for
    the Linux e2e failure where the proxy injected spawn_kwargs() wholesale and
    anyio rejected `preexec_fn` (the PR_SET_PDEATHSIG hook) with a TypeError."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def open_process(
        self,
        command,
        *,
        stdin=None,
        stdout=None,
        stderr=None,
        cwd=None,
        env=None,
        start_new_session=False,
    ) -> str:
        self.calls.append({"command": command, "start_new_session": start_new_session})
        return "PROC"


async def test_detach_filters_kwargs_the_real_open_process_rejects(
    monkeypatch,
) -> None:
    """On Linux spawn_kwargs() includes `preexec_fn`; the proxy must drop any
    kwarg the SDK transport's open_process signature doesn't accept instead of
    crashing the claude spawn with a TypeError."""
    import sys as _sys

    from claude_agent_sdk._internal.transport import subprocess_cli

    monkeypatch.setattr(_sys, "platform", "linux")  # force preexec_fn into spawn_kwargs
    fake = _StrictAnyio()
    monkeypatch.setattr(subprocess_cli, "anyio", fake)  # type: ignore[attr-defined]
    _detach_claude_from_controlling_terminal()

    result = await subprocess_cli.anyio.open_process(["claude"])
    assert result == "PROC"  # no TypeError — unsupported kwargs were filtered
    assert fake.calls[-1]["start_new_session"] is True  # the supported one still lands


def test_build_env_is_a_pure_overlay_that_never_touches_user_env_roots(tmp_path: Path) -> None:
    """The Claude Agent SDK merges `options.env` OVER the inherited process
    environment (override-only, no deletes), so the agent's Bash commands see
    the user's true environment plus our overlay — exactly Claude Code's
    normal shape. The overlay therefore must NEVER name a user env root
    (HOME / XDG_* / PATH / cache dirs): redirecting one here would silently
    sandbox every bash child (the opencode uv-cache bug, on this harness)."""
    adapter = _adapter(tmp_path)
    env = adapter._build_env()

    forbidden = {"HOME", "PATH", "SHELL", "TMPDIR"}
    assert not (set(env) & forbidden), set(env) & forbidden
    for key in env:
        assert not key.startswith(("XDG_", "UV_", "GIT_", "npm_")), key
        # Every overlay var is explicitly ours / Claude's — a new var outside
        # these families needs a deliberate decision, not an accident.
        assert key.startswith(("CLAUDE_", "ANTHROPIC_", "DISABLE_", "MCP_", "MAX_", "ALKERA_")), key
