"""Two lifecycle transcript cards for a BACKGROUND sql.query / bash job.

A backgrounded SQL/bash job renders as TWO "Background task" cards: a START
breadcrumb keyed ``bgjob:<id>`` (a RUNNING ``ToolCall`` published at SUBMIT where
the agent launched it, RESOLVED to completed — no result body — when the job ends),
and a separate FINISH card keyed ``bgdone:<id>`` (a self-contained ``ToolCall`` +
``ToolCallUpdate`` published on completion, carrying the native result) — a NEW id,
so it lands at the transcript tail when the job finishes. Drives a real
``ChatSession`` + ``EventBus`` (FakeAdapter) and submits jobs straight to the
registry, pinning:

* the START card opens RUNNING at submit and resolves to completed (no output) — it's
  a launch breadcrumb, always ``completed`` even on failure (it successfully started);
* the FINISH card (``bgdone:``) carries the native result — sql columns/rows, bash the
  BashResult dict — with the job's input (the query / command);
* the error card AND the cancelled→error mapping ride the FINISH card (no ``cancelled``
  tool status);
* the serialized output is byte-identical to the foreground serializer (incl. the
  ``$nonfinite`` sentinel a JSON float would otherwise lose).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.runtime import ChatSession, HarnessRuntime
from alkera_cli.plugins.plugin_base.bash_tool import BashResult
from alkera_cli.plugins.plugin_base.delivery import RESULT_INLINE_BYTE_CAP, SqlQueryResult
from alkera_cli.plugins.plugin_base.wire import model_facing_text, serialize_tool_result
from alkera_core.project.chats.blobs import BlobStore
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import (
    Event,
    ToolCall,
    ToolCallUpdate,
)

#: Real ChatSession pumps with wall-clock deadlines and ``to_thread`` hops: on
#: the shared session loop an earlier test can starve the executor and blow the
#: deadlines, so every test here runs on its own loop.
pytestmark = pytest.mark.asyncio(loop_scope="function")


async def _open(tmp_path: Path) -> tuple[HarnessRuntime, ChatSession]:
    factory = FakeAdapterFactory(lambda: FakeAdapter(reply_text="ok"), available=True)
    rt = HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory)
    session = await rt.open_chat(create=True, harness_type="agent")
    return rt, session


def _submit_sql_background(session: ChatSession, result: SqlQueryResult) -> str:
    """Launch a background SQL job that returns ``result`` at once, and hand back
    its job id (what the card keys are built from)."""

    async def _job() -> SqlQueryResult:
        return result

    job = session._background.submit(
        _job, kind="sql", title="q", input={"mode": "sql", "sql": "select 1"}
    )
    return str(job.job_id)


async def _collect_card(
    session: ChatSession,
    *,
    submit: Callable[[], str],
) -> tuple[list[ToolCall], list[ToolCallUpdate]]:
    """Submit a job (returns its job_id), pump the bus until the FINISH card's
    (``bgdone:<id>``) terminal ToolCallUpdate lands, and return ALL ToolCall /
    ToolCallUpdate frames (the START ``bgjob:<id>`` pair + the FINISH ``bgdone:<id>``
    pair) — tests partition them by prefix."""
    collected: list[Event] = []
    sub = session.subscribe()

    async def _pump() -> None:
        async for ev in sub:
            collected.append(ev)

    pump = asyncio.create_task(_pump())
    try:
        done = f"bgdone:{submit()}"
        for _ in range(250):
            if any(isinstance(e, ToolCallUpdate) and e.tool_call_id == done for e in collected):
                break
            await asyncio.sleep(0.02)
        calls = [e for e in collected if isinstance(e, ToolCall)]
        updates = [e for e in collected if isinstance(e, ToolCallUpdate)]
        return calls, updates
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await pump


#: The tag every injected background wake opens with.
_WAKE_TAG = "<backgrounded_tool_finished"


def _start(frames: list[Any]) -> list[Any]:
    return [f for f in frames if f.tool_call_id.startswith("bgjob:")]


def _finish(frames: list[Any]) -> list[Any]:
    return [f for f in frames if f.tool_call_id.startswith("bgdone:")]


async def test_background_sql_emits_completed_tool_card(tmp_path: Path) -> None:
    rt, session = await _open(tmp_path)
    result = SqlQueryResult(columns=["a"], preview_rows=[[1]], row_count=1, result_name="r")

    async def _job() -> SqlQueryResult:
        return result

    try:
        calls, updates = await _collect_card(
            session,
            submit=lambda: (
                session._background.submit(
                    _job, kind="sql", title="q", input={"mode": "sql", "sql": "select 1"}
                ).job_id
            ),
        )
        # START card (bgjob:): opens RUNNING at submit, then RESOLVES to completed with
        # NO output — a launch breadcrumb, not the result.
        start_calls, start_updates = _start(calls), _start(updates)
        assert start_calls[0].status == "running"
        assert start_calls[0].tool_name == "sql.query"
        assert start_calls[0].input.get("sql") == "select 1"
        assert start_updates[-1].status == "completed"
        assert start_updates[-1].output is None  # the breadcrumb carries no result

        # FINISH card (bgdone:): a NEW self-contained card carrying the native result.
        done_calls, done_updates = _finish(calls), _finish(updates)
        assert done_calls[0].status == "completed"
        assert done_calls[0].tool_name == "sql.query"
        assert done_calls[0].input.get("sql") == "select 1"
        done = done_updates[-1]
        assert done.status == "completed"
        parsed = json.loads(str(done.output))
        assert parsed["columns"] == ["a"]
        assert parsed["preview_rows"] == [[1]]
        assert done.error_text is None
    finally:
        await rt.close_chat(session.session_id)


async def test_background_bash_emits_completed_tool_card(tmp_path: Path) -> None:
    rt, session = await _open(tmp_path)
    result = BashResult(output="hello\n", exit_code=0)

    async def _job() -> BashResult:
        return result

    try:
        calls, updates = await _collect_card(
            session,
            submit=lambda: (
                session._background.submit(
                    _job, kind="bash", title="echo", input={"command": "echo hello", "cwd": "/x"}
                ).job_id
            ),
        )
        # START breadcrumb opens RUNNING at submit; the FINISH card carries the result.
        start_calls = _start(calls)
        assert start_calls[0].tool_name == "bash"
        assert start_calls[0].status == "running"
        assert start_calls[0].input.get("command") == "echo hello"
        done_calls, done_updates = _finish(calls), _finish(updates)
        assert done_calls[0].status == "completed"
        assert done_calls[0].input.get("command") == "echo hello"
        parsed = json.loads(str(done_updates[-1].output))
        assert parsed["output"] == "hello\n"
        assert parsed["exit_code"] == 0
    finally:
        await rt.close_chat(session.session_id)


async def test_background_failed_job_emits_error_card(tmp_path: Path) -> None:
    rt, session = await _open(tmp_path)

    async def _job() -> SqlQueryResult:
        raise RuntimeError("warehouse exploded")

    try:
        calls, updates = await _collect_card(
            session,
            submit=lambda: (
                session._background.submit(
                    _job, kind="sql", title="q", input={"mode": "sql", "sql": "select 1"}
                ).job_id
            ),
        )
        # The START breadcrumb opened running and RESOLVED to completed (it launched
        # fine — the FAILURE is the job's outcome, carried on the FINISH card).
        start_calls, start_updates = _start(calls), _start(updates)
        assert start_calls[0].status == "running"
        assert start_updates[-1].status == "completed"
        done_updates = _finish(updates)
        assert done_updates[0].status == "error"  # the finish card errors
        assert done_updates[0].output is None
        assert "warehouse exploded" in str(done_updates[0].error_text)
    finally:
        await rt.close_chat(session.session_id)


async def test_background_cancelled_job_emits_error_card_not_completed(tmp_path: Path) -> None:
    """A cancelled job (no ``cancelled`` tool status exists) surfaces as an errored
    card, never a misleading ``completed`` card with empty output."""
    rt, session = await _open(tmp_path)
    gate = asyncio.Event()

    async def _job() -> BashResult:
        await gate.wait()  # hang until cancelled
        return BashResult(output="never", exit_code=0)

    collected: list[Event] = []
    sub = session.subscribe()

    async def _pump() -> None:
        async for ev in sub:
            collected.append(ev)

    pump = asyncio.create_task(_pump())
    try:
        job = session._background.submit(_job, kind="bash", title="x", input={"command": "sleep"})
        done = f"bgdone:{job.job_id}"
        await session._background.cancel(job.job_id)
        for _ in range(250):
            if any(isinstance(e, ToolCallUpdate) and e.tool_call_id == done for e in collected):
                break
            await asyncio.sleep(0.02)
        updates = [e for e in collected if isinstance(e, ToolCallUpdate) and e.tool_call_id == done]
        assert updates and updates[0].status == "error"  # the FINISH card errors
        assert updates[0].output is None
    finally:
        pump.cancel()
        await rt.close_chat(session.session_id)


async def test_background_wake_matches_the_foreground_wire_for_a_bounded_result(
    tmp_path: Path,
) -> None:
    """Every real producer bounds its result through the door, so the wake must
    be byte-identical to the foreground dispatch wire for the same payload.
    Pinned at a near-cap size (where a wake that truncated or re-encoded would
    show) with CJK text and NaN cells, so a wake that escaped non-ASCII or
    nulled a non-finite float diverges."""
    from alkera_cli.plugins.plugin_base.blob_compute import build_rows_result
    from alkera_cli.plugins.plugin_base.tool import Tool, ToolRegistry, ToolSpec
    from pydantic import BaseModel

    blobs = ProjectDirectory(tmp_path / ".alkera").blobs()
    rows: list[list[object]] = [[i, f"{i:02d}-" + "あ" * 2000, math.nan] for i in range(30)]
    result = build_rows_result(blobs, ["id", "v", "n"], rows)
    assert result.truncated and result.blob is not None  # producer-bounded, near the cap

    class _CannedInput(BaseModel):
        pass

    class _CannedSql(Tool[_CannedInput, SqlQueryResult]):
        spec = ToolSpec(name="canned.sql", title="Canned", description="Returns a fixed result.")
        Input = _CannedInput
        Output = SqlQueryResult

        async def run(self, args: _CannedInput, ctx: object) -> SqlQueryResult:
            return result

    registry = ToolRegistry(blobs)
    registry.register(_CannedSql)
    foreground_wire = model_facing_text(await registry.dispatch("canned.sql", {}))

    rt, session = await _open(tmp_path)

    async def _job() -> SqlQueryResult:
        return result

    try:
        session._background.submit(
            _job, kind="sql", title="q", input={"mode": "sql", "sql": "select 1"}
        )
        adapter = session._adapter
        for _ in range(250):
            if any(_WAKE_TAG in p.text for p in adapter.sent_prompts):
                break
            await asyncio.sleep(0.02)
        tool_finishes = (p for p in adapter.sent_prompts if _WAKE_TAG in p.text)
        wake = next(tool_finishes)
        payload = wake.text.split("<result>\n", 1)[1].rsplit("\n</result>", 1)[0]
        assert payload == foreground_wire
    finally:
        await rt.close_chat(session.session_id)


async def test_an_irreducible_result_reaches_every_surface_as_the_same_wrap(
    tmp_path: Path,
) -> None:
    """A payload the door must wrap (a ~70,000-char column name makes the
    envelope irreducible) reaches the wake, the finish card, and a foreground
    dispatch as ONE string: the door's text wrap with a handle, rendered once
    and reused. A background render that bypassed the door would put 70KB of
    native bytes in the chat log and the injected prompt."""
    from alkera_cli.plugins.plugin_base.tool import Tool, ToolRegistry, ToolSpec
    from pydantic import BaseModel

    blobs = ProjectDirectory(tmp_path / ".alkera").blobs()
    result = SqlQueryResult(columns=["c" * 70_000], preview_rows=[], row_count=60, truncated=True)

    class _CannedInput(BaseModel):
        pass

    class _CannedSql(Tool[_CannedInput, SqlQueryResult]):
        spec = ToolSpec(name="sql.query", title="Canned", description="Returns a fixed result.")
        Input = _CannedInput
        Output = SqlQueryResult

        async def run(self, args: _CannedInput, ctx: object) -> SqlQueryResult:
            return result

    registry = ToolRegistry(blobs)
    registry.register(_CannedSql)
    foreground_wire = model_facing_text(await registry.dispatch("sql.query", {}))
    wrapped = json.loads(foreground_wire)
    assert wrapped["ref_type"] == "text"
    assert wrapped["blob"]["sha256"]

    rt, session = await _open(tmp_path)

    def _submit() -> str:
        return _submit_sql_background(session, result)

    try:
        _calls, updates = await _collect_card(session, submit=_submit)
        done = _finish(updates)[-1]
        adapter = session._adapter
        # The wake is injected independently of the finish card, so waiting for the
        # card is not waiting for the wake.
        for _ in range(250):
            if any(_WAKE_TAG in p.text for p in adapter.sent_prompts):
                break
            await asyncio.sleep(0.02)
        tool_finishes = (p for p in adapter.sent_prompts if _WAKE_TAG in p.text)
        wake = next(tool_finishes)
        payload = wake.text.split("<result>\n", 1)[1].rsplit("\n</result>", 1)[0]
        assert payload == str(done.output)  # rendered once, reused on both surfaces
        assert payload == foreground_wire
    finally:
        await rt.close_chat(session.session_id)


async def test_a_dead_store_errors_the_finish_card_but_keeps_the_shaped_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the store dead before submit, an oversized result cannot spill: the
    finish card must surface the delivery failure as an errored card whose output
    still carries the door's shaped body (bounded preview + the result's note,
    with the transport error flag stripped from the encoded text), and the wake
    must reuse those exact bytes. A completed card wrapping an error body would
    hide the failure from every surface that routes on status."""

    def _boom(self: object, data: object) -> tuple[str, int]:
        raise OSError("disk full")

    monkeypatch.setattr(BlobStore, "write", _boom)
    rt, session = await _open(tmp_path)
    result = SqlQueryResult(
        columns=["c" * 70_000],
        preview_rows=[],
        row_count=60,
        truncated=True,
        note="Do not repeat the write.",
    )
    encoded = model_facing_text(serialize_tool_result(result))
    assert len(encoded.encode()) > RESULT_INLINE_BYTE_CAP  # must need the store

    def _submit() -> str:
        return _submit_sql_background(session, result)

    try:
        _calls, updates = await _collect_card(session, submit=_submit)
        done = _finish(updates)[-1]
        assert done.status == "error"
        assert done.error_text and "blob store" in str(done.error_text)
        parsed: dict[str, Any] = json.loads(str(done.output))
        assert "_alkera_tool_error" not in parsed  # a transport signal, not content
        assert parsed["truncated"] is True
        assert parsed["preview"] and encoded.startswith(parsed["preview"])
        assert parsed["note"] == "Do not repeat the write."
        adapter = session._adapter

        wake = None
        for _ in range(250):
            tool_finishes = (p for p in adapter.sent_prompts if _WAKE_TAG in p.text)
            wake = next(tool_finishes, None)
            if wake is not None:
                break
            await asyncio.sleep(0.02)
        assert wake is not None, "the wake never fired"

        payload = wake.text.split("<result>\n", 1)[1].rsplit("\n</result>", 1)[0]
        assert payload == str(done.output)
    finally:
        await rt.close_chat(session.session_id)


async def test_a_retried_wake_reuses_the_persisted_cards_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wake that fails to send is requeued; the retry must reuse the bytes the
    finish card already persisted, not re-render. Re-rendering re-enters the
    door, and with the store dead in between it produces a different string than
    the card the user is looking at."""
    rt, session = await _open(tmp_path)
    result = SqlQueryResult(columns=["c" * 70_000], preview_rows=[], row_count=60, truncated=True)

    def _submit() -> str:
        return _submit_sql_background(session, result)

    def _boom(self: object, data: object) -> tuple[str, int]:
        raise OSError("disk full")

    try:
        adapter = session._adapter
        # The card renders against a live store; only the wake that follows fails.
        adapter.fail_next_prompt = True
        _calls, updates = await _collect_card(session, submit=_submit)
        done = _finish(updates)[-1]
        assert done.output is not None
        # The consumed one-shot flag is the failed wake: it raised before the
        # prompt was recorded, so no wake prompt may exist yet.
        for _ in range(250):
            if not adapter.fail_next_prompt:
                break
            await asyncio.sleep(0.02)
        assert not adapter.fail_next_prompt, "the wake never reached the adapter"
        assert not any(_WAKE_TAG in p.text for p in adapter.sent_prompts)
        # The store dies between the failed wake and the retry.
        monkeypatch.setattr(BlobStore, "write", _boom)
        # The production retry edge is the next turn ENDING: the turn-state pump
        # flushes queued wakes on a terminal stamped with the attempt it is waiting
        # on, and the failed wake left no attempt behind to stamp.
        await session.send_prompt("meanwhile the user types")
        wake = None
        for _ in range(250):
            tool_finishes = (p for p in adapter.sent_prompts if _WAKE_TAG in p.text)
            wake = next(tool_finishes, None)
            if wake is not None:
                break
            await asyncio.sleep(0.02)
        assert wake is not None, "the requeued wake never fired"
        payload = wake.text.split("<result>\n", 1)[1].rsplit("\n</result>", 1)[0]
        assert payload == str(done.output)
    finally:
        await rt.close_chat(session.session_id)


async def test_background_card_output_matches_foreground_serializer(tmp_path: Path) -> None:
    """The card's output is byte-identical to the foreground one — same serializer AND
    same encoder. The serializer keeps a non-finite float as the recoverable
    ``$nonfinite`` sentinel that model_dump_json() would have lost to null; the encoder
    is the one the transports send, which writes a Russian column name as itself. A
    second encoder here rendered the same result two ways depending on which card the
    reader was looking at."""
    rt, session = await _open(tmp_path)
    result = SqlQueryResult(columns=["количество"], preview_rows=[[math.inf]], row_count=1)

    async def _job() -> SqlQueryResult:
        return result

    try:
        _calls, updates = await _collect_card(
            session,
            submit=lambda: (
                session._background.submit(
                    _job, kind="sql", title="q", input={"mode": "sql", "sql": "select 1"}
                ).job_id
            ),
        )
        done = _finish(updates)[-1]  # the result rides the FINISH card
        assert str(done.output) == model_facing_text(serialize_tool_result(result))
        assert "количество" in str(done.output)  # readable, not a \\u escape
        # And the sentinel survived (a plain JSON dump would have written null).
        reparsed: dict[str, Any] = json.loads(str(done.output))
        assert reparsed["preview_rows"][0][0] == {"$nonfinite": "inf"}
    finally:
        await rt.close_chat(session.session_id)
