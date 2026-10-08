"""D3 daemon question-path parity tests.

These tests stand up the JSON-RPC daemon with a FakeAdapter-backed
HarnessRuntime, drive ``harness.open_chat`` end-to-end, then exercise
the question flow:

1. Round-trip — answer.
2. Reject path.
3. plan_approval question kind passes through unchanged.
4. Disconnect mid-question → broker auto-rejects (D2 integration).
5. Multiple concurrent questions resolve in any order, each routed
   to the correct ``request_id``.

The daemon's harness module is loaded via the existing methods package
import so ``@method`` decorators register before we instantiate the
server.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.daemon import (
    JsonRpcServer,
    PipeWriter,
    connect_pipe_reader,
    connect_pipe_writer,
    read_frame_async,
    write_frame_async,
)
from alkera_cli.daemon import methods as _register_methods  # noqa: F401
from alkera_cli.daemon import server as daemon_server
from alkera_cli.harness import HarnessRuntime
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import (
    MessageCompleted,
    MessageCreated,
    PermissionOption,
    PermissionRequest,
    QuestionPrompt,
    QuestionRequest,
    Retrying,
    SessionStatusChanged,
    ToolCall,
    ToolCallUpdate,
)

_T = datetime(2026, 5, 26, tzinfo=UTC)

# How long to wait for a single daemon frame. The FIRST frame after
# ``harness.open_chat`` is gated on a REAL uvicorn loopback MCP server
# cold-start (``mcp_server.py`` binds an ephemeral socket on the open path),
# which on a loaded self-hosted Windows runner can take a few seconds. The
# budget is therefore generous on purpose: it still bounds a genuine hang far
# below the 300s faulthandler, but never trips merely because the cold start
# was slow. Every drain waits FOR an expected frame, so a larger bound only
# delays a real failure — it can't turn a passing assertion into a flake.
_FRAME_TIMEOUT_SECONDS = 20.0


# ---------------------------------------------------------------------------
# Stream + server plumbing
# ---------------------------------------------------------------------------


async def _connected_streams() -> tuple[
    asyncio.StreamReader,
    PipeWriter,
    asyncio.StreamReader,
    PipeWriter,
]:
    # Through ``daemon.pipes`` so each platform runs its production transport
    # (raw connect_read_pipe never delivers on the Windows ProactorEventLoop).
    c2s_r, c2s_w = os.pipe()
    server_reader = await connect_pipe_reader(os.fdopen(c2s_r, "rb", buffering=0))
    client_writer = await connect_pipe_writer(os.fdopen(c2s_w, "wb", buffering=0))
    s2c_r, s2c_w = os.pipe()
    client_reader = await connect_pipe_reader(os.fdopen(s2c_r, "rb", buffering=0))
    server_writer = await connect_pipe_writer(os.fdopen(s2c_w, "wb", buffering=0))
    return server_reader, server_writer, client_reader, client_writer


async def _send(writer: PipeWriter, envelope: dict[str, Any]) -> None:
    await write_frame_async(writer, json.dumps(envelope).encode("utf-8"))


async def _recv(reader: asyncio.StreamReader) -> dict[str, Any]:
    return json.loads((await read_frame_async(reader)).decode("utf-8"))


@pytest.fixture
async def daemon_with_fake(tmp_path: Path):
    """Spin up a daemon whose harness runtime uses a FakeAdapter
    factory. Yields ``(server, cw, cr, factory, project_path, stop)``."""
    sr, sw, cr, cw = await _connected_streams()
    server = JsonRpcServer(reader=sr, writer=sw)

    project_path = tmp_path
    project = ProjectDirectory(project_path / ".alkera")
    factory = FakeAdapterFactory()
    rt = HarnessRuntime(project, adapter_factory=factory)
    server.harness_runtimes = {  # type: ignore[attr-defined]
        str(project_path.resolve()): rt
    }

    task = asyncio.create_task(server.serve())

    async def stop() -> None:
        server.request_shutdown()
        cw.close()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except TimeoutError:
            task.cancel()

    try:
        yield server, cw, cr, factory, project_path, stop
    finally:
        if not task.done():
            await stop()


async def _open_chat(
    cw: asyncio.StreamWriter,
    cr: asyncio.StreamReader,
    project_path: Path,
) -> tuple[str, list[dict[str, Any]]]:
    """Send open_chat with create=True and pump frames until the
    response comes back, returning ``(session_id, leftover_frames)``.

    Leftover frames are notifications (`harness.event`) that arrived
    before the response — common when the FakeAdapter starts emitting
    immediately."""
    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "harness.open_chat",
            "params": {
                "project_path": str(project_path),
                "create": True,
                "harness": "alkera",
                "title": "q-test",
            },
        },
    )
    leftover: list[dict[str, Any]] = []
    while True:
        frame = await asyncio.wait_for(_recv(cr), timeout=_FRAME_TIMEOUT_SECONDS)
        if frame.get("id") == 1:
            return frame["result"]["session_id"], leftover
        leftover.append(frame)


def _make_question(
    session_id: str,
    request_id: str,
    *,
    kind: str = "question",
    text: str = "pick one",
) -> QuestionRequest:
    return QuestionRequest(
        event_id=request_id,
        time=_T,
        session_id=session_id,
        request_id=request_id,
        kind=kind,  # type: ignore[arg-type]
        questions=[QuestionPrompt(question=text, options=[])],
    )


def test_resolve_daemon_harness_maps_and_rejects() -> None:
    from alkera_cli.daemon.methods.harness import _resolve_daemon_harness

    assert _resolve_daemon_harness("claude") == "claude-agent"
    assert _resolve_daemon_harness("alkera") == "agent"
    with pytest.raises(RuntimeError):
        _resolve_daemon_harness("bogus")


def test_list_pagination_rejects_negative_bounds() -> None:
    """A negative limit/offset is a typed INVALID_PARAMS rejection (the daemon
    turns the ValidationError into one), not a silent Python-slice wraparound
    that drops rows (rows[0:-2]) or mis-paginates from the tail."""
    import pydantic
    from alkera_cli.daemon.methods.harness import (
        HarnessListCostLedgerRequest,
        HarnessListDecisionsRequest,
    )

    for model in (HarnessListDecisionsRequest, HarnessListCostLedgerRequest):
        with pytest.raises(pydantic.ValidationError):
            model(session_id="s", limit=-1)
        with pytest.raises(pydantic.ValidationError):
            model(session_id="s", offset=-1)
        ok = model(session_id="s", limit=10, offset=5)
        assert (ok.limit, ok.offset) == (10, 5)


async def _drain_until(
    cr: asyncio.StreamReader,
    predicate,
    *,
    deadline_seconds: float = _FRAME_TIMEOUT_SECONDS,
    seed: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Pull frames until ``predicate(frame)`` returns True.

    ``seed`` allows the caller to pre-feed frames that were captured
    earlier (e.g. by ``_open_chat``).
    """
    if seed:
        for frame in seed:
            if predicate(frame):
                return frame
    async with asyncio.timeout(deadline_seconds):
        while True:
            frame = await _recv(cr)
            if predicate(frame):
                return frame


async def _drain_until_each(
    cr: asyncio.StreamReader,
    *predicates: Callable[[dict[str, Any]], bool],
    deadline_seconds: float = _FRAME_TIMEOUT_SECONDS,
    seed: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Pull frames until each predicate has matched one, in any arrival order.

    Returns the first match for each predicate, in the predicates' order. For a
    response and a notification the daemon sends from separate tasks, where
    ``_drain_until`` on one would drop the other if it arrived first.
    """
    found: list[dict[str, Any] | None] = [None] * len(predicates)

    def take(frame: dict[str, Any]) -> None:
        for index, predicate in enumerate(predicates):
            if found[index] is None and predicate(frame):
                found[index] = frame

    for frame in seed or ():
        take(frame)
    async with asyncio.timeout(deadline_seconds):
        while any(match is None for match in found):
            take(await _recv(cr))
    return [match for match in found if match is not None]


def _is_question_required(frame: dict[str, Any]) -> bool:
    return frame.get("method") == "harness.question_required" and "id" in frame


def _is_mode_changed(frame: dict[str, Any]) -> bool:
    return frame.get("method") == "harness.session_state_changed"


# ---------------------------------------------------------------------------
# 1. Round-trip — answer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_question_round_trip_answer(daemon_with_fake):
    """FakeAdapter emits a QuestionRequest → daemon forwards as
    harness.question_required → client answers → adapter.answer_question
    is called with the right args."""
    _server, cw, cr, factory, project_path, _stop = daemon_with_fake
    sid, leftover = await _open_chat(cw, cr, project_path)
    adapter = factory.adapters[0]
    assert adapter.started

    await adapter.feed(_make_question(sid, "q1"))

    asked = await _drain_until(cr, _is_question_required, seed=leftover)
    assert asked["params"]["session_id"] == sid
    assert asked["params"]["request"]["request_id"] == "q1"

    # Reply with kind="answer".
    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": asked["id"],
            "result": {"kind": "answer", "answers": [["red"]]},
        },
    )

    # Let the broker thread the answer through.
    for _ in range(50):
        if adapter.question_replies:
            break
        await asyncio.sleep(0.02)

    assert adapter.question_replies == [("q1", [["red"]])]
    assert adapter.question_rejects == []


# ---------------------------------------------------------------------------
# 2. Reject path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_question_round_trip_reject(daemon_with_fake):
    """kind=\"reject\" routes to adapter.reject_question with the
    reason string preserved."""
    _server, cw, cr, factory, project_path, _stop = daemon_with_fake
    sid, leftover = await _open_chat(cw, cr, project_path)
    adapter = factory.adapters[0]

    await adapter.feed(_make_question(sid, "q2"))
    asked = await _drain_until(cr, _is_question_required, seed=leftover)

    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": asked["id"],
            "result": {"kind": "reject", "reason": "user-closed-dialog"},
        },
    )

    for _ in range(50):
        if adapter.question_rejects:
            break
        await asyncio.sleep(0.02)

    assert adapter.question_rejects == [("q2", "user-closed-dialog")]
    assert adapter.question_replies == []


# ---------------------------------------------------------------------------
# 3. plan_approval kind preserved on the wire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_approval_kind_preserved_in_request_payload(
    daemon_with_fake,
):
    """When the IR event carries ``kind="plan_approval"``, the daemon
    forwards it unchanged in the request payload. The editor branches
    on this to render the green plan-approval panel instead of a
    generic question card."""
    _server, cw, cr, factory, project_path, _stop = daemon_with_fake
    sid, leftover = await _open_chat(cw, cr, project_path)
    adapter = factory.adapters[0]

    await adapter.feed(_make_question(sid, "plan-1", kind="plan_approval"))
    asked = await _drain_until(cr, _is_question_required, seed=leftover)

    assert asked["params"]["request"]["kind"] == "plan_approval"
    # Acknowledge so the broker doesn't keep waiting.
    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": asked["id"],
            "result": {"kind": "reject", "reason": "test-cleanup"},
        },
    )


@pytest.mark.asyncio
async def test_plan_approval_accept_flips_permission_mode(daemon_with_fake):
    """Approving a plan with an "Accept —" option flips the session's permission
    mode to match (parity with the CLI's `_maybe_apply_plan_choice`). The editor
    only sends `question.answer`; the daemon owns the mode switch."""
    from alkera_cli.harness.permission_mode import PLAN_ACCEPT_OPTIONS

    # The "auto mode" accept label and the mode it maps to (byte-for-byte
    # the same string opencode's plan_present offers).
    accept_label, expected_mode = PLAN_ACCEPT_OPTIONS[1]
    assert expected_mode == "auto"

    _server, cw, cr, factory, project_path, _stop = daemon_with_fake
    sid, leftover = await _open_chat(cw, cr, project_path)

    # Plans are presented while in plan mode; start there so the flip is visible.
    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 70,
            "method": "harness.set_permission_mode",
            "params": {"session_id": sid, "mode": "plan"},
        },
    )
    await _drain_until(cr, lambda f: f.get("id") == 70, seed=leftover)

    adapter = factory.adapters[0]
    await adapter.feed(_make_question(sid, "plan-1", kind="plan_approval"))
    asked = await _drain_until(cr, _is_question_required)
    assert asked["params"]["request"]["kind"] == "plan_approval"

    # Approve with the verbatim accept label.
    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": asked["id"],
            "result": {"kind": "answer", "answers": [[accept_label]]},
        },
    )

    # The accept PUSHES the new mode to the editor (the set-to-plan notification
    # at id 70 was already drained above), so the pill follows the server flip
    # without a re-fetch that would race the daemon's own commit.
    note = await _drain_until(cr, _is_mode_changed)
    assert (note["params"]["session_id"], note["params"]["permission_mode"]) == (sid, "auto")

    # The mode flips to auto (read back via the RPC).
    mode = "plan"
    for poll_id in range(71, 96):
        await _send(
            cw,
            {
                "jsonrpc": "2.0",
                "id": poll_id,
                "method": "harness.get_permission_mode",
                "params": {"session_id": sid},
            },
        )
        got = await _drain_until(cr, lambda f, _id=poll_id: f.get("id") == _id)
        mode = got["result"]["mode"]
        if mode == expected_mode:
            break
        await asyncio.sleep(0.02)
    assert mode == "auto"

    # The answer still reaches the adapter so opencode can switch plan→build.
    assert adapter.question_replies == [("plan-1", [[accept_label]])]


@pytest.mark.asyncio
async def test_plan_approval_freeform_answer_leaves_mode_in_plan(daemon_with_fake):
    """A non-accept (free-form revise) answer to a plan question is NOT a mode
    switch — `plan_label_to_mode` returns None, so the session stays in plan
    while opencode revises and re-presents."""
    _server, cw, cr, factory, project_path, _stop = daemon_with_fake
    sid, leftover = await _open_chat(cw, cr, project_path)

    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 80,
            "method": "harness.set_permission_mode",
            "params": {"session_id": sid, "mode": "plan"},
        },
    )
    await _drain_until(cr, lambda f: f.get("id") == 80, seed=leftover)

    adapter = factory.adapters[0]
    await adapter.feed(_make_question(sid, "plan-2", kind="plan_approval"))
    asked = await _drain_until(cr, _is_question_required)

    # A free-form rejection — not one of the verbatim "Accept —" labels.
    revise = "Please use a different approach for step 2."
    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": asked["id"],
            "result": {"kind": "answer", "answers": [[revise]]},
        },
    )

    # Give the broker time to thread the answer, then confirm the mode is
    # UNCHANGED (still plan) — the no-flip branch of _apply_plan_choice.
    for _ in range(25):
        if adapter.question_replies:
            break
        await asyncio.sleep(0.02)
    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 81,
            "method": "harness.get_permission_mode",
            "params": {"session_id": sid},
        },
    )
    got = await _drain_until(cr, lambda f: f.get("id") == 81)
    assert got["result"]["mode"] == "plan"
    assert adapter.question_replies == [("plan-2", [[revise]])]


# ---------------------------------------------------------------------------
# 4. Disconnect mid-question → broker auto-rejects (D2 integration)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disconnect_mid_question_auto_rejects(daemon_with_fake):
    """If the editor disconnects between the daemon firing
    harness.question_required and the user replying, the broker
    auto-rejects the question (D2: outbound futures fail fast on
    connection close)."""
    _server, cw, cr, factory, project_path, _stop = daemon_with_fake
    sid, leftover = await _open_chat(cw, cr, project_path)
    adapter = factory.adapters[0]

    await adapter.feed(_make_question(sid, "q-disco"))
    await _drain_until(cr, _is_question_required, seed=leftover)

    # Client disconnects without replying.
    cw.close()

    for _ in range(100):  # up to 2s
        if adapter.question_rejects:
            break
        await asyncio.sleep(0.02)

    assert len(adapter.question_rejects) == 1
    rid, reason = adapter.question_rejects[0]
    assert rid == "q-disco"
    # How the disconnect raced to the reject: the broker's outbound RPC failing
    # (transport-error) on POSIX, or the teardown cancellation winning
    # (client-disconnected) on Windows' thread-bridged pipes.
    assert reason in ("transport-error", "resolver-error", "client-disconnected")


# ---------------------------------------------------------------------------
# 5. Sequential questions across a single chat — request_ids stay distinct
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sequential_questions_route_to_correct_request_id(
    daemon_with_fake,
):
    """The QuestionBroker is single-flight per chat by design — at most
    one question awaits a user answer at a time. This test fires three
    in sequence, replies to each, and verifies every adapter callback
    saw the matching ``request_id`` (no cross-talk between rounds).
    """
    _server, cw, cr, factory, project_path, _stop = daemon_with_fake
    sid, leftover = await _open_chat(cw, cr, project_path)
    adapter = factory.adapters[0]

    seed = leftover
    for rid, answer in (("qa", "A"), ("qb", "B"), ("qc", "C")):
        await adapter.feed(_make_question(sid, rid))
        asked = await _drain_until(cr, _is_question_required, seed=seed)
        seed = []  # consume the leftover once
        assert asked["params"]["request"]["request_id"] == rid
        await _send(
            cw,
            {
                "jsonrpc": "2.0",
                "id": asked["id"],
                "result": {"kind": "answer", "answers": [[answer]]},
            },
        )
        for _ in range(100):
            if adapter.question_replies and adapter.question_replies[-1][0] == rid:
                break
            await asyncio.sleep(0.02)

    by_rid = dict(adapter.question_replies)
    assert by_rid == {
        "qa": [["A"]],
        "qb": [["B"]],
        "qc": [["C"]],
    }


# ---------------------------------------------------------------------------
# 6. Clear — RPC forwards to session.clear() + surfaces the event
# ---------------------------------------------------------------------------


def _is_clear_event(frame: dict[str, Any]) -> bool:
    return (
        frame.get("method") == "harness.event"
        and frame.get("params", {}).get("event", {}).get("event_type") == "conversation.cleared"
    )


# ---------------------------------------------------------------------------
# A model call that keeps failing ends the turn, over the daemon too
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_turn_stopped_for_retrying_reaches_the_extension_as_a_failure(
    daemon_with_fake, monkeypatch: pytest.MonkeyPatch
):
    """The bound lives in the runtime the daemon and the CLI share, so
    the extension sees the same thing the CLI does: each retry forwarded as
    it happened, then the turn's failure with its reason, and the agent told
    to stop."""
    monkeypatch.setattr("alkera_cli.harness.runtime.MAX_MODEL_RETRIES", 2)
    monkeypatch.setattr("alkera_cli.harness.runtime.MODEL_RETRY_WINDOW_SECONDS", None)
    _server, cw, cr, factory, project_path, _stop = daemon_with_fake
    sid, leftover = await _open_chat(cw, cr, project_path)
    adapter = factory.adapters[0]

    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 71,
            "method": "harness.send_prompt",
            "params": {"session_id": sid, "text": "count the mentions"},
        },
    )
    await _drain_until(cr, lambda f: f.get("id") == 71, seed=leftover)
    turn = adapter.sent_prompts[-1].turn_id
    await adapter.feed(
        SessionStatusChanged(
            event_id="run", time=_T, session_id=sid, status="running", turn_id=turn
        )
    )
    for attempt in (1, 2):
        await adapter.feed(
            Retrying(
                event_id=f"retry-{attempt}",
                time=_T,
                session_id=sid,
                attempt=attempt,
                reason="Unable to connect. http://10.0.4.7:8081/v1",
            )
        )

    def _event(frame: dict[str, Any]) -> dict[str, Any]:
        if frame.get("method") != "harness.event":
            return {}
        return frame.get("params", {}).get("event", {})

    failed = await _drain_until(
        cr,
        lambda f: (
            _event(f).get("event_type") == "session.status_changed"
            and _event(f).get("status") == "error"
        ),
    )
    event = _event(failed)
    assert event["detail"] == (
        "The model could not be reached after 2 attempts; the turn was stopped."
    )
    assert event["turn_id"] == turn
    assert adapter.cancel_count == 1


# ---------------------------------------------------------------------------
# Permission-mode parity (daemon ⇄ CLI)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permission_mode_get_set_round_trip(daemon_with_fake):
    """`harness.set_permission_mode` switches the session mode (parity with the
    CLI's `/mode`) + persists it; `harness.get_permission_mode` reads it back. A
    bad mode is rejected by the Literal at the JSON-RPC boundary (not a silent
    no-op)."""
    _server, cw, cr, _factory, project_path, _stop = daemon_with_fake
    sid, leftover = await _open_chat(cw, cr, project_path)

    # Fresh chats start in "default".
    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 50,
            "method": "harness.get_permission_mode",
            "params": {"session_id": sid},
        },
    )
    got = await _drain_until(cr, lambda f: f.get("id") == 50, seed=leftover)
    assert got["result"]["mode"] == "default"

    # Switch to plan.
    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 51,
            "method": "harness.set_permission_mode",
            "params": {"session_id": sid, "mode": "plan"},
        },
    )
    # The set PUSHES a session_state_changed notification (emitted before the RPC
    # response) so editors update the mode pill from the authority instead of
    # re-fetching on a timing guess — the generalized desync fix.
    note = await _drain_until(cr, _is_mode_changed)
    assert (note["params"]["session_id"], note["params"]["permission_mode"]) == (sid, "plan")
    set_res = await _drain_until(cr, lambda f: f.get("id") == 51)
    assert set_res["result"]["mode"] == "plan"

    # Read back — the switch stuck.
    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 52,
            "method": "harness.get_permission_mode",
            "params": {"session_id": sid},
        },
    )
    got2 = await _drain_until(cr, lambda f: f.get("id") == 52)
    assert got2["result"]["mode"] == "plan"

    # A bogus mode is a typed error (the PermissionMode Literal), never accepted.
    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 53,
            "method": "harness.set_permission_mode",
            "params": {"session_id": sid, "mode": "ludicrous"},
        },
    )
    bad = await _drain_until(cr, lambda f: f.get("id") == 53)
    assert "error" in bad and "result" not in bad


@pytest.mark.parametrize("mode", ["auto", "plan", "bypass"])
@pytest.mark.asyncio
async def test_send_prompt_leaves_the_permission_mode_untouched(daemon_with_fake, mode: str):
    """A prompt is not a mode write. ``harness.send_prompt`` carries no mode, so
    the session keeps the mode the user chose, in the live session AND in the
    manifest a later open rehydrates from. Add a mode field to send_prompt and
    this fails."""
    _server, cw, cr, factory, project_path, _stop = daemon_with_fake
    sid, leftover = await _open_chat(cw, cr, project_path)

    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 60,
            "method": "harness.set_permission_mode",
            "params": {"session_id": sid, "mode": mode},
        },
    )
    set_res = await _drain_until(cr, lambda f: f.get("id") == 60, seed=leftover)
    assert set_res["result"]["mode"] == mode

    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 61,
            "method": "harness.send_prompt",
            "params": {"session_id": sid, "text": "hi"},
        },
    )
    await _drain_until(cr, lambda f: f.get("id") == 61)
    adapter = factory.adapters[0]
    for _ in range(100):
        if adapter.sent_prompts:
            break
        await asyncio.sleep(0.02)
    assert adapter.sent_prompts, "the prompt never reached the adapter"

    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 62,
            "method": "harness.get_permission_mode",
            "params": {"session_id": sid},
        },
    )
    got = await _drain_until(cr, lambda f: f.get("id") == 62)
    assert got["result"]["mode"] == mode

    # Assert the persisted bytes too: a reopen seeds the mode from these.
    manifest_path = project_path / ".alkera" / "chats" / sid / "manifest.json"
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["permission_mode"] == mode


# ---------------------------------------------------------------------------
# History replay — the FULL persisted log, never a bounded tail
# ---------------------------------------------------------------------------


def _seed_chat_exceeding_tail_cap(project_path: Path, *, sid: str) -> int:
    """Persist a tool-heavy chat whose semantic event count clears the
    historical 100-event ``open_chat`` cap, and return that count.

    Turn 0 — a user ask, the ``write`` tool that scaffolds a file, then
    the assistant's "Done!" reply — lands at the FRONT of the log,
    exactly where a stale ``events[-100:]`` tail would silently drop it
    (the reported "only the last two messages survive; the write tool
    that prepended 'Done!' is gone" symptom). ``store.create`` stamps one
    ``session.created`` ahead of these, so the returned count exceeds the
    appended total by one.
    """
    store = ProjectDirectory(project_path / ".alkera").chats()
    with store.create(session_id=sid, title="Hi!", harness_type="alkera") as chat:
        chat.append_event(
            MessageCreated(
                event_id="HEAD_USER", time=_T, session_id=sid, message_id="m0u", role="user"
            )
        )
        chat.append_event(
            ToolCall(
                event_id="WRITE_TOOL",
                time=_T,
                session_id=sid,
                tool_call_id="tc0",
                message_id="m0a",
                tool_name="write",
                input={"path": "scratch/app.py"},
            )
        )
        # Complete the tool so the log is a CLEANLY-FINISHED conversation — an open
        # tool call would (correctly) be closed by resume reconciliation on open,
        # which would break this test's exact-count guard.
        chat.append_event(
            ToolCallUpdate(
                event_id="WRITE_TOOL_DONE",
                time=_T,
                session_id=sid,
                tool_call_id="tc0",
                status="completed",
            )
        )
        chat.append_event(
            MessageCreated(
                event_id="DONE_MSG", time=_T, session_id=sid, message_id="m0a", role="assistant"
            )
        )
        chat.append_event(
            MessageCompleted(event_id="DONE_DONE", time=_T, session_id=sid, message_id="m0a")
        )
        for i in range(1, 35):  # 34 follow-up turns x 4 events -> well past 100
            chat.append_event(
                MessageCreated(
                    event_id=f"u{i}", time=_T, session_id=sid, message_id=f"m{i}u", role="user"
                )
            )
            chat.append_event(
                SessionStatusChanged(
                    event_id=f"r{i}",
                    time=_T,
                    session_id=sid,
                    status="running",
                    phase="streaming_response",
                )
            )
            chat.append_event(
                MessageCreated(
                    event_id=f"a{i}", time=_T, session_id=sid, message_id=f"m{i}a", role="assistant"
                )
            )
            chat.append_event(
                MessageCompleted(event_id=f"c{i}", time=_T, session_id=sid, message_id=f"m{i}a")
            )
        # End idle — a finished conversation. A trailing `running` status would be
        # read as interrupted and (correctly) flipped to idle by resume reconciliation.
        chat.append_event(
            SessionStatusChanged(event_id="FINAL_IDLE", time=_T, session_id=sid, status="idle")
        )
        return sum(1 for _ in chat.events())


@pytest.mark.asyncio
async def test_open_chat_replays_full_log_without_tail_truncation(daemon_with_fake):
    """Reopening a chat replays its ENTIRE persisted log, not a bounded
    tail. A tool-heavy "short" conversation clears 100 semantic events
    fast (~13/turn: part lifecycle + status churn + diffs), so the old
    ``events[-100:]`` cap silently ate the head — the first user ask and
    the ``write`` tool that scaffolded a file vanished on reopen while
    the later reply survived. This asserts both front-of-log events come
    back and the reply count is the full length, so the cap can't return.
    """
    _server, cw, cr, _factory, project_path, _stop = daemon_with_fake
    total = _seed_chat_exceeding_tail_cap(project_path, sid="hi-chat")
    assert total > 100  # precondition: the old cap WOULD have truncated this

    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "harness.open_chat",
            "params": {
                "project_path": str(project_path),
                "create": False,
                "session_id": "hi-chat",
                "harness": "alkera",
            },
        },
    )
    resp = await _drain_until(cr, lambda f: f.get("id") == 1)
    recent = resp["result"]["recent_events"]
    by_id = {e["event_id"]: e for e in recent}

    # The head user ask survives — old [-100:] dropped it (index 1, far
    # inside the cut tail since total > 140).
    assert "HEAD_USER" in by_id
    # The write tool that prepended "Done!" survives, tool_name intact.
    assert by_id.get("WRITE_TOOL", {}).get("tool_name") == "write"
    # The whole log replays, not a 100-event tail. The FakeAdapter resume
    # appends nothing, so the count is exact — a tighter guard that also
    # catches any over-replay or duplication, not just truncation.
    assert len(recent) == total


@pytest.mark.asyncio
async def test_observe_chat_attaches_to_a_running_chat_without_the_lock(daemon_with_fake):
    """`harness.observe_chat` attaches READ-ONLY to a chat that is OPEN in the
    runtime — a running subagent's exact situation. It returns the replay + live=True
    and NEVER raises a lock conflict, because it does not call open_chat / take the
    write lock (the fix for "lock already taken" when inspecting a running subagent)."""
    _server, cw, cr, _factory, project_path, _stop = daemon_with_fake
    sid, _ = await _open_chat(cw, cr, project_path)

    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "harness.observe_chat",
            "params": {"project_path": str(project_path), "session_id": sid},
        },
    )
    resp = await _drain_until(cr, lambda f: f.get("id") == 7)
    result = resp["result"]
    assert result["session_id"] == sid
    assert result["live"] is True  # attached to the live in-process session
    assert len(result["recent_events"]) >= 1  # the persisted prefix replayed


# ---------------------------------------------------------------------------
# Slash-command surface (Python is the single source of truth)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_commands_mirrors_the_cli_registry(daemon_with_fake) -> None:
    """`harness.list_commands` serves chat_slash.COMMANDS verbatim — editors
    render the registry, never re-define it (no TS duplication)."""
    from alkera_cli.chat.slash import COMMANDS

    _server, cw, cr, _factory, _project_path, _stop = daemon_with_fake
    await _send(cw, {"jsonrpc": "2.0", "id": 5, "method": "harness.list_commands", "params": {}})
    got = await _drain_until(cr, lambda f: f.get("id") == 5)

    served = got["result"]["commands"]
    # Every non-editor_hidden command is served verbatim (no command is
    # editor_hidden any more — mode/cost/preferences are surfaced for the
    # composer's panels, with their shortcuts).
    assert [c["name"] for c in served] == [s.name for s in COMMANDS if not s.editor_hidden]
    by_name = {c["name"]: c for c in served}
    # mode/cost/preferences and the shortcuts now reach the editor menu.
    assert {"mode", "cost", "preferences", "plan", "yolo"} <= set(by_name)
    # `help`, `mode`, `cost`, `preferences` stay cli_only (ui=False): the editor
    # drives them through a panel / native control, not a dispatch handler.
    for name in ("help", "mode", "cost", "preferences"):
        assert by_name[name]["ui"] is False, name
    # The mode shortcuts are served but hidden from the browsable menu.
    for name in ("plan", "bypass", "yolo"):
        assert by_name[name]["hidden"] is True, name
    # The dispatch-backed editor commands stay ui=True.
    for name in ("compact", "clear", "title", "usage", "exit"):
        assert by_name[name]["ui"] is True, name


@pytest.mark.asyncio
async def test_run_command_title_sets_the_manifest_and_returns_structured_payload(
    daemon_with_fake,
) -> None:
    _server, cw, cr, _factory, project_path, _stop = daemon_with_fake
    sid, leftover = await _open_chat(cw, cr, project_path)

    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "harness.run_command",
            "params": {"session_id": sid, "line": "/title Data quality sweep"},
        },
    )
    got = await _drain_until(cr, lambda f: f.get("id") == 10, seed=leftover)
    assert got["result"]["kind"] == "ok"
    assert got["result"]["command"] == "title"
    assert got["result"]["payload"] == {"action": "set", "title": "Data quality sweep"}

    # The side effect lands (set_title is scheduled on the loop) — poll the
    # manifest via /title's show action.
    for poll_id in range(11, 31):
        await _send(
            cw,
            {
                "jsonrpc": "2.0",
                "id": poll_id,
                "method": "harness.run_command",
                "params": {"session_id": sid, "line": "/title"},
            },
        )
        shown = await _drain_until(cr, lambda f, _id=poll_id: f.get("id") == _id)
        if shown["result"]["payload"].get("title") == "Data quality sweep":
            break
        await asyncio.sleep(0.02)
    assert shown["result"]["payload"] == {"action": "show", "title": "Data quality sweep"}


@pytest.mark.asyncio
async def test_run_command_mode_answers_cli_only_not_a_mode_flip(
    daemon_with_fake,
) -> None:
    """`/mode` is surfaced for the editor but has no dispatch handler — the
    composer applies a mode via its panel (`harness.set_permission_mode`). A
    typed `run_command("/mode auto")` therefore answers `cli_only` and does NOT
    flip the session mode."""
    _server, cw, cr, _factory, project_path, _stop = daemon_with_fake
    sid, leftover = await _open_chat(cw, cr, project_path)

    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "harness.run_command",
            "params": {"session_id": sid, "line": "/mode auto"},
        },
    )
    got = await _drain_until(cr, lambda f: f.get("id") == 10, seed=leftover)
    assert got["result"]["kind"] == "cli_only"

    # And the session mode did NOT change.
    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "harness.get_permission_mode",
            "params": {"session_id": sid},
        },
    )
    mode = await _drain_until(cr, lambda f: f.get("id") == 11)
    assert mode["result"]["mode"] == "default"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("line", "expected_kind", "expected_message"),
    [
        pytest.param(
            "/usage nonsense", "bad_usage", "usage: /usage", id="bad-usage-carries-usage-line"
        ),
        pytest.param("/nope", "unknown", "unknown command", id="unknown-command-carries-hint"),
        pytest.param("/exit", "exit", None, id="exit-maps-to-exit-kind"),
        pytest.param("/quit", "exit", None, id="quit-alias-maps-to-exit-kind"),
        pytest.param("/cost set", "cli_only", "available in the CLI", id="cost-cli-only"),
        pytest.param("/prefs set x y", "cli_only", "preferences panel", id="prefs-alias-cli-only"),
        pytest.param("/help", "cli_only", "browse commands", id="cli-only-help"),
    ],
)
async def test_run_command_result_kinds(
    daemon_with_fake, line: str, expected_kind: str, expected_message: str | None
) -> None:
    _server, cw, cr, _factory, project_path, _stop = daemon_with_fake
    sid, leftover = await _open_chat(cw, cr, project_path)

    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "harness.run_command",
            "params": {"session_id": sid, "line": line},
        },
    )
    got = await _drain_until(cr, lambda f: f.get("id") == 12, seed=leftover)
    assert got["result"]["kind"] == expected_kind
    if expected_message is not None:
        assert expected_message in (got["result"]["message"] or "")


def _is_compaction_event(frame: dict[str, Any]) -> bool:
    return (
        frame.get("method") == "harness.event"
        and frame.get("params", {}).get("event", {}).get("event_type") == "compaction.applied"
    )


@pytest.mark.asyncio
async def test_run_command_compact_forwards_to_session_and_emits_compaction_event(
    daemon_with_fake,
) -> None:
    """`run_command("/compact")` is an intrinsic state mutation: it returns the
    structured `ok`/`compact` outcome, drives `session.compact()`, and forwards the
    resulting `CompactionApplied` as a `harness.event` — the one slash result that
    DOES leave a transcript mark (unlike /usage et al., which live in the panel)."""
    _server, cw, cr, factory, project_path, _stop = daemon_with_fake
    sid, leftover = await _open_chat(cw, cr, project_path)
    adapter = factory.adapters[0]

    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 15,
            "method": "harness.run_command",
            "params": {"session_id": sid, "line": "/compact"},
        },
    )
    # The compaction is fired onto the loop from the slash dispatch's thread, so
    # its event can be forwarded before or after the RPC response: take both in
    # whichever order they arrive.
    got, event = await _drain_until_each(
        cr, lambda f: f.get("id") == 15, _is_compaction_event, seed=leftover
    )
    assert got["result"]["kind"] == "ok"
    assert got["result"]["command"] == "compact"
    # No structured payload/message — the editor draws nothing from it; the event
    # is the persisted mark.
    assert got["result"]["payload"] == {}
    assert event["params"]["session_id"] == sid
    assert adapter.compact_count == 1


@pytest.mark.asyncio
async def test_run_command_persists_no_command_result(
    daemon_with_fake,
) -> None:
    """run_command persists NO `command.result` — slash results render in the
    composer's transient panel, never the transcript. `/clear` still leaves its
    own `ConversationCleared`. `/title` is sandwiched between two `/clear`s so
    the FIFO persist pump GUARANTEES its (absent) event would be on disk before
    the second clear — an empty command.result list is therefore conclusive."""
    _server, cw, cr, _factory, project_path, _stop = daemon_with_fake
    sid, _leftover = await _open_chat(cw, cr, project_path)

    for rpc_id, line in enumerate(["/clear", "/title Sweep", "/clear"], start=10):
        await _send(
            cw,
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "method": "harness.run_command",
                "params": {"session_id": sid, "line": line},
            },
        )
        await _drain_until(cr, lambda f, _id=rpc_id: f.get("id") == _id)

    chat_jsonl = project_path / ".alkera" / "chats" / sid / "chat.jsonl"
    command_results: list[dict[str, Any]] = []
    cleared_count = 0
    async with asyncio.timeout(_FRAME_TIMEOUT_SECONDS):
        while True:
            events = [
                json.loads(line)
                for line in chat_jsonl.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            command_results = [e for e in events if e.get("event_type") == "command.result"]
            cleared_count = sum(1 for e in events if e.get("event_type") == "conversation.cleared")
            # Both clears flushed → the /title between them flushed too (FIFO).
            if cleared_count >= 2:
                break
            await asyncio.sleep(0.02)

    # /clear still leaves its ConversationCleared boundary cards.
    assert cleared_count >= 2
    # ...but nothing — not even /title — persists a command.result any more.
    assert command_results == []


@pytest.mark.asyncio
async def test_set_model_effort_persists_to_the_manifest_and_rejects_unknown(
    daemon_with_fake,
) -> None:
    _server, cw, cr, _factory, project_path, _stop = daemon_with_fake
    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "harness.open_chat",
            "params": {
                "project_path": str(project_path),
                "create": True,
                "harness": "alkera",
                "title": "effort-test",
                # The editor sends the gateway SELECTION; the daemon builds the
                # pinned manifest (provider id + effort ladder) via build_manifest_model.
                "model": {
                    "id": "claude-sonnet-4-6",
                    "display_name": "Claude Sonnet 4.6",
                    "wire": "anthropic",
                    "efforts": ["low", "high"],
                    "default_effort": "low",
                },
                "effort": "low",
            },
        },
    )
    leftover: list[dict[str, Any]] = []
    while True:
        frame = await asyncio.wait_for(_recv(cr), timeout=_FRAME_TIMEOUT_SECONDS)
        if frame.get("id") == 1:
            sid = frame["result"]["session_id"]
            break
        leftover.append(frame)

    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 20,
            "method": "harness.set_model_effort",
            "params": {"session_id": sid, "effort": "high"},
        },
    )
    got = await _drain_until(cr, lambda f: f.get("id") == 20, seed=leftover)
    assert got["result"]["model"]["effort"] == "high"

    # Persisted: the chat metadata (manifest) on disk carries the choice, so a
    # resumed chat seeds the composer with it.
    manifest_path = project_path / ".alkera" / "chats" / sid / "manifest.json"
    persisted = json.loads(manifest_path.read_text(encoding="utf-8"))["model"]
    assert persisted["effort"] == "high"
    # The DAEMON resolved the gateway provider id from the selection's wire (the
    # single source) — the editor sent only {id, wire, efforts, …}, never the id.
    assert persisted["provider_id"] == "alkera-anthropic"
    assert persisted["model_id"] == "claude-sonnet-4-6"

    # Fail closed on an effort the pinned model doesn't offer.
    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 21,
            "method": "harness.set_model_effort",
            "params": {"session_id": sid, "effort": "max"},
        },
    )
    err = await _drain_until(cr, lambda f: f.get("id") == 21)
    assert "is not offered" in err["error"]["message"]
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["model"]["effort"] == "high"


@pytest.mark.asyncio
async def test_activity_log_returns_a_real_chats_permission_decision(daemon_with_fake):
    """The Decisions-tab pipeline end-to-end: a chat opened over RPC escalates a
    permission ask to the editor, the human's allow is audited to the per-chat
    ``decisions.jsonl``, and ``harness.list_decisions`` on the same connection
    returns it. Pins reader and writer to the SAME log — the editor once read
    the project-root fallback and rendered every populated chat empty."""
    _server, cw, cr, factory, project_path, _stop = daemon_with_fake
    sid, leftover = await _open_chat(cw, cr, project_path)
    adapter = factory.adapters[0]

    await adapter.feed(
        PermissionRequest(
            event_id="p1",
            time=_T,
            session_id=sid,
            request_id="p1",
            permission_kind="webfetch",
            canonical_kind="other",
            options=[
                PermissionOption(option_id="allow_once", name="Allow once"),
                PermissionOption(option_id="reject_once", name="Reject once"),
            ],
        )
    )
    asked = await _drain_until(
        cr,
        lambda f: f.get("method") == "harness.permission_required" and "id" in f,
        seed=leftover,
    )
    assert asked["params"]["request"]["request_id"] == "p1"
    await _send(
        cw,
        {"jsonrpc": "2.0", "id": asked["id"], "result": {"option_id": "allow_once"}},
    )
    # The pump audits BEFORE it replies to the adapter, so awaiting the reply
    # guarantees the decision row is on disk.
    async with asyncio.timeout(_FRAME_TIMEOUT_SECONDS):
        await adapter.wait_for_permission_reply("p1", "allow_once")

    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 90,
            "method": "harness.list_decisions",
            "params": {"session_id": sid},
        },
    )
    resp = await _drain_until(cr, lambda f: f.get("id") == 90)
    rows = resp["result"]["decisions"]
    assert [r["request_id"] for r in rows] == ["p1"]
    assert rows[0]["decision"] == "allow"
    on_disk = project_path / ".alkera" / "chats" / sid / "decisions.jsonl"
    assert on_disk.exists()


async def test_stale_session_id_answers_with_its_own_code_and_skips_sentry(
    daemon_with_fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    _server, cw, cr, _factory, _project_path, stop = daemon_with_fake
    captured: list[BaseException] = []
    monkeypatch.setattr(daemon_server, "capture_exception", lambda exc, **_kw: captured.append(exc))

    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "harness.get_permission_mode",
            "params": {"session_id": "gone-after-restart"},
        },
    )
    frame = await asyncio.wait_for(_recv(cr), timeout=_FRAME_TIMEOUT_SECONDS)
    await stop()

    assert frame["id"] == 7
    assert frame["error"]["code"] == daemon_server.SESSION_NOT_OPEN
    assert frame["error"]["data"]["session_id"] == "gone-after-restart"
    assert captured == []


# ---------------------------------------------------------------------------
# A chat opened with nothing said in it
# ---------------------------------------------------------------------------


async def _create_chat(
    cw: asyncio.StreamWriter,
    cr: asyncio.StreamReader,
    project_path: Path,
    *,
    req_id: int,
    title: str,
) -> dict[str, Any]:
    """Open a BRAND-NEW chat and return the full create result."""
    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "harness.open_chat",
            "params": {
                "project_path": str(project_path),
                "create": True,
                "harness": "alkera",
                "title": title,
            },
        },
    )
    frame = await _drain_until(cr, lambda f: f.get("id") == req_id)
    return dict(frame["result"])


def _user_prompts(project_path: Path, sid: str) -> list[str]:
    """Every user message persisted in the chat's log."""
    log = project_path / ".alkera" / "chats" / sid / "chat.jsonl"
    if not log.exists():
        return []
    texts: list[str] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("type") == "message_created" and event.get("role") == "user":
            texts.append(str(event.get("text", "")))
    return texts


@pytest.mark.asyncio
async def test_create_opens_a_chat_with_nothing_said_in_it(daemon_with_fake):
    """The editor's empty composer stages a pasted file into a chat's working
    folder, so it opens the chat BEFORE the message naming the file — the create
    must therefore leave the chat empty and still usable. A create that seeded a
    first message, or one whose chat then refused a prompt, fails here."""
    _server, cw, cr, factory, project_path, _stop = daemon_with_fake

    created = await _create_chat(cw, cr, project_path, req_id=200, title="compare these")
    sid = created["session_id"]

    # Nothing has been said: the replay is the session opening and no more,
    # and no user message reached the log.
    assert [event["event_type"] for event in created["recent_events"]] == ["session.created"]
    assert _user_prompts(project_path, sid) == []
    # It is a real, titled chat all the same.
    manifest = json.loads(
        (project_path / ".alkera" / "chats" / sid / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["title"] == "compare these"
    assert created["manifest"]["session_id"] == sid

    # And the message that opened it lands in it afterwards.
    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 201,
            "method": "harness.send_prompt",
            "params": {"session_id": sid, "text": "compare ![Image 1](paste-1.png) these"},
        },
    )
    sent = await _drain_until(cr, lambda f: f.get("id") == 201)
    assert "error" not in sent
    adapter = factory.adapters[0]
    for _ in range(100):
        if adapter.sent_prompts:
            break
        await asyncio.sleep(0.02)
    assert [prompt.text for prompt in adapter.sent_prompts] == [
        "compare ![Image 1](paste-1.png) these"
    ]


@pytest.mark.asyncio
async def test_a_chat_opened_for_its_files_is_shaped_like_any_other_new_chat(daemon_with_fake):
    """One creation path: a chat opened ahead of its first message comes out
    with the same manifest as one opened by the message itself — same harness,
    same title, same pinned model, same stance. Only the session id and the
    clock differ, and only the message is missing."""
    _server, cw, cr, _factory, project_path, _stop = daemon_with_fake

    quiet = await _create_chat(cw, cr, project_path, req_id=210, title="compare these")
    loud = await _create_chat(cw, cr, project_path, req_id=211, title="compare these")
    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 212,
            "method": "harness.send_prompt",
            "params": {"session_id": loud["session_id"], "text": "compare these"},
        },
    )
    await _drain_until(cr, lambda f: f.get("id") == 212)

    volatile = {"session_id", "created_at", "updated_at", "last_event_id", "last_seen_event_id"}

    def shape(manifest: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in manifest.items() if k not in volatile}

    assert shape(quiet["manifest"]) == shape(loud["manifest"])
    assert _user_prompts(project_path, quiet["session_id"]) == []


# ---------------------------------------------------------------------------
# The paged transcript: a tail on open, older pages on demand
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_chat_with_a_tail_replays_the_aligned_tail_and_says_what_is_older(
    daemon_with_fake,
):
    """With ``tail`` the open replays only the newest page — extended down to
    the turn it starts inside — and names the ordinal of its first event and
    whether the log holds anything older. Without ``tail`` the whole log still
    replays (pinned separately above)."""
    _server, cw, cr, _factory, project_path, _stop = daemon_with_fake
    total = _seed_chat_exceeding_tail_cap(project_path, sid="tail-chat")

    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "harness.open_chat",
            "params": {
                "project_path": str(project_path),
                "create": False,
                "session_id": "tail-chat",
                "harness": "alkera",
                "tail": 10,
            },
        },
    )
    resp = await _drain_until(cr, lambda f: f.get("id") == 1)
    result = resp["result"]
    ids = [event["event_id"] for event in result["recent_events"]]
    # The last ten events start at c32, inside turn 32; its user message u32
    # is three events below, so the page opens on it: turn 32's four events,
    # turns 33 and 34, and the closing idle.
    assert ids[0] == "u32"
    assert ids[-1] == "FINAL_IDLE"
    assert len(ids) == 13
    assert result["oldest_seq"] == total - 13 + 1
    assert result["has_older"] is True


@pytest.mark.asyncio
async def test_list_events_pages_a_chat_that_is_not_open_without_a_gap_or_duplicate(
    daemon_with_fake,
):
    """``harness.list_events`` reads the durable log lock-free, so it serves a
    chat nobody has open; walking it from the end covers every event once."""
    _server, cw, cr, _factory, project_path, _stop = daemon_with_fake
    total = _seed_chat_exceeding_tail_cap(project_path, sid="cold-chat")
    expected = [
        event.event_id
        for event in ProjectDirectory(project_path / ".alkera").chats().read_events("cold-chat")
    ]
    assert len(expected) == total

    seen: list[str] = []
    before = total + 1
    request_id = 10
    while True:
        request_id += 1
        await _send(
            cw,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "harness.list_events",
                "params": {
                    "project_path": str(project_path),
                    "session_id": "cold-chat",
                    "before": before,
                    "limit": 25,
                },
            },
        )
        resp = await _drain_until(cr, lambda f, rid=request_id: f.get("id") == rid)
        result = resp["result"]
        page = [event["event_id"] for event in result["events"]]
        assert 25 <= len(page) <= 50 or not result["has_older"]
        seen = page + seen
        if not result["has_older"]:
            assert result["oldest_seq"] == 1
            break
        assert result["oldest_seq"] == total - len(seen) + 1
        before = result["oldest_seq"]
        assert request_id < 100
    assert seen == expected

    # Below the first event there is nothing, and the answer says so.
    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 999,
            "method": "harness.list_events",
            "params": {"project_path": str(project_path), "session_id": "cold-chat", "before": 1},
        },
    )
    resp = await _drain_until(cr, lambda f: f.get("id") == 999)
    # ``cut`` is False, not absent: a page with no events cut nothing off, so
    # a reader is told it has the whole of what is there rather than being
    # sent looking for a page below the first event.
    assert resp["result"] == {
        "events": [],
        "oldest_seq": None,
        "has_older": False,
        "cut": False,
    }


# ---------------------------------------------------------------------------
# "Always allow" over JSON-RPC: how far a shell command's standing grant reaches
# ---------------------------------------------------------------------------


def _bash_ask(session_id: str, request_id: str, command: str) -> PermissionRequest:
    """A bash ask as the opencode translator hands it in: the real classifier's
    subject, and the decisions its own ``ask_options`` offers."""
    from alkera_cli.harness.adapters.opencode_translate import ask_options
    from alkera_cli.plugins.plugin_base.permissions.bash import classify_command

    descriptor = classify_command(command)
    return PermissionRequest(
        event_id=request_id,
        time=_T,
        session_id=session_id,
        request_id=request_id,
        tool_call_id=f"call-{request_id}",
        permission_kind="bash",
        canonical_kind="shell",
        patterns=[command],
        subject=descriptor.model_dump(mode="json"),
        options=ask_options(always=[command.split()[0] + " *"], descriptor=descriptor),
    )


def _is_permission_required(frame: dict[str, Any]) -> bool:
    return frame.get("method") == "harness.permission_required" and "id" in frame


@pytest.mark.parametrize(
    ("granted", "covered", "uncovered", "label"),
    [
        pytest.param(
            "rm -rf a/", "rm -rf a/", "rm -rf b/", "Always allow this exact command", id="rm-exact"
        ),
        pytest.param("touch a.txt", "touch b.txt", "rm -rf b/", "Always allow", id="touch-family"),
    ],
)
@pytest.mark.asyncio
async def test_always_over_rpc_reaches_what_the_consent_scope_records(
    daemon_with_fake, granted: str, covered: str, uncovered: str, label: str
):
    """A card answered "always" over the daemon: the destructive line is
    remembered as that line, so the identical line runs with no card and a
    different ``rm`` raises one; a non-destructive verb is remembered as its
    verb, so another ``touch`` runs with no card."""
    _server, cw, cr, factory, project_path, _stop = daemon_with_fake
    sid, leftover = await _open_chat(cw, cr, project_path)
    adapter = factory.adapters[0]

    await adapter.feed(_bash_ask(sid, "p1", granted))
    asked = await _drain_until(cr, _is_permission_required, seed=leftover)
    request = asked["params"]["request"]
    assert request["request_id"] == "p1"
    offered = {o["option_id"]: o["name"] for o in request["options"]}
    assert offered["allow_always"] == label
    await _send(cw, {"jsonrpc": "2.0", "id": asked["id"], "result": {"option_id": "allow_always"}})
    # A shell "always" never reaches the vendor as one: Alkera's rule carries it.
    async with asyncio.timeout(_FRAME_TIMEOUT_SECONDS):
        await adapter.wait_for_permission_reply("p1", "allow_once")

    await adapter.feed(_bash_ask(sid, "p2", covered))
    async with asyncio.timeout(_FRAME_TIMEOUT_SECONDS):
        await adapter.wait_for_permission_reply("p2", "allow_once")

    await adapter.feed(_bash_ask(sid, "p3", uncovered))
    card = await _drain_until(cr, _is_permission_required)
    # The first card after the grant is the uncovered command's: none for p2.
    assert card["params"]["request"]["request_id"] == "p3"
    await _send(cw, {"jsonrpc": "2.0", "id": card["id"], "result": {"option_id": "reject_once"}})
    async with asyncio.timeout(_FRAME_TIMEOUT_SECONDS):
        await adapter.wait_for_permission_reply("p3", "reject_once")


async def test_reopening_a_chat_whose_job_a_restart_lost_tells_the_editor_and_the_model(
    daemon_with_fake,
):
    """A local chat's background job dies with the daemon that ran it (a VS
    Code reload, a crash). Reopening the chat shows the editor an errored
    finish card saying so and wakes the model with the same words, once."""
    _server, cw, cr, factory, project_path, _stop = daemon_with_fake
    store = ProjectDirectory(project_path / ".alkera").chats()
    with store.create(session_id="lost-job", title="job", harness_type="alkera") as chat:
        chat.append_event(
            ToolCall(
                event_id="START",
                time=_T,
                session_id="lost-job",
                tool_call_id="bgjob:job_lost",
                message_id="",
                tool_name="bash",
                input={"command": "make build"},
                status="running",
            )
        )

    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "harness.open_chat",
            "params": {
                "project_path": str(project_path),
                "create": False,
                "session_id": "lost-job",
                "harness": "alkera",
            },
        },
    )

    def _finish_card(frame: dict[str, Any]) -> bool:
        event = frame.get("params", {}).get("event", {})
        return (
            frame.get("method") == "harness.event"
            and event.get("tool_call_id") == "bgdone:job_lost"
            and event.get("status") == "error"
            and "interrupted by a machine restart" in (event.get("error_text") or "")
        )

    await _drain_until(cr, _finish_card)
    # The open answers once the report is done: the model has been asked by then.
    await _drain_until(cr, lambda f: f.get("id") == 1)
    prompts = [p.text for a in factory.adapters for p in a.sent_prompts]
    assert len(prompts) == 1 and "job_lost (make build)" in prompts[0]
