"""Headless driver for the harness — send prompts to a project non-interactively.

The library core behind ``alkera run`` and scripted evaluation rigs: build the
SAME production runtime as the interactive client (gateway config, plugins,
tool server, scheduler beat), fire one or more prompts, auto-resolve
permissions/questions so nothing blocks on a human, and return a structured
result (final text, tool calls, permission decisions, tokens/cost).

No typer / textual / console imports here — callers own presentation. Failures
that a user must act on (not signed in, gateway down, unknown model) raise
``HeadlessError`` with a user-facing message.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Literal, cast

from alkera_core.project.locking import LockHeldError
from alkera_core.schemas.chat import (
    Event,
    MessageCompleted,
    MessageCreated,
    PartCreated,
    PermissionOptionId,
    PermissionRequest,
    SessionStatusChanged,
    TextPart,
    ToolCall,
    ToolCallUpdate,
    TurnFinished,
)

from alkera_cli.account.auth_file import ProfileResolutionError
from alkera_cli.account.binding import profile_for_project
from alkera_cli.app.compose import production_runtime
from alkera_cli.contracts.gateway_model import GatewayModel
from alkera_cli.gateway.client import (
    GatewayAuthError,
    GatewayUnavailableError,
    fetch_catalog,
    resolve_model_selection,
    selectable_models,
)
from alkera_cli.harness import HarnessRuntime, PermissionBroker, QuestionBroker
from alkera_cli.harness.adapter import (
    HarnessCrashError,
    HarnessStartError,
    HarnessUnavailableError,
)
from alkera_cli.harness.adapters.opencode_alkera import build_manifest_model
from alkera_cli.harness.orphan_sweep import sweep_orphaned_agents
from alkera_cli.harness.permission_mode import PermissionMode
from alkera_cli.harness.registry import CLAUDE_HARNESS, HARNESS_CHOICES, resolve_harness_type
from alkera_cli.harness.runtime import (
    SETTLED_STOP_REASONS,
    TRUNCATED_FINISH_REASONS,
    TURN_ENDING_STATUSES,
    AnalysisPipeline,
)
from alkera_cli.harness.web_flags import WebToolFlags
from alkera_cli.host.config import get_settings
from alkera_cli.host.paths import project_directory
from alkera_cli.observability.audit_report import close_default_reporter
from alkera_cli.observability.otel_export import close_default_exporter
from alkera_cli.preferences.chat_defaults import resolve_and_persist_chat_defaults

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping, Sequence

    from alkera_cli.harness.question_broker import QuestionResolution
    from alkera_cli.harness.runtime import ChatSession

logger = logging.getLogger(__name__)

#: The reason a headless run gives when it declines a mid-turn clarifier
#: question — a REASONED reject keeps the turn alive (the harness treats it as
#: steering, not a hard stop), so the agent proceeds instead of stalling.
QUESTION_DECLINE_REASON = (
    "This session is running unattended — nobody can answer questions. "
    "Use your best judgment and continue."
)

_INPUT_PREVIEW_CHARS = 500

#: How a settling status reads as a turn outcome.
_STOP_REASON_BY_STATUS = {"idle": "completed", "aborted": "cancelled", "error": "error"}


class HeadlessError(Exception):
    """A headless run couldn't start or finish for a reason the CALLER must fix
    (not signed in, gateway unreachable, unknown model/harness, chat locked).
    The message is user-facing."""


_NOT_SIGNED_IN_MESSAGE = "Not signed in. Run `alkera login` first."
_NO_PROMPT_MESSAGE = "No prompt given."
_INJECTED_RUNTIME_MODEL_MESSAGE = (
    "model/effort can't be resolved for an injected runtime — "
    "pin via turn_model or the chat manifest."
)
_INJECTED_RUNTIME_OVERRIDES_MESSAGE = (
    "system_block_overrides can't be applied to an injected runtime — "
    "pass them to HarnessRuntime instead."
)


def _gateway_session_message(detail: str, suffix: str) -> str:
    return f"Gateway rejected your session: {detail}{suffix}"


def _gateway_unavailable_message(exc: GatewayUnavailableError) -> str:
    return f"Model gateway unavailable: {exc}. Is the local stack up (`make dev-all`)?"


def _no_default_model_message() -> str:
    return "No model available: pass --model, or set a default chat model."


def _unknown_harness_message(harness: str) -> str:
    return f"Unknown harness {harness!r}. Choose: {', '.join(HARNESS_CHOICES)}"


def _chat_locked_message(exc: LockHeldError) -> str:
    return f"Chat is open in another client: {exc}. Close it first."


@dataclass(slots=True, frozen=True)
class HeadlessTurn:
    """One prompt → response round-trip."""

    prompt: str
    final_text: str
    stop_reason: str
    """``completed`` for a settled answer. ``max_tokens`` when the model hit its
    output cap mid-message, so ``final_text`` is whatever came before the cut,
    never a finished answer. Otherwise the adapter's own reason (``error``,
    ``cancelled``, ``refusal``, ``max_turn_requests``)."""
    error_detail: str | None = None
    origin: str = "caller"
    """``caller``, ``verification``, or a background ``continuation``."""
    delivered: bool = False
    """The prompt's answer once a verification ran: the restatement, else the caller turn."""


@dataclass(slots=True)
class HeadlessResult:
    """What a headless run produced — everything a script needs to inspect the
    outcome without replaying the event log (which ``events_path`` captures)."""

    session_id: str
    stop_reason: str
    """Whole-run outcome: the last turn's stop_reason, or ``timeout`` when the
    run budget expired before the harness settled."""
    turns: list[HeadlessTurn] = field(default_factory=list)
    permission_prompts: list[dict[str, Any]] = field(default_factory=list)
    """Requests that would have prompted a human (the policy auto-decides the
    rest) + the decision this run took for them."""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tokens: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    error_detail: str | None = None
    verification: str | None = None
    """``verified`` when the restatement is the delivery, else ``not re-verified: <reason>``."""
    verification_fired: bool = False
    """Whether a verification turn was issued for the last prompt, whatever its outcome."""

    @property
    def final_text(self) -> str:
        """The last prompt's delivered turn, else the last turn."""
        for turn in reversed(self.turns):
            if turn.delivered:
                return turn.final_text
            if turn.origin == "caller":
                break
        return self.turns[-1].final_text if self.turns else ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _truncate(text: str, limit: int = _INPUT_PREVIEW_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class _Collector:
    """Folds the live event stream into per-turn text / tool-call / status
    state. One instance per run; ``begin_turn`` resets the per-turn buffers."""

    def __init__(
        self,
        *,
        on_event: Callable[[Event], None] | None,
        events_file: IO[str] | None,
    ) -> None:
        self._on_event = on_event
        self._events_file = events_file
        #: While a list, observer delivery is deferred into it.
        self.held: list[Event] | None = None
        self.status: str = "idle"
        self.seen_running = False
        self.roles: dict[str, str] = {}
        self.turn_texts: list[tuple[str, str]] = []  # (message_id, text)
        self.turn_stop_reason: str | None = None
        self.turn_finish_reason: str | None = None
        self.turn_error_detail: str | None = None
        self.tool_calls: list[dict[str, Any]] = []
        self._tool_calls_by_id: dict[str, dict[str, Any]] = {}

    def deliver(self, event: Event) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception:
            logger.exception("headless: on_event callback raised")

    def release_held(self, *, deliver: bool) -> None:
        held, self.held = self.held or [], None
        if deliver:
            for event in held:
                self.deliver(event)

    def begin_turn(self) -> None:
        self.turn_texts = []
        self.turn_stop_reason = None
        self.turn_finish_reason = None
        self.turn_error_detail = None

    def handle(self, event: Event) -> None:
        self._record_event(event)
        self._fold_event(event)

    def _record_event(self, event: Event) -> None:
        """Persist and mirror one live event before folding it into the result."""
        if self._events_file is not None:
            try:
                self._events_file.write(event.model_dump_json() + "\n")
            except Exception:
                logger.exception("headless: events file write failed")
        if self.held is not None:
            self.held.append(event)
        else:
            self.deliver(event)

    def _fold_event(self, event: Event) -> None:
        """Apply the result-facing state transition for one event."""
        if isinstance(event, SessionStatusChanged):
            self.status = event.status
            if event.status == "running":
                self.seen_running = True
            if event.status == "error" and event.detail:
                self.turn_error_detail = event.detail
            return
        if isinstance(event, MessageCreated):
            self.roles[event.message_id] = event.role
            return
        if isinstance(event, MessageCompleted):
            if self.roles.get(event.message_id) != "user":
                self.turn_finish_reason = event.finish_reason or None
            return
        if isinstance(event, PartCreated):
            self._fold_part(event)
            return
        if isinstance(event, ToolCall):
            self._fold_tool_call(event)
            return
        if isinstance(event, ToolCallUpdate):
            self._fold_tool_update(event)
            return
        if isinstance(event, TurnFinished):
            self.turn_stop_reason = event.stop_reason
            if event.error_detail:
                self.turn_error_detail = event.error_detail

    def turn_boundary(self, event: Event) -> bool:
        """True when ``event`` closes the in-flight turn: the running → idle/error
        status transition (arrives AFTER TurnFinished, and is also emitted by
        adapters — like the scripted fake — that skip the typed turn events). A
        context overflow is not a boundary: the runtime resends the same turn on
        a compacted context, or publishes a separate give-up error that is."""
        return (
            isinstance(event, SessionStatusChanged)
            and event.status in TURN_ENDING_STATUSES
            and not event.overflow
            and self.seen_running
        )

    def _fold_part(self, event: PartCreated) -> None:
        """Record assistant text while ignoring echoed prompts and synthetic steering."""
        part = event.part
        if not isinstance(part, TextPart) or not part.text or part.synthetic:
            return
        if self.roles.get(part.message_id) != "user":
            self.turn_texts.append((part.message_id, part.text))

    def _fold_tool_call(self, event: ToolCall) -> None:
        """Start the result record for one tool call."""
        record: dict[str, Any] = {
            "tool_call_id": event.tool_call_id,
            "tool_name": event.tool_name,
            "tool_kind": event.tool_kind,
            "status": event.status,
            "input_preview": _truncate(json.dumps(event.input, default=str)),
            "error_text": None,
        }
        self.tool_calls.append(record)
        self._tool_calls_by_id[event.tool_call_id] = record

    def _fold_tool_update(self, event: ToolCallUpdate) -> None:
        """Merge streamed tool updates into the original result record."""
        record = self._tool_calls_by_id.get(event.tool_call_id)
        if record is None:
            return
        if event.status is not None:
            record["status"] = event.status
        if event.input is not None:
            record["input_preview"] = _truncate(json.dumps(event.input, default=str))
        if event.error_text is not None:
            record["error_text"] = event.error_text

    def end_turn(
        self,
        prompt: str,
        terminal: SessionStatusChanged | _StreamEnd | None = None,
        *,
        origin: str = "caller",
        settled: bool = True,
    ) -> HeadlessTurn:
        """Close the turn on the terminal the runtime settled it with. A typed
        ``TurnFinished`` or a truncated finish reason wins over the settlement
        status; a settlement carrying no terminal reads ``cancelled`` (only a
        user cancel or clear settles without one); ``_STREAM_END`` is the
        harness dying under the run and reads as an error with a detail."""
        stop_reason = self._stop_reason()
        detail = self.turn_error_detail
        if isinstance(terminal, _StreamEnd):
            stop_reason = "error"
            detail = detail or "the event stream closed before the turn settled"
        elif terminal is not None:
            if self.turn_stop_reason is None and stop_reason == "completed":
                stop_reason = _STOP_REASON_BY_STATUS.get(terminal.status, "completed")
            detail = terminal.detail or detail if terminal.status == "error" else None
        elif settled and self.turn_stop_reason is None and stop_reason == "completed":
            stop_reason = "cancelled"
        turn = HeadlessTurn(
            prompt=prompt,
            final_text=self._final_text(),
            stop_reason=stop_reason,
            error_detail=detail,
            origin=origin,
        )
        self.begin_turn()
        return turn

    def _stop_reason(self) -> str:
        """The turn's outcome. A typed ``TurnFinished`` wins. Without one, the
        last assistant message's finish reason tells a truncated turn from a
        settled one, since both end in the same idle status."""
        if self.turn_stop_reason is not None:
            if self.turn_stop_reason in SETTLED_STOP_REASONS:
                return "completed"
            return self.turn_stop_reason
        if self.status == "error":
            return "error"
        if self.turn_finish_reason in TRUNCATED_FINISH_REASONS:
            return "max_tokens"
        return "completed"

    def _final_text(self) -> str:
        """The final message's text: all text parts of the LAST message that
        produced any (a message's parts stay contiguous per the event ordering
        contract)."""
        if not self.turn_texts:
            return ""
        last_message_id = self.turn_texts[-1][0]
        return "\n\n".join(text for mid, text in self.turn_texts if mid == last_message_id)


class _StreamEnd:
    """The event stream closed. Distinct from a timeout: a closed stream never
    yields again, so a loop that treats it as "try again" spins without ever
    yielding to the event loop, and the run's own ``asyncio.timeout`` can never
    fire. Every consumer must exit on it."""


_STREAM_END = _StreamEnd()


@dataclass(slots=True, frozen=True)
class ProductionRuntimeOptions:
    """Inputs needed to build the same runtime as the interactive client."""

    project_root: Path
    harness_type: str
    model: str | None
    effort: str | None
    system_block_overrides: Mapping[str, str] | None


@dataclass(slots=True, frozen=True)
class HeadlessRunOptions:
    """Call options for one unattended conversation."""

    project_dir: Path
    prompts: Sequence[str]
    model: str | None = None
    effort: str | None = None
    harness: str = "alkera"
    permission_mode: PermissionMode = "auto"
    analysis_pipeline: AnalysisPipeline | None = None
    on_permission: Literal["allow", "reject"] = "allow"
    resume_session_id: str | None = None
    timeout_seconds: float | None = 1800.0
    verification_timeout_seconds: float | None = None
    wait_for_seed_seconds: float | None = None
    title: str | None = None
    on_event: Callable[[Event], None] | None = None
    events_path: Path | None = None
    runtime: HarnessRuntime | None = None
    turn_model: dict[str, str] | None = None
    system_block_overrides: Mapping[str, str] | None = None

    @classmethod
    def from_kwargs(
        cls, project_dir: Path, prompts: Sequence[str], kwargs: dict[str, Any]
    ) -> HeadlessRunOptions:
        """Build options from the historical keyword-only public call surface."""
        values = dict(kwargs)
        known = {
            "model",
            "effort",
            "harness",
            "permission_mode",
            "analysis_pipeline",
            "on_permission",
            "resume_session_id",
            "timeout_seconds",
            "verification_timeout_seconds",
            "wait_for_seed_seconds",
            "title",
            "on_event",
            "events_path",
            "runtime",
            "turn_model",
            "system_block_overrides",
        }
        unknown = values.keys() - known
        if unknown:
            name = next(iter(unknown))
            raise TypeError(f"run_headless() got an unexpected keyword argument {name!r}")
        return cls(project_dir=project_dir, prompts=prompts, **values)


@dataclass(slots=True)
class HeadlessRunState:
    """Mutable state for one headless run."""

    options: HeadlessRunOptions
    harness_type: str
    runtime: HarnessRuntime
    owns_runtime: bool
    pinned_model: dict[str, Any] | None
    started: float
    collector: _Collector
    permission_records: list[dict[str, Any]] = field(default_factory=list)
    session: ChatSession | None = None
    turns: list[HeadlessTurn] = field(default_factory=list)
    run_stop: str | None = None
    error_detail: str | None = None
    verification: str | None = None
    verification_fired: bool = False
    wedged: bool = False
    events_file: IO[str] | None = None
    current_prompt: str | None = None
    current_sub: AsyncIterator[Event] | None = None


#: After cancelling a wedged verification turn, how long the drain waits for the
#: session to settle before returning the first answer on its own authority.
_VERIFICATION_GIVE_UP_SECONDS = 30.0

#: Verification budget when the caller names none; a run deadline caps it.
_DEFAULT_VERIFICATION_BUDGET_SECONDS = 900.0


async def _settle_verification(
    session: Any,
    sub: AsyncIterator[Event],
    collector: Any,
    turns: list[HeadlessTurn],
    budget_seconds: float,
    grace_seconds: float,
    run_deadline: float | None,
) -> tuple[str | None, bool]:
    """Collect the one turn the runtime issues after a caller prompt (its
    verification, or a wake when none was earned) and record the outcome. Text is
    held from observers until the delivery is decided. Past the budget the turn is
    cancelled; past the grace the wait gives up, so the first answer always comes
    back. Returns the note (None when none ran) and whether the session is wedged."""
    deadline: float | None = None
    give_up: float | None = None
    cancelled = False
    wedged = False
    segment_done = False
    collector.held = []
    while not segment_done:
        now = time.monotonic()
        if session.analysis_verification_pending:
            deadline = deadline or (now + budget_seconds)
            if give_up is not None and now > give_up:
                wedged = True
                break
            if now > deadline and not cancelled:
                cancelled = True
                give_up = now + grace_seconds
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(grace_seconds):
                        await session.cancel()
        elif session.verification_outcome is not None:
            # Resolved before this subscriber saw the boundary; a fire that never
            # left the harness has no segment to take.
            if session.analysis_verification_fired:
                while (event := await _next_event(sub, 0.05)) is not None and not isinstance(
                    event, _StreamEnd
                ):
                    collector.handle(event)
                    if collector.turn_boundary(event):
                        break
            break
        elif session.turn_origin is None:
            break
        tick = min([1.0, *(t - now for t in (deadline, give_up, run_deadline) if t is not None)])
        event = await _next_event(sub, max(tick, 0.01))
        if isinstance(event, _StreamEnd):
            wedged = True
            break
        if event is not None:
            collector.handle(event)
            segment_done = collector.turn_boundary(event)
    # The pump records the outcome on its own subscription.
    for _ in range(50):
        if not session.analysis_verification_pending or wedged:
            break
        await asyncio.sleep(0.01)
    outcome = session.verification_outcome
    if not session.analysis_verification_fired and not cancelled:
        collector.release_held(deliver=True)
        if segment_done:
            turns.append(
                collector.end_turn(
                    "[background continuation]", origin="continuation", settled=False
                )
            )
        else:
            collector.begin_turn()
        if outcome is None:
            return None, wedged
        at = -2 if segment_done else -1
        turns[at] = dataclasses.replace(turns[at], delivered=True)
        return _verification_note(outcome, None), wedged
    turn = collector.end_turn("[verification]", origin="verification", settled=False)
    note = _verification_note(outcome, turn, cancelled=cancelled)
    collector.release_held(deliver=note == "verified")
    if note == "verified":
        turns.append(turn)
    turns[-1] = dataclasses.replace(turns[-1], delivered=True)
    return note, wedged


def _verification_note(
    outcome: str | None, turn: HeadlessTurn | None, *, cancelled: bool = False
) -> str:
    """``verified`` when the restatement is the delivery, else the reason the
    first answer stands. A clean restatement is authoritative; an empty one,
    a cut one, a failed fire, or a cancelled one is not."""
    if cancelled:
        return "not re-verified: cancelled at the verification deadline"
    if outcome is None or outcome.startswith("failed") or turn is None:
        return f"not re-verified: {(outcome or 'failed: unknown').removeprefix('failed: ')}"
    if turn.stop_reason != "completed":
        return f"not re-verified: {turn.stop_reason}"
    if not turn.final_text.strip():
        return "not re-verified: empty restatement"
    return "verified"


async def _drain_to_quiet(
    session: Any,
    sub: AsyncIterator[Event],
    collector: Any,
    turns: list[HeadlessTurn],
    *,
    settle: bool,
) -> None:
    """Collect each harness-issued turn (a wake) as its own continuation until the
    session is quiet; with ``settle`` also wait for running background jobs."""
    while session.turn_active or (settle and session.has_running_background):
        event = await _next_event(sub, 1.0)
        if isinstance(event, _StreamEnd):
            break
        if event is not None:
            collector.handle(event)
            if collector.turn_boundary(event) and collector.turn_texts:
                turns.append(
                    collector.end_turn(
                        "[background continuation]", origin="continuation", settled=False
                    )
                )
    while (event := await _next_event(sub, 0.05)) is not None and not isinstance(event, _StreamEnd):
        collector.handle(event)
    if collector.turn_texts:
        turns.append(
            collector.end_turn("[background continuation]", origin="continuation", settled=False)
        )


def _verification_budget(
    requested: float | None, run_deadline: float | None
) -> tuple[float, float]:
    """The verification budget and the cancel grace, both shrunk so the cancel and
    the give-up land before the run's own deadline."""
    budget = _DEFAULT_VERIFICATION_BUDGET_SECONDS if requested is None else requested
    grace = _VERIFICATION_GIVE_UP_SECONDS
    if run_deadline is not None:
        remaining = max(0.0, run_deadline - time.monotonic())
        grace = min(grace, remaining / 4)
        budget = max(0.0, min(budget, remaining - 2 * grace))
    return budget, grace


async def _next_event(
    sub: AsyncIterator[Event], budget_seconds: float | None
) -> Event | _StreamEnd | None:
    """The next live event, ``None`` on timeout, ``_STREAM_END`` when closed."""
    try:
        if budget_seconds is None:
            return await anext(sub)
        async with asyncio.timeout(budget_seconds):
            return await anext(sub)
    except TimeoutError:
        return None
    except StopAsyncIteration:
        return _STREAM_END


async def _settle(
    session: ChatSession, sub: AsyncIterator[Event], collector: _Collector, start: int
) -> SessionStatusChanged | _StreamEnd | None:
    """Collect events until the runtime settles the turn, returning the terminal
    it settled on. The settlement edge (`turn_settlements`) is the signal, never
    a sample of `turn_active` or an event's shape, so a wake turn or a status
    the runtime holds the turn through cannot close the record early. ``start``
    is the settlement cursor captured BEFORE the prompt was sent, so a turn that
    settles inside the send still lands on its own entry. The trailing drain
    collects up to the settling terminal, matched by identity; a wake turn's
    events stay queued for the caller. A stream that closes before the turn
    settles returns ``_STREAM_END``, never a cancel a human did not perform."""
    seen: set[str] = set()
    while session.turn_settlements <= start:
        event = await _next_event(sub, 0.1)
        if isinstance(event, _StreamEnd):
            if session.turn_settlements > start:
                return session.settlement(start)
            return _STREAM_END
        if event is not None:
            collector.handle(event)
            seen.add(event.event_id)
    terminal = session.settlement(start)
    # Drain up to the settling terminal ONLY if the first loop did not already
    # consume it; a second search would sweep the background wake turn the
    # flush injects into this turn's record.
    while terminal is not None and terminal.event_id not in seen:
        event = await _next_event(sub, 0.5)
        if event is None or isinstance(event, _StreamEnd):
            break
        collector.handle(event)
        if event.event_id == terminal.event_id:
            break
    return terminal


def _jobs_in_flight(jobs: Sequence[Any], *, now: datetime) -> bool:
    """A scheduler job is in flight iff RUNNING or SCHEDULED-and-due-now — the
    same predicate the lineage tools use to wait out a refresh."""
    for job in jobs:
        state = getattr(job, "state", "")
        if state == "running":
            return True
        if state == "scheduled":
            nxt = getattr(job, "next_run_at", None)
            if nxt is not None and nxt <= now:
                return True
    return False


async def _wait_for_seed(runtime: HarnessRuntime, budget_seconds: float) -> None:
    """Wait (bounded) for the initial lineage/KB seed to settle so tools that
    read the graph see a populated project. Best-effort: a budget overrun just
    proceeds — the tools themselves re-wait briefly per call."""
    deadline = time.monotonic() + budget_seconds
    # Grace window: give the beat a moment to claim the due-now seed jobs so an
    # instant "nothing running" right after open doesn't read as settled.
    grace_deadline = time.monotonic() + min(5.0, budget_seconds)
    seen_activity = False
    while time.monotonic() < deadline:
        try:
            jobs = runtime.scheduler().list_jobs()
        except Exception:
            logger.exception("headless: scheduler snapshot failed; skipping seed wait")
            return
        in_flight = _jobs_in_flight(jobs, now=datetime.now(UTC))
        if in_flight:
            seen_activity = True
        elif seen_activity or time.monotonic() >= grace_deadline:
            return
        await asyncio.sleep(1.0)
    logger.warning("headless: seed did not settle within %.0fs; proceeding", budget_seconds)


async def _build_production_runtime(
    project_root: Path | ProductionRuntimeOptions, **kwargs: Any
) -> tuple[HarnessRuntime, dict[str, Any]]:
    """Build the interactive-client runtime and its pinned manifest model."""
    options = _production_options(project_root, kwargs)
    try:
        sweep_orphaned_agents()
    except Exception:
        logger.debug("orphan-agent sweep failed", exc_info=True)

    auth_token = _auth_token(options.project_root)
    gateway_url = get_settings().alkera_gateway_url
    catalog = await _fetch_runtime_catalog(gateway_url, auth_token)
    models = catalog.models
    if options.harness_type == CLAUDE_HARNESS:
        models = [m for m in models if m.wire == "anthropic"]
    selectable = selectable_models(models)
    pinned = _resolve_runtime_model(models, selectable, options)

    runtime = production_runtime(
        "headless",
        options.project_root,
        catalog=selectable,
        web_tools=WebToolFlags(search=catalog.web_search_enabled, fetch=catalog.web_fetch_enabled),
        system_block_overrides=options.system_block_overrides,
    )
    return runtime, pinned


def _production_options(
    project_root: Path | ProductionRuntimeOptions, kwargs: Mapping[str, Any]
) -> ProductionRuntimeOptions:
    """Normalize the historical build seam to the options object."""
    if isinstance(project_root, ProductionRuntimeOptions):
        return project_root
    return ProductionRuntimeOptions(project_root=project_root, **dict(kwargs))


def _auth_token(project_root: Path) -> str:
    """The token of the sign-in this project's run acts as (the one its chat
    will bind), or raise the user-facing login or pinned-project error."""
    try:
        auth = profile_for_project(project_directory(project_root))
    except ProfileResolutionError as exc:
        raise HeadlessError(str(exc)) from exc
    if auth is None or not auth.token:
        raise HeadlessError(_NOT_SIGNED_IN_MESSAGE)
    return auth.token


async def _fetch_runtime_catalog(gateway_url: str, token: str) -> Any:
    """Fetch the model catalog and translate gateway failures for headless callers."""
    try:
        return await fetch_catalog(gateway_url=gateway_url, token=token)
    except GatewayAuthError as exc:
        suffix = " Run `alkera login`." if exc.status_code == 401 else ""
        raise HeadlessError(_gateway_session_message(exc.detail, suffix)) from exc
    except GatewayUnavailableError as exc:
        raise HeadlessError(_gateway_unavailable_message(exc)) from exc


def _resolve_runtime_model(
    models: list[GatewayModel], selectable: list[GatewayModel], options: ProductionRuntimeOptions
) -> dict[str, Any]:
    """Resolve explicit pins from the full catalog, otherwise use chat defaults."""
    if options.model is not None:
        try:
            chosen = resolve_model_selection(models, options.model, options.effort)
        except ValueError as exc:
            raise HeadlessError(str(exc)) from exc
        return build_manifest_model(chosen, options.effort)
    defaults = resolve_and_persist_chat_defaults(selectable)
    default_model = next((m for m in selectable if m.id == defaults.model_id), None)
    if default_model is None:
        raise HeadlessError(_no_default_model_message())
    return build_manifest_model(default_model, defaults.effort)


async def run_headless(project_dir: Path, prompts: Sequence[str], **kwargs: Any) -> HeadlessResult:
    """Drive prompts through one unattended chat and return the folded result."""
    options = HeadlessRunOptions.from_kwargs(project_dir, prompts, kwargs)
    return await _run_headless(options)


async def _run_headless(options: HeadlessRunOptions) -> HeadlessResult:
    """Execute the headless run lifecycle."""
    state = await _prepare_headless_run(options)
    try:
        await _execute_headless_run(state)
    finally:
        await _close_headless_run(state)
    return _headless_result(state)


async def _prepare_headless_run(options: HeadlessRunOptions) -> HeadlessRunState:
    """Validate the call and open the runtime/event sinks."""
    harness_type = _resolve_harness_or_raise(options)
    runtime, pinned_model, owns_runtime = await _resolve_runtime(options, harness_type)
    events_file = _open_events_file(options.events_path)
    collector = _Collector(on_event=options.on_event, events_file=events_file)
    return HeadlessRunState(
        options=options,
        harness_type=harness_type,
        runtime=runtime,
        owns_runtime=owns_runtime,
        pinned_model=pinned_model,
        started=time.monotonic(),
        collector=collector,
        events_file=events_file,
    )


def _resolve_harness_or_raise(options: HeadlessRunOptions) -> str:
    """Resolve the configured harness name."""
    if not options.prompts:
        raise HeadlessError(_NO_PROMPT_MESSAGE)
    harness_type = resolve_harness_type(options.harness)
    if harness_type is None:
        raise HeadlessError(_unknown_harness_message(options.harness))
    return harness_type


async def _resolve_runtime(
    options: HeadlessRunOptions, harness_type: str
) -> tuple[HarnessRuntime, dict[str, Any] | None, bool]:
    """Return the runtime, pinned model, and whether this run owns the runtime."""
    if options.runtime is not None:
        _validate_injected_runtime(options)
        return options.runtime, None, False
    runtime, pinned_model = await _build_production_runtime(
        options.project_dir,
        harness_type=harness_type,
        model=options.model,
        effort=options.effort,
        system_block_overrides=options.system_block_overrides,
    )
    return runtime, pinned_model, True


def _validate_injected_runtime(options: HeadlessRunOptions) -> None:
    """Reject options that only production runtime construction can honor."""
    if options.model is not None or options.effort is not None:
        raise HeadlessError(_INJECTED_RUNTIME_MODEL_MESSAGE)
    if options.system_block_overrides is not None:
        raise ValueError(_INJECTED_RUNTIME_OVERRIDES_MESSAGE)


def _open_events_file(path: Path | None) -> IO[str] | None:
    """Open the optional JSONL event sink."""
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("a", encoding="utf-8")


async def _execute_headless_run(state: HeadlessRunState) -> None:
    """Open the chat, drive prompts, and record timeout cancellation."""
    await _start_owned_runtime(state)
    try:
        state.session = await _open_headless_session(state)
        state.session.set_permission_mode(state.options.permission_mode)
        if state.options.analysis_pipeline is not None:
            state.session.set_analysis_pipeline(state.options.analysis_pipeline)
        sub = state.session.subscribe()
        state.current_sub = sub
        async with asyncio.timeout(state.options.timeout_seconds):
            await _drive_prompt_turns(state, sub)
            await _drain_background_continuations(state, sub)
    except TimeoutError:
        await _record_timeout(state)
        if state.session is not None:
            with contextlib.suppress(Exception):
                await state.session.cancel()


async def _record_timeout(state: HeadlessRunState) -> None:
    """Record a timeout without discarding a turn whose stream already died."""
    terminal = await _drain_timeout_terminal(state)
    prompt = state.current_prompt or _pending_prompt(state)
    if terminal is not None and prompt is not None:
        state.turns.append(state.collector.end_turn(prompt, terminal))
        state.current_prompt = None
        return
    state.run_stop = "timeout"
    state.error_detail = f"run did not settle within {state.options.timeout_seconds}s"


def _pending_prompt(state: HeadlessRunState) -> str | None:
    """Prompt whose turn has no recorded result yet."""
    index = len(state.turns)
    if index >= len(state.options.prompts):
        return None
    return state.options.prompts[index]


async def _drain_timeout_terminal(
    state: HeadlessRunState,
) -> SessionStatusChanged | _StreamEnd | None:
    """Drain already-queued events after timeout before declaring a true timeout."""
    sub = state.current_sub
    if sub is None:
        return None
    for _ in range(8):
        event = await _next_event(sub, 0.01)
        if event is None:
            return None
        if isinstance(event, _StreamEnd):
            return _STREAM_END
        state.collector.handle(event)
        if isinstance(event, SessionStatusChanged):
            return event
    return None


async def _start_owned_runtime(state: HeadlessRunState) -> None:
    """Start runtime-owned background services before opening the chat."""
    if not state.owns_runtime:
        return
    state.runtime.start_scheduler_beat()
    budget = state.options.wait_for_seed_seconds
    if budget is not None:
        await _wait_for_seed(state.runtime, budget)


async def _open_headless_session(state: HeadlessRunState) -> ChatSession:
    """Open or resume the chat with unattended permission/question brokers."""
    options = state.options
    try:
        return await state.runtime.open_chat(
            options.resume_session_id,
            create=options.resume_session_id is None,
            harness_type=state.harness_type,
            title=options.title or _truncate(options.prompts[0], 60),
            model=state.pinned_model,
            permission_broker=PermissionBroker(
                _permission_resolver(state), default_timeout_seconds=60.0
            ),
            question_broker=QuestionBroker(_question_resolver, default_timeout_seconds=60.0),
        )
    except LockHeldError as exc:
        raise HeadlessError(_chat_locked_message(exc)) from exc
    except (HarnessUnavailableError, HarnessStartError) as exc:
        raise HeadlessError(str(exc)) from exc


def _permission_resolver(
    state: HeadlessRunState,
) -> Callable[[PermissionRequest], Any]:
    """Build the unattended permission resolver that records promptable requests."""

    async def _resolve(request: PermissionRequest) -> PermissionOptionId:
        decision: PermissionOptionId = (
            "allow_once" if state.options.on_permission == "allow" else "reject_once"
        )
        state.permission_records.append(
            {
                "request_id": request.request_id,
                "permission_kind": request.permission_kind,
                "canonical_kind": request.canonical_kind,
                "patterns": list(request.patterns),
                "subject": request.subject,
                "decision": decision,
            }
        )
        return decision

    return _resolve


async def _question_resolver(_request: object) -> QuestionResolution:
    return ("reject", QUESTION_DECLINE_REASON)


async def _drive_prompt_turns(state: HeadlessRunState, sub: AsyncIterator[Event]) -> None:
    """Send prompts until one crashes, errors, is cancelled, or wedges."""
    session = _require_session(state)
    for index, prompt in enumerate(state.options.prompts):
        if index:
            # A wake landing after the previous prompt is its continuation.
            await _drain_to_quiet(session, sub, state.collector, state.turns, settle=False)
        state.verification, state.verification_fired = None, False
        turn = await _drive_one_prompt(state, session, sub, prompt)
        if turn.stop_reason in ("error", "cancelled"):
            return
        if state.wedged:
            left = len(state.options.prompts) - index - 1
            if left:
                state.run_stop = "timeout"
                state.error_detail = f"verification did not settle; {left} prompt(s) not sent"
            return


async def _drive_one_prompt(
    state: HeadlessRunState,
    session: ChatSession,
    sub: AsyncIterator[Event],
    prompt: str,
) -> HeadlessTurn:
    """Send one prompt and fold events until its settlement edge."""
    state.collector.begin_turn()
    start = session.turn_settlements
    state.current_prompt = prompt
    try:
        await session.send_prompt(prompt, model=state.options.turn_model)
    except HarnessCrashError as exc:
        state.current_prompt = None
        turn = HeadlessTurn(
            prompt=prompt, final_text="", stop_reason="error", error_detail=str(exc)
        )
        state.turns.append(turn)
        return turn
    terminal = await _settle(session, sub, state.collector, start)
    state.current_prompt = None
    turn = state.collector.end_turn(prompt, terminal)
    state.turns.append(turn)
    if turn.stop_reason not in ("error", "cancelled") and session.analysis_pipeline == "analyst":
        await _settle_prompt_verification(state, session, sub)
    return turn


async def _settle_prompt_verification(
    state: HeadlessRunState, session: ChatSession, sub: AsyncIterator[Event]
) -> None:
    """Collect the one turn the runtime may issue after the caller prompt and
    record its outcome. Nothing in the settle path may lose the first answer."""
    run_deadline = (
        None
        if state.options.timeout_seconds is None
        else state.started + state.options.timeout_seconds
    )
    try:
        budget, grace = _verification_budget(
            state.options.verification_timeout_seconds, run_deadline
        )
        state.verification, state.wedged = await _settle_verification(
            session, sub, state.collector, state.turns, budget, grace, run_deadline
        )
    except Exception:
        logger.exception("headless: verification settlement failed")
        state.collector.release_held(deliver=False)
        state.collector.begin_turn()
        state.turns[-1] = dataclasses.replace(state.turns[-1], delivered=True)
        state.verification = "not re-verified: settlement failed"
    state.verification_fired = session.analysis_verification_fired


async def _drain_background_continuations(
    state: HeadlessRunState, sub: AsyncIterator[Event]
) -> None:
    """Drain background wake turns so finished work lands in the result."""
    if state.wedged:
        return
    session = _require_session(state)
    await _drain_to_quiet(session, sub, state.collector, state.turns, settle=True)


async def _close_headless_run(state: HeadlessRunState) -> None:
    """Release only the resources this run owns."""
    with contextlib.suppress(Exception):
        if state.owns_runtime:
            await state.runtime.close_all()
            await asyncio.to_thread(close_default_reporter)
            await asyncio.to_thread(close_default_exporter)
        elif state.session is not None:
            await state.runtime.close_chat(state.session.session_id)
    if state.events_file is not None:
        with contextlib.suppress(Exception):
            state.events_file.close()


def _headless_result(state: HeadlessRunState) -> HeadlessResult:
    """Build the public result from collected events and the final manifest."""
    run_stop = state.run_stop or (state.turns[-1].stop_reason if state.turns else "error")
    error_detail = state.error_detail
    if error_detail is None and state.turns and state.turns[-1].error_detail:
        error_detail = state.turns[-1].error_detail
    manifest = state.session.manifest if state.session is not None else None
    return HeadlessResult(
        session_id=state.session.session_id if state.session is not None else "",
        stop_reason=run_stop,
        turns=state.turns,
        permission_prompts=state.permission_records,
        tool_calls=state.collector.tool_calls,
        tokens=manifest.tokens_total.model_dump() if manifest is not None else {},
        cost_usd=manifest.cost_total if manifest is not None else 0.0,
        duration_seconds=time.monotonic() - state.started,
        error_detail=error_detail,
        verification=state.verification,
        verification_fired=state.verification_fired,
    )


def _require_session(state: HeadlessRunState) -> ChatSession:
    """Return the opened session."""
    return cast("ChatSession", state.session)


__all__ = [
    "QUESTION_DECLINE_REASON",
    "HeadlessError",
    "HeadlessResult",
    "HeadlessTurn",
    "run_headless",
]
