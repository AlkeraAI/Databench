"""Turn settlement in the runtime, and the context-overflow recovery that is its
hardest case: an `overflow=True` error on a ROOT session auto-compacts and
re-sends the SAME prompt rather than surfacing the raw "prompt too long".

Driven against a `FakeAdapter`, whose `feed` publishes verbatim, so a status
carries the attempt this file gives it and nothing else.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from _adapter_factory import WAKE_TAG, fake_runtime, finished_sql_job
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.adapter import HarnessCrashError, PromptInput
from alkera_cli.harness.runtime import ChatSession
from alkera_core.schemas.chat import (
    CompactionApplied,
    Heartbeat,
    SessionStatusChanged,
)

_T = datetime(2026, 6, 29, tzinfo=UTC)


class _SilentCompactAdapter(FakeAdapter):
    """``compact()`` is fired and forgotten, so a test can publish the compaction's
    frames one at a time and drive the recovery's wait step by step."""

    async def compact(self) -> None:
        self._compacts += 1


class _SlowResendAdapter(_SilentCompactAdapter):
    """The re-send's transport is slow enough for a cancel to land inside it, and
    ``cancel()`` synthesizes a terminal as a real adapter does. Each cancel records
    how many prompts the transport had accepted, so a test can tell one that
    stopped the re-sent turn from one that only beat it."""

    def __init__(self) -> None:
        super().__init__()
        self.resending = asyncio.Event()
        self.release = asyncio.Event()
        self.cancel_marks: list[int] = []

    async def send_prompt(self, prompt: PromptInput) -> None:
        if len(self._sent_prompts) == 1:  # the overflow re-send
            self.resending.set()
            await self.release.wait()
        await super().send_prompt(prompt)

    async def cancel(self) -> None:
        await super().cancel()
        self.cancel_marks.append(len(self._sent_prompts))
        await self._bus.publish(self._status("ab", "aborted", self._last_turn_id()))


async def _open(
    tmp_path: Path, make=FakeAdapter
) -> tuple[HarnessRuntime, ChatSession, FakeAdapter]:
    """A root chat, open, with the adapter backing it."""
    rt, factory = fake_runtime(tmp_path, make)
    chat = rt._chats_store.create(title="c", harness_type="agent")
    sid = chat.session_id
    chat.close()
    session = await rt.open_chat(sid)
    return rt, session, factory.adapters[-1]


async def _status(adapter: FakeAdapter, status: str, *, turn_id: str | None = None, **kw) -> None:
    await adapter.feed(
        SessionStatusChanged(
            event_id=f"ev-{status}", time=_T, session_id="s", status=status, turn_id=turn_id, **kw
        )
    )


async def _overflow_error(adapter: FakeAdapter) -> None:
    await _status(
        adapter,
        "error",
        phase="error",
        detail="upstream error 400: prompt is too long: 9 > 8",
        overflow=True,
    )


async def _wait_until(predicate, *, timeout_s: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


async def _into_the_compaction_wait(adapter: FakeAdapter) -> str:
    """Overflow the turn in flight and wait until the recovery has asked for the
    compaction, which is where every interruption test starts. Returns the attempt
    that overflowed, the one the recovery is bound to."""
    overflowed = adapter.sent_prompts[0].turn_id
    await _overflow_error(adapter)
    await _wait_until(lambda: adapter.compact_count == 1)
    return overflowed


def _settlements(session: ChatSession) -> list[SessionStatusChanged | None]:
    """Every settlement still in the session's bounded history, in turn order.
    `None` where the runtime settled a turn itself (a cancel, a clear) and no
    terminal carried it."""
    out: list[SessionStatusChanged | None] = []
    for i in range(session.turn_settlements):
        with contextlib.suppress(IndexError):  # pruned past the history bound
            out.append(session.settlement(i))
    return out


def _is_giveup(settled: SessionStatusChanged | None, attempt: str) -> bool:
    """The notice the recovery settles a turn with when it cannot finish: an error
    the UI renders normally (``overflow`` cleared, so it is not re-recovered),
    naming the attempt that overflowed."""
    return (
        settled is not None
        and settled.status == "error"
        and not settled.overflow
        and settled.turn_id == attempt
    )


# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_overflow_compacts_once_and_resends_the_same_prompt(tmp_path: Path) -> None:
    rt, session, adapter = await _open(tmp_path)
    try:
        await session.send_prompt("do the thing")
        await _overflow_error(adapter)

        await _wait_until(lambda: adapter.compact_count == 1 and len(adapter.sent_prompts) == 2)
        # Exactly one compaction, and the ORIGINAL prompt re-sent verbatim.
        assert adapter.compact_count == 1
        assert adapter.sent_prompts[1].text == "do the thing"
    finally:
        await rt.close_chat(session.session_id)


@pytest.mark.asyncio
async def test_recovery_is_one_shot_then_surfaces_giveup(tmp_path: Path) -> None:
    rt, session, adapter = await _open(tmp_path)
    try:
        await session.send_prompt("do the thing")

        # First overflow → recover (compact #1, re-send #2).
        await _overflow_error(adapter)
        await _wait_until(lambda: len(adapter.sent_prompts) == 2)
        resent = adapter.sent_prompts[1].turn_id

        # The re-sent turn STILL overflows → budget spent → give up, no 2nd compaction.
        await _overflow_error(adapter)
        await _wait_until(lambda: session.turn_settlements == 1)
        assert adapter.compact_count == 1  # NOT compacted a second time
        assert len(adapter.sent_prompts) == 2  # NOT re-sent a third time

        # The turn settles ON the clean notice. The raw overflow the runtime
        # suppressed never becomes the record a consumer reads its outcome from.
        assert _is_giveup(_settlements(session)[0], resent)
    finally:
        await rt.close_chat(session.session_id)


@pytest.mark.asyncio
async def test_non_overflow_error_does_not_trigger_recovery(tmp_path: Path) -> None:
    rt, session, adapter = await _open(tmp_path)
    try:
        await session.send_prompt("do the thing")
        await _status(adapter, "error", phase="error", detail="boom")
        # Give the loop time to (not) act.
        await asyncio.sleep(0.1)
        assert adapter.compact_count == 0
        assert len(adapter.sent_prompts) == 1  # no re-send
    finally:
        await rt.close_chat(session.session_id)


@pytest.mark.asyncio
async def test_budget_resets_on_a_genuine_new_prompt(tmp_path: Path) -> None:
    rt, session, adapter = await _open(tmp_path)
    try:
        # Turn 1: overflow → recover, then the re-sent turn succeeds (idle).
        await session.send_prompt("first task")
        await _overflow_error(adapter)
        await _wait_until(lambda: adapter.compact_count == 1 and len(adapter.sent_prompts) == 2)
        await _status(adapter, "idle")

        # Turn 2 (a fresh user prompt) overflows too — the budget reset, so it recovers
        # AGAIN (a per-turn cap, not a per-session one).
        await session.send_prompt("second task")
        await _wait_until(lambda: len(adapter.sent_prompts) == 3)
        await _overflow_error(adapter)
        await _wait_until(lambda: adapter.compact_count == 2 and len(adapter.sent_prompts) == 4)
        assert adapter.sent_prompts[3].text == "second task"
    finally:
        await rt.close_chat(session.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("late", ["idle", "error", "aborted"])
async def test_only_a_different_attempts_terminal_is_inert(tmp_path: Path, late: str) -> None:
    """A terminal naming a superseded attempt never settles the live turn; one
    naming nobody does."""
    rt, session, adapter = await _open(tmp_path)
    try:
        await session.send_prompt("first")
        superseded = adapter.sent_prompts[0].turn_id

        await _status(adapter, late, turn_id=superseded)
        await session.send_prompt("second")
        await asyncio.sleep(0.1)
        assert session.turn_active, "the older terminal's flush cleared a newer attempt"

        await _status(adapter, late, turn_id=superseded)
        await asyncio.sleep(0.1)
        assert session.turn_active

        await _status(adapter, "aborted")  # unstamped
        await _wait_until(lambda: not session.turn_active)
    finally:
        await rt.close_chat(session.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_dies", [False, True], ids=["silent", "raising"])
async def test_a_user_cancel_releases_the_latch_whatever_the_adapter_says(
    tmp_path: Path, adapter_dies: bool
) -> None:
    """A user cancel settles the turn whether the adapter publishes nothing or
    dies inside the call."""

    class _CancelFails(FakeAdapter):
        async def cancel(self) -> None:
            await super().cancel()
            raise HarnessCrashError("the transport died on the way down")

    rt, session, adapter = await _open(tmp_path, _CancelFails if adapter_dies else FakeAdapter)
    try:
        await session.send_prompt("go")
        assert session.turn_active
        before = session.turn_settlements

        if adapter_dies:
            with pytest.raises(HarnessCrashError):
                await session.cancel()
        else:
            await session.cancel()

        assert not session.turn_active
        assert session.turn_settlements == before + 1
        assert adapter.cancel_count == 1
    finally:
        await rt.close_chat(session.session_id)


@pytest.mark.asyncio
async def test_a_clear_settles_the_turn_it_was_asked_on_and_no_other(
    tmp_path: Path,
) -> None:
    """A clear settles the turn live when it was asked, never the one dequeued
    under its event later."""
    rt, session, adapter = await _open(tmp_path)
    try:
        await session.send_prompt("the cleared one")
        await session.clear()

        assert not session.turn_active
        assert _settlements(session) == [None]
        assert adapter.clear_count == 1

        await session.send_prompt("a fresh start")
        session._background.submit(finished_sql_job, kind="sql", title="q", input={"mode": "sql"})
        await _wait_until(lambda: bool(session._pending_background))
        await asyncio.sleep(0.1)  # long enough for the clear's event to be dequeued

        assert session.turn_active, "the clear's event settled the turn typed after it"
        assert session.turn_settlements == 1
        assert not any(WAKE_TAG in p.text for p in adapter.sent_prompts)

        await _status(adapter, "idle", turn_id=adapter.sent_prompts[-1].turn_id)
        await _wait_until(lambda: any(WAKE_TAG in p.text for p in adapter.sent_prompts))
        assert session.turn_settlements == 2
    finally:
        await rt.close_chat(session.session_id)


@pytest.mark.asyncio
async def test_a_cancel_during_the_compaction_wait_ends_the_turn(tmp_path: Path) -> None:
    """A cancel inside the compaction wait ends the turn without re-sending the
    abandoned prompt."""
    rt, session, adapter = await _open(tmp_path, _SilentCompactAdapter)
    try:
        await session.send_prompt("do the thing")
        overflowed = await _into_the_compaction_wait(adapter)

        await _status(adapter, "aborted", turn_id=overflowed)
        await _wait_until(lambda: not session.turn_active)

        assert len(adapter.sent_prompts) == 1
        # The turn the user ended reads as ended, never as the raw overflow it was
        # recovering from and never as the give-up it never reached.
        settled = _settlements(session)
        assert [s.status for s in settled if s is not None] == ["aborted"]
        assert not _is_giveup(settled[0], overflowed)
    finally:
        await rt.close_chat(session.session_id)


@pytest.mark.asyncio
async def test_a_prompt_sent_while_the_transport_swaps_queues_behind_it(
    tmp_path: Path,
) -> None:
    """A prompt typed while the clear swaps the transport queues behind the whole
    swap and still runs."""

    class _SlowClear(FakeAdapter):
        clearing = asyncio.Event()
        release = asyncio.Event()

        async def clear(self) -> None:
            self.clearing.set()
            await self.release.wait()
            await super().clear()

    rt, session, adapter = await _open(tmp_path, _SlowClear)
    try:
        await session.send_prompt("the cleared one")
        clearing = asyncio.create_task(session.clear())
        await asyncio.wait_for(adapter.clearing.wait(), timeout=3.0)

        sending = asyncio.create_task(session.send_prompt("typed mid-swap"))
        await asyncio.sleep(0.1)
        assert len(adapter.sent_prompts) == 1, "B registered into the transport being reset"

        adapter.release.set()
        await asyncio.wait_for(clearing, timeout=3.0)
        live = await asyncio.wait_for(sending, timeout=3.0)

        assert _settlements(session) == [None]  # A settled once, by the clear
        assert session.turn_active  # ...and B is running, not lost
        await _status(adapter, "idle", turn_id=live)
        await _wait_until(lambda: session.turn_settlements == 2)
        assert _settlements(session)[1].turn_id == live
    finally:
        await rt.close_chat(session.session_id)


@pytest.mark.asyncio
async def test_a_giveup_never_surfaces_on_a_turn_the_user_already_ended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The give-up is never published on a turn the user already ended."""
    monkeypatch.setattr("alkera_cli.harness.runtime.OVERFLOW_COMPACTION_TIMEOUT_SECONDS", 0.2)
    rt, session, adapter = await _open(tmp_path, _SilentCompactAdapter)
    sub = session.subscribe()
    try:
        await session.send_prompt("do the thing")
        overflowed = await _into_the_compaction_wait(adapter)

        await session.cancel()  # the fake publishes nothing; the runtime settles
        await asyncio.sleep(0.4)  # well past the compaction deadline

        assert _settlements(session) == [None]
        assert len(adapter.sent_prompts) == 1
        published = []
        while True:
            try:
                async with asyncio.timeout(0.05):
                    published.append(await anext(sub))
            except (TimeoutError, StopAsyncIteration):
                break
        assert not any(_is_giveup(e, overflowed) for e in published)
    finally:
        await rt.close_chat(session.session_id)


@pytest.mark.asyncio
async def test_a_clear_that_fails_leaves_the_turn_running(tmp_path: Path) -> None:
    """A clear the adapter refuses raises to the caller and leaves the turn
    running."""

    class _ClearFails(_SilentCompactAdapter):
        async def clear(self) -> None:
            raise HarnessCrashError("the transport refused the clear")

    rt, session, adapter = await _open(tmp_path, _ClearFails)
    try:
        await session.send_prompt("do the thing")
        overflowed = await _into_the_compaction_wait(adapter)

        with pytest.raises(HarnessCrashError):
            await session.clear()

        # The recovery never heard of it: it settles the compaction and re-sends.
        await adapter.feed(CompactionApplied(event_id="ca", time=_T, session_id="s"))
        await _status(adapter, "idle", turn_id=overflowed)
        await _wait_until(lambda: len(adapter.sent_prompts) == 2)

        resent = adapter.sent_prompts[1].turn_id
        await _status(adapter, "idle", turn_id=resent)
        await _wait_until(lambda: session.turn_settlements == 1)
        assert _settlements(session) == [None] or not _is_giveup(
            _settlements(session)[0], overflowed
        )
    finally:
        await rt.close_chat(session.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settled_by", "status"),
    [("recovery", "error"), ("turn loop", "idle"), ("clear", None)],
    ids=["recovery", "turn loop", "clear"],
)
async def test_a_prompt_during_the_compaction_wait_takes_over_the_chat(
    tmp_path: Path, settled_by: str, status: str | None
) -> None:
    """A prompt typed inside the compaction wait takes over the chat and settles
    on its own terminal, whichever side of the handover reads it."""
    rt, session, adapter = await _open(tmp_path, _SilentCompactAdapter)
    try:
        await session.send_prompt("the overflowing one")
        overflowed = await _into_the_compaction_wait(adapter)

        await session.send_prompt("never mind, do this instead")
        live = adapter.sent_prompts[1].turn_id
        if settled_by == "turn loop":
            # The recovery yields on the compaction's own frames, so the new turn's
            # terminal reaches the caller's loop instead.
            await adapter.feed(CompactionApplied(event_id="ca", time=_T, session_id="s"))
            await _status(adapter, "idle", turn_id=overflowed)
            await asyncio.sleep(0.1)
            assert session.turn_active, "the abandoned recovery took the new turn's latch"
        if status is None:
            await session.clear()
        else:
            await _status(adapter, status, turn_id=live)

        await _wait_until(lambda: not session.turn_active)
        assert [p.text for p in adapter.sent_prompts] == [
            "the overflowing one",
            "never mind, do this instead",
        ]
        settled = _settlements(session)[-1]
        if status is None:
            assert settled is None  # the clear carried no terminal of its own
        else:
            assert (settled.status, settled.turn_id) == (status, live)
    finally:
        await rt.close_chat(session.session_id)


@pytest.mark.asyncio
async def test_the_resend_waits_for_the_compactions_own_terminal(tmp_path: Path) -> None:
    """The re-send waits for the compaction's own trailing terminal."""
    rt, session, adapter = await _open(tmp_path, _SilentCompactAdapter)
    try:
        await session.send_prompt("do the thing")
        overflowed = await _into_the_compaction_wait(adapter)

        await adapter.feed(CompactionApplied(event_id="ca", time=_T, session_id="s"))
        await asyncio.sleep(0.1)
        assert len(adapter.sent_prompts) == 1

        await _status(adapter, "idle", turn_id=overflowed)  # the compaction's terminal
        await _wait_until(lambda: len(adapter.sent_prompts) == 2)
        assert session.turn_active

        await _status(adapter, "idle", turn_id=adapter.sent_prompts[1].turn_id)
        await _wait_until(lambda: not session.turn_active)
    finally:
        await rt.close_chat(session.session_id)


@pytest.mark.asyncio
async def test_a_resend_that_never_fires_still_settles_the_turn(tmp_path: Path) -> None:
    """A re-send that raises still settles the turn with the give-up and releases
    the latch."""
    rt, session, adapter = await _open(tmp_path, _SilentCompactAdapter)
    try:
        await session.send_prompt("do the thing")
        overflowed = adapter.sent_prompts[0].turn_id
        session._background.submit(finished_sql_job, kind="sql", title="q", input={"mode": "sql"})
        await _wait_until(lambda: bool(session._pending_background))

        await _overflow_error(adapter)
        await _wait_until(lambda: adapter.compact_count == 1)
        adapter.fail_next_prompt = True
        await adapter.feed(CompactionApplied(event_id="ca", time=_T, session_id="s"))
        await _status(adapter, "idle", turn_id=overflowed)

        await _wait_until(lambda: session.turn_settlements == 1)
        assert _is_giveup(_settlements(session)[0], overflowed)
        await _wait_until(lambda: any(WAKE_TAG in p.text for p in adapter.sent_prompts))
        assert adapter.sent_prompts[-1].turn_id != overflowed
    finally:
        await rt.close_chat(session.session_id)


@pytest.mark.asyncio
async def test_a_cancel_during_the_resends_transport_stops_the_resent_turn(
    tmp_path: Path,
) -> None:
    """A cancel that lands inside the re-send's transport stops the re-sent turn,
    which settles exactly once."""
    rt, session, adapter = await _open(tmp_path, _SlowResendAdapter)
    try:
        await session.send_prompt("do the thing")
        overflowed = await _into_the_compaction_wait(adapter)
        await adapter.feed(CompactionApplied(event_id="ca", time=_T, session_id="s"))
        await _status(adapter, "idle", turn_id=overflowed)
        await asyncio.wait_for(adapter.resending.wait(), timeout=3.0)

        cancelling = asyncio.create_task(session.cancel())
        await asyncio.sleep(0.05)
        adapter.release.set()
        await asyncio.wait_for(cancelling, timeout=3.0)

        # A cancel mark of 2 means the transport was cancelled AFTER accepting the
        # re-send, so the withdrawn turn was stopped, not left running.
        assert 2 in adapter.cancel_marks, "the withdrawn turn was left running"
        assert len(adapter.sent_prompts) == 2
        await _wait_until(lambda: not session.turn_active)

        # The cancel's finally and the loop's `aborted` race to settle the re-sent
        # turn; either reading says cancelled, and it settles ONCE.
        resent = adapter.sent_prompts[1].turn_id
        settled = _settlements(session)
        assert len(settled) == 1
        assert settled[0] is None or (settled[0].status, settled[0].turn_id) == (
            "aborted",
            resent,
        )
    finally:
        await rt.close_chat(session.session_id)


@pytest.mark.asyncio
async def test_a_terminal_queued_before_the_turn_started_cannot_settle_it(
    tmp_path: Path,
) -> None:
    """An unstamped terminal published before the turn started cannot settle it."""

    class _GatedCompact(FakeAdapter):
        compacting = asyncio.Event()
        release = asyncio.Event()

        async def compact(self) -> None:
            self._compacts += 1
            self.compacting.set()
            await self.release.wait()

    rt, session, adapter = await _open(tmp_path, _GatedCompact)
    try:
        await session.send_prompt("the overflowing one")
        await _overflow_error(adapter)
        await asyncio.wait_for(adapter.compacting.wait(), timeout=3.0)

        # Nobody is reading the stream while the compaction is held, so this stays
        # queued; the prompt after it starts a turn the queue predates.
        await _status(adapter, "aborted")  # unstamped
        live = await session.send_prompt("typed while it churned")
        adapter.release.set()
        await asyncio.sleep(0.2)

        assert session.turn_active, "a terminal from before the turn settled it"
        assert session.turn_settlements == 0

        await _status(adapter, "idle", turn_id=live)
        await _wait_until(lambda: session.turn_settlements == 1)
        assert _settlements(session)[0].turn_id == live
    finally:
        await rt.close_chat(session.session_id)


@pytest.mark.asyncio
async def test_the_settlement_ledger_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The settlement ledger keeps a bounded window with absolute indices, and
    reaching past it raises."""
    monkeypatch.setattr("alkera_cli.harness.runtime._SETTLEMENT_HISTORY", 4)
    rt, session, adapter = await _open(tmp_path)
    try:
        for i in range(6):
            await session.send_prompt(f"turn {i}")
            await _status(adapter, "idle", turn_id=adapter.sent_prompts[-1].turn_id)
            await _wait_until(lambda n=i: session.turn_settlements == n + 1)

        assert session.turn_settlements == 6  # the count is of turns, not of rows
        with pytest.raises(IndexError):
            session.settlement(0)
        assert session.settlement(5).turn_id == adapter.sent_prompts[-1].turn_id
        assert len(_settlements(session)) == 4
    finally:
        await rt.close_chat(session.session_id)


@pytest.mark.asyncio
async def test_subagent_session_does_not_self_recover(tmp_path: Path) -> None:
    # A subagent (child) session must NOT drive overflow recovery from its turn-state
    # loop — that would race `_run_subagent_turn`. Subagents rely on proactive native
    # compaction + opencode's own in-stream recovery instead.
    rt, factory = fake_runtime(tmp_path)
    parent = rt._chats_store.create(title="p", harness_type="agent")
    psid = parent.session_id
    parent.close()
    child_manifest = await rt.new_chat(harness_type="agent", parent_session_id=psid)
    child = await rt.open_chat(child_manifest.session_id)
    try:
        assert child.is_subagent
        await child.send_prompt("explore the repo")
        adapter = factory.adapters[-1]
        await _overflow_error(adapter)
        await asyncio.sleep(0.1)
        assert adapter.compact_count == 0  # the harness did NOT compact a subagent
        assert len(adapter.sent_prompts) == 1
    finally:
        await rt.close_chat(child_manifest.session_id)


# ---------------------------------------------------------------------------
# A compaction that is still working is never abandoned
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_compaction_that_keeps_talking_is_never_abandoned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Summarizing a near-full window on a slow model takes far longer than any
    one wait. What the recovery bounds is SILENCE: a compaction that keeps
    publishing keeps its turn, however long the whole of it takes."""
    monkeypatch.setattr("alkera_cli.harness.runtime.OVERFLOW_COMPACTION_TIMEOUT_SECONDS", 0.2)
    rt, session, adapter = await _open(tmp_path, _SilentCompactAdapter)
    try:
        await session.send_prompt("summarize the warehouse")
        overflowed = await _into_the_compaction_wait(adapter)

        # Well past a 0.2s total deadline, but never 0.2s without a word.
        for _ in range(8):
            await asyncio.sleep(0.1)
            await _status(adapter, "running", turn_id=overflowed)

        await adapter.feed(CompactionApplied(event_id="ca", time=_T, session_id="s"))
        await _status(adapter, "idle", turn_id=overflowed)
        await _wait_until(lambda: len(adapter.sent_prompts) == 2)

        resent = adapter.sent_prompts[1].turn_id
        await _status(adapter, "idle", turn_id=resent)
        await _wait_until(lambda: session.turn_settlements == 1)
        assert not _is_giveup(_settlements(session)[0], overflowed)
    finally:
        await rt.close_chat(session.session_id)


@pytest.mark.asyncio
async def test_a_silent_compaction_is_abandoned_once_the_window_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side of the same bound: a compaction that says nothing at all for
    the whole window is wedged, and the reader is told rather than left waiting."""
    monkeypatch.setattr("alkera_cli.harness.runtime.OVERFLOW_COMPACTION_TIMEOUT_SECONDS", 0.2)
    rt, session, adapter = await _open(tmp_path, _SilentCompactAdapter)
    try:
        await session.send_prompt("do the thing")
        overflowed = await _into_the_compaction_wait(adapter)

        await _wait_until(lambda: session.turn_settlements == 1, timeout_s=3.0)

        assert _is_giveup(_settlements(session)[0], overflowed)
        assert len(adapter.sent_prompts) == 1
    finally:
        await rt.close_chat(session.session_id)


@pytest.mark.asyncio
async def test_the_liveness_tick_does_not_buy_a_wedged_compaction_another_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real harness beats the event stream every 10 seconds whether or not
    anything is working, and the beat lands on the very subscription the wait
    reads. A compaction that says nothing but is beaten at is still wedged, so
    the window has to run out and the reader has to be told."""
    monkeypatch.setattr("alkera_cli.harness.runtime.OVERFLOW_COMPACTION_TIMEOUT_SECONDS", 0.2)
    rt, session, adapter = await _open(tmp_path, _SilentCompactAdapter)
    try:
        await session.send_prompt("do the thing")
        overflowed = await _into_the_compaction_wait(adapter)

        async def _beat() -> None:
            while True:
                await asyncio.sleep(0.05)
                await adapter.feed(
                    Heartbeat(event_id="hb", time=_T, session_id="s", last_activity_ms=0)
                )

        beating = asyncio.create_task(_beat())
        try:
            await _wait_until(lambda: session.turn_settlements == 1, timeout_s=3.0)
        finally:
            beating.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beating

        assert _is_giveup(_settlements(session)[0], overflowed)
        assert len(adapter.sent_prompts) == 1
    finally:
        await rt.close_chat(session.session_id)


@pytest.mark.asyncio
async def test_the_compaction_wait_can_be_removed_entirely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the window removed, a compaction is bounded by the request itself —
    the deployment asked for exactly that, and no clock overrules it."""
    monkeypatch.setattr("alkera_cli.harness.runtime.OVERFLOW_COMPACTION_TIMEOUT_SECONDS", None)
    rt, session, adapter = await _open(tmp_path, _SilentCompactAdapter)
    try:
        await session.send_prompt("do the thing")
        overflowed = await _into_the_compaction_wait(adapter)

        await asyncio.sleep(0.5)  # silent throughout
        assert session.turn_settlements == 0

        await adapter.feed(CompactionApplied(event_id="ca", time=_T, session_id="s"))
        await _status(adapter, "idle", turn_id=overflowed)
        await _wait_until(lambda: len(adapter.sent_prompts) == 2)
    finally:
        await rt.close_chat(session.session_id)


@pytest.mark.asyncio
async def test_the_retry_budget_can_be_removed_so_compaction_keeps_trying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One retry is a loop guard, not a law: with the budget removed a second
    overflow compacts again instead of surfacing "could not free space"."""
    monkeypatch.setattr("alkera_cli.harness.runtime.MAX_OVERFLOW_RETRIES", None)
    rt, session, adapter = await _open(tmp_path, _SilentCompactAdapter)
    try:
        await session.send_prompt("do the thing")
        overflowed = await _into_the_compaction_wait(adapter)
        await adapter.feed(CompactionApplied(event_id="ca", time=_T, session_id="s"))
        await _status(adapter, "idle", turn_id=overflowed)
        await _wait_until(lambda: len(adapter.sent_prompts) == 2)

        resent = adapter.sent_prompts[1].turn_id
        await _status(
            adapter,
            "error",
            turn_id=resent,
            phase="error",
            detail="upstream error 400: prompt is too long: 9 > 8",
            overflow=True,
        )
        await _wait_until(lambda: adapter.compact_count == 2)

        assert session.turn_settlements == 0
    finally:
        await rt.close_chat(session.session_id)
