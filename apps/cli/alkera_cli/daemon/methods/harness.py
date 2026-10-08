"""Daemon JSON-RPC methods for the harness layer.

The daemon is a **thin façade** over `alkera_cli.harness.HarnessRuntime`.
Every method translates JSON-RPC calls into runtime calls + streams
events back as `harness.event` notifications. Permission/question
asks flow as server→client requests the editor must answer.

The adapter contract and its invariants are in
`apps/cli/alkera_cli/harness/README.md`.

Method surface (request/response):

  - `harness.list_models()` → `{models: [GatewayModelInfo, …]}` (AUTH_REQUIRED if signed out)
  - `harness.list_chats(project_path)` → `{chats: [ChatManifest + status/pending_ask, …]}`
  - `harness.mark_seen(project_path, session_id)` → `{ok, last_seen_event_id}`
  - `harness.open_chat(project_path, session_id?, create?, harness?, title?, model?)`
    (`harness` REQUIRED when `create` is True; ignored on resume)
        → `{session_id, manifest, recent_events: [Event, …]}`
  - `harness.close_chat(session_id)` → `{ok: true}`
  - `harness.delete_chat(project_path, session_id, recursive?)` → `{ok: true}`
  - `harness.send_prompt(session_id, text, model?, variant?)` → `{ok: true}`
  - `harness.cancel(session_id)` → `{ok: true}`
  - `harness.get_permission_mode(session_id)` → `{mode}`
  - `harness.set_permission_mode(session_id, mode)` → `{mode}` (persisted; survives resume)

Notifications (server → client):
  - `harness.event(session_id, event)` — one IR event for a session
  - `harness.lock_held(session_id, holder)` — open_chat refused by lock

Server→client requests:
  - `harness.permission_required(request)` — editor returns chosen optionId
  - `harness.question_required(request)` — editor returns answers or reject
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast, get_args

from alkera_core.project.locking import LockHeldError
from alkera_core.schemas.chat import (
    Event,
    PermissionOptionId,
    PermissionRequest,
    QuestionRequest,
    SessionStatusChanged,
)
from pydantic import Field

from alkera_cli.app.runtime_pool import runtime_for
from alkera_cli.contracts.gateway_model import GatewayModel
from alkera_cli.daemon.profiles import require_project_profile
from alkera_cli.daemon.protocol import _DaemonModel, client_request, method, notification
from alkera_cli.daemon.server import (
    AuthRequiredError,
    SessionNotOpenError,
    register_shutdown_hook,
)
from alkera_cli.gateway.client import (
    GatewayAuthError,
    GatewayUnavailableError,
    fetch_models,
    selectable_models,
)
from alkera_cli.harness import (
    HARNESS_CHOICES,
    OPENCODE_HARNESS,
    ChatSession,
    HarnessRuntime,
    PermissionBroker,
    QuestionBroker,
    QuestionResolution,
    resolve_harness_type,
)
from alkera_cli.harness.adapter import (
    HarnessCrashError,
    HarnessStartError,
    HarnessUnavailableError,
)
from alkera_cli.harness.adapters.opencode_alkera import build_manifest_model
from alkera_cli.harness.claude_gateway import (
    ClaudeModelNotSupportedError,
)
from alkera_cli.harness.event_stream import ChatEventStream
from alkera_cli.harness.gateway_session import (
    GatewayAuthRequiredError,
)
from alkera_cli.harness.permission_mode import PermissionMode, plan_label_to_mode
from alkera_cli.harness.transcript_page import page_before
from alkera_cli.host.config import get_settings

if TYPE_CHECKING:
    from alkera_cli.daemon.server import JsonRpcServer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class _ProjectScoped(_DaemonModel):
    """Mixin: every method that touches a project takes its absolute path."""

    project_path: str


class HarnessListChatsRequest(_ProjectScoped):
    pass


class HarnessListChatsResponse(_DaemonModel):
    chats: list[dict[str, Any]] = Field(default_factory=list)
    """Serialized `ChatManifest` dicts, each enriched with two live-derived
    keys the manifest never persists: ``status`` ("idle" | "running") and
    ``pending_ask`` ("permission" | "question" | "plan" | null). Live-only on
    purpose — a crashed process can't leave a stale spinner behind. The
    manifest's ``last_event_id`` / ``last_seen_event_id`` ride along for the
    unread badge (unread = the two differ)."""


class GatewayModelInfo(_DaemonModel):
    """A model the gateway can serve (from `GET /v1/models`), for the editor's
    model + variant picker."""

    id: str
    display_name: str
    wire: Literal["anthropic", "openai"]
    efforts: list[str] = Field(default_factory=list)
    default_effort: str | None = None


class HarnessListModelsRequest(_DaemonModel):
    """The gateway catalog as ``project_path``'s sign-in (the daemon's when None)."""

    project_path: str | None = None


class HarnessListModelsResponse(_DaemonModel):
    models: list[GatewayModelInfo] = Field(default_factory=list)


class GatewayModelSelection(_DaemonModel):
    """A model the editor chose from ``harness.list_models``, sent on new/open so
    the DAEMON builds the pinned manifest (via ``build_manifest_model``) — ONE
    source for the pinned shape (provider id, effort ladder) across the CLI and
    the editor, instead of the editor re-encoding the gateway provider ids and the
    effort-selection logic in TypeScript."""

    id: str
    display_name: str = ""
    wire: Literal["anthropic", "openai"]
    efforts: list[str] = Field(default_factory=list)
    default_effort: str | None = None


class HarnessOpenChatRequest(_ProjectScoped):
    session_id: str | None = None
    create: bool = False
    harness: str | None = None
    """Which agent harness backs the chat — REQUIRED when ``create`` is True
    (``alkera`` | ``claude`` / slug); IGNORED on resume (the manifest pins it)."""
    title: str | None = None
    model: GatewayModelSelection | None = None
    """The chosen gateway model for a newly-created chat (ignored on resume); the
    daemon builds the pinned manifest from it."""
    effort: str | None = None
    """The chosen reasoning effort for a newly-created chat (ignored on resume)."""
    tail: int | None = Field(default=None, ge=1)
    """Replay only the newest ``tail`` events (extended down to the turn they start
    inside) instead of the whole log; ``oldest_seq`` / ``has_older`` on the response
    then say where the replay begins and whether ``harness.list_events`` has more.
    ``None`` replays everything, as before."""


class HarnessOpenChatResponse(_DaemonModel):
    session_id: str
    manifest: dict[str, Any]
    recent_events: list[dict[str, Any]] = Field(default_factory=list)
    oldest_seq: int | None = None
    """The 1-based ordinal of ``recent_events[0]`` in the persisted log; ``None`` when
    nothing was replayed."""
    has_older: bool = False
    """Whether the log holds events older than ``recent_events`` — only ever True for
    a ``tail`` replay."""
    cut: bool = False
    """Whether the replayed page opens mid-turn, or carries a message whose opening
    event it could not reach: the reader should read the page below before showing
    its first turn as a whole one."""


class HarnessCloseChatRequest(_DaemonModel):
    session_id: str


class HarnessObserveChatRequest(_ProjectScoped):
    session_id: str
    """The chat to OBSERVE read-only (e.g. a running subagent the parent is
    driving). No write lock is taken — observing never conflicts with the owner."""
    tail: int | None = Field(default=None, ge=1)
    """As on ``harness.open_chat``: replay only the newest page."""


class HarnessObserveChatResponse(_DaemonModel):
    session_id: str
    manifest: dict[str, Any] | None = None
    """The observed chat's manifest when it's live in this daemon (for the viewer's
    header/title); ``None`` for a lock-free replay of a chat that isn't running."""
    recent_events: list[dict[str, Any]] = Field(default_factory=list)
    live: bool = False
    """True when a live stream is attached (the chat is open in this daemon — e.g. a
    running subagent); False = a one-shot lock-free replay of a chat that isn't
    currently running."""
    oldest_seq: int | None = None
    has_older: bool = False
    cut: bool = False
    """As on ``harness.open_chat``."""


class HarnessListEventsRequest(_ProjectScoped):
    """A page of a chat's persisted events BELOW ordinal ``before`` — how the editor
    reads older transcript as the reader scrolls up. Lock-free, so it serves a chat
    that is open here, open elsewhere, or not open at all."""

    session_id: str
    before: int = Field(ge=1)
    """Exclusive: the ``oldest_seq`` the previous page (or the open) answered with."""
    limit: int = Field(default=200, ge=1, le=1000)


class HarnessListEventsResponse(_DaemonModel):
    events: list[dict[str, Any]] = Field(default_factory=list)
    """Ascending; extended down to the row that started the page's first turn and
    to the opening event of every message the page carries, within the read's
    budget."""
    oldest_seq: int | None = None
    has_older: bool = False
    cut: bool = False
    """As on ``harness.open_chat``."""


class HarnessStopObserveRequest(_DaemonModel):
    session_id: str


class HarnessOkResponse(_DaemonModel):
    ok: bool = True


class HarnessDeleteChatRequest(_ProjectScoped):
    session_id: str
    recursive: bool = False


class HarnessMarkSeenRequest(_ProjectScoped):
    session_id: str
    """The chat the user's client just rendered — its manifest's
    ``last_seen_event_id`` is stamped from ``last_event_id``, clearing the
    unread badge in every chat list."""


class HarnessMarkSeenResponse(_DaemonModel):
    ok: bool = True
    """False when the chat couldn't be stamped because another live process
    holds its lock — that holder is the one rendering it."""
    last_seen_event_id: str | None = None
    """The stamped tail (mirrors the manifest after the write), so the caller
    can update its cached listing without a re-fetch."""


class HarnessSendPromptRequest(_DaemonModel):
    session_id: str
    text: str
    model: dict[str, str] | None = None
    variant: str | None = None
    """Reasoning-effort variant for this turn (model stays pinned to the chat)."""


class HarnessCancelRequest(_DaemonModel):
    session_id: str


class HarnessGetPermissionModeRequest(_DaemonModel):
    session_id: str


class HarnessSetPermissionModeRequest(_DaemonModel):
    session_id: str
    mode: PermissionMode
    """``read_only`` / ``default`` / ``auto`` / ``plan`` / ``bypass``. Validated
    by the Literal at the JSON-RPC boundary, so a bad value is a typed error, not
    a silent no-op."""


class HarnessPermissionModeResponse(_DaemonModel):
    mode: PermissionMode
    """The session's permission mode after the call (the persisted value)."""


class HarnessGetCostStateRequest(_DaemonModel):
    session_id: str


class HarnessSetCostLimitsRequest(_DaemonModel):
    session_id: str
    caps: dict[str, float]
    """``{window: usd}`` for ``per-query`` / ``chat`` / ``day`` / ``week`` — written
    to the gitignored ``permissions.local.yml`` (parity with the CLI ``/cost set``)."""


class HarnessCostStateResponse(_DaemonModel):
    spent: dict[str, float]
    """``{chat, day, week}`` tool-execution spend (USD) for this chat's windows."""
    caps: dict[str, float]
    """``{per_query, chat, day, week}`` effective caps (USD)."""
    org_managed: bool
    unknown_cost_keys: list[str] = Field(default_factory=list)
    """Unrecognized ``cost:`` keys (a hand-edit typo that silently did nothing) —
    the editor can flag them, same as the CLI ``/cost`` view."""


class HarnessListDecisionsRequest(_DaemonModel):
    session_id: str
    limit: int = Field(default=200, ge=0)
    offset: int = Field(default=0, ge=0)
    decided_by: str | None = None
    """Filter to one decider — ``judge`` powers the Safety tab (the safety-judge
    verdicts), which aren't persisted separately from the decision log."""


class HarnessDecisionView(_DaemonModel):
    """One audited permission decision, flattened for the editor's activity log.
    Mirrors ``DecisionRecord`` minus the internals the UI doesn't render."""

    at: float = 0.0
    request_id: str = ""
    source: str = ""
    capability: str = ""
    effect: str = ""
    operation: str = ""
    targets: list[str] = Field(default_factory=list)
    mode: str = ""
    decision: str = ""
    decided_by: str = ""
    reasons: list[str] = Field(default_factory=list)


class HarnessListDecisionsResponse(_DaemonModel):
    decisions: list[HarnessDecisionView] = Field(default_factory=list)
    has_more: bool = False


class HarnessListCostLedgerRequest(_DaemonModel):
    session_id: str
    limit: int = Field(default=200, ge=0)
    offset: int = Field(default=0, ge=0)


class HarnessCostLedgerEntryView(_DaemonModel):
    """One warehouse charge in a chat's ledger. ``charged_usd`` is the amount
    that counted toward the windows (settled actual where known, else estimate)."""

    entry_id: str = ""
    connection: str = ""
    operation: str = ""
    estimate_usd: float = 0.0
    actual_usd: float | None = None
    charged_usd: float = 0.0
    wallet_currency: str = "usd"
    at: float = 0.0


class HarnessListCostLedgerResponse(_DaemonModel):
    entries: list[HarnessCostLedgerEntryView] = Field(default_factory=list)
    has_more: bool = False


class HarnessListCommandsRequest(_DaemonModel):
    pass


class SlashCommandInfo(_DaemonModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    summary: str
    usage: str
    """``()`` marks a required arg, ``[]`` an optional one — the CLI's own
    convention, rendered verbatim by editors."""
    hidden: bool = False
    """Dispatchable but left out of menus (the mode shortcuts)."""
    ui: bool = True
    """False for CLI-only commands (the editor has a native equivalent —
    mode picker, preferences tab); editors exclude them from menus and a
    typed invocation answers with the spec's hint."""


class HarnessListCommandsResponse(_DaemonModel):
    commands: list[SlashCommandInfo]
    """The CLI's slash-command registry, verbatim — Python is the single
    source of truth; editors render and dispatch, never re-define."""


class HarnessRunCommandRequest(_DaemonModel):
    session_id: str
    line: str
    """The raw composer line, leading ``/`` included (``/mode auto``)."""


class HarnessRunCommandResponse(_DaemonModel):
    kind: Literal["ok", "bad_usage", "exit", "cli_only", "unknown"]
    """``exit`` is /exit//quit — the editor leaves the chat surface (the
    daemon session stays open). ``cli_only`` answers a command whose
    interaction belongs to the REPL — ``message`` points at the editor's
    native equivalent."""
    command: str | None = None
    """Canonical command name (aliases resolved), for the editor's renderer
    switch."""
    payload: dict[str, Any] = Field(default_factory=dict)
    """Structured result data (e.g. /usage's credits + totals) — the editor
    draws native UI from this instead of a text dump."""
    message: str | None = None
    """Human text for bad_usage (the usage line), cli_only (the hint), and
    unknown."""


class HarnessSetModelEffortRequest(_DaemonModel):
    session_id: str
    effort: str


class HarnessSetModelEffortResponse(_DaemonModel):
    model: dict[str, Any]
    """The manifest's pinned model dict after the update (id/efforts/effort)."""


# --- Notifications -------------------------------------------------------


@notification("harness.event")
class HarnessEventNotification(_DaemonModel):
    """One IR event for a session."""

    session_id: str
    event: dict[str, Any]


@notification("harness.lock_held")
class HarnessLockHeldNotification(_DaemonModel):
    session_id: str
    holder: dict[str, Any]


@notification("harness.session_state_changed")
class HarnessSessionStateChangedNotification(_DaemonModel):
    """Server → client: session state the editor mirrors changed out-of-band
    (e.g. a plan approval flipped the permission mode). The client updates its
    view from this push instead of re-fetching on a timing guess. Extensible —
    further mirrored fields can join ``permission_mode`` here."""

    session_id: str
    permission_mode: PermissionMode | None = None


# --- Server → client request ---------------------------------------------


class HarnessPermissionRequiredRequest(_DaemonModel):
    session_id: str
    request: dict[str, Any]


class HarnessPermissionRequiredResponse(_DaemonModel):
    option_id: PermissionOptionId


_VALID_OPTION_IDS = frozenset(get_args(PermissionOptionId))


def parse_permission_option(resp: Any) -> PermissionOptionId:
    """An editor's reply to a ``*.permission_required`` request → a known option
    id; ANY unrecognized / malformed reply fails closed to ``reject_once``. One
    parser for every editor-prompt path (chat + the direct ``tool.call``), so the
    option vocabulary can't drift between them."""
    opt = resp.get("option_id") if isinstance(resp, dict) else None
    return cast("PermissionOptionId", opt) if opt in _VALID_OPTION_IDS else "reject_once"


_perm_pair = client_request("harness.permission_required")(
    (HarnessPermissionRequiredRequest, HarnessPermissionRequiredResponse)
)


class HarnessQuestionRequiredRequest(_DaemonModel):
    """Daemon → editor: a clarifier question awaits a user answer."""

    session_id: str
    request: dict[str, Any]
    """Serialized ``QuestionRequest``. The editor renders the prompt
    UI and POSTs back a `HarnessQuestionRequiredResponse`."""


class HarnessQuestionRequiredResponse(_DaemonModel):
    """Editor → daemon: how the user answered.

    Discriminated by ``kind``:
    - ``"answer"`` carries ``answers: list[list[str]]`` parallel to
      ``QuestionRequest.questions``.
    - ``"reject"`` carries an optional ``reason`` (free text); the
      harness treats it as a hard rejection."""

    kind: Literal["answer", "reject"]
    answers: list[list[str]] = Field(default_factory=list)
    reason: str | None = None


_question_pair = client_request("harness.question_required")(
    (HarnessQuestionRequiredRequest, HarnessQuestionRequiredResponse)
)


def _event_forwarders(server: JsonRpcServer) -> dict[str, asyncio.Task[None]]:
    forwarders: dict[str, asyncio.Task[None]] | None = getattr(server, "harness_forwarders", None)
    if forwarders is None:
        forwarders = {}
        server.harness_forwarders = forwarders  # type: ignore[attr-defined]
    return forwarders


def _observe_forwarders(server: JsonRpcServer) -> dict[str, asyncio.Task[None]]:
    """Forward tasks for READ-ONLY observers (harness.observe_chat), kept in a map
    SEPARATE from the open-for-write forwarders so an observer's lifecycle can never
    cancel an owner's live stream (and vice-versa) even for the same session_id."""
    forwarders: dict[str, asyncio.Task[None]] | None = getattr(server, "harness_observers", None)
    if forwarders is None:
        forwarders = {}
        server.harness_observers = forwarders  # type: ignore[attr-defined]
    return forwarders


async def _cancel_forwarder(forwarders: dict[str, asyncio.Task[None]], session_id: str) -> None:
    """Cancel + await an existing forward task for ``session_id`` (if any)."""
    fwd = forwarders.pop(session_id, None)
    if fwd is not None and not fwd.done():
        fwd.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await fwd


@dataclass(frozen=True, slots=True)
class _Replay:
    """What an open / observe hands back about the persisted prefix."""

    events: list[dict[str, Any]]
    oldest_seq: int | None
    has_older: bool
    cut: bool = False


async def _attach_stream(
    server: JsonRpcServer,
    session_id: str,
    stream: ChatEventStream,
    *,
    forwarders: dict[str, asyncio.Task[None]],
    tail: int | None = None,
) -> _Replay:
    """Forward a ``ChatEventStream``'s LIVE events as ``harness.event`` notifications
    and return its HISTORY (the replay) for the RPC response. The single replay+forward
    path shared by ``open_chat`` (owner) and ``observe_chat`` (read-only observer).

    ``tail`` slices the replay to its newest page (``page_before``); the live
    de-duplication still works from the whole history snapshot, so an event in the
    subscribe overlap is dropped whether or not it made the page."""
    await _cancel_forwarder(forwarders, session_id)
    forwarders[session_id] = asyncio.create_task(
        _forward_loop(server, session_id, stream.live()),
        name=f"harness-forward-{session_id}",
    )
    history = stream.history
    if tail is None:
        return _Replay(
            events=[event.model_dump(mode="json") for event in history],
            oldest_seq=1 if history else None,
            has_older=False,
        )
    page = page_before(history, before=None, limit=tail)
    return _Replay(
        events=[event.model_dump(mode="json") for event in page.events],
        oldest_seq=page.oldest_seq,
        has_older=page.has_older,
        cut=page.cut,
    )


def _session_to_runtime(server: JsonRpcServer) -> dict[str, HarnessRuntime]:
    sm: dict[str, HarnessRuntime] | None = getattr(server, "harness_session_runtime", None)
    if sm is None:
        sm = {}
        server.harness_session_runtime = sm  # type: ignore[attr-defined]
    return sm


def _session_op_lock(server: JsonRpcServer, session_id: str) -> asyncio.Lock:
    """A per-session lock serializing open vs. close/reap for one ``session_id``.

    The daemon dispatches every RPC in its own task, so a ``close_chat`` and an
    ``open_chat`` for the SAME chat can interleave at awaits. A full teardown pops the
    addressing map and then awaits ``rt.close_chat`` — which holds the chat's on-disk
    ``.lock`` through a multi-step drain (background drain, adapter stop, persist pump)
    and releases it only at the very end. Without serialization a concurrent re-attach
    can't take the resident fast path (the map was already popped) and falls through to
    a full re-open, hitting ``LockHeldError`` on the still-held ``.lock``. Holding this
    lock around the open body and every close/reap site forces the open to wait out the
    teardown, then re-open cleanly. Bounded on both sides (open + teardown are bounded),
    keyed by sid so distinct chats never contend; never acquired re-entrantly."""
    locks: dict[str, asyncio.Lock] | None = getattr(server, "harness_session_op_locks", None)
    if locks is None:
        locks = {}
        server.harness_session_op_locks = locks  # type: ignore[attr-defined]
    lock = locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        locks[session_id] = lock
    return lock


#: How often a resident-session reaper checks whether the last background job has
#: finished. A coarse poll is fine — it only runs for a detached chat that still has
#: jobs, and the reap is not latency-sensitive.
_RESIDENT_REAP_POLL_SECONDS = 2.0


def _resident_reapers(server: JsonRpcServer) -> dict[str, asyncio.Task[None]]:
    """Per-session "reap when idle" tasks for chats kept RESIDENT after a UI detach
    because they still had running background jobs — each closes its session once the
    last job finishes (a re-attach cancels it first)."""
    reapers: dict[str, asyncio.Task[None]] | None = getattr(
        server, "harness_resident_reapers", None
    )
    if reapers is None:
        reapers = {}
        server.harness_resident_reapers = reapers  # type: ignore[attr-defined]
    return reapers


async def _cancel_resident_reaper(server: JsonRpcServer, session_id: str) -> None:
    task = _resident_reapers(server).pop(session_id, None)
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def _spawn_resident_reaper(server: JsonRpcServer, rt: HarnessRuntime, session_id: str) -> None:
    """Keep a detached chat resident until its last background job finishes, then
    reap it (pop the addressing map + release the lock + free the adapter). A
    re-attach cancels the reaper first, so it never closes a chat the user came back
    to; and it reaps only while the chat is still DETACHED (no live forwarder)."""
    reapers = _resident_reapers(server)

    async def _reap() -> None:
        try:
            while True:
                session = rt.open_session(session_id)
                if session is None or not session.has_running_background:
                    break
                await asyncio.sleep(_RESIDENT_REAP_POLL_SECONDS)
        except asyncio.CancelledError:
            reapers.pop(session_id, None)
            raise  # re-attached / shutting down → leave the session as-is
        reapers.pop(session_id, None)
        # Serialize the reap-close (pop map + drain under the .lock) against a concurrent
        # same-sid open, exactly like the full-teardown branch — otherwise a re-attach
        # landing between the pop and the lock release hits LockHeldError. Safe vs.
        # _cancel_resident_reaper: a reaper still WAITING on this lock is cancelled
        # cleanly out of the acquire (it already removed itself from `reapers` above).
        async with _session_op_lock(server, session_id):
            # Idle now — reap only if still resident AND still detached (no forwarder).
            still_resident = session_id in _session_to_runtime(server)
            still_detached = session_id not in _event_forwarders(server)
            if still_resident and still_detached:
                session = rt.open_session(session_id)
                if session is not None and not session.has_running_background:
                    _session_to_runtime(server).pop(session_id, None)
                    with contextlib.suppress(Exception):
                        await rt.close_chat(session_id)

    reapers[session_id] = asyncio.create_task(_reap(), name=f"harness-reaper-{session_id}")


# ---------------------------------------------------------------------------
# Method handlers
# ---------------------------------------------------------------------------


@method("harness.list_chats")
async def harness_list_chats(
    server: JsonRpcServer, params: HarnessListChatsRequest
) -> HarnessListChatsResponse:
    rt = runtime_for(server, params.project_path)

    def _list() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in rt.list_chats():
            status, pending_ask = rt.chat_list_status(m.session_id)
            out.append({**m.model_dump(mode="json"), "status": status, "pending_ask": pending_ask})
        return out

    chats = await asyncio.get_running_loop().run_in_executor(None, _list)
    return HarnessListChatsResponse(chats=chats)


@method("harness.mark_seen")
async def harness_mark_seen(
    server: JsonRpcServer, params: HarnessMarkSeenRequest
) -> HarnessMarkSeenResponse:
    rt = runtime_for(server, params.project_path)
    # Serialize against a concurrent same-sid open/close so the stamp never races
    # a teardown into LockHeldError (same discipline as open_chat).
    async with _session_op_lock(server, params.session_id):
        try:
            seen = await rt.mark_chat_seen(params.session_id)
        except LockHeldError:
            return HarnessMarkSeenResponse(ok=False)
    return HarnessMarkSeenResponse(last_seen_event_id=seen)


@method("harness.list_models")
async def harness_list_models(
    server: JsonRpcServer, params: HarnessListModelsRequest
) -> HarnessListModelsResponse:
    """The models (+ reasoning-effort variants) the gateway can serve for the
    signed-in user. Raises AUTH_REQUIRED (-32001) when not signed in so the
    extension can trigger its login flow."""
    auth = require_project_profile(params.project_path)
    try:
        models = await fetch_models(gateway_url=get_settings().alkera_gateway_url, token=auth.token)
    except GatewayAuthError as exc:
        raise AuthRequiredError(exc.detail or "rejected") from exc
    except GatewayUnavailableError as exc:
        raise RuntimeError(f"model gateway unavailable: {exc}") from exc
    # Present only human-pickable models — drop e2e test-fixture families so the
    # editor's model dropdown (and the new-chat default resolver) match the TUI
    # picker, which filters the same way (`selectable_models`).
    return HarnessListModelsResponse(
        models=[
            GatewayModelInfo(
                id=m.id,
                display_name=m.display_name,
                wire=m.wire,
                efforts=list(m.efforts),
                default_effort=m.default_effort,
            )
            for m in selectable_models(models)
        ]
    )


def _resolve_daemon_harness(harness: str) -> str:
    """Friendly daemon ``harness`` param → canonical slug; ``RuntimeError`` on an
    unknown value (surfaces as a JSON-RPC error)."""
    slug = resolve_harness_type(harness)
    if slug is None:
        raise RuntimeError(f"unknown harness {harness!r}; choose: {', '.join(HARNESS_CHOICES)}")
    return slug


def _manifest_from_selection(
    selection: GatewayModelSelection | None, effort: str | None
) -> dict[str, Any] | None:
    """Build the pinned manifest model dict from the editor's chosen gateway model
    via the SHARED ``build_manifest_model`` — the same code the CLI's `_to_selection`
    calls, so a chat pins the identical shape whether it was started from `alkera
    chat` or the editor, and the gateway provider id lives in exactly one place."""
    if selection is None:
        return None
    model = GatewayModel(
        id=selection.id,
        display_name=selection.display_name,
        wire=selection.wire,
        efforts=tuple(selection.efforts),
        default_effort=selection.default_effort,
    )
    return build_manifest_model(model, effort)


@method("harness.open_chat")
async def harness_open_chat(
    server: JsonRpcServer, params: HarnessOpenChatRequest
) -> HarnessOpenChatResponse:
    # Serialize against a concurrent same-sid close/reap (see _session_op_lock): an open
    # that interleaves a teardown waits out the .lock-holding drain, then re-opens
    # cleanly instead of racing it into LockHeldError. Bounded; distinct sids don't contend.
    async with _session_op_lock(server, params.session_id or ""):
        return await _open_chat_locked(server, params)


async def _open_chat_locked(
    server: JsonRpcServer, params: HarnessOpenChatRequest
) -> HarnessOpenChatResponse:
    rt = runtime_for(server, params.project_path)

    # Re-attach to a RESIDENT session — one kept alive after a UI detach because it
    # still had running background jobs. Re-bind its live stream WITHOUT re-opening
    # (which would hit the still-held write lock). The brokers route through `server`
    # (stable across the detach/re-attach), so the user can drive the chat again.
    if not params.create and params.session_id:
        resident = rt.open_session(params.session_id)
        if resident is not None and params.session_id in _session_to_runtime(server):
            await _cancel_resident_reaper(server, params.session_id)
            replay = await _attach_stream(
                server,
                params.session_id,
                ChatEventStream.for_session(resident),
                forwarders=_event_forwarders(server),
                tail=params.tail,
            )
            return HarnessOpenChatResponse(
                session_id=resident.session_id,
                manifest=resident.manifest.model_dump(mode="json"),
                recent_events=replay.events,
                oldest_seq=replay.oldest_seq,
                has_older=replay.has_older,
                cut=replay.cut,
            )

    # A new chat must declare its harness (parity with the CLI's --harness);
    # on resume the harness is pinned in the manifest and `harness_type` is ignored.
    if params.create:
        if not params.harness:
            raise RuntimeError("a new chat requires 'harness' (alkera | claude)")
        harness_type = _resolve_daemon_harness(params.harness)
    else:
        harness_type = OPENCODE_HARNESS

    async def editor_resolver(req: PermissionRequest) -> PermissionOptionId:
        try:
            resp = await server.request(
                "harness.permission_required",
                HarnessPermissionRequiredRequest(
                    session_id=req.session_id,
                    request=req.model_dump(mode="json"),
                ),
                timeout_seconds=None,
            )
        except Exception:
            logger.exception("editor permission_required failed; rejecting")
            return "reject_once"
        return parse_permission_option(resp)

    broker = PermissionBroker(editor_resolver, default_timeout_seconds=None)

    async def editor_question_resolver(req: QuestionRequest) -> QuestionResolution:
        try:
            resp = await server.request(
                "harness.question_required",
                HarnessQuestionRequiredRequest(
                    session_id=req.session_id,
                    request=req.model_dump(mode="json"),
                ),
                timeout_seconds=None,
            )
        except Exception:
            logger.exception("editor question_required failed; rejecting")
            return ("reject", "transport-error")
        if not isinstance(resp, dict):
            return ("reject", "malformed-response")
        kind = resp.get("kind")
        if kind == "answer":
            answers_raw = resp.get("answers") or []
            answers: list[list[str]] = []
            if isinstance(answers_raw, list):
                for inner in answers_raw:
                    if isinstance(inner, list):
                        answers.append([str(x) for x in inner if isinstance(x, str)])
            applied_mode = _apply_plan_choice(server, req, answers)
            if applied_mode is not None:
                await server.notify(
                    "harness.session_state_changed",
                    HarnessSessionStateChangedNotification(
                        session_id=req.session_id,
                        permission_mode=applied_mode,
                    ),
                )
            return ("answer", answers)
        if kind == "reject":
            reason = resp.get("reason")
            return ("reject", reason if isinstance(reason, str) else None)
        return ("reject", "unknown-kind")

    question_broker = QuestionBroker(editor_question_resolver, default_timeout_seconds=None)

    try:
        session = await rt.open_chat(
            params.session_id,
            create=params.create,
            harness_type=harness_type,
            title=params.title,
            model=_manifest_from_selection(params.model, params.effort),
            permission_broker=broker,
            question_broker=question_broker,
        )
    except LockHeldError as exc:
        await server.notify(
            "harness.lock_held",
            HarnessLockHeldNotification(
                session_id=str(params.session_id or ""),
                holder=exc.holder or {},
            ),
        )
        raise
    except ClaudeModelNotSupportedError as exc:
        # A claude chat pinned a non-Anthropic model — NOT an auth problem, so a
        # login flow won't help. Surface as a plain error. (Must precede the
        # GatewayAuthRequiredError clause it subclasses.)
        raise RuntimeError(str(exc)) from exc
    except GatewayAuthRequiredError as exc:
        # The chat routes through the gateway but there's no valid token —
        # surface AUTH_REQUIRED so the extension fires its login flow.
        raise AuthRequiredError("missing") from exc
    except HarnessUnavailableError as exc:
        raise RuntimeError(f"harness unavailable: {exc}") from exc
    except (HarnessStartError, HarnessCrashError) as exc:
        raise RuntimeError(f"harness failed to start: {exc}") from exc

    _session_to_runtime(server)[session.session_id] = rt

    # Replay the log, then forward live events, through the path observe_chat uses.
    replay = await _attach_stream(
        server,
        session.session_id,
        ChatEventStream.for_session(session),
        forwarders=_event_forwarders(server),
        tail=params.tail,
    )
    await session.report_interrupted_jobs()  # a job the last daemon lost, now streamed

    return HarnessOpenChatResponse(
        session_id=session.session_id,
        manifest=session.manifest.model_dump(mode="json"),
        recent_events=replay.events,
        oldest_seq=replay.oldest_seq,
        has_older=replay.has_older,
        cut=replay.cut,
    )


@method("harness.close_chat")
async def harness_close_chat(
    server: JsonRpcServer, params: HarnessCloseChatRequest
) -> HarnessOkResponse:
    sid = params.session_id
    # Serialize against a concurrent same-sid open/re-attach: the full-teardown branch
    # below pops the addressing map then drains the session while still holding the
    # chat's on-disk .lock, so an interleaved open must wait it out (see _session_op_lock).
    async with _session_op_lock(server, sid):
        sm = _session_to_runtime(server)
        rt = sm.get(sid)
        session = rt.open_session(sid) if rt is not None else None
        # Navigate-away from a chat that still has RUNNING background jobs keeps the chat
        # RESIDENT so the jobs survive (and keep notifying + persisting): detach the UI
        # stream, but don't pop the addressing map or tear the session down. A reaper
        # closes it once the last job finishes (or a re-attach cancels the reaper).
        if rt is not None and session is not None and session.has_running_background:
            await _cancel_forwarder(_event_forwarders(server), sid)
            _spawn_resident_reaper(server, rt, sid)
            return HarnessOkResponse()
        # No running jobs → full teardown.
        sm.pop(sid, None)
        await _cancel_forwarder(_event_forwarders(server), sid)
        if rt is not None:
            await rt.close_chat(sid)
        return HarnessOkResponse()


@method("harness.observe_chat")
async def harness_observe_chat(
    server: JsonRpcServer, params: HarnessObserveChatRequest
) -> HarnessObserveChatResponse:
    """Attach to a chat's event stream READ-ONLY — replay its persisted prefix +
    stream live events — WITHOUT taking the write lock. The fix for "lock already
    taken" when inspecting a RUNNING subagent (the parent holds the write lock): we
    never call ``open_chat`` here. If the chat is open in this runtime (a running
    subagent), we subscribe to its live in-process bus; otherwise we replay its
    persisted events lock-free. Live events flow as ``harness.event`` notifications
    tagged with the observed ``session_id`` (the same channel open_chat uses, so the
    editor folds them identically)."""
    rt = runtime_for(server, params.project_path)
    session = rt.open_session(params.session_id)
    manifest: dict[str, Any] | None = None
    if session is not None:
        stream = ChatEventStream.for_session(session)
        manifest = session.manifest.model_dump(mode="json")
        live = True
    else:
        stream = ChatEventStream.replay(rt.read_chat_events(params.session_id))
        live = False
    replay = await _attach_stream(
        server,
        params.session_id,
        stream,
        forwarders=_observe_forwarders(server),
        tail=params.tail,
    )
    return HarnessObserveChatResponse(
        session_id=params.session_id,
        manifest=manifest,
        recent_events=replay.events,
        live=live,
        oldest_seq=replay.oldest_seq,
        has_older=replay.has_older,
        cut=replay.cut,
    )


@method("harness.list_events")
async def harness_list_events(
    server: JsonRpcServer, params: HarnessListEventsRequest
) -> HarnessListEventsResponse:
    """A page of the chat's persisted events below ``before`` — the read behind
    scrolling up. It goes through the lock-free log reader, so it never blocks
    (or is blocked by) the chat's writer, and it serves a chat that is not open
    here at all. The log is parsed off the loop: a long chat is a long file."""
    rt = runtime_for(server, params.project_path)
    session_id = params.session_id

    def _read() -> list[Event]:
        return list(rt.read_chat_events(session_id))

    events = await asyncio.to_thread(_read)
    page = page_before(events, before=params.before, limit=params.limit)
    return HarnessListEventsResponse(
        events=[event.model_dump(mode="json") for event in page.events],
        oldest_seq=page.oldest_seq,
        has_older=page.has_older,
        cut=page.cut,
    )


@method("harness.stop_observe")
async def harness_stop_observe(
    server: JsonRpcServer, params: HarnessStopObserveRequest
) -> HarnessOkResponse:
    """Detach a read-only observer (cancel its forward task). Does NOT close the
    chat — observing never opened it."""
    await _cancel_forwarder(_observe_forwarders(server), params.session_id)
    return HarnessOkResponse()


@method("harness.delete_chat")
async def harness_delete_chat(
    server: JsonRpcServer, params: HarnessDeleteChatRequest
) -> HarnessOkResponse:
    rt = runtime_for(server, params.project_path)
    sid = params.session_id
    # Delete is a FORCE teardown — it must kill any background jobs and never leave a
    # resident session behind (unlike close_chat, which keeps one alive). Serialize the
    # whole teardown under the per-session op-lock (like open/close/reaper) so a
    # concurrent re-open can't attach a forwarder + return a live session to the client
    # while we pop the map, cancel that forwarder, and close+delete it underneath them.
    async with _session_op_lock(server, sid):
        # Cancel any pending reaper, pop the map, and close (close_chat drains the
        # registry → kills the jobs) before deleting the chat on disk.
        await _cancel_resident_reaper(server, sid)
        if sid in _session_to_runtime(server):
            _session_to_runtime(server).pop(sid, None)
            await _cancel_forwarder(_event_forwarders(server), sid)
            with contextlib.suppress(Exception):
                await rt.close_chat(sid)
        await rt.delete_chat(sid, recursive=params.recursive)
    return HarnessOkResponse()


@method("harness.send_prompt")
async def harness_send_prompt(
    server: JsonRpcServer, params: HarnessSendPromptRequest
) -> HarnessOkResponse:
    session = _require_session(server, params.session_id)
    await session.send_prompt(params.text, model=params.model, variant=params.variant)
    return HarnessOkResponse()


class HarnessStageFileRequest(_DaemonModel):
    """A file the reader pasted or attached in the webview, to be written into
    the chat's working directory under a name the daemon picks."""

    session_id: str
    name: str
    content_base64: str
    kind: str = "file"
    n: int = 1


class HarnessStageFileResponse(_DaemonModel):
    """Where the file landed, relative to the chat's working directory — the
    path the message carries."""

    path: str


@method("harness.stage_file")
async def harness_stage_file(
    server: JsonRpcServer, params: HarnessStageFileRequest
) -> HarnessStageFileResponse:
    import asyncio
    import base64
    import binascii

    from alkera_cli.harness.chat_files import (
        STAGE_FILE_MAX_BYTES,
        UploadKind,
        stage_file_size_label,
        write_staged_file,
    )

    session = _require_session(server, params.session_id)
    root = session.sandbox_dir
    if root is None:
        raise ValueError("this chat has no working directory to put a file in")
    try:
        data = base64.b64decode(params.content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("the file's bytes are not valid base64") from exc
    if len(data) > STAGE_FILE_MAX_BYTES:
        raise ValueError(
            f"{params.name} exceeded the maximum upload size of {stage_file_size_label()}"
        )
    kind: UploadKind = "image" if params.kind == "image" else "file"
    path = await asyncio.to_thread(write_staged_file, root, kind, params.n, params.name, data)
    return HarnessStageFileResponse(path=path)


class HarnessResolveFileRequest(_DaemonModel):
    """A chat-relative path out of a message, to be mapped to a file on disk."""

    session_id: str
    path: str


class HarnessResolveFileResponse(_DaemonModel):
    """The absolute path, or ``None`` when the path leaves the chat's working
    directory or names nothing there."""

    abs_path: str | None = None


@method("harness.resolve_file")
async def harness_resolve_file(
    server: JsonRpcServer, params: HarnessResolveFileRequest
) -> HarnessResolveFileResponse:
    from alkera_cli.harness.chat_files import resolve_chat_file

    session = _require_session(server, params.session_id)
    root = session.sandbox_dir
    found = resolve_chat_file(root, params.path) if root is not None else None
    return HarnessResolveFileResponse(abs_path=str(found) if found is not None else None)


@method("harness.cancel")
async def harness_cancel(server: JsonRpcServer, params: HarnessCancelRequest) -> HarnessOkResponse:
    session = _require_session(server, params.session_id)
    await session.cancel()
    return HarnessOkResponse()


@method("harness.get_permission_mode")
async def harness_get_permission_mode(
    server: JsonRpcServer, params: HarnessGetPermissionModeRequest
) -> HarnessPermissionModeResponse:
    """Read the session's current permission mode (so the editor can reflect it —
    e.g. after a resume restored a persisted ``plan``/``bypass``)."""
    session = _require_session(server, params.session_id)
    return HarnessPermissionModeResponse(mode=session.permission_mode)


@method("harness.set_permission_mode")
async def harness_set_permission_mode(
    server: JsonRpcServer, params: HarnessSetPermissionModeRequest
) -> HarnessPermissionModeResponse:
    """Switch the session's permission mode (parity with the CLI's ``/mode``).
    Persisted to the manifest, so it survives resume. Returns the active mode."""
    session = _require_session(server, params.session_id)
    session.set_permission_mode(params.mode)
    await server.notify(
        "harness.session_state_changed",
        HarnessSessionStateChangedNotification(
            session_id=session.session_id,
            permission_mode=session.permission_mode,
        ),
    )
    return HarnessPermissionModeResponse(mode=session.permission_mode)


def _cost_state(session: Any) -> HarnessCostStateResponse:
    from datetime import UTC, datetime

    from alkera_cli.plugins.plugin_base.cost import (
        CostLedger,
        CostLimits,
        cost_overview,
        unknown_cost_keys,
    )
    from alkera_cli.plugins.plugin_base.permissions import load_permissions

    project = session.project
    cost = load_permissions(project.path).cost
    limits = CostLimits.from_config(cost)
    ov = cost_overview(CostLedger(project), session.session_id, limits, now=datetime.now(UTC))
    return HarnessCostStateResponse(
        spent=ov["spent"],
        caps=ov["caps"],
        org_managed=ov["org_managed"],
        unknown_cost_keys=unknown_cost_keys(cost),
    )


@method("harness.get_cost_state")
async def harness_get_cost_state(
    server: JsonRpcServer, params: HarnessGetCostStateRequest
) -> HarnessCostStateResponse:
    """The chat/day/week tool-execution spend vs caps (parity with ``/cost``), so
    the editor can render a budget gauge + warn near a cap."""
    # _cost_state reads + parses the whole cost ledger (JSONL) — off the event loop,
    # like the sibling list_decisions / list_cost_ledger handlers.
    return await asyncio.to_thread(_cost_state, _require_session(server, params.session_id))


@method("harness.set_cost_limits")
async def harness_set_cost_limits(
    server: JsonRpcServer, params: HarnessSetCostLimitsRequest
) -> HarnessCostStateResponse:
    """Set per-window cost caps (parity with ``/cost set``) — written to the
    gitignored ``permissions.local.yml``. Returns the refreshed cost state."""
    from alkera_cli.plugins.plugin_base.permissions.config import update_local_cost_caps

    session = _require_session(server, params.session_id)
    update_local_cost_caps(session.project.path, params.caps)
    return await asyncio.to_thread(_cost_state, session)


@method("harness.list_decisions")
async def harness_list_decisions(
    server: JsonRpcServer, params: HarnessListDecisionsRequest
) -> HarnessListDecisionsResponse:
    """The permission-decision audit log for a chat
    (``.alkera/chats/<id>/decisions.jsonl``), newest-first + paginated, for the
    editor's Decisions/Safety activity tabs. ``decided_by="judge"`` narrows to
    the safety-judge verdicts."""
    from alkera_cli.plugins.plugin_base.permissions.audit import DecisionSink

    session = _require_session(server, params.session_id)
    chat_dir = session.project.chats_path / params.session_id
    records = await asyncio.to_thread(DecisionSink(chat_dir).read)
    rows = [r for r in records if r.session_id == params.session_id]
    if params.decided_by:
        rows = [r for r in rows if r.decided_by == params.decided_by]
    rows.reverse()  # newest first
    window = rows[params.offset : params.offset + params.limit]
    return HarnessListDecisionsResponse(
        decisions=[
            HarnessDecisionView(
                at=r.at,
                request_id=r.request_id,
                source=r.source,
                capability=r.capability,
                effect=r.effect,
                operation=r.operation,
                targets=list(r.targets),
                mode=r.mode,
                decision=r.decision,
                decided_by=r.decided_by,
                reasons=list(r.reasons),
            )
            for r in window
        ],
        has_more=params.offset + len(window) < len(rows),
    )


@method("harness.list_cost_ledger")
async def harness_list_cost_ledger(
    server: JsonRpcServer, params: HarnessListCostLedgerRequest
) -> HarnessListCostLedgerResponse:
    """The per-chat warehouse cost ledger (``.alkera/chats/<id>/
    cost_ledger.jsonl``), newest-first + paginated — the detail behind the Cost
    tab's spent/caps gauge."""
    from alkera_cli.plugins.plugin_base.cost import CostLedger

    session = _require_session(server, params.session_id)
    entries = await asyncio.to_thread(CostLedger(session.project).read_entries, session.session_id)
    entries.reverse()  # newest first
    window = entries[params.offset : params.offset + params.limit]
    return HarnessListCostLedgerResponse(
        entries=[
            HarnessCostLedgerEntryView(
                entry_id=e.entry_id,
                connection=e.connection,
                operation=e.operation,
                estimate_usd=e.estimate_usd,
                actual_usd=e.actual_usd,
                charged_usd=e.charged_usd,
                wallet_currency=e.wallet_currency,
                at=e.at,
            )
            for e in window
        ],
        has_more=params.offset + len(window) < len(entries),
    )


@method("harness.list_commands")
async def harness_list_commands(
    server: JsonRpcServer, params: HarnessListCommandsRequest
) -> HarnessListCommandsResponse:
    """The CLI's slash-command registry, for editor command menus. Served
    from `chat_slash.COMMANDS` so the vocabulary can't drift from the CLI."""
    from alkera_cli.chat.slash import COMMANDS

    return HarnessListCommandsResponse(
        commands=[
            SlashCommandInfo(
                name=spec.name,
                aliases=list(spec.aliases),
                summary=spec.summary,
                usage=spec.usage,
                hidden=spec.hidden,
                ui=not spec.cli_only,
            )
            for spec in COMMANDS
            # `editor_hidden` commands (the mode/cost/preferences family) have a
            # native control and aren't part of the editor's vocabulary at all —
            # so they never reach a menu or an unknown-vs-known check.
            if not spec.editor_hidden
        ]
    )


@method("harness.run_command")
async def harness_run_command(
    server: JsonRpcServer, params: HarnessRunCommandRequest
) -> HarnessRunCommandResponse:
    """Execute a slash command through the CLI's OWN registry against this
    daemon session, returning the STRUCTURED editor outcome (`dispatch_ui`):
    parsing, validation, and side effects stay in `chat_slash`; the editor
    draws native UI from the payload instead of a text dump. CLI-only
    commands answer with their hint.

    The dispatch runs in a worker thread (some handlers do blocking I/O,
    e.g. /usage's gateway call); handlers that need the event loop
    (compact/clear/set_title schedule session coroutines) marshal back via
    `run_coroutine_threadsafe`, mirroring the CLI's fire-and-forget closures.
    """
    from io import StringIO

    from rich.console import Console

    from alkera_cli.chat.slash import DisplayState, SlashContext, dispatch_ui
    from alkera_cli.preferences import user as preferences_file

    session = _require_session(server, params.session_id)
    loop = asyncio.get_running_loop()

    def _fire(factory: Callable[[], Coroutine[Any, Any, None]]) -> None:
        future = asyncio.run_coroutine_threadsafe(factory(), loop)

        def _log_failure(fut: concurrent.futures.Future[None]) -> None:
            if not fut.cancelled() and fut.exception() is not None:
                logger.warning("slash-command side effect failed", exc_info=fut.exception())

        future.add_done_callback(_log_failure)

    # The console is part of the SlashContext contract but UI handlers never
    # print; a sink keeps any stray write off stdout (the JSON-RPC pipe).
    console = Console(file=StringIO(), record=True, width=72, force_terminal=False)
    ctx = SlashContext(
        session=session,
        display=DisplayState(),
        console=console,
        load_prefs=lambda: preferences_file.load_preferences_as(session.credential),
        set_pref=lambda k, v: preferences_file.set_preference_as(session.credential, k, v),
        compact=lambda: _fire(session.compact),
        clear=lambda: _fire(session.clear),
        get_title=lambda: session.manifest.title,
        set_title=lambda title: _fire(lambda: session.set_title(title)),
    )

    outcome = await loop.run_in_executor(None, dispatch_ui, params.line, ctx)

    # The editor renders the outcome in a transient command panel straight from
    # this RPC return — nothing is persisted for usage/title/etc. The operations
    # that DO leave a transcript mark emit their own dedicated events from inside
    # the handler (`/clear` → ConversationCleared, `/compact` → CompactionApplied).
    return HarnessRunCommandResponse(
        kind=outcome.kind,
        command=outcome.command,
        payload=outcome.payload,
        message=outcome.message,
    )


@method("harness.set_model_effort")
async def harness_set_model_effort(
    server: JsonRpcServer, params: HarnessSetModelEffortRequest
) -> HarnessSetModelEffortResponse:
    """Persist the user's effort choice into the chat's manifest
    (`manifest.model["effort"]`) so it survives a resume. Fails closed on an
    effort the pinned model doesn't offer (surfaces as a JSON-RPC error)."""
    session = _require_session(server, params.session_id)
    await session.set_model_effort(params.effort)
    return HarnessSetModelEffortResponse(model=dict(session.manifest.model))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_session(server: JsonRpcServer, session_id: str) -> ChatSession:
    s = _try_get_session(server, session_id)
    if s is None:
        raise SessionNotOpenError(session_id)
    return s


def _try_get_session(server: JsonRpcServer, session_id: str) -> ChatSession | None:
    rt = _session_to_runtime(server).get(session_id)
    if rt is None:
        return None
    return rt._sessions.get(session_id)


def _apply_plan_choice(
    server: JsonRpcServer, req: QuestionRequest, answers: list[list[str]]
) -> PermissionMode | None:
    """Plan-approval side effect, parity with the CLI's `_maybe_apply_plan_choice`:
    when the editor accepts a plan with one of the `Accept —` options, flip the
    session's permission mode to match (default / auto / bypass). The
    answer still flows back so opencode switches plan→build; a free-form
    rejection maps to no mode (`plan_label_to_mode` returns None) and is left in
    plan mode.

    Returns the mode it applied so the caller can push it to the editor, or None
    when the choice changed no mode (not a plan, free-form reject, dead session).
    """
    if req.kind != "plan_approval":
        return None
    chosen = answers[0][0] if answers and answers[0] else ""
    mode = plan_label_to_mode(chosen)
    if mode is None:
        return None
    session = _try_get_session(server, req.session_id)
    if session is None:
        return None
    session.set_permission_mode(mode)
    return mode


async def _forward_loop(server: JsonRpcServer, session_id: str, sub: Any) -> None:
    try:
        async for event in sub:
            try:
                await server.notify(
                    "harness.event",
                    HarnessEventNotification(
                        session_id=session_id,
                        event=event.model_dump(mode="json"),
                    ),
                )
            except Exception:
                logger.exception("harness.event notify failed for %s", session_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("harness forward loop crashed for %s", session_id)
        # The event stream is gone — don't leave the editor spinning forever.
        # Synthesize a terminal error status so it stops the turn + can offer a
        # retry (best-effort; the connection may already be down).
        with contextlib.suppress(Exception):
            await server.notify(
                "harness.event",
                HarnessEventNotification(
                    session_id=session_id,
                    event=SessionStatusChanged(
                        event_id=uuid.uuid4().hex,
                        time=datetime.now(UTC),
                        session_id=session_id,
                        status="error",
                        phase="error",
                        detail="daemon event stream lost; reopen the chat to continue.",
                    ).model_dump(mode="json"),
                ),
            )


# ---------------------------------------------------------------------------
# Shutdown hook
# ---------------------------------------------------------------------------


@register_shutdown_hook
async def _harness_shutdown(server: JsonRpcServer) -> None:
    registry: dict[str, HarnessRuntime] = getattr(server, "harness_runtimes", {})
    forwarders: dict[str, asyncio.Task[None]] = getattr(server, "harness_forwarders", {})
    for task in forwarders.values():
        if not task.done():
            task.cancel()
    for rt in registry.values():
        with contextlib.suppress(Exception):
            await rt.close_all()
    registry.clear()
    forwarders.clear()
    # Flush after the runtimes close so their finished events ride along.
    from alkera_cli.observability.audit_report import close_default_reporter
    from alkera_cli.observability.otel_export import close_default_exporter

    await asyncio.to_thread(close_default_reporter)
    await asyncio.to_thread(close_default_exporter)


__all__ = [
    "HarnessCancelRequest",
    "HarnessCloseChatRequest",
    "HarnessCostLedgerEntryView",
    "HarnessDecisionView",
    "HarnessDeleteChatRequest",
    "HarnessEventNotification",
    "HarnessGetPermissionModeRequest",
    "HarnessListChatsRequest",
    "HarnessListChatsResponse",
    "HarnessListCostLedgerRequest",
    "HarnessListCostLedgerResponse",
    "HarnessListDecisionsRequest",
    "HarnessListDecisionsResponse",
    "HarnessLockHeldNotification",
    "HarnessObserveChatRequest",
    "HarnessObserveChatResponse",
    "HarnessOkResponse",
    "HarnessOpenChatRequest",
    "HarnessOpenChatResponse",
    "HarnessPermissionModeResponse",
    "HarnessPermissionRequiredRequest",
    "HarnessPermissionRequiredResponse",
    "HarnessQuestionRequiredRequest",
    "HarnessQuestionRequiredResponse",
    "HarnessSendPromptRequest",
    "HarnessSessionStateChangedNotification",
    "HarnessSetPermissionModeRequest",
    "HarnessStopObserveRequest",
]
