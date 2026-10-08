"""End-to-end opencode tests with a real bun-driven subprocess.

E3 of the harness stabilization sweep. Marked with the ``opencode_e2e``
pytest marker so the default ``pytest`` invocation skips this whole
file (it spawns subprocesses + needs bun on PATH); CI's required
``e2e`` job and the ``make e2e`` target opt in.

Coverage spans the spawn/connect/prompt-routing smoke path AND full model
round-trips: the mock provider is wired as `@ai-sdk/openai-compatible`
(chat-completions), which opencode actually calls — the default `openai`
provider uses the OpenAI *Responses* API (`POST /v1/responses`) that the
mock doesn't speak, so `opencode_e2e_adapter` forces the compatible SDK.
That unlocks the deterministic compaction tests below (real opencode
summarize + context fold, with the mock faking the summary). For the
IR-level translation coverage, see ``test_harness_opencode_translate.py``.

The smoke tests still catch regressions in spawn, listen-URL detection,
/session creation, prompt_async POST, SSE subscription, and the reaper /
synthesize-close paths.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest
from _helpers.opencode_runner import _bun_binary, opencode_e2e_adapter, opencode_e2e_runtime
from _mocks.mock_openai_server import (
    COMPACTION_MARKER,
    MockOpenAIServer,
    MockScript,
    text_chunks,
)
from alkera_cli.harness.adapter import PromptInput
from alkera_core.schemas.chat import (
    CompactionApplied,
    ConversationCleared,
    Event,
    SessionStatusChanged,
    SessionUpdated,
)

pytestmark = pytest.mark.opencode_e2e


def _running_alkera_threads() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name.startswith("alkera-")]


async def _next_event_within(sub, deadline_seconds: float) -> Event | None:
    """Pull the next event from the subscription or ``None`` on
    timeout. Doesn't raise — the caller decides whether the absence
    is a failure."""
    try:
        async with asyncio.timeout(deadline_seconds):
            return await anext(sub)
    except (TimeoutError, StopAsyncIteration):
        return None


@pytest.mark.asyncio
async def test_adapter_starts_and_attaches_to_opencode_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The adapter spawns opencode, the subprocess listens, /session
    creation succeeds (or attaches), and an opencode session id is
    pinned. Catches regressions in start(), the listen-URL parsing,
    and _ensure_opencode_session."""
    async with opencode_e2e_adapter(
        tmp_path,
        monkeypatch,
        mock_script={"*": text_chunks("ok")},
    ) as (adapter, _server):
        # native_state() must surface the pinned opencode session id —
        # what C2 relies on for deterministic resume.
        native = adapter.native_state()
        assert "agent_session_id" in native
        assert isinstance(native["agent_session_id"], str)
        assert native["agent_session_id"].startswith("ses_")


@pytest.mark.asyncio
async def test_no_phone_home_and_stable_on_disk_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end no-phone-home and on-disk layout guarantees against a REAL harness:

    1. The adapter reaches "listening" at all, through the listen file or
       opencode's banner (`opencode server listening on …`). Guards the
       vendor↔adapter readiness coupling.
    2. No `models.json` is written — proof that ALKERA_DISABLE_MODELS_FETCH
       stopped the boot-time models.dev fetch (the catalog comes from the
       snapshot compiled into the binary).
    3. The session database lives at `.runtime/agent/agent.db`, where every
       existing chat keeps its state, so a resumed chat finds its session.

    (2) and (3) are checked against BOTH roots the spawn writes to: the per-chat
    `.runtime` (XDG_DATA_HOME) and the agent config root (XDG_CONFIG/STATE/CACHE,
    which is where `models.json` would land — `Global.Path.cache`). Checking only
    `.runtime` made both assertions vacuously true once the config roots moved
    out of the chat dir.
    """
    async with opencode_e2e_adapter(
        tmp_path,
        monkeypatch,
        mock_script={"*": text_chunks("ok")},
    ) as (adapter, _server):
        sandbox = tmp_path / "chat" / ".runtime"
        config_root = adapter._agent_config_root
        # (3) The DB is agent.db under .runtime/agent/.
        assert (sandbox / "agent" / "agent.db").is_file()
        # The old .harness/oc path must NOT exist.
        assert not (tmp_path / "chat" / ".harness").exists()
        # The config/cache/state roots are OUTSIDE the project the agent can write.
        assert str(tmp_path / "chat") not in str(config_root)
        assert config_root.is_dir()
        for root in (sandbox, config_root):
            # (2) The models.dev fetch is disabled → no catalog file on disk.
            assert not list(root.rglob("models.json")), (
                f"models.json was written under {root} — models.dev fetch was NOT disabled"
            )


@pytest.mark.asyncio
async def test_send_prompt_does_not_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The async prompt_async POST returns 204 and the adapter
    accepts the call. We don't assert on model output here (see
    module docstring) — only that the wire path is healthy."""
    async with opencode_e2e_adapter(
        tmp_path,
        monkeypatch,
        mock_script={"*": text_chunks("ok")},
    ) as (adapter, _server):
        await adapter.send_prompt(
            PromptInput(
                text="say hi",
                model={"provider_id": "mock", "model_id": "mock-model"},
            )
        )


def _header_of(request: dict[str, Any], name: str) -> str | None:
    return request.get("__headers__", {}).get(name)


@pytest.mark.asyncio
async def test_explicit_reasoning_efforts_enable_display_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch routing that preserves the effort suffix but forgets to derive
    thinking display from each non-None reasoning effort."""
    async with opencode_e2e_adapter(
        tmp_path,
        monkeypatch,
        mock_script={"*": text_chunks("ok")},
    ) as (adapter, server):
        for effort in ("low", "medium", "high"):
            marker = f"reason-with-{effort}"
            await adapter.send_prompt(
                PromptInput(
                    text=marker,
                    model={"provider_id": "mock", "model_id": "mock-model"},
                    variant=effort,
                )
            )
            main = await _wait_for_request(server, marker)
            assert _header_of(main, "x-alkera-thinking-display") == "summarized"


@pytest.mark.asyncio
async def test_opencode_null_inherits_effort_but_none_variant_disables_display(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch an implementation that maps Python ``None`` to disabled thinking:
    omitted/null inherit the effective medium effort, while literal ``"none"``
    is the only variant that removes the display header."""
    async with opencode_e2e_adapter(
        tmp_path,
        monkeypatch,
        mock_script={"*": text_chunks("ok")},
    ) as (adapter, server):
        await adapter.send_prompt(
            PromptInput(
                text="seed-medium-effort",
                model={"provider_id": "mock", "model_id": "mock-model"},
                variant="medium",
            )
        )
        await _wait_for_request(server, "seed-medium-effort")

        await adapter.send_prompt(PromptInput(text="omitted-inherits-medium"))
        omitted = await _wait_for_request(server, "omitted-inherits-medium")
        assert _header_of(omitted, "x-alkera-thinking-display") == "summarized"

        await adapter.send_prompt(PromptInput(text="null-also-inherits-medium", variant=None))
        null_variant = await _wait_for_request(server, "null-also-inherits-medium")
        assert _header_of(null_variant, "x-alkera-thinking-display") == "summarized"

        await adapter.send_prompt(PromptInput(text="literal-none-disables-display", variant="none"))
        disabled = await _wait_for_request(server, "literal-none-disables-display")
        assert _header_of(disabled, "x-alkera-thinking-display") is None


@pytest.mark.asyncio
async def test_no_leaked_alkera_threads_after_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the adapter stops cleanly, no ``alkera-*`` background
    thread should remain — a regression here means we're leaking
    SSE pumps or stdout drainers."""
    before = set(_running_alkera_threads())
    async with opencode_e2e_adapter(
        tmp_path,
        monkeypatch,
        mock_script={"*": text_chunks("ok")},
    ) as (adapter, _server):
        await adapter.send_prompt(
            PromptInput(
                text="hello",
                model={"provider_id": "mock", "model_id": "mock-model"},
            )
        )
        # Let the SSE pump tick once.
        await asyncio.sleep(0.5)

    # Context exit ran adapter.stop(). Give threads a brief tail to settle.
    for _ in range(150):
        if set(_running_alkera_threads()) - before == set():
            break
        await asyncio.sleep(0.02)
    after = set(_running_alkera_threads())
    leaked = after - before
    assert leaked == set(), f"leaked alkera-* threads: {leaked}"


@pytest.mark.asyncio
async def test_cancel_is_safe_when_no_turn_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``adapter.cancel()`` without an active turn is a no-op — must
    not raise, must not leave state inconsistent. Verifies the
    synthesize-close path's idempotency in the no-state case."""
    async with opencode_e2e_adapter(
        tmp_path,
        monkeypatch,
        mock_script={"*": text_chunks("ok")},
    ) as (adapter, _server):
        await adapter.cancel()
        # And a second cancel still no-ops.
        await adapter.cancel()


# ---------------------------------------------------------------------------
# Multi-turn + restart scenarios (wire-path only — no LLM round-trip)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_send_prompts_all_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three back-to-back prompts on the same chat: each prompt_async
    POST must succeed (204). Catches regressions where a prior turn's
    state leaks and breaks subsequent prompt_async calls."""
    async with opencode_e2e_adapter(
        tmp_path,
        monkeypatch,
        mock_script={"*": text_chunks("ok")},
    ) as (adapter, _server):
        for i in range(3):
            await adapter.send_prompt(
                PromptInput(
                    text=f"turn {i}",
                    model={"provider_id": "mock", "model_id": "mock-model"},
                )
            )


@pytest.mark.asyncio
async def test_markdown_reply_finalizes_as_a_text_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A multi-line markdown assistant reply must FINALIZE as a `PartCreated`
    `TextPart` carrying the full markdown — the event the TUI swaps to themed
    markdown on. The 'I see raw `##` headings' symptom is this part never
    arriving: the stream block would then stay on its plain streamed text. Drives
    a REAL opencode against the mock so the actual event ordering is exercised,
    not the fake adapter's."""
    from alkera_core.schemas.chat import AgentMessageChunk, PartCreated, TextPart

    reply = "## Tool Capabilities\n\nHere is what I did:\n\n- read a file\n- ran a query"
    async with opencode_e2e_adapter(
        tmp_path,
        monkeypatch,
        mock_script={"*": text_chunks(reply)},
    ) as (adapter, _server):
        sub = adapter.subscribe()
        collected: list[Event] = []
        await adapter.send_prompt(
            PromptInput(text="demo", model={"provider_id": "mock", "model_id": "mock-model"})
        )
        await _drain_turn(sub, collected)

        # The reply must have STREAMED (deltas) AND finalized (one PartCreated
        # TextPart with the whole markdown). A finalize MUST carry the heading —
        # that's the swap-to-markdown trigger the UI needs.
        chunks = [e for e in collected if isinstance(e, AgentMessageChunk)]
        finals = [
            e for e in collected if isinstance(e, PartCreated) and isinstance(e.part, TextPart)
        ]
        assert chunks, "no streamed deltas — the reply never reached the UI"
        assert finals, "the markdown reply NEVER finalized as a PartCreated TextPart"
        final_text = finals[-1].part.text
        assert "## Tool Capabilities" in final_text, (
            f"finalized text lost the heading: {final_text!r}"
        )
        # The finalize's part_id MUST match the streamed deltas' — else the UI
        # finalizes a DIFFERENT block and the streamed (raw) one stays raw.
        assert finals[-1].part.part_id == chunks[-1].part_id, (
            "finalize part_id != streamed part_id — the raw stream block is orphaned"
        )


@pytest.mark.asyncio
async def test_reaper_kills_stale_pid_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C3: a stale PID file in .harness/oc/pid (from a prior crashed
    alkera) is reaped before the new opencode subprocess spawns. We
    can't simulate the exact production case without a real prior
    spawn, but we can write a dead-PID breadcrumb beforehand and
    verify start() still succeeds (i.e. the reaper handled the stale
    breadcrumb gracefully)."""
    import subprocess
    import sys

    chat_dir = tmp_path / "chat"
    chat_dir.mkdir()
    harness_dir = chat_dir / ".runtime"
    harness_dir.mkdir(parents=True)

    # Spawn + reap a real subprocess so we have a known-dead PID written to the
    # breadcrumb. proc.wait() reaps the zombie itself. `sys.executable -c ""`
    # is the cross-platform stand-in for POSIX `true` (instant clean exit).
    proc = subprocess.Popen([sys.executable, "-c", ""])  # noqa: ASYNC220
    proc.wait()
    (harness_dir / "pid").write_text(str(proc.pid))

    async with opencode_e2e_adapter(
        tmp_path,
        monkeypatch,
        mock_script={"*": text_chunks("ok")},
    ) as (adapter, _server):
        # If the reaper didn't handle the stale breadcrumb, start()
        # would have raised. Getting here means we're healthy.
        assert adapter.native_state().get("agent_session_id", "").startswith("ses_")


@pytest.mark.asyncio
async def test_restart_adapter_reuses_pinned_opencode_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2 with a REAL adapter. After a clean stop, a fresh adapter
    pointed at the same chat dir + given the pinned opencode session
    id MUST attach to that exact session (no silent pick-latest)."""
    from alkera_cli.harness.adapter import SessionConfig
    from alkera_cli.harness.adapters.opencode_http import OpencodeHttpAdapter
    from alkera_cli.harness.event_bus import EventBus

    # First run.
    async with opencode_e2e_adapter(
        tmp_path,
        monkeypatch,
        mock_script={"*": text_chunks("ok")},
    ) as (adapter1, _server1):
        pinned = adapter1.native_state()["agent_session_id"]

    # Second run — same tmp_path, so the same chat dir + the same opencode
    # SQLite. Resolve the binary the SAME way the fixtures do (via _bun_binary)
    # so this honors ALKERA_OPENCODE_BIN (compiled-binary mode) instead of
    # forcing bun-dev — otherwise this lone test would demand
    # vendor/opencode/node_modules even when the rest of the suite runs the
    # prebuilt binary. We don't re-launch the mock server; opencode won't make
    # LLM calls on attach, so attach succeeds without it.
    binary = _bun_binary()
    config = SessionConfig(
        session_id="e2e-sid",
        project_dir=tmp_path,
        chat_dir=tmp_path / "chat",
        harness_native={"agent_session_id": pinned},
    )
    adapter2 = OpencodeHttpAdapter(config, binary=binary, event_bus=EventBus())
    try:
        await adapter2.start()
        assert adapter2.native_state()["agent_session_id"] == pinned
    finally:
        await adapter2.stop()


# ---------------------------------------------------------------------------
# Compaction (real opencode summarize + fold, mock-faked summary)
# ---------------------------------------------------------------------------

_MODEL = {"provider_id": "mock", "model_id": "mock-model"}
# Force the fold to actually elide: keep only the single most-recent turn as
# the tail, with a budget big enough to hold it. (compaction.ts:select)
_COMPACTION_CFG = {"compaction": {"tail_turns": 1, "preserve_recent_tokens": 100_000}}
_SUMMARY = "## Goal\n- unique-summary-marker\n## Next Steps\n- (none)"


async def _drain_turn(sub: Any, collected: list[Event], *, budget_seconds: float = 45.0) -> None:
    """Drain exactly one full turn: wait for a `running` status, THEN
    return on the next idle/error. Waiting for `running` first skips any
    idle left buffered from a prior turn (otherwise the next turn's drain
    could return before that turn ever reaches the model). Each event is
    appended to `collected`."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds
    seen_running = False
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        ev = await _next_event_within(sub, remaining)
        if ev is None:
            return
        collected.append(ev)
        if isinstance(ev, SessionStatusChanged):
            if ev.status == "running":
                seen_running = True
            elif ev.status in ("idle", "error") and seen_running:
                return


def _request_texts(request: dict[str, Any]) -> str:
    """Concatenate every message's text in a recorded ChatCompletion
    request (any role) — what the model actually saw that turn."""
    out: list[str] = []
    for m in request.get("messages", []):
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            out.extend(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return "\n".join(out)


def _system_text(request: dict[str, Any]) -> str:
    """Concatenate only the SYSTEM/developer message content of a recorded
    ChatCompletion request — i.e. the system prompt the model actually received
    that turn (not the user/assistant turns)."""
    out: list[str] = []
    for m in request.get("messages", []):
        if not isinstance(m, dict) or m.get("role") not in ("system", "developer"):
            continue
        content = m.get("content")
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            out.extend(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return "\n".join(out)


async def _wait_for_request(
    server: Any, marker: str, *, budget_seconds: float = 60.0
) -> dict[str, Any]:
    """Wait until a chat-completion request whose text contains `marker`
    reaches the mock, then return the newest such request.

    This ties a "the turn ran with this context" assertion to the OBSERVABLE
    side effect — the request body landed in the mock's log — instead of to
    event ordering. opencode buffers status events across a compaction/clear
    boundary, so a turn's `running`→`idle` can be *observed* a beat before that
    turn's model request actually lands; gating on the next `idle` then
    snapshotting `server.requests` races that gap (the request shows up ~0.3s
    later). Polling the request log removes the race entirely while still
    failing loudly — never flakily — if the request genuinely never arrives.
    The mock appends a request only after fully parsing its JSON body, so a
    returned request is always complete."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds
    while loop.time() < deadline:
        for request in reversed(server.requests):
            if marker in _request_texts(request):
                return request
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"no model request containing {marker!r} reached the mock in {budget_seconds}s"
    )


async def _wait_for_system_prompt(server: Any, needle: str, *, budget_seconds: float = 60.0) -> str:
    """Poll until SOME recorded request carries `needle` verbatim in its SYSTEM
    prompt, and return that system text. opencode fires auxiliary model calls
    (e.g. title generation) with their OWN, smaller system prompts, so we scan
    ALL requests for the main agent turn instead of guessing which request is it
    (which races: the auxiliary call can be the newest "ping"-bearing request).
    Fails loudly — listing the distinct system-prompt openings actually seen — if
    no request ever carries it (a corrupted/empty prompt decode, or rewired
    prompt selection)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds
    while loop.time() < deadline:
        for request in server.requests:
            system_text = _system_text(request)
            if needle in system_text:
                return system_text
        await asyncio.sleep(0.1)
    seen = sorted({_system_text(r)[:80] for r in server.requests if _system_text(r)})
    raise AssertionError(
        f"no model request carried the expected system prompt verbatim within "
        f"{budget_seconds}s — corrupted/empty prompt decode or rewired selection. "
        "Distinct system-prompt openings seen:\n" + "\n".join(repr(s) for s in seen)
    )


DEFAULT_PROMPT_FILE = (
    Path(__file__).resolve().parents[4]
    / "vendor"
    / "opencode"
    / "packages"
    / "opencode"
    / "src"
    / "session"
    / "prompt"
    / "default.txt"
)


@pytest.mark.asyncio
async def test_default_system_prompt_is_the_real_txt_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real `default.txt` must reach the model as the system prompt, verbatim.

    Every OTHER e2e here passes regardless of prompt CONTENT — the mock
    returns scripted output and never inspects the system prompt — so a corrupted,
    empty, or truncated decode would slip through unnoticed. Not here: we read the
    source-of-truth file from disk and assert its full content appears verbatim in
    the system messages on the wire.

    The mock model id (`mock/mock-model`) matches none of the model-specific
    branches in `session/system.ts:provider()`, so it falls through to
    PROMPT_DEFAULT (default.txt). Run against bun-dev (`make e2e`) this pins
    source↔wire; run against the compiled binary
    (`ALKERA_OPENCODE_BIN=<staged> … pytest`, via the opencode_runner seam) it
    pins that the compiled binary carries the prompt asset unchanged."""
    expected = DEFAULT_PROMPT_FILE.read_text().strip()
    # Guard: we read the intended file, and it's substantial (not an empty decode).
    assert "You are Alkera" in expected and len(expected) > 500

    async with opencode_e2e_adapter(
        tmp_path,
        monkeypatch,
        mock_script={"ping": text_chunks("pong")},
    ) as (adapter, server):
        await adapter.send_prompt(PromptInput(text="ping"))
        # Assert the MAIN agent turn carried default.txt verbatim as its system
        # prompt. opencode also fires a title-generation call with a DIFFERENT,
        # smaller system prompt, so we match on content (not request order, which
        # races): _wait_for_system_prompt scans all requests for the one bearing
        # the agent prompt and raises with detail if none ever does.
        system_text = await _wait_for_system_prompt(server, expected)

    assert expected in system_text  # guaranteed by the waiter; explicit for intent


@pytest.mark.asyncio
async def test_manual_compaction_summarizes_and_folds_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive a real opencode through manual compaction against the mock:

    1. Two turns of history (markers `unique-alpha`, `unique-beta`).
    2. `adapter.compact()` → opencode's /summarize → the mock detects the
       compaction call and returns a summary → we surface a single
       `CompactionApplied` carrying that summary.
    3. A third turn must use ONLY the compacted context: the request the
       model sees contains the SUMMARY + the retained tail (turn 2) but
       NOT the elided head (turn 1).
    """
    async with opencode_e2e_adapter(
        tmp_path,
        monkeypatch,
        mock_script={"*": text_chunks("ok")},
        compaction_summary=_SUMMARY,
        extra_config=_COMPACTION_CFG,
    ) as (adapter, server):
        sub = adapter.subscribe()

        await adapter.send_prompt(PromptInput(text="alpha unique-alpha", model=_MODEL))
        await _drain_turn(sub, [])
        await adapter.send_prompt(PromptInput(text="beta unique-beta", model=_MODEL))
        await _drain_turn(sub, [])

        # Force compaction; collect the events it streams back.
        collected: list[Event] = []
        await adapter.compact()
        await _collect_until(sub, collected, CompactionApplied)

        compactions = [e for e in collected if isinstance(e, CompactionApplied)]
        assert compactions, "expected a CompactionApplied from /summarize"
        assert "unique-summary-marker" in compactions[0].summary_text

        # Third turn — must run against the compacted context.
        await adapter.send_prompt(PromptInput(text="gamma unique-gamma", model=_MODEL))
        turn3 = await _wait_for_request(server, "unique-gamma")
        seen = _request_texts(turn3)
        assert "unique-summary-marker" in seen, "summary not injected post-compaction"
        assert "unique-beta" in seen, "retained tail (turn 2) missing"
        assert "unique-alpha" not in seen, "elided head (turn 1) leaked into context"


@pytest.mark.asyncio
async def test_compaction_persists_across_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After compaction, closing + re-opening the SAME chat (pinned
    opencode session) must preserve the compacted context: a turn on the
    resumed session still sees the summary and NOT the elided head. Proves
    the fold lives in opencode's own session store, which resume re-attaches."""
    chat_dir = tmp_path / "chat"

    async with opencode_e2e_adapter(
        tmp_path,
        monkeypatch,
        mock_script={"*": text_chunks("ok")},
        chat_dir=chat_dir,
        compaction_summary=_SUMMARY,
        extra_config=_COMPACTION_CFG,
    ) as (adapter1, _s1):
        sub1 = adapter1.subscribe()
        await adapter1.send_prompt(PromptInput(text="alpha unique-alpha", model=_MODEL))
        await _drain_turn(sub1, [])
        await adapter1.send_prompt(PromptInput(text="beta unique-beta", model=_MODEL))
        await _drain_turn(sub1, [])
        collected: list[Event] = []
        await adapter1.compact()
        await _collect_until(sub1, collected, CompactionApplied)
        assert any(isinstance(e, CompactionApplied) for e in collected)
        pinned = adapter1.native_state()["agent_session_id"]

    # Resume the SAME chat dir + opencode session in a fresh adapter.
    async with opencode_e2e_adapter(
        tmp_path,
        monkeypatch,
        mock_script={"*": text_chunks("ok")},
        chat_dir=chat_dir,
        compaction_summary=_SUMMARY,
        extra_config=_COMPACTION_CFG,
        harness_native={"agent_session_id": pinned},
    ) as (adapter2, server2):
        assert adapter2.native_state()["agent_session_id"] == pinned
        await adapter2.send_prompt(PromptInput(text="gamma unique-gamma", model=_MODEL))
        turn = await _wait_for_request(server2, "unique-gamma")
        seen = _request_texts(turn)
        assert "unique-summary-marker" in seen, "summary lost across resume"
        assert "unique-alpha" not in seen, "elided head reappeared after resume"


@pytest.mark.asyncio
async def test_compaction_full_runtime_persists_and_survives_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The WHOLE alkera path (HarnessRuntime → ChatSession → persist pump →
    chat.jsonl → manifest pinning), not just the adapter:

    1. Run two turns + compact through the ChatSession.
    2. close_chat → assert chat.jsonl persisted the CompactionApplied (with
       summary) — the alkera save format records the compaction.
    3. Re-open via the runtime (resume) → a new turn still uses ONLY the
       compacted context (summary present, elided head absent).
    """
    async with opencode_e2e_runtime(
        tmp_path,
        mock_script={"*": text_chunks("ok")},
        compaction_summary=_SUMMARY,
        extra_config=_COMPACTION_CFG,
    ) as (runtime, sid, server):
        session = await runtime.open_chat(sid)
        sub = session.subscribe()
        await session.send_prompt("alpha unique-alpha", model=_MODEL)
        await _drain_turn(sub, [])
        await session.send_prompt("beta unique-beta", model=_MODEL)
        await _drain_turn(sub, [])
        collected: list[Event] = []
        await session.compact()
        await _collect_until(sub, collected, CompactionApplied)
        assert any(isinstance(e, CompactionApplied) for e in collected)
        await runtime.close_chat(sid)  # drains persist pump + releases lock

        # (2) chat.jsonl must hold the CompactionApplied with the summary.
        chat = runtime.project.chats().open(sid)
        try:
            persisted = list(chat.events())
        finally:
            chat.close()
        comps = [e for e in persisted if isinstance(e, CompactionApplied)]
        assert comps, "CompactionApplied was not persisted to chat.jsonl"
        assert "unique-summary-marker" in comps[0].summary_text
        # The suppressed summary message must NOT have leaked into the log
        # as a normal assistant text part (the raw "## Goal …" template).
        from alkera_core.schemas.chat import PartCreated, TextPart

        leaked = [
            e
            for e in persisted
            if isinstance(e, PartCreated)
            and isinstance(e.part, TextPart)
            and "unique-summary-marker" in e.part.text
        ]
        assert not leaked, "raw summary template leaked into chat.jsonl"

        # (3) Resume via the runtime → compacted context only.
        session2 = await runtime.open_chat(sid)
        await session2.send_prompt("gamma unique-gamma", model=_MODEL)
        turn = await _wait_for_request(server, "unique-gamma")
        await runtime.close_chat(sid)
        seen = _request_texts(turn)
        assert "unique-summary-marker" in seen, "summary lost across runtime resume"
        assert "unique-alpha" not in seen, "elided head reappeared after resume"


_OVERFLOW_PROMPT = "alpha unique-alpha"


def _overflow_script(usage_total: int) -> MockScript:
    """Report `usage_total` tokens for the overflow prompt and nothing after it.

    Only the one turn we drive may overflow. opencode answers a compaction by
    appending a synthetic "continue" prompt and running ANOTHER turn; a script
    that reports the overflowing usage for every completion (a bare ``"*"`` key)
    therefore overflows that turn too — compact, continue, overflow, forever, with
    the session never reaching idle. The follow-up turns fall through to ``"*"``,
    report no usage, and the turn settles after exactly one compaction."""
    return {_OVERFLOW_PROMPT: text_chunks("ok", usage_total=usage_total), "*": text_chunks("ok")}


def _summarize_calls(server: MockOpenAIServer) -> int:
    """How many summarization requests opencode sent the mock — i.e. how many
    times it actually started a compaction, independent of what we surfaced."""
    return sum(1 for r in server.requests if COMPACTION_MARKER in _request_texts(r))


async def _drain_to_idle(sub: Any, collected: list[Event], *, budget_seconds: float = 30.0) -> bool:
    """Drain until the session reports idle. Unlike `_drain_turn` this does not
    require a fresh `running` first, so it can follow a mid-turn event (the
    compaction) to the end of the turn it belongs to. False on timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        ev = await _next_event_within(sub, remaining)
        if ev is None:
            return False
        collected.append(ev)
        if isinstance(ev, SessionStatusChanged) and ev.status in ("idle", "error"):
            return ev.status == "idle"


@pytest.mark.asyncio
async def test_auto_compaction_triggers_on_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AUTO compaction: a tiny model context limit + a turn whose reported usage
    overflows it makes opencode compact on its own — no manual /compact. We must
    surface that as a CompactionApplied, and the turn must then settle: opencode's
    post-compaction auto-continue may not overflow again into a compaction loop."""
    async with opencode_e2e_adapter(
        tmp_path,
        monkeypatch,
        # The turn reports 5000 tokens; usable window is 2000 - 500 → overflow.
        mock_script=_overflow_script(5000),
        compaction_summary=_SUMMARY,
        model_limit={"context": 2000, "output": 500},
        extra_config=_COMPACTION_CFG,
    ) as (adapter, server):
        sub = adapter.subscribe()
        collected: list[Event] = []
        await adapter.send_prompt(PromptInput(text=_OVERFLOW_PROMPT, model=_MODEL))
        await _collect_until(sub, collected, CompactionApplied)
        assert any(isinstance(e, CompactionApplied) for e in collected), (
            "auto-compaction did not fire on token overflow"
        )
        assert await _drain_to_idle(sub, collected), (
            "the session never went idle after auto-compaction"
        )
        assert _summarize_calls(server) == 1, (
            f"expected one summarization, got {_summarize_calls(server)} — "
            "auto-compaction is looping"
        )


@pytest.mark.asyncio
async def test_compaction_headroom_triggers_earlier_via_input_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The compaction-headroom fix: a `limit.input` BELOW the context window makes
    opencode trigger auto-compaction at a usage the default (context - max_output)
    threshold would NOT — so a long chat compacts while it's still small enough to
    summarize, instead of only at the hard wall (where summarization itself overflows
    and 'compaction couldn't free enough space').

    Both halves report the SAME 4000 tokens against the SAME context=10000: with
    `input=3000` (usable ≈ 2000) compaction fires, and without it (usable ≈ 9000) the
    turn ends untouched. So the compaction can ONLY be the headroom doing its job."""
    async with opencode_e2e_adapter(
        tmp_path,
        monkeypatch,
        mock_script=_overflow_script(4000),
        compaction_summary=_SUMMARY,
        model_limit={"context": 10000, "input": 3000, "output": 1000},
        extra_config=_COMPACTION_CFG,
    ) as (adapter, server):
        sub = adapter.subscribe()
        collected: list[Event] = []
        await adapter.send_prompt(PromptInput(text=_OVERFLOW_PROMPT, model=_MODEL))
        await _collect_until(sub, collected, CompactionApplied)
        assert any(isinstance(e, CompactionApplied) for e in collected), (
            "limit.input headroom did not trigger compaction below the context threshold"
        )
        assert await _drain_to_idle(sub, collected), (
            "the session never went idle after auto-compaction"
        )
        assert _summarize_calls(server) == 1, (
            f"expected one summarization, got {_summarize_calls(server)} — "
            "auto-compaction is looping"
        )

    # Same tokens, same context window, no `limit.input` — nothing compacts.
    async with opencode_e2e_adapter(
        tmp_path / "no-headroom",
        monkeypatch,
        mock_script=_overflow_script(4000),
        compaction_summary=_SUMMARY,
        model_limit={"context": 10000, "output": 1000},
        extra_config=_COMPACTION_CFG,
    ) as (adapter, server):
        sub = adapter.subscribe()
        collected = []
        await adapter.send_prompt(PromptInput(text=_OVERFLOW_PROMPT, model=_MODEL))
        assert await _drain_to_idle(sub, collected), "the turn never went idle"
        assert not [e for e in collected if isinstance(e, CompactionApplied)], (
            "compaction fired at 4000 tokens without the limit.input headroom — "
            "the headroom test proves nothing"
        )
        assert _summarize_calls(server) == 0, (
            "opencode started a summarization without the limit.input headroom"
        )


_CSV_ROWS = 60
_HIDDEN_ROW = "60035,"


def _csv_rows(count: int) -> list[str]:
    return [
        f"{60001 + i},2026-07-{(i % 28) + 1:02d},REGION-{i % 7},REP-{i % 15},PRODUCT-{i % 10},"
        f"{100 + i},34.50,{(100 + i) * 34.5:.2f},"
        f'"Note {i} padded to exactly forty five characters"'
        for i in range(count)
    ]


_CSV_DUMP = "\n".join(["id,date,region,rep,product,qty,unit,total,note", *_csv_rows(_CSV_ROWS)])
# The bulk rides in the PROMPT, not in the reply: the mock streams a reply one chunk per
# character, so a reply this size would be thousands of SSE frames and the turn's own
# status events would be lost behind them. The anchor facts lead it so they survive the
# summariser's head+tail cap while the middle rows do not.
_ANCHOR = (
    "Remember: depot PELICAN-7741 holds 8312 pallets and ships on lane ALPHA-9.\n"
    f"Here is the sales CSV it came from:\n{_CSV_DUMP}"
)
# What a real model wrote in the live run: thinking aloud before the template, rows it
# was told not to copy, and a question to the reader after it. What gets STORED — and
# read back as the chat's memory of itself — must be the template alone.
_RAW_SUMMARY = "\n\n".join(
    [
        "The user is asking me to create an anchored summary. Let me review what happened.",
        "## Goal\n- Depot report for PELICAN-7741 (8312 pallets, lane ALPHA-9)",
        "## Constraints & Preferences\n- (none)",
        "## Progress\n### Done\n- Printed the sales CSV\n"
        "### In Progress\n- (none)\n### Blocked\n- (none)",
        "## Key Decisions\n- (none)",
        "## Next Steps\n- (none)",
        "## Critical Context\n- unique-summary-marker\n- the rows I was given:\n"
        + "\n".join(_csv_rows(14)),
        "## Relevant Files\n- (none)",
        "I've completed the work outlined in our previous conversation. "
        "How would you like to proceed?",
    ]
)
# The nudge opencode appends when nothing was left in progress — upstream's own wording,
# which the alkera edit keeps for exactly that case. Scripted so the step after the
# compaction reports a usage UNDER the window and the chat settles instead of
# compacting its own summary forever.
_CONTINUE_NUDGE = (
    "Continue if you have next steps, or stop and ask for clarification "
    "if you are unsure how to proceed."
)


def _longest_csv_run(text: str) -> int:
    best = run = 0
    for line in text.split("\n"):
        run = run + 1 if line.strip().startswith("600") and line.count(",") >= 8 else 0
        best = max(best, run)
    return best


@pytest.mark.asyncio
async def test_auto_compaction_summarises_instead_of_answering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live defect: the summariser was run as one more assistant step on the same
    message list, so it copied hundreds of raw CSV rows into the summary, answered the
    conversation instead of describing it, and closed by asking the reader what to do.

    Through a real opencode against the mock provider: one prompt carrying an anchor
    fact and a 60-row CSV, reported at a usage that overflows the window, so opencode
    compacts. On the wire the summarisation call must carry the summariser's OWN system
    prompt, the anchor facts, a CAPPED view of the dump, and the summarise instruction
    as its final turn — the position a model answers from. What is then STORED, and read
    back as the chat's memory of itself on the next step, must be the template alone:
    the model's preamble cut, the rows it copied anyway collapsed, the closing question
    dropped. (The wire-level system prompt is only observable here; the unit suite in
    vendor/opencode replaces the LLM service.)"""
    async with opencode_e2e_adapter(
        tmp_path,
        monkeypatch,
        # usable = input - min(20 000, output) = 2 000, so this turn's reported usage
        # overflows it and opencode compacts at the end of the step that reported it.
        mock_script={
            "*": text_chunks("ok", usage_total=4_000),
            _CONTINUE_NUDGE: text_chunks("done", usage_total=100),
        },
        compaction_summary=_RAW_SUMMARY,
        model_limit={"context": 10_000, "input": 3_000, "output": 1_000},
        extra_config={"compaction": {"tail_turns": 1, "preserve_recent_tokens": 100_000}},
    ) as (adapter, server):
        sub = adapter.subscribe()
        collected: list[Event] = []
        await adapter.send_prompt(PromptInput(text=_ANCHOR, model=_MODEL))
        await _collect_until(sub, collected, CompactionApplied, budget_seconds=120.0)
        applied = [e for e in collected if isinstance(e, CompactionApplied)]
        trace = _request_trace(server)
        assert applied, f"auto-compaction did not fire on the overflowing report\n{trace}"

        # The summarisation call, as the provider saw it.
        summarising = [
            (i, r) for i, r in enumerate(server.requests) if COMPACTION_MARKER in _last_user_text(r)
        ]
        assert len(summarising) == 1, f"expected exactly one summarisation call\n{trace}"
        index, request = summarising[0]
        system = _system_text(request)
        assert "never continue it" in system, "the summariser ran on the agent's own prompt"
        assert "Do not carry it out" in system
        everything = _request_texts(request)
        assert "PELICAN-7741" in everything and "8312" in everything
        assert "not shown to the summarizer" in everything, (
            "the dump reached the summariser uncapped"
        )
        assert _HIDDEN_ROW not in everything, "the middle of the dump reached the summariser"
        assert _longest_csv_run(everything) < _CSV_ROWS // 2
        # The instruction is the LAST turn: anything after it is what the model answers.
        assert request["messages"][-1].get("role") == "user", (
            "the summarisation instruction is not the final turn"
        )
        last_turn = _message_text(request["messages"][-1])
        assert COMPACTION_MARKER in last_turn
        assert "Keep the whole summary under" in last_turn

        # What is stored is the template alone.
        summary = applied[0].summary_text
        assert summary.startswith("## Goal"), summary[:120]
        assert "unique-summary-marker" in summary
        assert "Let me review" not in summary, "the model's preamble was stored as summary"
        assert "How would you like to proceed" not in summary
        assert not summary.rstrip().endswith("?")
        assert _longest_csv_run(summary) <= 3, "the copied rows were stored verbatim"
        assert "more rows not kept in the summary" in summary

        # The next agent step reads the compacted context: the cleaned summary, and no
        # trace of the elided dump or of the model's scratchpad.
        followup = await _agent_request_after(server, index, budget_seconds=120.0)
        after = _request_texts(followup)
        trace = _request_trace(server)
        assert "unique-summary-marker" in after, trace
        assert "Let me review" not in after, trace
        assert "How would you like to proceed" not in after, trace
        assert _HIDDEN_ROW not in after
        assert _longest_csv_run(after) <= 3


def _request_trace(server: Any) -> str:
    """Every call the mock saw, one line each — the map to read when an assertion
    about WHICH call carried what fails."""
    rows = []
    for i, request in enumerate(server.requests):
        text = _request_texts(request)
        flags = "".join(
            [
                "S" if COMPACTION_MARKER in _last_user_text(request) else "-",
                "M" if "unique-summary-marker" in text else "-",
                "D" if _HIDDEN_ROW in text else "-",
                "N" if _CONTINUE_NUDGE in text else "-",
            ]
        )
        rows.append(f"  [{i}] {flags} last-user={_last_user_text(request)[:90]!r}")
    return "calls seen (S=summarise M=summary D=dump-middle N=nudge):\n" + "\n".join(rows)


async def _agent_request_after(
    server: Any, index: int, *, budget_seconds: float = 60.0
) -> dict[str, Any]:
    """The first MAIN-agent call the mock records after `index`. opencode fires
    auxiliary calls (title generation) with their own small system prompts, so the
    step that actually continues the chat is identified by the agent's own prompt,
    not by position."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds
    while loop.time() < deadline:
        for request in server.requests[index + 1 :]:
            if "You are Alkera" in _system_text(request):
                return request
        await asyncio.sleep(0.1)
    raise AssertionError(f"no agent step followed the summarisation call in {budget_seconds}s")


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return ""


def _last_user_text(request: dict[str, Any]) -> str:
    users = [
        m for m in request.get("messages", []) if isinstance(m, dict) and m.get("role") == "user"
    ]
    return _message_text(users[-1]) if users else ""


@pytest.mark.asyncio
async def test_turn_failure_surfaces_session_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed turn (provider error) surfaces as a SessionStatusChanged
    error — the signal the CLI turns into the `/compact` recovery offer."""
    async with opencode_e2e_adapter(
        tmp_path,
        monkeypatch,
        mock_script={"*": text_chunks("ok")},
        error_on_marker="FAILTHISTURN",
    ) as (adapter, _server):
        sub = adapter.subscribe()
        await adapter.send_prompt(PromptInput(text="please FAILTHISTURN", model=_MODEL))
        collected: list[Event] = []
        await _drain_turn(sub, collected)
        errors = [
            e for e in collected if isinstance(e, SessionStatusChanged) and e.status == "error"
        ]
        assert errors, "turn failure did not surface as a session error"


@pytest.mark.asyncio
async def test_compaction_failure_surfaces_error_without_compaction_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the summarization model call itself fails, opencode never emits
    session.compacted — so we surface a session error and emit NO
    CompactionApplied (and the half-captured summary is dropped)."""
    async with opencode_e2e_adapter(
        tmp_path,
        monkeypatch,
        mock_script={"*": text_chunks("ok")},
        # The compaction call carries opencode's SUMMARY_TEMPLATE marker →
        # the mock 400s it, failing the compaction.
        error_on_marker=COMPACTION_MARKER,
        extra_config=_COMPACTION_CFG,
    ) as (adapter, _server):
        sub = adapter.subscribe()
        await adapter.send_prompt(PromptInput(text="alpha unique-alpha", model=_MODEL))
        await _drain_turn(sub, [])
        collected: list[Event] = []
        await adapter.compact()
        await _drain_turn(sub, collected)
        errors = [
            e for e in collected if isinstance(e, SessionStatusChanged) and e.status == "error"
        ]
        comps = [e for e in collected if isinstance(e, CompactionApplied)]
        assert errors, "failed compaction did not surface a session error"
        assert not comps, "a failed compaction must not emit CompactionApplied"


# ---------------------------------------------------------------------------
# Clear — fresh-session context reset
# ---------------------------------------------------------------------------


async def _collect_until(
    sub: Any,
    collected: list[Event],
    want: type[Event] | tuple[type[Event], ...],
    *,
    budget_seconds: float = 90.0,
) -> None:
    """Drain events into `collected` until an instance of `want` arrives, an error
    status surfaces, or the (generous) budget expires.

    Unlike `_drain_turn` (which stops at the first `idle`), this waits for the
    SPECIFIC event. opencode can emit `CompactionApplied` / `ConversationCleared`
    slightly AFTER the turn's idle (clear has no model turn at all), and that
    ordering jitters under CPU load — gating on idle then snapshotting races it.
    Bounded so a genuinely-absent event still fails, just never flakily."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        ev = await _next_event_within(sub, remaining)
        if ev is None:
            return
        collected.append(ev)
        if isinstance(ev, want):
            return
        if isinstance(ev, SessionStatusChanged) and ev.status == "error":
            return


@pytest.mark.asyncio
async def test_clear_resets_context_for_next_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive a real opencode through `/clear`:

    1. Two turns of history (markers `unique-alpha`, `unique-beta`).
    2. `adapter.clear()` → mints a fresh opencode session, re-pins to it,
       and surfaces ONE `ConversationCleared` (the opencode session id
       changes — clear does not touch the working tree).
    3. The next turn must run against an EMPTY context: the request the
       model sees contains NEITHER prior turn.
    """
    async with opencode_e2e_adapter(
        tmp_path,
        monkeypatch,
        mock_script={"*": text_chunks("ok")},
    ) as (adapter, server):
        sub = adapter.subscribe()
        old_sid = adapter.native_state()["agent_session_id"]

        await adapter.send_prompt(PromptInput(text="alpha unique-alpha", model=_MODEL))
        await _drain_turn(sub, [])
        await adapter.send_prompt(PromptInput(text="beta unique-beta", model=_MODEL))
        await _drain_turn(sub, [])

        collected: list[Event] = []
        await adapter.clear()
        await _collect_until(sub, collected, ConversationCleared)

        clears = [e for e in collected if isinstance(e, ConversationCleared)]
        assert clears, "expected a ConversationCleared from clear()"
        new_sid = adapter.native_state()["agent_session_id"]
        assert new_sid != old_sid, "clear must mint a fresh opencode session"

        # Next turn — must run against the cleared (empty) context.
        await adapter.send_prompt(PromptInput(text="gamma unique-gamma", model=_MODEL))
        turn3 = await _wait_for_request(server, "unique-gamma")
        seen = _request_texts(turn3)
        assert "unique-alpha" not in seen, "pre-clear turn 1 leaked into context"
        assert "unique-beta" not in seen, "pre-clear turn 2 leaked into context"


@pytest.mark.asyncio
async def test_clear_full_runtime_persists_and_survives_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The WHOLE alkera path for clear (HarnessRuntime → ChatSession →
    persist pump → chat.jsonl → manifest re-pinning):

    1. Two turns, then `session.clear()` through the ChatSession.
    2. close_chat → chat.jsonl persisted the ConversationCleared, AND the
       manifest re-pinned to the NEW opencode session (so resume won't
       re-attach to the old, non-empty one).
    3. Re-open via the runtime (resume) → a new turn still sees an EMPTY
       context (neither pre-clear marker).
    """
    async with opencode_e2e_runtime(
        tmp_path,
        mock_script={"*": text_chunks("ok")},
    ) as (runtime, sid, server):
        session = await runtime.open_chat(sid)
        sub = session.subscribe()
        await session.send_prompt("alpha unique-alpha", model=_MODEL)
        await _drain_turn(sub, [])
        await session.send_prompt("beta unique-beta", model=_MODEL)
        await _drain_turn(sub, [])
        old_oc = session.manifest.harness.get("agent_session_id")

        collected: list[Event] = []
        await session.clear()
        await _collect_until(sub, collected, ConversationCleared)
        assert any(isinstance(e, ConversationCleared) for e in collected)
        await runtime.close_chat(sid)  # drains persist pump + releases lock

        # (2) chat.jsonl holds the ConversationCleared; manifest re-pinned.
        chat = runtime.project.chats().open(sid)
        try:
            persisted = list(chat.events())
            harness_after = dict(chat.manifest.harness)
        finally:
            chat.close()
        assert any(isinstance(e, ConversationCleared) for e in persisted), (
            "ConversationCleared was not persisted to chat.jsonl"
        )
        new_oc = harness_after.get("agent_session_id")
        assert new_oc and new_oc != old_oc, (
            "manifest did not re-pin to the cleared opencode session"
        )

        # (3) Resume via the runtime → empty context.
        session2 = await runtime.open_chat(sid)
        await session2.send_prompt("gamma unique-gamma", model=_MODEL)
        turn = await _wait_for_request(server, "unique-gamma")
        await runtime.close_chat(sid)
        seen = _request_texts(turn)
        assert "unique-alpha" not in seen, "pre-clear head reappeared after resume"
        assert "unique-beta" not in seen, "pre-clear tail reappeared after resume"


@pytest.mark.asyncio
async def test_clear_then_compact_operates_on_post_clear_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No-breakage: after a clear, compaction still works AND only sees the
    post-clear context. Proves clear (fresh session) + compact (/summarize)
    + the read-only DB enrichment cooperate across the session swap."""
    async with opencode_e2e_adapter(
        tmp_path,
        monkeypatch,
        mock_script={"*": text_chunks("ok")},
        compaction_summary=_SUMMARY,
        extra_config=_COMPACTION_CFG,
    ) as (adapter, server):
        sub = adapter.subscribe()

        # Pre-clear turn — must NOT survive into the compacted context.
        await adapter.send_prompt(PromptInput(text="alpha unique-alpha", model=_MODEL))
        await _drain_turn(sub, [])

        await adapter.clear()
        await _collect_until(sub, [], ConversationCleared)

        # Two post-clear turns, then compact.
        await adapter.send_prompt(PromptInput(text="delta unique-delta", model=_MODEL))
        await _drain_turn(sub, [])
        await adapter.send_prompt(PromptInput(text="epsilon unique-epsilon", model=_MODEL))
        await _drain_turn(sub, [])
        collected: list[Event] = []
        await adapter.compact()
        await _collect_until(sub, collected, CompactionApplied)
        assert any(isinstance(e, CompactionApplied) for e in collected), (
            "compaction after clear did not emit CompactionApplied"
        )

        # A turn after the post-clear compaction: summary present, the
        # elided post-clear head (delta) gone, and the PRE-clear turn
        # (alpha) never resurfaces.
        await adapter.send_prompt(PromptInput(text="zeta unique-zeta", model=_MODEL))
        turn = await _wait_for_request(server, "unique-zeta")
        seen = _request_texts(turn)
        assert "unique-summary-marker" in seen, "summary missing post clear+compact"
        assert "unique-epsilon" in seen, "retained post-clear tail missing"
        assert "unique-delta" not in seen, "elided post-clear head leaked"
        assert "unique-alpha" not in seen, "pre-clear turn resurfaced after compact"


# ---------------------------------------------------------------------------
# Title — /title rename survives a turn + resume
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_title_survives_turn_and_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The WHOLE alkera path for `/title`:

    1. `session.set_title(...)` sets the title (manifest + a
       `SessionUpdated` on the firehose).
    2. A real turn runs — opencode auto-titles sessions from the first
       message, but our translator drops that, so it must NOT clobber the
       user title.
    3. close → resume → the chat still carries the user title, and the
       rename is on the durable log.

    (The untitled-on-create + the dispatcher behaviour are covered by the
    fast FakeAdapter + slash unit tests; the e2e runtime helper seeds a
    placeholder title, so here we assert the rename takes + survives.)
    """
    async with opencode_e2e_runtime(
        tmp_path,
        mock_script={"*": text_chunks("ok")},
    ) as (runtime, sid, _server):
        session = await runtime.open_chat(sid)
        sub = session.subscribe()

        await session.set_title("Investigate slow query")
        assert session.manifest.title == "Investigate slow query"

        # A real turn — opencode may auto-title internally; our title wins.
        await session.send_prompt("hello unique-hello", model=_MODEL)
        await _drain_turn(sub, [])
        assert session.manifest.title == "Investigate slow query", (
            "opencode's auto-title clobbered the user-set title"
        )
        await runtime.close_chat(sid)

        # The rename is on the durable log (audit + resume-fold fallback).
        chat = runtime.project.chats().open(sid)
        try:
            persisted = list(chat.events())
        finally:
            chat.close()
        renames = [
            e
            for e in persisted
            if isinstance(e, SessionUpdated) and e.title == "Investigate slow query"
        ]
        assert renames, "the /title rename was not persisted to chat.jsonl"

        # Resume → manifest still carries the user title.
        session2 = await runtime.open_chat(sid)
        try:
            assert session2.manifest.title == "Investigate slow query"
        finally:
            await runtime.close_chat(sid)
