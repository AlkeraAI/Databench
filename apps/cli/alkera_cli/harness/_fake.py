"""`FakeAdapter` — for testing the harness layer without a real harness.

The driver controls the event stream via `feed()`. The adapter under
test behaves like a real one from the caller's perspective: it has a
lifecycle, a subscription stream, and synchronous methods. Tests use
this to exercise `HarnessRuntime`, daemon JSON-RPC plumbing, and CLI
chat behavior without spawning opencode.

Usage::

    adapter = FakeAdapter()
    await adapter.start()
    sub = adapter.subscribe()
    await adapter.feed(SomeEvent(...))
    async for ev in sub:
        ...   # asserts on the events
    await adapter.stop()
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from alkera_core.schemas.chat import (
    CompactionApplied,
    ConversationCleared,
    MessageCompleted,
    MessageCreated,
    PartCreated,
    SessionStatusChanged,
    TextPart,
    TurnFinished,
)

from alkera_cli.harness.adapter import (
    HarnessAdapter,
    HarnessCrashError,
    HarnessGoneError,
    HarnessNotReadyError,
    HarnessStartError,
    PromptInput,
)
from alkera_cli.harness.event_bus import EventBus

if TYPE_CHECKING:
    from alkera_core.schemas.chat import Event, PermissionOptionId


@dataclass(frozen=True)
class ScriptedMessage:
    """One message inside a scripted turn. ``role=None`` skips the
    ``MessageCreated`` announcement, the shape a harness emits when it streams
    text before naming the speaker."""

    text: str = ""
    finish_reason: str = "stop"
    message_id: str = "m1"
    role: str | None = "assistant"


@dataclass(frozen=True)
class ScriptedTurn:
    """A turn's event stream as data. ``turn_stop_reason`` emits a typed
    ``TurnFinished``; ``error_detail`` settles the turn on an ``error`` status
    instead of ``idle`` (the two ways a turn can end besides a clean idle)."""

    messages: tuple[ScriptedMessage, ...] = ()
    turn_stop_reason: str | None = None
    error_detail: str | None = None


class FakeAdapter(HarnessAdapter):
    """Programmable fake. Doesn't spawn anything; doesn't talk HTTP."""

    name = "fake"
    capabilities = frozenset({"resume", "clear", "summarize"})

    def __init__(
        self,
        *,
        start_should_fail: bool = False,
        crash_after_prompt: bool = False,
        native_state_value: dict[str, Any] | None = None,
        reply_text: str | None = None,
        scripted_turns: list[ScriptedTurn] | None = None,
        start_gate: asyncio.Event | None = None,
    ) -> None:
        self._bus = EventBus()
        self._started = False
        self._stopped = False
        self._dead = False
        self._start_should_fail = start_should_fail
        # When set, `start()` blocks on this event until the test releases it —
        # lets a test observe the pre-session window (the slow real-harness spawn)
        # deterministically, instead of racing a `pilot.pause()`.
        self._start_gate = start_gate
        self._crash_after_prompt = crash_after_prompt
        # When set, each send_prompt auto-emits a minimal scripted turn
        # (running → a text PartCreated → idle) — lets tests drive a real turn
        # to completion (e.g. a spawned subagent's reply) with no real harness.
        self._reply_text = reply_text
        # When set, each send_prompt replays the next ScriptedTurn (the last
        # repeats) — the same event shape as reply_text, with control over
        # finish reasons, multiple messages, typed stop reasons, and errors.
        self._scripted_turns = scripted_turns
        self._turn_no = 0
        self._native_state_value: dict[str, Any] = (
            dict(native_state_value) if native_state_value else {}
        )
        # One-shot: the next send_prompt raises BEFORE recording the prompt, so
        # a test can drive a failed wake injection through the adapter seam.
        self.fail_next_prompt = False
        self._sent_prompts: list[PromptInput] = []
        self._permission_replies: list[tuple[str, str]] = []
        # Fires on every resolve_permission so a test can await the permission
        # pump's reply deterministically (no polling, no fixed sleep).
        self._permission_reply_event = asyncio.Event()
        self._permission_reply_reasons: list[tuple[str, str | None]] = []
        self._question_replies: list[tuple[str, list[list[str]]]] = []
        self._question_rejects: list[tuple[str, str | None]] = []
        self._cancels = 0
        self._clears = 0
        self._compacts = 0
        # The turn indices (0-based, in prompts sent) at whose start the session
        # asked this adapter to list the tool server's tools again.
        self._tool_refreshes: list[int] = []

    # --- introspection (test-only) ------------------------------------

    @property
    def sent_prompts(self) -> list[PromptInput]:
        return list(self._sent_prompts)

    @property
    def permission_replies(self) -> list[tuple[str, str]]:
        return list(self._permission_replies)

    @property
    def permission_reply_reasons(self) -> list[tuple[str, str | None]]:
        """The (request_id, reason) per reply — the deny reason the model would see."""
        return list(self._permission_reply_reasons)

    @property
    def question_replies(self) -> list[tuple[str, list[list[str]]]]:
        return list(self._question_replies)

    @property
    def question_rejects(self) -> list[tuple[str, str | None]]:
        return list(self._question_rejects)

    @property
    def cancel_count(self) -> int:
        return self._cancels

    @property
    def clear_count(self) -> int:
        return self._clears

    @property
    def compact_count(self) -> int:
        return self._compacts

    @property
    def started(self) -> bool:
        return self._started

    @property
    def stopped(self) -> bool:
        return self._stopped

    async def feed(self, event: Event) -> None:
        """Inject an event into the fanout, verbatim — what a real adapter would
        emit when translating native protocol messages. No implicit stamping:
        tests choose the exact attribution shape they need."""
        if not self._started:
            raise HarnessNotReadyError("FakeAdapter.feed() called before start()")
        await self._bus.publish(event)

    def _last_turn_id(self) -> str | None:
        return self._sent_prompts[-1].turn_id if self._sent_prompts else None

    async def feed_many(self, events: list[Event]) -> None:
        for ev in events:
            await self.feed(ev)

    # --- HarnessAdapter contract --------------------------------------

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("FakeAdapter already started")
        if self._start_gate is not None:
            await self._start_gate.wait()
        if self._start_should_fail:
            raise HarnessStartError("fake adapter configured to fail start")
        await asyncio.sleep(0)  # yield to mimic async startup
        self._started = True

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        await self._bus.close()

    async def die(self, detail: str = "agent exited unexpectedly (rc=137)") -> None:
        """The agent process dies under the fake, the way the real adapter
        reports one: the attempt in flight ends on an ``error`` terminal that
        carries ``detail``, and every later call finds the agent gone
        (:class:`HarnessGoneError`) until a fresh adapter is started."""
        self._dead = True
        await self._bus.publish(
            SessionStatusChanged(
                event_id=f"fake-dead-{self._turn_no}",
                time=datetime.now(UTC),
                session_id="fake",
                status="error",
                phase="error",
                detail=detail,
                turn_id=self._last_turn_id(),
            )
        )

    @property
    def dead(self) -> bool:
        return self._dead

    @property
    def tool_refreshes(self) -> list[int]:
        """For each re-list the session asked for, the index of the turn it
        preceded (the number of prompts sent before it)."""
        return list(self._tool_refreshes)

    async def refresh_tools(self) -> None:
        self._tool_refreshes.append(len(self._sent_prompts))

    async def send_prompt(self, prompt: PromptInput) -> None:
        if not self._started:
            raise HarnessNotReadyError("FakeAdapter.send_prompt() before start()")
        if self.dead:
            raise HarnessGoneError("FakeAdapter's agent is gone")
        if self._stopped:
            raise HarnessCrashError("FakeAdapter is stopped")
        if self.fail_next_prompt:
            self.fail_next_prompt = False
            raise HarnessCrashError("simulated prompt failure")
        self._sent_prompts.append(prompt)
        if self._crash_after_prompt:
            raise HarnessCrashError("simulated crash after prompt")
        if self._scripted_turns is not None:
            turn = self._scripted_turns[min(self._turn_no, len(self._scripted_turns) - 1)]
            await self._replay_turn(turn, prompt.turn_id)
        elif self._reply_text is not None:
            await self._emit_scripted_turn(self._reply_text, prompt.turn_id)

    def _status(
        self, event_id: str, status: Any, turn_id: str | None, *, phase: Any = None
    ) -> SessionStatusChanged:
        return SessionStatusChanged(
            event_id=event_id,
            time=datetime.now(UTC),
            session_id="fake",
            status=status,
            phase=phase,
            turn_id=turn_id,
        )

    async def _emit_scripted_turn(self, text: str, turn_id: str) -> None:
        await self._replay_turn(ScriptedTurn((ScriptedMessage(text=text),)), turn_id)

    async def _replay_turn(self, turn: ScriptedTurn, turn_id: str) -> None:
        """Publish the event stream for one turn: running, each message, then a
        typed TurnFinished (when scripted) and the terminal idle-or-error, every
        status stamped with the turn that produced it."""
        now = datetime.now(UTC)
        n = self._turn_no
        self._turn_no += 1
        await self._bus.publish(self._status(f"fake-run-{n}", "running", turn_id))
        for index, message in enumerate(turn.messages):
            await self._emit_message(message, f"{n}-{index}", now)
        if turn.turn_stop_reason is not None:
            await self._bus.publish(
                TurnFinished(
                    event_id=f"fake-fin-{n}",
                    time=now,
                    session_id="fake",
                    turn_id=turn_id,
                    stop_reason=turn.turn_stop_reason,  # type: ignore[arg-type]
                    error_detail=turn.error_detail,
                )
            )
        if turn.error_detail is not None and turn.turn_stop_reason is None:
            await self._bus.publish(
                SessionStatusChanged(
                    event_id=f"fake-err-{n}",
                    time=now,
                    session_id="fake",
                    status="error",
                    phase="error",
                    detail=turn.error_detail,
                    turn_id=turn_id,
                )
            )
        else:
            await self._bus.publish(self._status(f"fake-idle-{n}", "idle", turn_id))

    async def _emit_message(self, message: ScriptedMessage, tag: str, now: datetime) -> None:
        if message.role is not None:
            await self._bus.publish(
                MessageCreated(
                    event_id=f"fake-created-{tag}",
                    time=now,
                    session_id="fake",
                    message_id=message.message_id,
                    role=message.role,  # type: ignore[arg-type]
                )
            )
        if message.text:
            await self._bus.publish(
                PartCreated(
                    event_id=f"fake-part-{tag}",
                    time=now,
                    session_id="fake",
                    part=TextPart(
                        part_id=f"p-{tag}", message_id=message.message_id, text=message.text
                    ),
                )
            )
        await self._bus.publish(
            MessageCompleted(
                event_id=f"fake-msg-{tag}",
                time=now,
                session_id="fake",
                message_id=message.message_id,
                finish_reason=message.finish_reason,
            )
        )

    async def cancel(self) -> None:
        self._cancels += 1

    async def clear(self) -> None:
        self._clears += 1
        # Mirror the real adapter: a clear surfaces a ConversationCleared
        # on the stream (the daemon forwards it as a harness.event).
        if self._started and not self._stopped:
            await self._bus.publish(
                ConversationCleared(
                    event_id="fake-clear",
                    time=datetime.now(UTC),
                    session_id="fake",
                )
            )

    async def compact(self) -> None:
        self._compacts += 1
        # Mirror the real adapter: a compaction surfaces the compacting status
        # (which opens the card) then the applied summary (which finalizes it).
        if self._started and not self._stopped:
            await self._bus.publish(
                self._status("fake-compacting", "running", self._last_turn_id(), phase="compacting")
            )
            await self._bus.publish(
                CompactionApplied(
                    event_id="fake-compacted",
                    time=datetime.now(UTC),
                    session_id="fake",
                    summary_text="Folded the earlier turns into this summary.",
                )
            )
            # The compaction runs as the current attempt and ends with its idle
            # (opencode: `compacted, busy, idle`), which overflow recovery awaits.
            await self._bus.publish(
                self._status("fake-compacted-idle", "idle", self._last_turn_id())
            )

    async def resolve_permission(
        self, request_id: str, option_id: PermissionOptionId, *, reason: str | None = None
    ) -> None:
        self._permission_replies.append((request_id, option_id))
        self._permission_reply_reasons.append((request_id, reason))
        self._permission_reply_event.set()

    async def wait_for_permission_reply(self, request_id: str, option_id: str) -> None:
        """Block until ``(request_id, option_id)`` has been recorded as a reply.

        Event-driven (no polling, no timeout): the permission pump calls
        ``resolve_permission``, which sets the flag awaited here — so a test can
        synchronize on the pump's side-effect deterministically instead of racing
        a fixed sleep. Clearing before each wait (with no await in between, so the
        single-threaded loop can't slip a reply past us) avoids a lost wakeup when
        an earlier reply already set the flag."""
        while (request_id, option_id) not in self._permission_replies:
            self._permission_reply_event.clear()
            await self._permission_reply_event.wait()

    async def answer_question(self, request_id: str, answers: list[list[str]]) -> None:
        self._question_replies.append((request_id, answers))

    async def reject_question(self, request_id: str, reason: str | None = None) -> None:
        self._question_rejects.append((request_id, reason))

    def subscribe(self) -> AsyncIterator[Event]:
        return self._bus.subscribe()

    def native_state(self) -> dict[str, Any]:
        return dict(self._native_state_value)


__all__ = ["FakeAdapter"]
