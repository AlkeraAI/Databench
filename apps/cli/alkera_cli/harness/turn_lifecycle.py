"""Foreground turn lifecycle and settlement ownership."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, TypeGuard

from alkera_core.schemas.chat import (
    CompactionApplied,
    ConversationCleared,
    Event,
    Heartbeat,
    SessionStatusChanged,
)

from alkera_cli.harness.adapter import HarnessAdapter, PromptInput
from alkera_cli.harness.background import BackgroundJob
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.model_retry import ModelRetryBudget

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({"idle", "error", "completed", "aborted"})


class SendPromptLocked(Protocol):
    """Callback shape for composing a prompt while the turn lock is already held."""

    async def __call__(self, prompt: ReplayPrompt, *, overflow_retry: bool = False) -> str: ...


@dataclass(frozen=True)
class ReplayPrompt:
    """Prompt inputs retained for a possible overflow replay."""

    text: str
    model: dict[str, str] | None
    variant: str | None
    parts: list[dict[str, object]] | None
    system_addendum: str | None
    #: Agent-only context for this one turn (a channel briefing, a saved report's
    #: brief). Delivered on the hidden system channel, so the harness's echo of
    #: the prompt (what every transcript reader shows) carries ``text`` alone.
    context: str | None = None


@dataclass(frozen=True)
class TurnLifecycleConfig:
    """Dependencies the lifecycle needs from its owning chat session."""

    adapter: HarnessAdapter
    bus: EventBus
    session_id: str
    is_closed: Callable[[], bool]
    is_subagent: Callable[[], bool]
    compact: Callable[[], Awaitable[None]]
    send_prompt_locked: SendPromptLocked
    inject_background_results: Callable[[list[tuple[BackgroundJob, str]]], Awaitable[bool]]
    #: ``None`` keeps every settlement for the life of the session.
    settlement_history: int | None
    max_overflow_retries: int | None
    overflow_compaction_timeout_seconds: float | None
    # Observation seams for the owning session: every event seen while an attempt
    # is live, and the settling edge itself (under the turn lock, before queued
    # background results are flushed, so a follow-up turn can take the idle edge
    # ahead of them).
    on_turn_event: Callable[[Event], None] | None = None
    on_turn_end: Callable[[SessionStatusChanged | None], Awaitable[None]] | None = None
    #: Consecutive failed model calls a turn may retry through, and how long it
    #: may spend retrying, before the turn is stopped and ends failed. ``None``
    #: leaves that bound off.
    max_model_retries: int | None = None
    model_retry_window_seconds: float | None = None
    #: A monotonic clock in seconds; ``None`` reads ``time.monotonic``.
    clock: Callable[[], float] | None = None


def ends_attempt(ev: Event, attempt: str | None) -> TypeGuard[SessionStatusChanged]:
    """Whether ``ev`` is the terminal of ``attempt``."""
    return (
        isinstance(ev, SessionStatusChanged)
        and attempt is not None
        and ev.turn_id in (attempt, None)
        and ev.status in _TERMINAL_STATUSES
    )


class TurnLifecycle:
    """Owns a chat's foreground turn latch, settlement ledger, and overflow recovery."""

    def __init__(self, config: TurnLifecycleConfig) -> None:
        self._adapter = config.adapter
        self._bus = config.bus
        self._session_id = config.session_id
        self._is_closed = config.is_closed
        self._is_subagent = config.is_subagent
        self._compact = config.compact
        self._send_prompt_locked = config.send_prompt_locked
        self._inject_background_results = config.inject_background_results
        self._settlement_history = config.settlement_history
        self._max_overflow_retries = config.max_overflow_retries
        self._overflow_compaction_timeout_seconds = config.overflow_compaction_timeout_seconds
        self._on_turn_event = config.on_turn_event
        self._on_turn_end = config.on_turn_end
        self._model_retries = ModelRetryBudget(
            max_attempts=config.max_model_retries,
            max_seconds=config.model_retry_window_seconds,
        )
        self._clock: Callable[[], float] = config.clock or (lambda: time.monotonic())

        self._active_attempt: str | None = None
        self._active_started_after_sequence = 0
        self._settlements: list[SessionStatusChanged | None] = []
        self._settlements_base = 0
        self._settled_attempts: dict[str, None] = {}
        self._supersede_generation = 0
        self._pending_background: list[tuple[BackgroundJob, str]] = []
        self._turn_lock = asyncio.Lock()
        self._turn_abort: asyncio.Event = asyncio.Event()
        self._last_prompt: ReplayPrompt | None = None
        self._overflow_retries = 0

    @property
    def lock(self) -> asyncio.Lock:
        """The critical section for foreground sends, clears, and settlement flushes."""
        return self._turn_lock

    @property
    def active_attempt(self) -> str | None:
        """The transport attempt currently holding the foreground-turn latch."""
        return self._active_attempt

    @property
    def turn_abort(self) -> asyncio.Event:
        """The current per-turn abort signal used by foreground tools."""
        return self._turn_abort

    @property
    def pending_background(self) -> list[tuple[BackgroundJob, str]]:
        """Finished background jobs queued until the current foreground turn settles."""
        return self._pending_background

    @property
    def settlements(self) -> list[SessionStatusChanged | None]:
        """The bounded settlement history, exposed for compatibility with old probes."""
        return self._settlements

    @property
    def settlements_base(self) -> int:
        """The absolute index of ``settlements[0]`` after pruning."""
        return self._settlements_base

    @property
    def turn_active(self) -> bool:
        """Whether a foreground turn is currently running."""
        return self._active_attempt is not None

    @property
    def turn_settlements(self) -> int:
        """How many foreground turns have settled."""
        return self._settlements_base + len(self._settlements)

    def settlement(self, index: int) -> SessionStatusChanged | None:
        """The terminal settlement ``index`` ended on, or ``None`` for runtime settlement."""
        if index < self._settlements_base:
            raise IndexError(f"settlement {index} was pruned")
        return self._settlements[index - self._settlements_base]

    def reset_abort(self) -> asyncio.Event:
        """Install a fresh clear abort signal for a newly composed turn."""
        self._turn_abort = asyncio.Event()
        return self._turn_abort

    def remember_prompt(self, prompt: ReplayPrompt, *, overflow_retry: bool) -> None:
        """Record the inputs needed to replay a root turn after context compaction."""
        self._last_prompt = prompt
        if not overflow_retry:
            self._overflow_retries = 0

    async def fire_prompt(
        self, prompt: PromptInput, *, latch_briefs: Callable[[], None] | None
    ) -> str:
        """Fire one prompt and keep the foreground latch consistent on adapter failure."""
        self._active_attempt = prompt.turn_id
        self._active_started_after_sequence = self._bus.published_count
        try:
            await self._adapter.send_prompt(prompt)
        except BaseException:
            self._active_attempt = None
            self._active_started_after_sequence = 0
            raise
        if latch_briefs is not None:
            latch_briefs()
        return prompt.turn_id

    async def run(self, sub: AsyncIterator[Event]) -> None:
        """Track foreground-turn liveness and flush queued background completions.
        The observation seam opens only after the attempt's own ``running`` status,
        so work landing between the fire and the adapter picking it up is never
        credited to the turn."""
        running_attempt: str | None = None
        async for ev in sub:
            attempt = self._active_attempt
            if attempt is None:
                continue
            if self._on_turn_event is not None and running_attempt == attempt:
                self._on_turn_event(ev)
            if isinstance(ev, SessionStatusChanged) and ev.status == "running":
                running_attempt = attempt
            if not self._ends_active_attempt(ev, attempt):
                given_up = self._model_retries.observe(attempt, ev, self._clock())
                if given_up is not None:
                    await self._stop_retrying(attempt, given_up)
                continue
            if ev.status == "error" and ev.overflow and not self._is_subagent():
                outcome, giveup = await self._recover_from_overflow(sub, attempt)
                if outcome == "giveup":
                    await self.flush(attempt, giveup)
                continue
            await self.flush(attempt, ev)

    async def queue_or_inject_background(self, job: BackgroundJob, delivered: str) -> None:
        """Queue a finished background job mid-turn, otherwise inject its wake immediately."""
        async with self._turn_lock:
            if self._active_attempt is not None:
                self._pending_background.append((job, delivered))
                return
            injected = await self._inject_background_results([(job, delivered)])
            if not injected:
                self._pending_background.append((job, delivered))

    async def cancel(self) -> None:
        """Cancel the in-flight adapter turn and settle the observed attempt."""
        if self._is_closed():
            return
        self._turn_abort.set()
        self._supersede_generation += 1
        attempt = self._active_attempt
        try:
            await self._adapter.cancel()
        except asyncio.CancelledError:
            # This task is itself being cancelled (a wedged adapter cancel timed
            # out): settle without awaiting so the cancellation keeps unwinding.
            if attempt is not None:
                self._settle_abandoned(attempt)
            raise
        finally:
            if attempt is not None and self._active_attempt == attempt:
                await self.flush(attempt, None)

    def _settle_abandoned(self, attempt: str) -> None:
        """Release the latch and record the settlement with no awaits."""
        if self._active_attempt is not None and self._active_attempt != attempt:
            return
        self._active_attempt = None
        self._active_started_after_sequence = 0
        if attempt in self._settled_attempts:
            return
        self._settled_attempts[attempt] = None
        self._settlements.append(None)
        limit = self._settlement_history
        while limit is not None and len(self._settlements) > limit:
            self._settlements.pop(0)
            self._settlements_base += 1
        limit = self._settlement_history
        while limit is not None and len(self._settled_attempts) > limit:
            del self._settled_attempts[next(iter(self._settled_attempts))]

    async def clear(self) -> None:
        """Clear the adapter conversation and settle the attempt observed before the swap."""
        if self._is_closed():
            return
        async with self._turn_lock:
            attempt = self._active_attempt
            await self._adapter.clear()
            self._supersede_generation += 1
            if attempt is not None:
                await self.flush_locked(attempt, None)

    async def _recover_from_overflow(
        self, sub: AsyncIterator[Event], attempt: str
    ) -> tuple[Literal["resent", "settled", "giveup"], SessionStatusChanged | None]:
        generation = self._supersede_generation
        last = self._last_prompt
        spent = (
            self._max_overflow_retries is not None
            and self._overflow_retries >= self._max_overflow_retries
        )
        if spent or last is None or not (last.text.strip() or last.parts):
            return await self._overflow_giveup_unless_superseded(attempt, generation)

        outcome = await self._compact_for_overflow(sub, attempt)
        if outcome == "ready":
            return await self._resend_after_compaction(attempt, generation, last)
        if outcome == "cancelled":
            return "settled", None
        return await self._overflow_giveup_unless_superseded(attempt, generation)

    async def _compact_for_overflow(
        self, sub: AsyncIterator[Event], attempt: str
    ) -> Literal["ready", "cancelled", "failed"]:
        self._overflow_retries += 1
        try:
            await self._compact()
        except Exception:
            logger.warning("overflow recovery: compaction request failed", exc_info=True)
            return "failed"
        return await self._await_compaction(sub, attempt)

    async def _resend_after_compaction(
        self, attempt: str, generation: int, prompt: ReplayPrompt
    ) -> tuple[Literal["resent", "settled", "giveup"], SessionStatusChanged | None]:
        async with self._turn_lock:
            if self._active_attempt != attempt or self._supersede_generation != generation:
                return "settled", None
            try:
                await self._send_prompt_locked(prompt, overflow_retry=True)
            except Exception:
                logger.warning("overflow recovery: re-send failed", exc_info=True)
                return await self._overflow_giveup_unless_superseded(attempt, generation)

        if self._supersede_generation != generation:
            with contextlib.suppress(Exception):
                await self._adapter.cancel()
        return "resent", None

    async def _overflow_giveup_unless_superseded(
        self, attempt: str, generation: int
    ) -> tuple[Literal["giveup", "settled"], SessionStatusChanged | None]:
        if self._supersede_generation != generation or attempt in self._settled_attempts:
            return "settled", None
        return "giveup", await self._publish_overflow_giveup(attempt)

    async def _await_compaction(
        self, sub: AsyncIterator[Event], attempt: str
    ) -> Literal["ready", "cancelled", "failed"]:
        """Wait for the recovery compaction to settle.

        The bound is on SILENCE, not on the whole compaction: summarizing a
        near-full window on a slow model is one long request that keeps emitting,
        and a total deadline abandons the reader's message while the work is
        visibly progressing. Every event the session publishes therefore buys the
        full window again; a compaction that says nothing for that long is wedged.

        The liveness tick is the exception: the harness beats the stream on a
        timer whether or not anything is working, so counting it would re-arm the
        window forever and leave the wait with no bound at all.
        """
        compacted = False
        deadline = self._compaction_silence_deadline()
        while True:
            ev = await self._next_compaction_event(sub, deadline)
            if ev is None:
                return "failed"
            if not isinstance(ev, Heartbeat):
                deadline = self._compaction_silence_deadline()
            outcome = await self._compaction_outcome(ev, attempt, compacted)
            if outcome == "compacted":
                compacted = True
                continue
            if outcome is not None:
                return outcome

    def _compaction_silence_deadline(self) -> float | None:
        """When a compaction silent from now on is spent; ``None`` is no bound."""
        silence = self._overflow_compaction_timeout_seconds
        if silence is None:
            return None
        return asyncio.get_running_loop().time() + silence

    async def _next_compaction_event(
        self, sub: AsyncIterator[Event], deadline: float | None
    ) -> Event | None:
        """The next event the compaction publishes, or ``None`` when it stayed
        silent past ``deadline`` (or the bus closed) and recovery is spent."""
        try:
            if deadline is None:
                return await anext(sub)
            async with asyncio.timeout_at(deadline):
                return await anext(sub)
        except TimeoutError:
            logger.warning(
                "overflow recovery: compaction went silent for %ss",
                self._overflow_compaction_timeout_seconds,
            )
            return None
        except StopAsyncIteration:
            return None

    async def _compaction_outcome(
        self, ev: Event, attempt: str, compacted: bool
    ) -> Literal["ready", "cancelled", "failed", "compacted"] | None:
        """Evaluate one event consumed while overflow compaction settles."""
        if isinstance(ev, ConversationCleared):
            return "cancelled"
        current = self._active_attempt
        if current != attempt:
            if current is not None and self._ends_active_attempt(ev, current):
                await self.flush(current, ev)
            return "cancelled"
        if isinstance(ev, CompactionApplied):
            return "compacted"
        if not self._ends_active_attempt(ev, attempt):
            return None
        if ev.status == "aborted":
            await self.flush(attempt, ev)
            return "cancelled"
        if compacted and ev.status != "error":
            return "ready"
        return "failed"

    def _ends_active_attempt(
        self, ev: Event, attempt: str | None
    ) -> TypeGuard[SessionStatusChanged]:
        """Reject unstamped terminals that were already queued before this attempt started."""
        if not ends_attempt(ev, attempt):
            return False
        sequence = self._bus.sequence_for(ev)
        if ev.turn_id is None:
            return sequence is not None and sequence > self._active_started_after_sequence
        return True

    async def _stop_retrying(self, attempt: str, detail: str) -> None:
        """End a turn whose model call kept failing: stop the agent the way the
        Stop button does, then settle the turn as failed with ``detail``.

        The agent is stopped FIRST so the failure is the turn's last word: the
        agent's own close (an "aborted" for the attempt) lands before it, and
        a reader that shows a turn's latest status shows why it ended.
        """
        self._model_retries.reset()
        self._turn_abort.set()
        self._supersede_generation += 1
        try:
            await self._adapter.cancel()
        except Exception:
            logger.warning("model retry bound: stopping the agent failed", exc_info=True)
        if self._active_attempt != attempt or attempt in self._settled_attempts:
            return
        logger.warning("turn %s stopped: %s", attempt, detail)
        failed = SessionStatusChanged(
            event_id=secrets.token_hex(10),
            time=datetime.now(UTC),
            session_id=self._session_id,
            status="error",
            phase="error",
            detail=detail,
            turn_id=attempt,
        )
        await self._bus.publish(failed)
        await self.flush(attempt, failed)

    async def _publish_overflow_giveup(self, attempt: str) -> SessionStatusChanged:
        giveup = SessionStatusChanged(
            event_id=secrets.token_hex(10),
            time=datetime.now(UTC),
            session_id=self._session_id,
            status="error",
            phase="error",
            detail=(
                "context window full — automatic compaction couldn't free enough "
                "space. Start a new chat or shorten your request."
            ),
            turn_id=attempt,
        )
        await self._bus.publish(giveup)
        return giveup

    async def flush(self, attempt: str, terminal: SessionStatusChanged | None) -> None:
        """Settle ``attempt`` under the turn lock."""
        async with self._turn_lock:
            await self.flush_locked(attempt, terminal)

    async def flush_locked(self, attempt: str, terminal: SessionStatusChanged | None) -> None:
        """Settle ``attempt``; the caller already holds the turn lock."""
        if self._active_attempt is not None and self._active_attempt != attempt:
            return

        self._active_attempt = None
        self._active_started_after_sequence = 0
        if attempt not in self._settled_attempts:
            self._settled_attempts[attempt] = None
            self._settlements.append(terminal)
            limit = self._settlement_history
            while limit is not None and len(self._settlements) > limit:
                self._settlements.pop(0)
                self._settlements_base += 1
            limit = self._settlement_history
            while limit is not None and len(self._settled_attempts) > limit:
                del self._settled_attempts[next(iter(self._settled_attempts))]

        if self._on_turn_end is not None:
            await self._on_turn_end(terminal)
            if self._active_attempt is not None:  # the settle hook fired a follow-up turn
                return

        if not self._pending_background:
            return

        jobs = self._pending_background
        self._pending_background = []
        injected = await self._inject_background_results(jobs)
        if not injected:
            self._pending_background.extend(jobs)


__all__ = ["ReplayPrompt", "TurnLifecycle", "TurnLifecycleConfig", "ends_attempt"]
