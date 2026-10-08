"""End-to-end tests for the Claude Agent harness against a REAL ``claude`` binary.

Two tiers:

`@pytest.mark.claude_e2e` — DETERMINISTIC, no network/keys. A real ``claude``
subprocess (driven by the SDK) is pointed at the scripted ``MockAnthropicServer``.
Claude Code refuses to run a turn's *content* without verifying model access
against a real key, so these tests validate the **adapter IR-pipeline contract**
against the real binary (lifecycle → connect → pump → translate → clean close +
per-chat state isolation + deterministic resume) rather than the mock's reply
text. That's exactly what the adapter is responsible for; the translator's content
mapping is covered exhaustively by the unit tests + the live tier below.

`@pytest.mark.live_provider` — hits the REAL Anthropic API with ``ANTHROPIC_API_KEY``
(from ``.env.local`` / env); validates real streamed content + the real
tool→permission flow. Skipped without a key.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from _helpers.claude_runner import claude_e2e_adapter
from _mocks.mock_anthropic_server import text_events
from alkera_cli.harness.adapter import PromptInput, SessionConfig
from alkera_cli.harness.adapters.claude_agent import ClaudeAgentAdapter
from alkera_cli.harness.claude_binary import resolve_claude_binary
from alkera_cli.harness.event_bus import EventBus
from alkera_core.schemas.chat import (
    Event,
    MessageCompleted,
    MessageCreated,
    PartCreated,
    PartStarted,
    PermissionRequest,
    SessionStatusChanged,
    TextPart,
    TurnFinished,
)

_SID = "11111111-1111-4111-8111-111111111111"


async def _next_within(sub: object, timeout_s: float) -> Event | None:
    try:
        async with asyncio.timeout(timeout_s):
            return await anext(sub)  # type: ignore[arg-type]
    except (TimeoutError, StopAsyncIteration):
        return None


async def _drain_turn(sub: object, collected: list[Event], *, budget: float = 60.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    seen_running = False
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        ev = await _next_within(sub, remaining)
        if ev is None:
            return
        collected.append(ev)
        if isinstance(ev, TurnFinished):
            return
        if isinstance(ev, SessionStatusChanged):
            if ev.status == "running":
                seen_running = True
            elif ev.status in ("idle", "error", "aborted") and seen_running:
                return


def _text(collected: list[Event]) -> str:
    return "".join(
        e.part.text
        for e in collected
        if isinstance(e, PartCreated) and isinstance(e.part, TextPart)
    )


def _assert_pipeline_contract(collected: list[Event]) -> None:
    """The invariants ANY real claude turn must drive through the adapter."""
    assert any(isinstance(e, MessageCreated) and e.role == "assistant" for e in collected), (
        "no assistant MessageCreated"
    )
    assert any(isinstance(e, PartStarted) and e.part_type == "text" for e in collected), (
        "no text PartStarted"
    )
    finalized = [
        e for e in collected if isinstance(e, PartCreated) and isinstance(e.part, TextPart)
    ]
    assert finalized, "no finalized TextPart"
    assert any(p.part.text for p in finalized), "finalized text parts all empty"
    assert any(isinstance(e, MessageCompleted) for e in collected), "no MessageCompleted"
    assert any(isinstance(e, TurnFinished) for e in collected) or any(
        isinstance(e, SessionStatusChanged) and e.status == "idle" for e in collected
    ), "turn did not close (no TurnFinished / idle)"
    # Every PartStarted(text) must be matched by a PartCreated (streaming closure).
    started = {e.part_id for e in collected if isinstance(e, PartStarted)}
    created = {e.part.part_id for e in collected if isinstance(e, PartCreated)}
    assert started <= created, f"orphan open parts: {started - created}"


# ---------------------------------------------------------------------------
# Deterministic mock e2e (adapter contract vs the real binary)
# ---------------------------------------------------------------------------


@pytest.mark.claude_e2e
@pytest.mark.asyncio
async def test_pipeline_contract_against_real_binary(tmp_path: Path) -> None:
    async with claude_e2e_adapter(
        tmp_path, mock_script={"*": text_events("Hello from the mock")}
    ) as (adapter, _server):
        sub = adapter.subscribe()
        await adapter.send_prompt(PromptInput(text="say hi"))
        collected: list[Event] = []
        await _drain_turn(sub, collected)
    _assert_pipeline_contract(collected)


@pytest.mark.claude_e2e
@pytest.mark.asyncio
async def test_transcript_isolated_in_runtime_dir(tmp_path: Path) -> None:
    chat_dir = tmp_path / "chat"
    async with claude_e2e_adapter(
        tmp_path, mock_script={"*": text_events("hi")}, chat_dir=chat_dir
    ) as (adapter, _server):
        sub = adapter.subscribe()
        await adapter.send_prompt(PromptInput(text="hello"))
        await _drain_turn(sub, [])
        # let the CLI flush its transcript
        for _ in range(150):
            if list((chat_dir / ".runtime").glob("projects/**/*.jsonl")):
                break
            await asyncio.sleep(0.02)

    runtime = chat_dir / ".runtime"
    assert runtime.is_dir(), "CLAUDE_CONFIG_DIR should isolate state under .runtime"
    assert list(runtime.glob("projects/**/*.jsonl")), f"no transcript under {runtime}"


@pytest.mark.claude_e2e
@pytest.mark.asyncio
async def test_resume_reattaches_to_pinned_session(tmp_path: Path) -> None:
    chat_dir = tmp_path / "chat"
    async with claude_e2e_adapter(
        tmp_path, mock_script={"*": text_events("first")}, chat_dir=chat_dir
    ) as (adapter_a, _s1):
        assert adapter_a._resume is False
        sub = adapter_a.subscribe()
        await adapter_a.send_prompt(PromptInput(text="first"))
        await _drain_turn(sub, [])
        pinned = adapter_a.native_state()["agent_session_id"]

    # Resume the SAME pinned session id + chat dir — must start cleanly and run.
    async with claude_e2e_adapter(
        tmp_path,
        mock_script={"*": text_events("second")},
        chat_dir=chat_dir,
        harness_native={"agent_session_id": pinned},
    ) as (adapter_b, _s2):
        assert adapter_b._resume is True
        assert adapter_b.session_id == pinned
        sub_b = adapter_b.subscribe()
        await adapter_b.send_prompt(PromptInput(text="second"))
        collected: list[Event] = []
        await _drain_turn(sub_b, collected)
    _assert_pipeline_contract(collected)


# ---------------------------------------------------------------------------
# Live provider (real Anthropic) — real content + real tool/permission flow
# ---------------------------------------------------------------------------


def _load_anthropic_key() -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    here = Path(__file__).resolve()
    for parent in here.parents:
        env_local = parent / ".env.local"
        if env_local.is_file():
            for line in env_local.read_text().splitlines():
                s = line.strip()
                if s.startswith("ANTHROPIC_API_KEY="):
                    return s.split("=", 1)[1].strip().strip('"').strip("'")
            break
    return None


def _live_adapter(tmp_path: Path, **extra_env: str) -> ClaudeAgentAdapter:
    key = _load_anthropic_key()
    if not key:
        pytest.skip("ANTHROPIC_API_KEY not set (and not in .env.local)")
    chat_dir = tmp_path / "chat"
    chat_dir.mkdir(parents=True, exist_ok=True)
    env = {
        # No ANTHROPIC_BASE_URL → real api.anthropic.com (real catalog + access).
        # Opus 4.5 — the supported model class (Opus + newer). Claude Code sends
        # its own reasoning controls (output_config.effort + thinking:{adaptive})
        # for every turn; Opus-class models accept them, the smaller Haiku tier
        # 400s — and Haiku isn't a model we route the claude harness to. Pin EVERY
        # selector (incl. the background/small-fast) to Opus so no turn falls back
        # to a tier that would reject those controls.
        "ANTHROPIC_API_KEY": key,
        "ANTHROPIC_MODEL": "claude-opus-4-5",
        "ANTHROPIC_SMALL_FAST_MODEL": "claude-opus-4-5",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-5",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-opus-4-5",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-opus-4-5",
        **extra_env,
    }
    config = SessionConfig(
        session_id=_SID,
        project_dir=tmp_path,
        chat_dir=chat_dir,
        harness_native={"claude_env": env},
    )
    return ClaudeAgentAdapter(config, binary=resolve_claude_binary(), event_bus=EventBus())


@pytest.mark.live_provider
@pytest.mark.asyncio
async def test_live_anthropic_text_turn(tmp_path: Path) -> None:
    adapter = _live_adapter(tmp_path)
    await adapter.start()
    try:
        sub = adapter.subscribe()
        await adapter.send_prompt(PromptInput(text="Reply with exactly the single word: PONG"))
        collected: list[Event] = []
        await _drain_turn(sub, collected, budget=90.0)
    finally:
        await adapter.stop()
    _assert_pipeline_contract(collected)
    assert "PONG" in _text(collected).upper()


@pytest.mark.live_provider
@pytest.mark.asyncio
async def test_live_tool_routes_through_permission(tmp_path: Path) -> None:
    """A real turn that runs a permissioned NATIVE tool must route through OUR
    can_use_tool — surfacing a PermissionRequest — proving the bridge against the
    real binary. We use the native ``Write`` tool (``canonical_kind="edit"``): the
    native ``Bash`` is disabled in favor of our parent-hosted shell tool, whose
    permission is gated INSIDE the tool (covered by the opencode_e2e auto tests +
    test_bash_gate), not by the claude binary's can_use_tool. We reject + cancel."""
    adapter = _live_adapter(tmp_path)
    await adapter.start()
    try:
        sub = adapter.subscribe()
        await adapter.send_prompt(
            PromptInput(
                text="Use the Write tool to create a file named alkera-e2e.txt "
                "containing exactly: hello. Do not explain — just create it."
            )
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 90.0
        req: PermissionRequest | None = None
        while loop.time() < deadline:
            ev = await _next_within(sub, deadline - loop.time())
            if ev is None:
                break
            if isinstance(ev, PermissionRequest):
                req = ev
                break
        assert req is not None, "Write tool did not route through can_use_tool"
        assert req.permission_kind == "Write"
        assert req.canonical_kind == "edit"
        await adapter.resolve_permission(req.request_id, "reject_once")
        await adapter.cancel()
    finally:
        await adapter.stop()
