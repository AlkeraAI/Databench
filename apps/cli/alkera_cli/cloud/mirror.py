"""One cloud chat, mirrored: the local harness session behind ``doc:chat:<id>``.

A :class:`ChatMirror` binds a local :class:`~alkera_cli.harness.ChatSession`
(its session id IS the cloud chat id, its workspace the daemon's project) to
the chat document on the gateway, as the publisher peer, and runs four pumps:

* **publish** — every persisted harness event becomes one durable ``append``
  entry (``publish.append_entry``: role, kind, the bounded payload), sent in
  order and resent after a drop or an epoch change (the server dedupes by
  ``event_id``); token deltas are coalesced and streamed as ``chunk`` ops on
  the ephemeral lane, dropped when the socket is down. Text that names files
  in the chat's folder (``![chart](charts/x.png)``) is held, streamed or
  durable, until those files' bytes are on the drive (``land_files``), so a
  reader is never shown a reference to a file it cannot open yet;
* **consume** — a ``user_message`` relay from a reader is one of three things:
  a ``prompt`` (the human's next message, or — with ``interrupt_id`` — their
  answer to a permission or question card), a ``run_query`` (a saved query to
  re-run with bound parameters through the read-only SQL tool) or a
  ``promote`` (upload a result's full payload and receipt to the object the
  cloud created);
* **budget** — every turn runs under a :class:`TurnBudget`, which by default
  caps nothing at all: no wall clock, no tool-call ceiling, no token ceiling
  (spend is the credit meter's job at the gateway). Where a deployment sets a
  cap through the environment, the mirror stops the turn — cancelling the
  harness and appending a visible ``stopped`` status — the moment it is hit,
  and never while an ask waits on a person. It publishes a ``working`` state
  when a turn starts either way;
* **interrupts** — the session's permission and question brokers park each ask
  until a relay answers it; the card itself reaches the browser as the
  harness's own ``permission.request`` / ``question.request`` append.

The cloud session opens as an analyst's, in ``read_only`` permission mode: there
the harness itself refuses an edit, a shell command or a write-class SQL
statement before any reader is asked. A reader may move the chat to another
mode, and a write — to a file in the chat's folder or to connected data —
then follows that mode (asked in ``default``, run in ``bypass``); a relayed
answer can only ever approve what was offered for the ask that is actually
outstanding.

A relay is a claim, not an instruction. The gateway hands the publisher every
``user_message`` any reader sends, so ``run_query`` and ``promote`` are
honoured only once the object they name exists (read back through REST as
this chat's agent), is bound to THIS chat (and, for a promote, to the event
named), and — when the object says who owns it — belongs to the chat's owner.
A relay that fails any check is logged and ignored; nothing runs.

Nothing here starts a turn on its own: the harness only runs when a human
relayed a prompt (or a re-run they asked for). What the mirror itself puts on
the transcript — a note, a re-run's tool call, a budget stop — is stamped as
its own so it never counts against, or ends, the turn the harness is running.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
import time
import uuid
from collections import OrderedDict
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Iterable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, cast, get_args

import httpx
from alkera_core.chat_paths import chat_path, chat_references
from alkera_core.compute.liveness import HEARTBEAT_INTERVAL_SECONDS
from alkera_core.files.objects_bridge import chat_sandbox_path
from alkera_core.project.chats.store import ChatStore
from alkera_core.project.chats.trace import TraceVerdict, verify_trace_file
from alkera_core.schemas.chat import (
    PROMPT_CANCELLED_FAILED,
    PROMPT_CANCELLED_STOPPED,
    AgentMessageChunk,
    AgentThoughtChunk,
    Event,
    MessageCreated,
    PartCreated,
    PermissionOptionId,
    PermissionRequest,
    PermissionResolved,
    PromptCancelled,
    QuestionAnswered,
    QuestionRejected,
    QuestionRequest,
    SessionStatusChanged,
    ToolCall,
    ToolCallPart,
    ToolCallUpdate,
)
from alkera_core.schemas.objects import (
    CHAT_RELAY_ADAPTER,
    MAX_MESSAGE_LIMIT,
    PROMPT_KIND,
    AnswerRelay,
    ChatPromptRecord,
    PromoteRelay,
    PromptRelay,
    RunQueryRelay,
    answers_a_waiting_message,
    prompt_cancelled_event_id,
    prompt_entry_id,
    reads_as_prompt,
)
from alkera_core.schemas.objects.transcript import (
    RECORDED_ANSWER_ROLE,
    RESOLUTION_KINDS,
    stopped_turn_terminal_id,
)
from pydantic import ValidationError

from alkera_cli.cloud import fence
from alkera_cli.cloud.activity import ChatActivity
from alkera_cli.cloud.attachments import MaterializedAttachments, prompt_with_attachments
from alkera_cli.cloud.budget import BudgetExceeded, TurnBudget, TurnMeter
from alkera_cli.cloud.gateway_renewal import GatewayTokenRenewal
from alkera_cli.cloud.limits import (
    mirror_start_timeout_seconds,
    own_event_memory,
    publish_chunk_interval_seconds,
    relay_memory,
    rerun_limit,
    shell_read_roots,
)
from alkera_cli.cloud.machine_card import MachineCardHolder, MachineNote
from alkera_cli.cloud.model_follow import PinnedModelFollowing, pin_reasoning
from alkera_cli.cloud.notes import note_events
from alkera_cli.cloud.pressure_notice import owe_note, pending_notes, settle_notes
from alkera_cli.cloud.promoted_result import find_tool_result, result_envelope
from alkera_cli.cloud.publish import (
    ChunkCoalescer,
    RoleIndex,
    append_entry,
    bound_entry,
    entry_size,
    is_chunk,
)
from alkera_cli.cloud.publisher_identity import (
    PublishingRefusal,
    TurnAttribution,
    is_publishing_refusal,
    receipt_principal,
    relaying_user_of,
)
from alkera_cli.cloud.query_params import QueryCompileError, compile_query
from alkera_cli.cloud.receipt import ReceiptPrincipal, ResultReceipt, receipt_for_tool_result
from alkera_cli.cloud.refusal import (
    RefusalNote,
    RefusalWatch,
    fence_reason,
    quote_statement,
)
from alkera_cli.cloud.replay import ReplayResult, replay_file_tool
from alkera_cli.cloud.reply_files import MissingFile, ReplyFileCheck, missing_files_note
from alkera_cli.cloud.rest import CloudApiError, CloudRestClient, chat_credential
from alkera_cli.cloud.sandbox_bag import sandbox_bag_on
from alkera_cli.cloud.source_context import source_brief, source_document, template_notes
from alkera_cli.cloud.transport import CloudSocket, DocHandle, DocOp, DocOpError
from alkera_cli.cloud.turn_admission import turn_refusal
from alkera_cli.cloud.turn_context import (
    continuation_after_allow,
    continuation_after_answer,
    hidden_turn,
)
from alkera_cli.contracts.gateway_model import GatewayModel
from alkera_cli.harness import (
    ChatSession,
    HarnessGoneError,
    HarnessRuntime,
    PathFence,
    PermissionBroker,
    QuestionBroker,
)
from alkera_cli.harness.adapters.opencode_alkera import build_manifest_model
from alkera_cli.harness.agent_root import agent_config_root
from alkera_cli.harness.permission_broker import ResolverRefusedError
from alkera_cli.harness.permission_mode import (
    MODES_THAT_DISCARD_AN_APPROVAL,
    PermissionMode,
    plan_answers_with_note,
    plan_note_of,
    with_plan_note,
)
from alkera_cli.harness.question_broker import QuestionResolution
from alkera_cli.harness.registry import OPENCODE_HARNESS
from alkera_cli.harness.sandbox import (
    RUNTIME_STATE_SUBDIR,
    SandboxSettings,
    agent_home_for,
    mount_aliases,
    session_envs_dir,
)
from alkera_cli.harness.sandbox_probe import current_capability
from alkera_cli.harness.turn_restart import TURN_RESTART_FAILED
from alkera_cli.plugins.plugin_base.permissions import as_answered_by
from alkera_cli.plugins.plugin_base.sql.provenance import team_connection_id

logger = logging.getLogger(__name__)

#: How many transcript rows one catch-up page asks for — the most the route
#: serves. The catch-up walks to the END of the transcript however many pages
#: that takes: a chat that said more than one box's worth of rows while this
#: box was away (a turn that ran for days) would otherwise have its tail
#: ignored, and the tail is exactly where the stop, the answer or the queued
#: question that still needs running sits. The loop is bounded by the cursor
#: itself — a page that does not advance it ends the read. Not a knob: it is
#: already the route's own ceiling, and asking for more would be refused.
TRANSCRIPT_PAGE_ROWS = MAX_MESSAGE_LIMIT
#: Slack over the turn budget before the catch-up gives up waiting for a turn to
#: settle. The budget is what stops a turn; this only has to outlast it.
IDLE_WAIT_MARGIN_SECONDS = 10.0
RETRY_SLEEP_SECONDS = 0.5
#: How a catch-up whose transcript read failed tries again: the waits between
#: attempts, doubling to the last. A failed read (the API rate-limiting the
#: box, a restart, a timeout) left the question it would have found unrun
#: until the next frame named the chat — and a chat's first question has no
#: next frame until the person gives up waiting and types again. While
#: the stream is up, the discovery pass reads no transcript either, so nothing
#: else would retry it. About four minutes in all, then the next frame or pass.
CATCH_UP_RETRY_SECONDS: tuple[float, ...] = (
    2.0,
    4.0,
    8.0,
    16.0,
    30.0,
    30.0,
    30.0,
    30.0,
    30.0,
    60.0,
)
BUDGET_TICK_SECONDS = 1.0
#: How often a turn that is under way says so again. The machine's own
#: heartbeat interval, read from the one place that states it rather than
#: spelled again here: the reader's only evidence that a long turn is alive is
#: this stamp next to a machine that is still beating, and a stamp slower than
#: the beat would make the two disagree about the same box.
TURN_STAMP_SECONDS = float(HEARTBEAT_INTERVAL_SECONDS)
#: How many start timeouts a mirror will spend waiting for its own socket to
#: come up before it gives up on the chat for this round. A box that cannot
#: reach the backend at all must not hold the discovery pass open behind one
#: chat; a box that is merely still connecting must not blame the server.
WAITING_FOR_SOCKET_MULTIPLIER = 3
SQL_TOOL = "sql.query"
#: How many times an entry the server calls too large is shrunk and resent.
MAX_SHRINKS = 4

#: Where a query or result object says which chat it belongs to, and — for a
#: result awaiting its payload — which transcript event it was promoted from.
#: Looked up in the object's ``spec`` first, then at its top level.
CHAT_BINDING_KEYS = ("source_chat_id", "chat_id")
EVENT_BINDING_KEYS = ("source_event_id", "event_id")
#: Permission kinds that MUTATE — they write files, run commands, spawn work or
#: reach outside the project. Whether one may be approved depends on the chat's
#: permission mode and on where it would write; that it is a write does not.
WRITE_CLASS_KINDS = frozenset({"edit", "shell", "task", "external"})

#: The permission modes a web chat may be put into — the same set the route
#: accepts. What ``bypass`` hands over is the ASKING, never the boundary: a
#: file write is fenced to the chat's own folder in every mode, and a write to
#: connected data follows the mode like a file write does (refused here and in
#: ``plan``, asked in ``default``, run in ``bypass``) — only a connection that
#: cannot be written by its nature refuses one in every mode. ``auto`` is the
#: ceiling a reader can raise a chat to when the model needs something the
#: analyst modes refuse outright — a page nobody's search returned, say: its
#: write and egress middle is cleared by the grounded safety judge the box
#: builds at start (``cloud.command``), the same judge the editor uses.
CLOUD_PERMISSION_MODES: frozenset[str] = frozenset(
    {"read_only", "default", "auto", "plan", "bypass"}
)

#: The modes in which no write is approved at all — an analyst's chat and a
#: planning one. In the remaining modes a write inside the chat's own folder is
#: approvable; outside it, nothing is, in any mode.
#:
#: Taken from the stance table rather than listed again: a mode refuses a
#: relayed allow for exactly the reason its own row refuses the write, and a
#: stance added to the table after this line was written would otherwise have to
#: be remembered here too.
NO_WRITE_MODES: frozenset[str] = MODES_THAT_DISCARD_AN_APPROVAL

#: The visible line a continued turn starts from, once a reader has answered an
#: ask the harness was no longer holding. What was answered rides with it as a
#: steering part the renderer hides.
CONTINUE_TEXT = "Continue."

#: The relay kind that carries a mode switch. Spelled here and read duck-typed,
#: so a box meets the reader's switch whether or not its build of the shared
#: relay union already names the variant.
MODE_RELAY_KIND = "mode"

#: How many missing files the agent's next turn can be told about; the note
#: itself names fewer, so this only bounds what a long turn accumulates.
_MISSING_FILES_KEPT = 50


def pinned_model(pin: Any) -> dict[str, Any] | None:
    """The manifest model dict for the model a cloud chat was started on.

    The chat row carries the gateway catalog's own description of the model the
    reader picked; the harness wants its own pinned shape, and
    ``build_manifest_model`` is the ONE place that translation is written — the
    same call the CLI's picker and the editor's ``new_chat`` make, so a chat
    pins identically whichever surface started it.

    ``None`` (no pick, or a row from before the picker) leaves the box on its default.
    """
    if not isinstance(pin, dict):
        return None
    model_id = pin.get("id")
    wire = pin.get("wire")
    if not isinstance(model_id, str) or not model_id or wire not in ("anthropic", "openai"):
        return None
    effort = pin.get("effort")
    display = pin.get("display_name")
    return build_manifest_model(
        GatewayModel(
            id=model_id,
            display_name=display if isinstance(display, str) and display else model_id,
            wire=wire,
            efforts=tuple(e for e in (pin.get("efforts") or []) if isinstance(e, str)),
            context_window=_pin_int(pin.get("context_window")),
            max_output_tokens=_pin_int(pin.get("max_output_tokens")),
            **pin_reasoning(pin),
        ),
        effort if isinstance(effort, str) else None,
    )


def _pin_int(value: Any) -> int:
    """A non-negative int from a pin field, else 0 (= unknown to the harness)."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


#: The relay kind that carries a model switch. Read duck-typed for the same
#: reason the mode is: a box built before the variant existed would otherwise
#: log the reader's switch as an unknown kind and leave the chat on the model
#: they just moved it off.
MODEL_RELAY_KIND = "model"

#: The relay kind that ends the running turn. Dispatched by kind ahead of
#: the union for the same reason the two switches are, and for one more: the
#: stop must survive a body this box cannot fully read, because a refused
#: stop leaves the turn running and the reader with nothing else to press.
STOP_RELAY_KIND = "stop"

_EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TERMINAL_STATUSES = frozenset({"idle", "error", "completed", "aborted"})
#: The refusal a mirror reports when the document opened as a reader's: this
#: box is not the chat's publisher, so nothing it ran could be published.
NOT_PUBLISHER = "not_publisher"
#: What a reader is told when the workspace came back to find a turn it had
#: been killed in the middle of. It states the fact only: the composer is
#: right there, so the line does not tell them to resend.
TURN_LOST_TO_RESTART = (
    "The workspace restarted while it was answering, so that turn did not finish."
)


def turn_stopped_after_tools(count: int) -> str:
    """What a reader is told instead of the turn a restart cut short being run
    again: it had started ``count`` tools (one maybe cut part way), and running
    the message again would run them again: a delete or an insert twice."""
    tools = "tool" if count == 1 else "tools"
    return (
        f"This turn was interrupted by a restart after it started {count} {tools}; "
        "send the message again to continue."
    )


#: How many times the mirror re-cancels a harness that keeps working after a
#: reader's stop. Enough for an agent loop that has one more step in it; not a
#: loop of its own against an adapter that has stopped listening.
_MAX_STOP_CANCELS = 3

MirrorState = Literal["starting", "running", "stopped", "failed"]
#: How the original call closes when the reader refused it after a restart.
DENIED_BY_USER = "denied by user"
#: How the original call closes when the workspace could not re-run it: the
#: agent is told to retry, and the retry is a new call with its own result.
NOT_RUN_BEFORE_RESTART = "not run — the workspace restarted before this ran; the agent retries it"

#: The closure a restored call gets when this box's copy of the transcript
#: failed its integrity check, so the call could not be rebuilt from it.
TRANSCRIPT_MISMATCH = (
    "not run — this workspace's copy of the transcript does not match its pinned digest, "
    "so the call was not rebuilt from it; the agent retries it"
)

#: The log a gated call is rebuilt from, and the one the check reads.
TRANSCRIPT_FILENAME = "chat.jsonl"

_PERMISSION_OPTIONS: frozenset[str] = frozenset(get_args(PermissionOptionId))


class ChatMirrorRefusedError(Exception):
    """The chat document would not open for this mirror — the id was never
    declared to the cloud (or belongs to someone else), so no local session
    is created for it and nothing is published."""

    def __init__(self, chat_id: str, code: str) -> None:
        super().__init__(f"chat {chat_id} refused: {code}")
        self.chat_id = chat_id
        self.code = code


class ChatMirrorStoppedError(Exception):
    """The mirror was stopped while it was still starting.

    A start is a sequence of awaits — the document, the source brief, the
    harness session — and a drain that reaches the box in any of them takes
    the mirror down under it: the document is closed and dropped, and the
    start would go on to use it. Raised instead of carrying on, so what the
    stop tore down stays torn down and the caller knows the chat is not served
    here rather than reading a crash as a fault in the chat.
    """

    def __init__(self, chat_id: str) -> None:
        super().__init__(f"chat {chat_id} was stopped while its mirror was starting")
        self.chat_id = chat_id


class RelayRefusedError(Exception):
    """A relay failed a check and is ignored — never executed."""


def _op_id(prefix: str, key: str) -> str:
    """A deterministic op id under the 64-char cap: retries of one entry share
    it, so a retry after a lost ack is still one op to the server."""
    safe = "".join(ch if ch.isalnum() or ch in "._:-" else "-" for ch in key)
    return f"{prefix}-{safe}"[:64]


#: The harness events a catch-up folds something out of: an ask, its
#: resolution, and the tool call an ask gates — everything ``_rearm_asks``
#: reads the chat's pending state out of. A row carrying anything else has
#: given up its two watermarks by the time its page is dropped.
_REARM_KINDS: Final = frozenset(
    {"tool.call", "tool.call_update", "permission.request", "question.request", *RESOLUTION_KINDS}
)

#: The tool-call states that mean the call is over. A permission ask gates the
#: call, so a call that reached one of these is not waiting on anybody, and
#: neither is the ask that let it run.
_SETTLED_CALL_STATES: Final = frozenset({"completed", "error"})


@dataclass(slots=True)
class _CatchUpRead:
    """What a walk of the transcript keeps, page by page, instead of the rows.

    An ask is folded as it is walked and dropped again as soon as the walk
    proves nobody is waiting on it, so what this holds is the chat's OPEN
    questions and not its history of them: ``asks`` the unanswered ones by
    request id, ``announced`` which of those the harness already put on a card,
    ``recorded`` an answer a reader gave while no box held the ask, and
    ``calls`` / ``aliases`` the still-running tool calls a permission ask names
    (a finished call is dropped, along with the provider ids that pointed at
    it). ``users`` are the reader's own rows a question could be relayed from —
    which of them is still unanswered is not known until the walk ends —
    ``answered_to`` the last thing the machine published and ``highest`` the
    end of the transcript. ``rows`` is how many were walked: "the transcript
    said nothing new" is not the same claim as "it held nothing this pass acts
    on".
    """

    asks: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    announced: set[str] = field(default_factory=set)
    recorded: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    users: list[dict[str, Any]] = field(default_factory=list)
    resolved: set[str] = field(default_factory=set)
    answered_to: int = 0
    #: The last row that ended a turn — a finished answer, a turn's terminal,
    #: the session going idle. A message below it was answered in full, so a
    #: box coming up over a document still saying "working" has nothing to
    #: restart for it.
    finished_to: int = 0
    highest: int = 0
    rows: int = 0
    #: Where each answer a reader recorded sits in the transcript. A turn the
    #: machine ended after it means a harness carried the answer on, even when
    #: the machine's own resolution of the ask never reached the record.
    recorded_at: dict[str, int] = field(default_factory=dict)
    #: The tool calls the machine started after the person's last message,
    #: finished or cut by the kill: the work a turn restarted from that message
    #: would do a second time.
    tools_since_prompt: set[str] = field(default_factory=set)


class _Interrupt:
    """One ask parked on a person.

    ``restored`` marks an ask rebuilt from the transcript rather than raised by
    the running harness: nothing in the process is awaiting its future, so the
    mirror itself acts on the answer (records it, continues or ends the turn).
    ``tool`` is the call a restored permission ask gated — the transcript's
    key for it, its tool name and its input as the transcript recorded them —
    when the transcript still has it, so an allow can re-run the call instead
    of hoping the agent does, and the closure lands on the call the reader
    sees rather than on whichever id the ask happened to name.
    """

    __slots__ = ("future", "kind", "request", "restored", "tool", "tool_refusal")

    def __init__(
        self,
        kind: str,
        future: asyncio.Future[Any],
        request: PermissionRequest | QuestionRequest,
        *,
        restored: bool = False,
        tool: tuple[str, str, dict[str, Any]] | None = None,
        tool_refusal: str | None = None,
    ) -> None:
        self.kind = kind
        self.future = future
        self.request = request
        self.restored = restored
        self.tool = tool
        #: Why the call was NOT rebuilt when ``tool`` is ``None`` for a reason
        #: the reader should see — the box's copy of the transcript failed
        #: its integrity check — rather than because the transcript simply no
        #: longer had it.
        self.tool_refusal = tool_refusal


class ChatMirror(PinnedModelFollowing, GatewayTokenRenewal):
    """See the module docstring."""

    def __init__(
        self,
        *,
        chat_id: str,
        runtime: HarnessRuntime,
        socket: CloudSocket,
        rest: CloudRestClient,
        budget: TurnBudget | None = None,
        user_id: str = "",
        owner_user_id: str | None = None,
        machine_id: str | None = None,
        harness_type: str = OPENCODE_HARNESS,
        model: dict[str, Any] | None = None,
        permission_mode: PermissionMode = "read_only",
        title: str | None = None,
        source_object_id: str | None = None,
        files_drive_id: str | None = None,
        prepare_attachments: (
            Callable[[str, Mapping[str, Any]], Awaitable[MaterializedAttachments]] | None
        ) = None,
        chunk_interval: float | None = None,
        start_timeout: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_refused: Callable[[str, PublishingRefusal], Awaitable[None]] | None = None,
        on_turn_end: Callable[[str], None] | None = None,
        sandbox: Mapping[str, Any] | None = None,
        land_files: Callable[[list[str]], Awaitable[object]] | None = None,
        machine_card: MachineCardHolder | None = None,
    ) -> None:
        self._chat_id = chat_id
        self._machine_note = MachineNote(machine_card)
        self._runtime = runtime
        # The chat row's own sandbox figures (``sandbox_fields_of``), copied
        # onto the manifest's harness bag before the open so the adapter reads
        # them beside everything else the session is built from.
        self._sandbox = dict(sandbox or {})
        self._socket = socket
        # Every REST call this mirror makes names the chat as the agent.
        self._rest = rest if rest.agent_id == chat_id else rest.for_agent(chat_id)
        self._budget = budget or TurnBudget()
        self._user_id = user_id
        # The chat's owner when the chat record says; the machine's user otherwise.
        self._owner_user_id = owner_user_id or user_id
        # The machine this box registered as — the agent a receipt names once
        # the box has one; the chat id stands in before it registers.
        self._machine_id = machine_id
        # The transcript is read as that machine. A chat is private until its
        # node is shared, and the operator whose token the box holds owns no
        # rung on a colleague's chat: the binding is what admits the catch-up
        # read, so before the box has registered only the owner's own chats
        # can be caught up.
        self._machine_rest = self._rest.for_agent(machine_id) if machine_id else self._rest
        self._transcript_rest = self._machine_rest
        # The credential the agent presents to the gateway, and when it lapses:
        # minted as the machine for this chat alone at every session open. The
        # box's own bearer never reaches the agent.
        self._gateway_token_expires_at: datetime | None = None
        # The token the running agent was opened on: what a revival falls back
        # to when a fresh one cannot be minted right now.
        self._gateway_token: str | None = None
        # What the session was opened with, kept so the session can be opened
        # again on a fresh gateway token between turns.
        self._broker: PermissionBroker | None = None
        self._questions: QuestionBroker | None = None
        # The task reading the session's events — the one a reopen replaces.
        self._pump_task: asyncio.Task[None] | None = None
        self._attribution = TurnAttribution(fallback_user_id=user_id)
        self._refused: PublishingRefusal | None = None
        # Told when the gateway refuses publishing mid-turn, so the service can
        # put the refusal on the chat where the reader sees it.
        self._on_refused = on_refused
        # Told the moment a turn is over, so what the turn wrote can leave for
        # the drive now rather than on the next poll tick.
        self._on_turn_end = on_turn_end
        # Puts the bytes of the files a reply names on the drive before the
        # reply is published: handed the targets as the agent wrote them, and
        # returns once they have landed or the deployment's wait is spent.
        self._land_files = land_files
        # One landing per file per text part, shared by the streamed chunks
        # and the durable part that closes them, keyed ``(message, part)``.
        self._landings: OrderedDict[tuple[str, str], dict[str, asyncio.Task[None]]] = OrderedDict()
        # The text streamed so far per part: a reference is only whole once
        # its closing parenthesis has arrived, maybe several frames later.
        self._streamed: OrderedDict[tuple[str, str], str] = OrderedDict()
        # Which file each held result was written out to, and the check of a
        # published reply's references against the chat's folder. Made on
        # first use: the folder is the runtime's, which is not ours to touch
        # while this object is still being built.
        self._reply_check: ReplyFileCheck | None = None
        # The files the last published reply named that are not in the chat,
        # told to the agent with its next turn and then forgotten.
        self._missing_files: list[MissingFile] = []
        self._harness_type = harness_type
        self._model = model
        self._respawned_for: dict[str, Any] | None = None
        # Held from the check that the agent carries the chat's model until the
        # turn has been handed to it, and by every repin: a switch that lands
        # while a turn is being prepared waits for the next turn, never slips
        # in between the check and the send onto an agent spawned without it.
        self._pin_lock = asyncio.Lock()
        self._title = title
        # The chat template this chat was started FROM. Its brief goes in
        # front of the agent ONCE, before its first turn — it is what the
        # chat opens from, not something to repeat on every question.
        self._source_object_id = source_object_id
        self._source_brief: str | None = None
        # The drive the chat's folder is on, off the chat's own record: what
        # its attachments are read from. A pool box serves chats from many
        # orgs, and a box on its machine credential has no drive of its own
        # to read "the caller's" — so the drive is the chat's, never the box's.
        self._files_drive_id = files_drive_id
        # Puts the question's attachments on this box's disk before the turn
        # reads them. A mirror with no such hook runs a chat that cannot carry
        # attachments at all, so the question goes to the harness unchanged.
        self._prepare_attachments = prepare_attachments
        # Unnamed by the caller means the deployment's own figure: a box whose
        # backend answers its hello slowly, or whose operator wants the
        # streaming publisher to send fewer, larger frames, says so once in its
        # environment rather than at every construction site.
        self._chunk_interval = (
            publish_chunk_interval_seconds() if chunk_interval is None else chunk_interval
        )
        self._start_timeout = (
            mirror_start_timeout_seconds() if start_timeout is None else start_timeout
        )
        self._clock = clock
        self._sleep = sleep
        self._session: ChatSession | None = None
        self._doc: DocHandle | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._outbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._sending = False
        self._coalescer = ChunkCoalescer()
        self._roles = RoleIndex()
        self._refusals = RefusalWatch(read_only=lambda: self._mode in NO_WRITE_MODES)
        self._meter = TurnMeter(self._budget, clock=clock)
        self._interrupts: dict[str, _Interrupt] = {}
        # Asks this mirror has already settled, by request id: a catch-up read
        # that lands between the answer and the harness's echo of it must not
        # rebuild an ask that was just answered.
        self._settled_asks: OrderedDict[str, None] = OrderedDict()
        # Asks the harness in THIS process raised, by request id. The harness
        # owns each one: its policy answers it, or it reaches the broker and is
        # parked. The transcript shows such an ask unresolved from the moment
        # its request lands until its resolution does, so a catch-up read in
        # that window must never re-offer it to a reader — an ask the mode
        # already refused would otherwise come back as a card with an Allow.
        self._live_asks: OrderedDict[str, None] = OrderedDict()
        # An answer that arrived before its ask was parked here. The harness
        # publishes the ask before the policy has run, so a reader can answer
        # it — and the server records that answer — while the policy is still
        # deciding whether anyone is asked at all. Kept by request id and
        # applied the moment the ask is parked, never dropped: the server
        # refuses a second answer to a recorded one, so a dropped first
        # answer would leave the ask unanswerable.
        self._early_answers: OrderedDict[str, dict[str, Any]] = OrderedDict()
        # The asks this mirror has put on the wire and not seen resolved, by
        # request id: only for one of these is an early answer held.
        self._published_asks: OrderedDict[str, PermissionRequest | QuestionRequest] = OrderedDict()
        # Asks a resolution has closed — seen on the wire in this process or
        # read back from the transcript: no request is published for one again.
        self._resolved_asks: OrderedDict[str, None] = OrderedDict()
        # An approval given to an ask the harness was no longer holding (the
        # box slept or restarted with it pending). The continuation makes the
        # agent raise the same ask again, and the person has already answered
        # it: the next identical ask is granted from here, once.
        self._standing_approvals: dict[str, PermissionOptionId] = {}
        # Calls the workspace re-ran itself after a reader's allow reached a
        # restored ask, by tool call id. The real result closed each of them;
        # a harness closing the same call later — opencode aborts the part it
        # finds pending when the continuation arrives — must not overwrite it.
        self._replayed_calls: OrderedDict[str, None] = OrderedDict()
        # Set when the document said a turn was working as this box came up
        # and no harness here is running it. Whether the reader is told the
        # turn was lost waits for the first catch-up: an ask the transcript
        # still holds (or an answer recorded for it) means the turn was parked
        # on a person, not lost, and the mirror is the one continuing it.
        self._lost_turn_unexplained = False
        #: The messages whose interrupted turn this mirror queued to run again,
        #: by the transcript id every reader holds them under: the lane runs
        #: them as a restart (the model is told so) rather than as a new turn.
        self._restart_marks: set[str] = set()
        #: Set once the first catch-up pass has read the transcript and queued
        #: whatever it owed: what :meth:`wait_for_owed_turn` waits on.
        self._first_pass = asyncio.Event()
        #: Set while the lane is handing a message to the harness.
        self._asking = False
        self._seen_relays: OrderedDict[str, None] = OrderedDict()
        # How far into the transcript this mirror has consumed. A relay is
        # live-only fan-out, so a message sent while this box was not subscribed
        # reaches nobody; the transcript is durable, and `catch_up` reads it.
        # Both of these are what keeps that read from answering a question
        # twice: the sequence never moves backwards, and a message taken by the
        # live path is remembered by the id the browser gave it.
        self._consumed_seq = 0
        self._consumed_clients: OrderedDict[str, None] = OrderedDict()
        # Clear while a turn is running. Prompts are handed over one at a time
        # because a prompt sent over a live turn SUPERSEDES it — catching up on
        # two messages by sending both at once would answer only the second.
        self._idle = asyncio.Event()
        self._idle.set()
        # Every question this chat has been asked and not yet handed over, in
        # the order it was asked. One lane, whichever path the question came
        # in on — a live relay or a catch-up read — because "one at a time" is
        # a property of the harness, not of the path that reaches it.
        self._prompts: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        # The question the lane has taken off that queue and has not yet handed
        # to the harness — waiting out the running turn, or being prepared. It
        # is still queued as far as a reader is concerned, so a stop has to be
        # able to take it back.
        self._holding: dict[str, Any] | None = None
        # How many stops this mirror has been sent. Only ever compared: a
        # prompt that was in flight while the count moved was overtaken by a
        # reader ending the turn, and the turn it started is cancelled.
        self._stops = 0
        # Whether a reader ended the turn the harness is still winding down.
        # Cancelling stops the tool, not the agent loop: the harness gets the
        # cancelled call back as a result and takes ANOTHER model step on it.
        # Nothing that step produces is this chat's record — the turn ended
        # when the reader said so — so it is dropped rather than published, and
        # the agent is told again to stop.
        self._ending_turn = False
        self._ending_cancels = 0
        #: Whether this stopped turn's end is already on the transcript. Two
        #: paths stamp it and a turn ends once.
        self._stamped_stop = False
        # The tool calls this turn has put on the reader's screen. A stop
        # settles those; one the agent opens after it is never published at
        # all, so its result has no card to close.
        self._live_calls: OrderedDict[str, None] = OrderedDict()
        self._catching_up = False
        # Whether the last catch-up pass could not read the transcript, so the
        # questions it would have found are still unknown here.
        self._catch_up_unread = False
        self._own_events: OrderedDict[str, None] = OrderedDict()
        self._relay_tasks: set[asyncio.Task[None]] = set()
        # The subset of ``_relay_tasks`` that is doing nothing but waiting for
        # an ask to be answered. Held apart because "an ask is parked" and "a
        # relay is running" are different claims on the chat, and only the
        # first of them may ever go cold (see ``parked_only``).
        self._ask_waiters: set[asyncio.Task[None]] = set()
        # When a reader was last seen doing something here. Stamped from the
        # mirror's own clock at construction, so a chat opened for a reader
        # and then abandoned measures its silence from the open.
        self._reader_at = clock()
        self._ready = asyncio.Event()
        self._state: MirrorState = "starting"
        self._failure: str | None = None
        self._current_turn_id: str | None = None
        #: The harness ids of the turns that already ended here. The lane hands
        #: the next message over the moment a turn ends, and until the harness
        #: reports that new turn running it has no id of its own — so a late
        #: status for the turn before it would otherwise be read as the new
        #: turn's, end it, and let the message queued behind it in.
        self._ended_turns: OrderedDict[str, None] = OrderedDict()
        #: What this box calls the turn it is on, for the rows it has to be
        #: able to write the same way twice. The harness's own turn id when
        #: there is one, else the transcript id of the message that started it
        #: — a name a box that comes back to this chat can read off the record,
        #: which the harness's id (minted inside a process that is gone) is
        #: not. Never cleared: a turn that has ended is still the turn a row
        #: written about it belongs to.
        self._turn_mark: str | None = None
        self._turn_announced = False
        #: When the turn state was last put on the wire, on the mirror's own
        #: monotonic clock. A running turn re-stamps off this rather than off
        #: the loop's tick count, so a slow pass under-says it and never
        #: over-says it.
        self._working_stamped = 0.0
        self._published_count = 0
        self._chunk_count = 0
        self._ignored_relays = 0
        # The stance the chat's own record stores, which a reader chose and the
        # mode relay changes while it runs. A chat whose record names none opens
        # as an analyst's. The fence below does not move with any of them.
        self._mode: PermissionMode = permission_mode

    # -- observability ----------------------------------------------------------

    @property
    def chat_id(self) -> str:
        return self._chat_id

    @property
    def files_drive_id(self) -> str | None:
        """The drive the chat's folder is on, as the chat record named it."""
        return self._files_drive_id

    @property
    def state(self) -> MirrorState:
        return self._state

    @property
    def failure(self) -> str | None:
        return self._failure

    @property
    def published_count(self) -> int:
        return self._published_count

    @property
    def chunk_count(self) -> int:
        return self._chunk_count

    @property
    def ignored_relays(self) -> int:
        """Relays refused by a check (logged, never executed)."""
        return self._ignored_relays

    @property
    def session(self) -> ChatSession | None:
        return self._session

    @property
    def doc(self) -> DocHandle | None:
        return self._doc

    @property
    def pending_interrupts(self) -> list[str]:
        return list(self._interrupts)

    @property
    def turn_running(self) -> bool:
        """Whether the budget meter is counting a turn right now."""
        return self._meter.running

    @property
    def _queued_prompts(self) -> bool:
        """Whether any message this box has taken is still waiting to be asked
        — on the queue, or held by the lane for the running turn to end."""
        return self._holding is not None or not self._prompts.empty()

    @property
    def waiting_on_a_person(self) -> bool:
        """Whether an ask is parked on a reader — live or rebuilt from the
        transcript. Such a chat is mid-turn: the answer continues the turn the
        ask interrupted, so the chat holds its session until it comes."""
        return bool(self._interrupts)

    @property
    def reader_seen_at(self) -> float:
        """When a reader was last seen in this chat, on the mirror's clock."""
        return self._reader_at

    def note_reader(self) -> None:
        """A reader did something here — sent a message, answered an ask,
        switched the stance, or opened the chat. What the parked-ask window is
        measured from: an ask still has somebody behind it for as long as this
        keeps moving."""
        self._reader_at = self._clock()

    @property
    def parked_only(self) -> bool:
        """Whether the ONLY thing this chat owes is an ask parked on a person.

        A turn parked on an ask holds its session for as long as somebody may
        still answer it — but nothing else here is in flight, so the session
        is all that would be lost by sleeping, and the ask itself is durable:
        it stays in the transcript and the next open re-offers it. That makes
        this the one kind of busy a window may run against, and the service is
        where that window lives.

        Everything else is excluded: a message taken but not handed over, a
        relay still running (an ask's own waiter is not one), a turn the meter
        says is RUNNING rather than held on a person, and a background shell,
        query or subagent the session is still carrying.
        """
        if not self._interrupts:
            return False
        if self._meter.running and not self._meter.waiting:
            return False
        if self._queued_prompts or (self._relay_tasks - self._ask_waiters):
            return False
        session = self._session
        return session is None or not session.has_running_background

    @property
    def current_turn_id(self) -> str | None:
        return self._current_turn_id

    @property
    def permission_mode(self) -> PermissionMode:
        """What this chat's asks are decided by right now."""
        return self._mode

    @property
    def chat_folder(self) -> Path:
        """The one directory this chat may write, on this box.

        Its own folder under ``.alkera/chats`` — the local half of the folder
        the box leases while the chat is awake. Every other location on the
        machine is somebody else's: the project, another chat's transcript, the
        operator's credentials, the rest of the disk.
        """
        return self._runtime.project.chats().path / self._chat_id

    @property
    def working_dir(self) -> Path:
        """The directory the agent runs in: the chat's working directory,
        :func:`chat_sandbox_path` inside :attr:`chat_folder`.

        It is the effective root of the chat — what the person sees when they
        open the chat's files, where a relative path the model writes or reads
        resolves, where a dropped file lands, and the one place a write needs
        nobody's answer in any mode. The fence is the folder above it, so the
        chat's own records beside it stay the box's.
        """
        return chat_sandbox_path(self.chat_folder)

    async def open_session(
        self,
        broker: PermissionBroker,
        questions: QuestionBroker | None = None,
    ) -> ChatSession:
        """Open this chat's harness session, bounded the way a cloud chat is.

        The agent RUNS in the chat's working directory (:attr:`working_dir`),
        which is also the session's sandbox: a model asked to "create a file"
        names it relatively, and there the write lands where the person will
        look for it, free in every mode. The write fence is the chat folder
        around it — the whole of the chat's durable state on this box — so an
        absolute write into the folder's own top level is the mode's business
        and one onto the chat's records is refused outright.

        Reads are the wider bound and are unchanged: the whole workspace, minus
        the daemon's own ``.alkera`` state (other chats' transcripts,
        file-custody credentials, the connections manifest) and minus
        ``ALKERA_HOME``, plus this chat's own folder. That bound rides INTO the
        session because an in-root read never reaches the broker below; the
        broker hook stays as the second line.
        """
        gateway_token = await self._mint_gateway_token()
        return await self._open_on(broker, questions, gateway_token)

    async def _open_on(
        self,
        broker: PermissionBroker,
        questions: QuestionBroker | None,
        gateway_token: str,
    ) -> ChatSession:
        working_dir = self.working_dir
        working_dir.mkdir(parents=True, exist_ok=True)
        self._broker = broker
        self._questions = questions
        self._gateway_token = gateway_token
        return await self._runtime.open_chat(
            self._chat_id,
            permission_broker=broker,
            question_broker=questions,
            working_dir=working_dir,
            sandbox_dir=working_dir,
            credential=chat_credential(self._machine_rest, self._chat_id, gateway_token),
            # The chat's owner, as the server placed it here: the principal
            # this session's notes are filed under and read back for on a box
            # whose one knowledge store serves several people's chats.
            knowledge_owner=self._owner_user_id or "",
            path_fence=PathFence(
                escape=self._fence_escape,
                reason=self._fence_reason,
                session=self.session_fence,
                must_ask=self._fence_must_ask,
                agent_home=agent_home_for(SandboxSettings.from_env(), current_capability()),
                canonical=self.session_fence.canonical,
            ),
        )

    async def _mint_gateway_token(self) -> str:
        """The chat's own gateway credential, minted as the machine right before
        the session opens — fresh at every open, so a reopened chat never runs
        on a token that is about to lapse. A mint the backend refuses is a
        session that does not open: the agent runs with this credential or not
        at all, never with the box's own bearer in its place."""
        body = await self._machine_rest.mint_gateway_token(self._chat_id)
        token = body.get("token")
        if not isinstance(token, str) or not token:
            raise ValueError("the backend minted no gateway token for this chat")
        expires = body.get("expires_at")
        self._gateway_token_expires_at = (
            datetime.fromisoformat(expires) if isinstance(expires, str) and expires else None
        )
        return token

    @property
    def gateway_token_expires_at(self) -> datetime | None:
        """When the credential the running agent holds lapses; ``None`` before
        the first open."""
        return self._gateway_token_expires_at

    async def _reopen_on(self, gateway_token: str) -> None:
        """Close the agent and open it again on ``gateway_token``, with the
        same brokers, stance and event pump the mirror started it with."""
        assert self._broker is not None
        pump = self._pump_task
        if pump is not None:
            pump.cancel()
            with contextlib.suppress(BaseException):
                await pump
            if pump in self._tasks:
                self._tasks.remove(pump)
            self._pump_task = None
        await self._runtime.close_chat(self._chat_id)
        try:
            session = await self._open_on(self._broker, self._questions, gateway_token)
        except Exception as exc:
            # The old agent is gone and no new one came up: the mirror is
            # failed the way a start that could not open is, not left holding
            # a closed session every later message would silently fall into.
            self._session = None
            self._state = "failed"
            self._failure = f"{type(exc).__name__}: {exc}"
            raise
        self._session = session
        session.set_permission_mode(self._mode)
        self._start_pump(session)
        logger.info("mirror %s: the agent now runs on a renewed gateway token", self._chat_id)

    def _start_pump(self, session: ChatSession) -> asyncio.Task[None]:
        """Read ``session``'s events onto the chat. Subscribed HERE rather
        than on the task's first step: the bus has no replay, so anything
        published between creating the task and its first step would reach
        nobody."""
        events = session.subscribe()
        task = asyncio.get_running_loop().create_task(
            self._pump(events), name=f"mirror-pump:{self._chat_id}"
        )
        self._pump_task = task
        self._tasks.append(task)
        return task

    @property
    def session_fence(self) -> fence.SessionFence:
        """This chat's bounds, as the one judge every decision path asks: the
        harness's chokepoint, the in-tool shell gate, and the two mirror paths
        below. Reads are the workspace for a file tool and the working directory
        (plus the system roots) for the shell; writes are the working directory.

        The write bound is the working directory, not the chat folder around
        it: that folder's top level holds the chat's own records (the
        transcript, the decision and cost ledgers, the manifest), which are the
        box's to write, and a ``..`` from where the agent runs lands right
        beside them.

        The chat also owns its default Python environment under the runtime
        state (with its caches) and, under gVisor, the sandbox's own process table."""
        envs = session_envs_dir(self._chat_id, self.chat_folder / RUNTIME_STATE_SUBDIR)
        return fence.SessionFence(
            root=self._runtime.project.path.parent,
            folder=self.working_dir,
            working_dir=self.working_dir,
            system_roots=shell_read_roots(),
            aliases=self._mount_aliases(),
            own_trees=(envs,),
            sandboxed=SandboxSettings.from_env().mode == "gvisor" and current_capability().gvisor,
        )

    def _mount_aliases(self) -> tuple[tuple[str, Path], ...]:
        """The paths the agent sees host trees at (:func:`mount_aliases`)."""
        return mount_aliases(
            SandboxSettings.from_env(),
            current_capability(),
            working_dir=self.working_dir,
            runtime_dir=self.chat_folder / RUNTIME_STATE_SUBDIR,
            agent_config_root=agent_config_root(self._chat_id),
            envs_dir=session_envs_dir(self._chat_id, self.chat_folder / RUNTIME_STATE_SUBDIR),
        )

    def _fence_escape(self, request: PermissionRequest) -> str | None:
        """The first location ``request`` names out of bounds, for the harness's
        chokepoint — an ask whose reach cannot be proved is refused here when it
        is a write (the fence's standing posture) and left to
        :meth:`_fence_must_ask` when it is a read."""
        verdict = self.session_fence.judge_ask(request, writing=_authorizes_a_write(request))
        return verdict.target if verdict.escaped else None

    def _fence_must_ask(self, request: PermissionRequest) -> bool:
        verdict = self.session_fence.judge_ask(request, writing=_authorizes_a_write(request))
        return verdict.unknown

    def _seed_local_chat(self, store: ChatStore) -> None:
        """The local chat record, carrying the row's picks, before the session opens.

        The cloud chose the id; the local chat is created under it so a
        reconnect (or a re-provisioned box that still has the workspace) lands
        on the same transcript and the same harness session. The chat's
        DIRECTORY may already be there with no record in it: the folder lease
        pulls the chat's files down into that very directory before this mirror
        starts. A box that decided "already created" by the directory alone
        skipped the record, opened the chat on a reconstructed manifest that
        named no model, and the harness refused every turn of every chat a
        browser started — while the composer showed the model the reader had
        picked.

        A record that does exist is moved onto the row's picks when they differ.
        The row is the durable word on the model and the stance: a chat pinned
        server-side after this box last opened it, or switched while it slept,
        resumes on what the row says, through the same translation the model
        relay applies to a running session. The runtime reads the manifest to
        build the session — the pin becomes the gateway config, the stance the
        mode the source instructions are rendered for — so both have to be on
        the manifest before the open, not set on the session after it.
        """
        if not store.has_record(self._chat_id):
            chat = store.create(
                session_id=self._chat_id,
                title=self._title,
                model=self._model,
                harness_type=self._harness_type,
                adopt_dir=True,
            )
            chat.manifest.permission_mode = self._mode
            if self._sandbox:
                chat.manifest.harness = {**chat.manifest.harness, **self._sandbox}
            chat.flush_manifest()
            chat.close()
            return
        chat = store.open(self._chat_id)
        try:
            changed = False
            if (pin := self._seeded_pin(chat.manifest.model)) != chat.manifest.model:
                chat.manifest.model = pin
                changed = True
            if chat.manifest.permission_mode != self._mode:
                chat.manifest.permission_mode = self._mode
                changed = True
            bag = sandbox_bag_on(chat.manifest.harness, self._sandbox)
            if bag != chat.manifest.harness:
                chat.manifest.harness = bag
                changed = True
            if changed:
                chat.flush_manifest()
        finally:
            chat.close()

    @property
    def activity(self) -> ChatActivity:
        """What this chat owes right now (``cloud/activity.py``), the one answer
        the service reads, from the finer questions below."""
        if self.parked_only:
            return ChatActivity.AWAITING_USER
        if self.quiescent:
            return ChatActivity.IDLE
        job = self._session is not None and self._session.has_running_background
        return ChatActivity.RUNNING_JOB if job else ChatActivity.WORKING

    @property
    def quiescent(self) -> bool:
        """Whether this chat owes nothing, and so may be put to sleep.

        The budget meter sees a foreground turn; the session sees the rest — a
        background shell, SQL job or subagent still running, and a tool or
        permission ask waiting on a human. The mirror sees what only it knows:
        a message it has taken but not yet handed over, and a relay or catch-up
        still running. Anything in flight makes the chat busy, however long it
        has been quiet — a turn may legitimately run for hours, and a step that
        says nothing while it works is still work.

        A mirror with no session (one that failed to start, or has already
        stopped) is quiescent: there is nothing left in it to lose, and calling
        it busy would leave it in the box's slot forever.
        """
        if self._meter.running or self._interrupts:
            return False
        if self._queued_prompts or self._relay_tasks or self._unpublished:
            # A question taken off the wire but not yet put to the agent, a relay
            # still running, or an event the server has not answered for: the
            # sleep would drop it (a turn's end, leaving the chat owing a turn).
            return False
        session = self._session
        if session is None:
            return True
        return session.is_quiescent()

    @property
    def publishing_refusal(self) -> PublishingRefusal | None:
        """Why the mirror stopped publishing, when the gateway refused it."""
        return self._refused

    @property
    def turn_user_id(self) -> str:
        """The member the running (or last) turn runs for."""
        return self._attribution.current

    # -- lifecycle --------------------------------------------------------------

    async def start(self) -> None:
        if self._session is not None:
            return
        # The document first: a chat the cloud never declared answers the
        # hello with not_found, and such a chat gets no local session, no
        # harness and no transcript here.
        # No presence: the roster answers "who else is READING this", and the
        # box is not a reader. Joining it put a second "person" in every shared
        # chat — the machine, wearing the operator's user id — and left the
        # reader counting a participant who is not one.
        doc = self._socket.open_doc("chat", self._chat_id, presence=False)
        self._doc = doc
        doc.on_op(self._on_doc_op)
        refusal = await self._await_doc(doc)
        if self._stopped_mid_start():
            # A drain reached the box while the document was opening: the stop
            # closed and dropped it, and everything below reads it.
            doc.close()
            raise ChatMirrorStoppedError(self._chat_id)
        if refusal is None and not self._doc.can_write:
            # The document opened, as a reader's: the gateway does not hold
            # this box to be the chat's publisher (the chat is bound to another
            # machine, or to none, and the box's user does not own it). A turn
            # run here could never be published, so none is: no session, no
            # harness, no spend — and the reason is on record.
            refusal = NOT_PUBLISHER
            logger.warning(
                "mirror %s: this machine (%s) is not the chat's publisher; the chat is not served",
                self._chat_id,
                self._machine_id or "unregistered",
            )
        if refusal is not None:
            self._state = "failed"
            self._failure = f"chat document refused: {refusal}"
            self._doc.close()
            self._doc = None
            raise ChatMirrorRefusedError(self._chat_id, refusal)
        self._seed_local_chat(self._runtime.project.chats())
        await self._load_source_brief()
        self._refuse_if_stopped()
        broker = PermissionBroker(self._resolve_permission, default_timeout_seconds=None)
        questions = QuestionBroker(self._resolve_question, default_timeout_seconds=None)
        try:
            session = await self.open_session(broker, questions)
        except Exception as exc:
            self._state = "failed"
            self._failure = f"{type(exc).__name__}: {exc}"
            raise
        if self._stopped_mid_start():
            # The stop ran while this was opening, so it saw no session to
            # close and left the harness this call just spawned behind. Nothing
            # else will: the mirror is off the service's table already.
            with contextlib.suppress(Exception):
                await self._runtime.close_chat(self._chat_id)
            raise ChatMirrorStoppedError(self._chat_id)
        self._session = session
        # The stance the chat was opened in — the one its record stores, so a
        # chat that slept in `default` resumes in `default` rather than dropping
        # back to the floor its reader had already moved it off. `read_only` is
        # an analyst's: the harness refuses a mutation itself, before any reader
        # is asked, and a re-run through the SQL tool is gated the same way.
        # What the mode changes is whether a write inside the chat's own folder
        # prompts, never where a write may land.
        self._session.set_permission_mode(self._mode)
        loop = asyncio.get_running_loop()
        self._tasks = []
        # First, so a note this very method is about to write reaches the chat.
        self._start_pump(self._session)
        self._tasks += [
            loop.create_task(self._publisher(), name=f"mirror-publish:{self._chat_id}"),
            loop.create_task(self._flusher(), name=f"mirror-chunks:{self._chat_id}"),
            loop.create_task(self._budget_watch(), name=f"mirror-budget:{self._chat_id}"),
            loop.create_task(self._prompt_lane(), name=f"mirror-prompts:{self._chat_id}"),
        ]
        self._state = "running"
        self._ready.set()
        # Only now: the first snapshot arrives while the document is still
        # opening, and there is nothing to run a message against until the
        # session exists.
        self._doc.on_snapshot(self._on_snapshot)
        await self._settle_interrupted_turn()
        await self._session.report_interrupted_jobs()
        await self.post_owed_notes()
        # The chat may already be holding a question nobody answered — the very
        # first one, or everything typed while this box was away. In the
        # background: the pass waits for each turn it starts, and the caller
        # that started this mirror has other chats to bring up.
        self.request_catch_up()

    def _stopped_mid_start(self) -> bool:
        """Whether a stop ran in the window the start was awaiting something in.

        Asked rather than compared in place at each resumption point: a start
        reads the state again after every await, and the reader of the code —
        like the type checker — should not take the first read for the answer
        to the last.
        """
        return self._state == "stopped"

    def _refuse_if_stopped(self) -> None:
        """Stop the start where it is if the mirror was taken down under it."""
        if self._stopped_mid_start():
            raise ChatMirrorStoppedError(self._chat_id)

    async def _settle_interrupted_turn(self) -> None:
        """Tell the transcript about a turn this box was killed in the middle of.

        The supervisor brings the mirror back in a couple of seconds — well
        inside the heartbeat window — so the machine never reads anything but
        ``ready`` and no banner ever says a word. The only record that a turn
        was in flight is the document itself, which still says ``working``
        while the reopened harness session runs nothing. Left alone, the
        composer sits in its working state until the browser's 60 s stall
        watchdog gives up on it: a minute of silence with nothing to read.
        """
        state = (self._doc.state if self._doc is not None else None) or {}
        meta = state.get("meta")
        turn_state = meta.get("turn_state") if isinstance(meta, dict) else None
        if not isinstance(turn_state, dict) or turn_state.get("state") != "working":
            return
        if self._meter.running:
            return
        self._queue_meta({"state": "idle"})
        # Whether that turn was LOST — as against parked on a reader, whose
        # answer this box will act on — is what the transcript says; the first
        # catch-up reads it and tells the reader only when nothing is picking
        # the turn back up.
        self._lost_turn_unexplained = True

    async def _explain_lost_turn(
        self,
        users: Sequence[Mapping[str, Any]] = (),
        *,
        answered_to: int = 0,
        finished_to: int = 0,
        tools_run: int = 0,
    ) -> None:
        """Pick up the turn a restart cut short — once, and only when no ask in
        the transcript is carrying it on.

        The turn is restarted from the person's last message, read off the
        chat's record (a box that replaced the one that died has no other copy
        of it). A message the record shows nobody answered at all is left to
        the catch-up that runs unanswered messages, so it is not run twice.
        The reader is told the turn is lost only when there is no message to
        restart from, and told it failed when its restarts are spent."""
        if not self._lost_turn_unexplained:
            return
        self._lost_turn_unexplained = False
        relay = _last_prompt_relay(users, chat_id=self._chat_id)
        asked_at = int(relay.get("seq") or 0) if relay is not None else 0
        if relay is not None and asked_at > answered_to:
            # Nothing answered it: the catch-up runs it as the message it is.
            return
        session = self._session
        if relay is None or session is None or not session.found_interrupted:
            # The record shows the machine answered this message and nothing
            # on this disk says a turn died here, so it is never restarted: a
            # box in a fresh workspace has no such mark, and the answer on the
            # record is the only word on the turn. Said lost only when no turn
            # end was recorded after the message.
            if relay is None or asked_at >= finished_to:
                await self._note(TURN_LOST_TO_RESTART, answers_nothing=True)
            return
        if tools_run:
            # The restarted turn would be a fresh harness session handed the
            # message again, so every tool the lost turn finished would run a
            # second time. The person decides whether to send it again.
            logger.info(
                "mirror %s: not restarting the interrupted turn: it already ran %d tool(s)",
                self._chat_id,
                tools_run,
                extra={"chat_id": self._chat_id, "tools_run": tools_run},
            )
            await self._note(turn_stopped_after_tools(tools_run), answers_nothing=True)
            return
        text = relay.get("text")
        if not isinstance(text, str) or not session.can_restart(text):
            await self._note(TURN_RESTART_FAILED, answers_nothing=True)
            return
        logger.info(
            "mirror %s: restarting the turn a restart interrupted",
            self._chat_id,
            extra={"chat_id": self._chat_id},
        )
        self._restart_marks.add(_prompt_mark(relay))
        await self._on_prompt(relay)

    async def _await_doc(self, doc: DocHandle) -> str | None:
        """``None`` once the document is live; the refusal code when the
        server would not open it; ``timeout`` when it never answered.

        The clock starts when the SOCKET is up, not when the mirror asked. A
        box that has just launched, or one whose socket is mid-reconnect, has
        not sent the hello yet — timing that wait as if the server were sitting
        on the question makes a chat read as refused for a fault that is the
        box's own connect, and costs the reader a whole discovery tick.
        """
        deadline = self._clock() + self._start_timeout
        # …but not forever: a backend that cannot be reached at all must not
        # hold the whole discovery pass open behind one chat.
        give_up = self._clock() + self._start_timeout * WAITING_FOR_SOCKET_MULTIPLIER
        while True:
            if doc.live.is_set():
                return None
            if doc.error is not None:
                return doc.error
            now = self._clock()
            if self._socket.state in ("idle", "connecting", "down") and now < give_up:
                deadline = now + self._start_timeout
            elif now >= deadline:
                return "timeout"
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(doc.live.wait(), 0.05)

    async def stop(self) -> None:
        if self._state == "stopped":
            return
        self._state = "stopped"
        # A pending ask is NOT settled here: it stays unresolved in the
        # transcript, which is what lets the next mirror — after a sleep, a
        # restart, a move to another box — re-offer the very same ask. The
        # harness is closed underneath it below; its own teardown hands the
        # adapter a reject that is never published.
        self._interrupts.clear()
        for task in [*self._tasks, *self._relay_tasks]:
            task.cancel()
        for task in [*self._tasks, *self._relay_tasks]:
            with contextlib.suppress(BaseException):
                await task
        self._tasks = []
        self._pump_task = None
        self._relay_tasks.clear()
        self._ask_waiters.clear()
        if self._doc is not None:
            self._doc.close()
            self._doc = None
        if self._session is not None:
            with contextlib.suppress(Exception):
                await self._runtime.close_chat(self._chat_id)
            self._session = None

    # -- publish -------------------------------------------------------------------

    async def _pump(self, events: AsyncIterator[Event]) -> None:
        assert self._session is not None
        async for event in events:
            self._roles.observe(event)
            if self._refused is not None:
                # Nothing more is published: the local transcript still records
                # the harness winding down, the cloud is told nothing.
                continue
            if is_chunk(event):
                if isinstance(event, AgentMessageChunk | AgentThoughtChunk):
                    self._coalescer.add(event)
                continue
            if self._ending_turn and str(event.event_id) not in self._own_events:
                if not await self._quell(event):
                    continue
            if (
                isinstance(event, ToolCallUpdate)
                and event.status == "error"
                and event.tool_call_id in self._replayed_calls
                and str(event.event_id) not in self._own_events
            ):
                # The harness closing a call the workspace already re-ran under
                # the reader's allow: the real result is on the transcript, and
                # a late "aborted" for the same call is not the result.
                continue
            exceeded: BudgetExceeded | None = None
            if self._own_events.pop(str(event.event_id), False) is False:
                # Only the harness's own events drive the turn: a note, a
                # re-run's tool call or a budget stop the mirror put on the
                # stream neither counts against the running turn nor ends it.
                self._track_turn(event)
                exceeded = self._meter.note(event)
                self._attribution.observe(event)
                if isinstance(event, PermissionRequest | QuestionRequest):
                    self._live_asks[event.request_id] = None
                    while len(self._live_asks) > relay_memory():
                        self._live_asks.popitem(last=False)
            if not self._note_ask_published(event):
                continue
            if isinstance(event, ToolCall):
                self._live_calls[event.tool_call_id] = None
                while len(self._live_calls) > relay_memory():
                    self._live_calls.popitem(last=False)
            await self._outbound.put(
                append_entry(event, self._roles, stopping=self._state == "stopped")
            )
            refusal = self._refusals.note(event)
            if refusal is not None:
                await self._say_refusal(refusal)
            if exceeded is not None:
                await self._stop_turn(exceeded)

    def _track_turn(self, event: Event) -> None:
        if not isinstance(event, SessionStatusChanged):
            return
        if (
            event.turn_id is not None
            and self._current_turn_id is None
            and self._meter.running
            and event.turn_id in self._ended_turns
        ):
            # A straggler from a turn that is already over, arriving after the
            # next message was handed over and before the harness said a word
            # about it. It speaks for neither turn.
            return
        if event.status == "running":
            self._begin_turn(event.turn_id)
            return
        if event.status not in _TERMINAL_STATUSES or not self._meter.running:
            return
        if (
            event.turn_id is not None
            and self._current_turn_id is not None
            and event.turn_id != self._current_turn_id
        ):
            # Another attempt's terminal: the turn the meter is keyed to is
            # still running.
            return
        self._end_turn()

    def _begin_turn(self, turn_id: str | None, *, mark: str | None = None) -> None:
        """A turn is under way: start the meter (once) and show the working
        state (once). Called when the human's prompt is handed to the harness —
        so a harness that never answers still runs down the wall clock — and
        again when the harness reports ``running`` with the attempt's id, which
        it does several times over inside one turn.

        ``mark`` is what the caller can name this turn by on the record; the
        harness's id only fills it in when nothing better was given, because
        the next process to open this chat cannot re-derive that one.
        """
        if not self._meter.running:
            self._meter.start()
        self._idle.clear()
        if mark:
            self._turn_mark = mark
        if turn_id:
            self._current_turn_id = turn_id
            if not self._turn_mark:
                self._turn_mark = turn_id
        if not self._turn_announced:
            self._turn_announced = True
            self._queue_meta({"state": "working", "budget": self._budget.as_dict()})

    async def _quell(self, event: Event) -> bool:
        """Whether one event the harness produced for a turn a reader ended
        still goes on the transcript. Most do not.

        What is dropped is everything that would ANSWER: a message the agent
        opened after the cancel, its text, its reasoning. Publishing that would
        put an answer on a chat whose own record says the turn was stopped, and
        would spend the tokens of a model step nobody asked for.

        What is kept is everything that CLOSES: the result of the call the
        cancel killed, and the harness's own terminal for the turn. Those are
        not an answer — they are how the reader's screen stops saying a tool is
        running and how the log describes, on its own rows, a turn that
        finished. Swallowing them left a tool card spinning for good.

        A harness that is STILL going after the cancel (it opened another
        message, or reported itself running again) is told again, a bounded
        number of times: a cancel that has to be repeated without limit is a
        wedged adapter, and hammering it would never free the chat that is
        already stamped idle.
        """
        if isinstance(event, ToolCallUpdate) and event.status in _SETTLED_CALL_STATES:
            # Only for a call the reader is already watching. A call the agent
            # opened AFTER the stop was never on screen, so its result closes
            # nothing — it would draw a card for work nobody saw start.
            return event.tool_call_id in self._live_calls
        if isinstance(event, SessionStatusChanged) and event.status in _TERMINAL_STATUSES:
            # The harness is done with an attempt. Published, because a
            # terminal only ever closes — but it does NOT lift the drop: this
            # is the terminal for the attempt the cancel killed, and the agent
            # loop goes straight on from it into another model step on the same
            # turn. Lifting here is what let a paragraph about being cancelled
            # reach a chat that had already said the turn was over.
            return True
        starting_again = isinstance(event, MessageCreated) or (
            isinstance(event, SessionStatusChanged) and event.status == "running"
        )
        if (
            starting_again
            and self._ending_cancels < _MAX_STOP_CANCELS
            and self._session is not None
        ):
            self._ending_cancels += 1
            logger.info(
                "mirror %s: the harness took another step after the reader's stop; cancelling it",
                self._chat_id,
                extra={"chat_id": self._chat_id, "cancels": self._ending_cancels},
            )
            await self._session.cancel()
        return False

    def _end_turn(self) -> None:
        if self._current_turn_id is not None:
            self._ended_turns[self._current_turn_id] = None
            while len(self._ended_turns) > relay_memory():
                self._ended_turns.popitem(last=False)
        self._meter.stop()
        self._turn_announced = False
        self._current_turn_id = None
        self._idle.set()
        self._queue_meta({"state": "idle"})
        self._turn_over()

    def _turn_over(self) -> None:
        """Say the turn is over to whoever asked to hear it. Never raises: the
        listener's trouble is not the turn's."""
        if self._on_turn_end is None:
            return
        try:
            self._on_turn_end(self._chat_id)
        except Exception:
            logger.exception("mirror %s: the turn-end listener failed", self._chat_id)

    def _queue_meta(self, turn_state: dict[str, Any]) -> None:
        stamp = datetime.now(UTC).isoformat()
        self._working_stamped = self._clock()
        self._outbound.put_nowait(
            {"__meta__": {"turn_state": {**turn_state, "at": stamp}, "turn_state_at": stamp}}
        )

    async def _publisher(self) -> None:
        assert self._doc is not None
        while self._refused is None:
            entry = await self._outbound.get()
            self._sending = True
            try:
                self._reply_files().observe(entry)
                await self._land_entry(entry)
                self._check_reply_files(entry)
                await self._send_durable(entry)
            finally:
                self._sending = False

    @property
    def _unpublished(self) -> bool:
        """Whether an event is queued for the server or being sent to it."""
        return self._sending or not self._outbound.empty()

    def _reply_files(self) -> ReplyFileCheck:
        if self._reply_check is None:
            self._reply_check = ReplyFileCheck(
                chat_id=self._chat_id, chat_folder=self.chat_folder, working_dir=self.working_dir
            )
        return self._reply_check

    def _check_reply_files(self, entry: Mapping[str, Any]) -> None:
        """Note the files an agent's finished text part names that are not in
        the chat, once the landing that would have put them on the drive is
        done. The reply still goes: every reader shows such a reference as not
        in the chat, and the agent is told on its next turn so it can put the
        file back. Never raises — a check is not a reason to hold a reply."""
        try:
            missing = self._reply_files().missing_in(entry)
        except Exception:
            logger.warning(
                "mirror %s: the files a reply names could not be checked",
                self._chat_id,
                exc_info=True,
            )
            return
        if not missing:
            return
        logger.warning(
            "mirror %s: a reply names files that are not in the chat: %s",
            self._chat_id,
            ", ".join(item.shown_as for item in missing),
            extra={"chat_id": self._chat_id},
        )
        known = {item.shown_as for item in self._missing_files}
        self._missing_files.extend(item for item in missing if item.shown_as not in known)
        del self._missing_files[: max(0, len(self._missing_files) - _MISSING_FILES_KEPT)]

    def _missing_files_context(self) -> str | None:
        """The note about the last reply's missing files, for the turn about to
        start, once: only the ones still missing now, since the agent may have
        written one back after the reply that named it."""
        noted, self._missing_files = self._missing_files, []
        if not noted:
            return None
        still = self._reply_files().still_missing(noted)
        return missing_files_note(still) if still else None

    async def _send_durable(self, entry: dict[str, Any]) -> None:
        """Send one durable op and do not return until the server has
        answered THIS op: a drop, a moved epoch or a silent server means a
        resend (the server dedupes by event id); a verdict on the op itself
        — too large, refused — is acted on, and only that ends the attempt."""
        assert self._doc is not None
        meta = entry.get("__meta__")
        shrinks = 0
        while True:
            try:
                if isinstance(meta, dict):
                    await self._doc.send_op(
                        "set_meta", meta=meta, op_id=_op_id("meta", secrets.token_hex(6))
                    )
                else:
                    await self._doc.send_op(
                        "append", events=[entry], op_id=_op_id("append", str(entry["event_id"]))
                    )
                self._published_count += 1
                return
            except DocOpError as exc:
                if exc.code in ("disconnected", "stale_epoch", "timeout"):
                    # The socket will come back (or the epoch moved): resend at
                    # the new epoch; the server dedupes by event_id.
                    await self._sleep(RETRY_SLEEP_SECONDS)
                    continue
                if exc.code == "op_too_large" and meta is None and shrinks < MAX_SHRINKS:
                    before = entry_size(entry)
                    entry = bound_entry(entry, limit=max(1024, before // 2))
                    shrinks += 1
                    if entry_size(entry) < before:
                        continue
                if exc.code == "closed":
                    return
                if is_publishing_refusal(exc.code):
                    await self._refuse_publishing(exc.code, exc.message)
                    return
                logger.warning(
                    "mirror %s: op refused (%s): %s", self._chat_id, exc.code, exc.message
                )
                return

    async def _refuse_publishing(self, code: str, message: str) -> None:
        """The gateway will not take this chat's transcript from this socket.

        Not a condition to wait out: the box is not the chat's publisher any
        more (the chat moved to another machine, the box's user left the
        audience, the org is full). So the turn is stopped — the harness is
        cancelled and nothing more is spent through the gateway — nothing
        more is published, the mirror is ``failed`` with the reason, and the
        log names the chat and the refusal. The service sees the state on its
        next poll and retries the chat only once the gateway admits it again.
        """
        if self._refused is not None:
            return
        self._refused = PublishingRefusal(code=code, message=message)
        self._state = "failed"
        self._failure = f"publishing refused: {self._refused.reason}"
        logger.error(
            "mirror %s: publishing refused (%s); the turn is stopped and nothing more is "
            "published from this machine (%s)",
            self._chat_id,
            self._refused.reason,
            self._machine_id or "unregistered",
        )
        self._meter.stop()
        self._turn_announced = False
        self._current_turn_id = None
        for interrupt in list(self._interrupts.values()):
            self._settle_interrupt(interrupt, cancelled=True)
        self._interrupts.clear()
        while not self._outbound.empty():
            self._outbound.get_nowait()
        if self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.cancel()
        if self._on_refused is not None:
            try:
                await self._on_refused(self._chat_id, self._refused)
            except Exception:
                logger.exception("mirror %s: the refusal could not be reported", self._chat_id)

    async def _flusher(self) -> None:
        assert self._doc is not None
        while self._refused is None:
            await self._sleep(self._chunk_interval)
            if not len(self._coalescer):
                continue
            events = self._coalescer.flush()
            await self._land_streamed(events)
            for event in events:
                for piece in _split_chunk(event, self._socket.sizes.chunk_max_bytes):
                    if await self._doc.send_chunk([piece]):
                        self._chunk_count += 1

    # -- landing ---------------------------------------------------------------

    async def _land_streamed(self, events: Sequence[dict[str, Any]]) -> None:
        """Hold these frames until every file the text streamed so far names
        is on the drive. A reference split across frames is whole once its
        closing parenthesis arrives, so the text is kept per part."""
        if self._land_files is None:
            return
        for event in events:
            if event.get("event_type") != "agent.message_chunk":
                continue
            key = (str(event.get("message_id", "")), str(event.get("part_id", "")))
            text = self._streamed.get(key, "") + str(event.get("text") or "")
            self._streamed[key] = text
            while len(self._streamed) > relay_memory():
                self._streamed.popitem(last=False)
            await self._land_named(key, text)
            if event.get("is_final"):
                self._streamed.pop(key, None)

    async def _land_entry(self, entry: Mapping[str, Any]) -> None:
        """Hold one durable entry until the files its text part names are on
        the drive. What the stream already waited for is not waited for again."""
        if self._land_files is None or entry.get("role") == "user":
            return
        payload = entry.get("payload")
        part = payload.get("part") if isinstance(payload, Mapping) else None
        if not isinstance(part, Mapping) or part.get("type") != "text":
            return
        text = part.get("text")
        if not isinstance(text, str) or not text:
            return
        key = (str(part.get("message_id", "")), str(part.get("part_id", "")))
        await self._land_named(key, text)
        # The part is closed: nothing more streams for it.
        self._landings.pop(key, None)
        self._streamed.pop(key, None)

    async def _land_named(self, key: tuple[str, str], text: str) -> None:
        """Wait for the landing of every chat file ``text`` names.

        Each file is asked for once per part; a frame or entry that names it
        again waits on the same landing rather than starting another, so the
        durable part cannot overtake the stream that is still waiting for it.
        """
        assert self._land_files is not None
        targets = [
            reference.target
            for reference in chat_references(text)
            if chat_path(reference.target, chat_id=self._chat_id) is not None
        ]
        if not targets:
            return
        asked = self._landings.get(key)
        if asked is None:
            asked = self._landings[key] = {}
            while len(self._landings) > relay_memory():
                self._landings.popitem(last=False)
        fresh = [target for target in targets if target not in asked]
        if fresh:
            task = asyncio.ensure_future(self._land_quietly(fresh))
            for target in fresh:
                asked[target] = task
        waiting = {asked[target] for target in targets if not asked[target].done()}
        if waiting:
            await asyncio.gather(*(asyncio.shield(task) for task in waiting))

    async def _land_quietly(self, targets: list[str]) -> None:
        """One landing. Its trouble is a reply that goes without the bytes —
        the reader's own open asks the box for them — never a reply that
        does not go."""
        assert self._land_files is not None
        try:
            await self._land_files(targets)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "mirror %s: the files a reply names could not be put on the drive first",
                self._chat_id,
                exc_info=True,
            )

    async def _publish_own(self, event: Event) -> None:
        """Put one of the mirror's own events on the stream, remembered so the
        publish pump neither meters it nor lets it end the running turn."""
        if self._session is None:
            return
        self._own_events[str(event.event_id)] = None
        while len(self._own_events) > own_event_memory():
            self._own_events.popitem(last=False)
        await self._session.publish_event(event)

    # -- budget ------------------------------------------------------------------------

    async def _budget_watch(self) -> None:
        """The wall clock on a clock, not on the event stream.

        A model step that stalls emits nothing at all, so a budget checked only
        as events arrive would never fire on the one turn that most needs it —
        and this loop is the only thing standing between a stalled step and a
        reader watching a spinner until they give up. It therefore survives its
        own exceptions: one that escaped would end the watchdog for the life of
        the chat, and every later turn would run uncapped.

        It is also where the turn says it is still working (see
        :meth:`_restamp_working`) — for the same reason: a turn that says
        nothing on the event stream is exactly the one whose reader needs to be
        told it is alive.
        """
        while True:
            await self._sleep(BUDGET_TICK_SECONDS)
            try:
                exceeded = self._meter.check()
                if exceeded is not None:
                    await self._stop_turn(exceeded)
                else:
                    self._restamp_working()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("mirror %s: the turn budget could not be enforced", self._chat_id)

    def _restamp_working(self) -> None:
        """Say again that the turn is working, at the machine's own heartbeat
        cadence, for as long as it runs.

        The working state was stamped once, when the turn began, and a reader
        who opened the chat hours later met a stamp hours old — which reads as
        a turn that died rather than one that is thinking. The stamp is the
        only thing on the wire that says a turn is under way, so it has to be
        as current as the machine's own heartbeat: a chat that is working says
        so every fifteen seconds, and a stamp older than a few of those is a
        box that stopped, which is what it should mean.
        """
        if not self._meter.running or not self._turn_announced:
            return
        if self._clock() - self._working_stamped < TURN_STAMP_SECONDS:
            return
        self._queue_meta({"state": "working", "budget": self._budget.as_dict()})

    async def _stop_turn(self, exceeded: BudgetExceeded) -> None:
        if not self._meter.running:
            return
        turn_id = self._current_turn_id
        self._meter.stop()
        self._turn_announced = False
        self._current_turn_id = None
        # A stopped turn is over. Without this the chat never reads as free
        # again — the harness's own terminal status arrives with the meter
        # already stopped, so nothing else sets it — and the next question
        # would wait out the whole idle budget behind a turn that ended.
        self._idle.set()
        logger.warning("mirror %s: %s", self._chat_id, exceeded.message)
        for interrupt in list(self._interrupts.values()):
            self._settle_interrupt(interrupt, cancelled=True)
        self._interrupts.clear()
        if self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.cancel()
        self._queue_meta(
            {
                "state": "stopped",
                "reason": exceeded.cap,
                "message": exceeded.message,
                "limit": exceeded.limit,
                "observed": exceeded.observed,
            }
        )
        # Stamped with the attempt it ends, so the harness settles that
        # attempt; the mirror's own meter is already stopped above.
        await self._publish_own(
            SessionStatusChanged(
                event_id=f"budget-{secrets.token_hex(8)}",
                time=datetime.now(UTC),
                session_id=self._chat_id,
                status="aborted",
                phase="idle",
                detail=exceeded.message,
                turn_id=turn_id,
            )
        )
        # Last, once the agent has been told to stop: what it wrote up to here
        # is what the turn leaves behind.
        self._turn_over()

    # -- consume -----------------------------------------------------------------------

    # -- catching up on what was said while nobody was listening -----------------

    async def catch_up(self, *, last_seq: int | None = None) -> int:
        """Answer every question this chat is holding that nobody answered.

        A ``user_message`` relay is live fan-out and nothing else: a message
        posted while this box was not subscribed to the chat's document is
        broadcast to zero subscribers and dropped. That is not an edge case —
        it is the FIRST question of every chat (the browser creates the chat and
        posts into it milliseconds later, long before this box has heard the
        chat exists) and everything typed while the box was down.

        The transcript is durable, so it is also the record of what still needs
        an answer: everything up to the last entry the MACHINE published has
        been dealt with, and a user message after that was never consumed.
        Returns how many prompts were handed to the harness.

        Idempotent by construction — a message this mirror already took is
        skipped by its client id, and the consumed sequence only moves forward —
        and safe to call on every reconnect, every discovery tick and at start.
        ``last_seq`` (the chat row's own counter) short-circuits the read when
        the transcript cannot have moved since the last pass.
        """
        if self._state not in ("running", "starting") or self._session is None:
            return 0
        if last_seq is not None and last_seq <= self._consumed_seq:
            return 0
        if self._catching_up:
            # A pass is already walking this transcript. Two of them would both
            # read the same rows before either had marked one consumed.
            return 0
        self._catching_up = True
        try:
            return await self._catch_up_locked()
        finally:
            self._catching_up = False

    async def _catch_up_locked(self) -> int:
        try:
            read = await self._transcript_since(self._consumed_seq)
        except (CloudApiError, httpx.HTTPError, OSError, ValueError) as exc:
            logger.info(
                "mirror %s: could not read the transcript to catch up: %s", self._chat_id, exc
            )
            self._catch_up_unread = True
            return 0
        self._catch_up_unread = False
        if not read.rows:
            await self._explain_lost_turn()
            return 0
        # Before anything else: what this chat's turn is called. A box that
        # restarted holds no memory of it, and the rows it may still have to
        # write about that turn — its terminal, above all — have to spell the
        # id the box before it would have spelled.
        self._recover_turn_mark(read.users)
        # Before the questions: an ask the transcript holds unanswered is
        # somebody's to answer, whether or not a new question follows it.
        self._remember_resolved(read.resolved)
        self._rearm_asks(read)
        # A turn this box came up in the middle of, with nothing in the
        # transcript picking it back up, is restarted from its message.
        await self._explain_lost_turn(
            read.users,
            answered_to=read.answered_to,
            finished_to=read.finished_to,
            tools_run=len(read.tools_since_prompt),
        )

        answered_to = max(self._consumed_seq, read.answered_to)
        highest = read.highest

        # Only a question is a question: a user-role row the reader never wrote
        # — the harness's own echo of the message it accepted — reads back as
        # nothing, and a row that cannot be read at all is stepped over rather
        # than raised on, because one raise here ends the whole pass and the
        # chat never catches up again.
        relays = [
            _relay_from_transcript(row, chat_id=self._chat_id)
            for row in read.users
            if int(row["seq"]) > answered_to
        ]
        pending = [
            relay
            for relay in relays
            if relay is not None
            and relay.get("text")
            and relay.get("client_id") not in self._consumed_clients
        ]
        if not pending:
            # Nothing to run, but the read still tells us where the transcript
            # stands, so the next pass can skip it entirely.
            self._consumed_seq = max(self._consumed_seq, highest)
            return 0

        logger.info(
            "mirror %s: %d unanswered message(s) in the transcript; running them in order",
            self._chat_id,
            len(pending),
            extra={"chat_id": self._chat_id, "pending": len(pending)},
        )
        for relay in pending:
            # Into the same lane a live relay rides, in the order they were
            # asked. The lane hands them over one at a time — a prompt sent
            # over a live turn supersedes it, so firing two at once would
            # answer only the last.
            await self._handle_relay(dict(relay))
        self._consumed_seq = max(self._consumed_seq, highest)
        return len(pending)

    def _recover_turn_mark(self, users: Sequence[Mapping[str, Any]]) -> None:
        """Name the turn this chat is on from the record, for a box that was
        not the one that started it.

        The name is the transcript id of the person's last message, read back
        through the same two functions a live dispatch goes through, so a box
        that came up after a restart and the box it replaced spell one id
        rather than two — which is the whole of what makes a terminal written
        twice one row.

        A mark this process set itself wins: it is the turn actually running
        here, and the record only ever says what the last one was.
        """
        if self._turn_mark is not None:
            return
        for row in reversed(list(users)):
            if not _is_prompt_row(row):
                # A reader's row that is not a message — a recorded answer to
                # an ask. It restarts a turn, but it is not what a message is
                # named by, and reading it as one would log a refusal per row.
                continue
            relay = _relay_from_transcript(row, chat_id=self._chat_id)
            mark = _prompt_mark(relay) if relay is not None else ""
            if mark:
                self._turn_mark = mark
                return

    def request_catch_up(self, *, last_seq: int | None = None) -> None:
        """Catch up in the background. Never awaited by a caller on a loop of
        its own — a pass hands prompts over one at a time and waits for each
        turn, which no discovery tick or start-up path may block on."""
        if self._state not in ("running", "starting"):
            return
        task = asyncio.get_running_loop().create_task(self._catch_up_quietly(last_seq=last_seq))
        self._relay_tasks.add(task)
        task.add_done_callback(self._relay_tasks.discard)

    def _on_snapshot(self, _state: dict[str, Any], _epoch: int, _seq: int) -> None:
        """A fresh snapshot means the subscription was (re-)established — which
        is exactly when messages relayed into the gap have to be collected. The
        snapshot itself carries only what the machine published, so the gap is
        invisible in it."""
        self.request_catch_up()

    async def _catch_up_quietly(self, *, last_seq: int | None = None) -> None:
        try:
            await self.catch_up(last_seq=last_seq)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("mirror %s: catching up failed", self._chat_id)
        finally:
            self._first_pass.set()
        await self._retry_unread_catch_up()

    async def _retry_unread_catch_up(self) -> None:
        """Read the transcript again, on a widening wait, while the last pass
        could not read it. A pass that another caller runs meanwhile and that
        reads it ends the retries; so does the mirror stopping."""
        for wait in CATCH_UP_RETRY_SECONDS:
            if not self._catch_up_unread or self._state not in ("running", "starting"):
                return
            await self._sleep(wait)
            if not self._catch_up_unread or self._state not in ("running", "starting"):
                return
            try:
                await self.catch_up()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("mirror %s: catching up failed", self._chat_id)
                return

    async def wait_for_owed_turn(self, within: float) -> bool:
        """Wait, at most ``within`` seconds, until the turn this chat owes has
        been handed to the harness: the first catch-up has read the transcript,
        and the message it queued (a message nobody answered, or the restart
        of an interrupted turn) has left the lane. ``True`` when it has, or
        when nothing was owed after all.

        A box taking chats one by one calls this for a chat the listing says
        owes a turn, so the person waiting on it is answered before the box
        spends the next minutes taking chats nobody is waiting on."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + within
        try:
            await asyncio.wait_for(self._first_pass.wait(), within)
        except TimeoutError:
            return False
        while not self._prompts.empty() or self._holding is not None or self._asking:
            if self._state not in ("running", "starting") or loop.time() >= deadline:
                return False
            await asyncio.sleep(0.05)
        return True

    async def _await_idle(self) -> bool:
        """Wait for the current turn to settle. ``False`` when it never does —
        the caller leaves the rest for a later pass rather than superseding a
        turn that is still working."""
        if self._idle.is_set():
            return True
        if self._budget.wall_clock_seconds is None:
            # No wall clock: a turn waits as long as it needs to — on the
            # model, or on a person who has not come back to the chat yet.
            await self._idle.wait()
            return True
        budget = self._budget.wall_clock_seconds + IDLE_WAIT_MARGIN_SECONDS
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._idle.wait(), budget)
        return self._idle.is_set()

    async def _transcript_since(self, after_seq: int) -> _CatchUpRead:
        """Walk the chat's transcript after ``after_seq`` to the end, keeping
        only what the catch-up acts on.

        The walk is deliberately unbounded — a turn that publishes every part,
        every tool call and every status change for days is exactly the chat
        this read exists to resume, and stopping early silently drops its tail.
        What it HOLDS is bounded instead: a page is folded into
        :class:`_CatchUpRead` and then dropped, and the fold keeps only what is
        still open — so a turn of half a million tool calls costs the asks
        nobody has answered and the reader's own messages, not the transcript,
        on every mirror the box serves at once.
        """
        read = _CatchUpRead()
        cursor = after_seq
        while True:
            page = await self._transcript_rest.list_chat_messages(
                self._chat_id, after_seq=cursor, limit=TRANSCRIPT_PAGE_ROWS
            )
            items = page.get("items")
            if not isinstance(items, list) or not items:
                break
            self._fold_page(read, items)
            nxt = page.get("next_after_seq")
            if not isinstance(nxt, int) or nxt <= cursor:
                break
            cursor = nxt
        return read

    def _fold_page(self, read: _CatchUpRead, items: Sequence[Any]) -> None:
        """Fold one page into the read, keeping no row the pass will not use."""
        for row in items:
            if not isinstance(row, dict):
                continue
            read.rows += 1
            seq = row.get("seq")
            role = str(row.get("role") or "")
            if isinstance(seq, int):
                read.highest = max(read.highest, seq)
                # Everything at or below the last thing the machine published
                # is done — except a note the mirror wrote about itself, which
                # answers nothing. The rule is api-core's, so the server reads
                # the same rows the same way when it decides which of this
                # reader's messages a Stop means were never run.
                if answers_a_waiting_message(role=role, event_id=str(row.get("event_id") or "")):
                    read.answered_to = max(read.answered_to, seq)
                if role == "user":
                    # A reader's own row: the only kind a question can be
                    # relayed from, and the only kind kept whole. Which of them
                    # is still unanswered is not known until the walk ends.
                    read.users.append(row)
                    if _is_prompt_row(row):
                        read.tools_since_prompt.clear()
            event = _harness_event_of(row)
            if event is not None and isinstance(seq, int) and _ends_a_turn(event):
                read.finished_to = max(read.finished_to, seq)
                if role != RECORDED_ANSWER_ROLE:
                    _retire_answers_before(read, seq)
            if (
                event is not None
                and role != "user"
                and event.get("event_type") in ("tool.call", "tool.call_update")
                and isinstance(event.get("tool_call_id"), str)
            ):
                read.tools_since_prompt.add(event["tool_call_id"])
            if event is not None and event.get("event_type") in _REARM_KINDS:
                # An ask, its resolution, or the call an ask gates — what
                # ``_rearm_asks`` reads the chat's pending state out of. A
                # reader's recorded answer is BOTH this and a user row.
                _fold_ask(read, row, event)

    def _on_doc_op(self, op: DocOp) -> None:
        if op.intent != "user_message":
            return
        if self._socket.peer_id is not None and op.peer_id == self._socket.peer_id:
            return
        # Anything another peer puts on this chat's document is a reader in it:
        # a message, an answer to an ask, a stance switch. The box takes no
        # presence on the document, so this is the whole of what it can see of
        # a reader — and it is what re-arms the window a parked ask goes cold on.
        self.note_reader()
        if op.op_id:
            if op.op_id in self._seen_relays:
                return
            self._seen_relays[op.op_id] = None
            while len(self._seen_relays) > relay_memory():
                self._seen_relays.popitem(last=False)
        for relay in op.events:
            task = asyncio.get_running_loop().create_task(self._handle_relay(relay))
            self._relay_tasks.add(task)
            task.add_done_callback(self._relay_tasks.discard)

    def _mark_consumed(self, relay: Mapping[str, Any]) -> None:
        """Remember that this message has been taken, by the id the browser gave
        it and by its place in the transcript — so the catch-up read below can
        never hand the same question to the agent twice."""
        client_id = relay.get("client_id")
        if isinstance(client_id, str) and client_id:
            self._consumed_clients[client_id] = None
            while len(self._consumed_clients) > relay_memory():
                self._consumed_clients.popitem(last=False)
        seq = relay.get("seq")
        if isinstance(seq, int) and seq > self._consumed_seq:
            self._consumed_seq = seq

    def _taken_before(self, relay: Mapping[str, Any]) -> bool:
        """Whether ``relay`` is a person's message this mirror already took.

        A message reaches the box by two paths — the live relay and the
        transcript read — and whichever lands second is the same message. Only
        a message is matched: an answer to an ask rides the same kind but is
        applied by its ask's id, which settles it once on its own.
        """
        if relay.get("kind") != PROMPT_KIND or relay.get("interrupt_id"):
            return False
        client_id = relay.get("client_id")
        return (
            isinstance(client_id, str) and bool(client_id) and client_id in self._consumed_clients
        )

    async def _handle_relay(self, relay: dict[str, Any]) -> None:
        kind = relay.get("kind")
        try:
            await self._ready.wait()
            if self._taken_before(relay):
                # The same message by its second path — the transcript read
                # took it and the live relay followed, or the reverse. It was
                # queued once; a second copy is a second turn for one question.
                logger.info(
                    "mirror %s: message %s already taken; not running it twice",
                    self._chat_id,
                    relay.get("client_id"),
                    extra={"chat_id": self._chat_id, "client_id": relay.get("client_id")},
                )
                return
            self._mark_consumed(relay)
            if kind == MODE_RELAY_KIND:
                # Read duck-typed, ahead of the union: the browser's side of the
                # mode switch lands separately, and a box that met the relay
                # before the variant existed would otherwise log it as unknown
                # and leave the reader's mode switch doing nothing.
                await self._on_mode(relay)
                return
            if kind == MODEL_RELAY_KIND:
                await self._on_model(relay)
                return
            if kind == STOP_RELAY_KIND:
                await self._on_stop()
                return
            # The body is read through the shared union rather than dispatched
            # on a bare string: the variant IS the routing decision, a field
            # the server types differently is refused here instead of deeper in
            # a handler, and a kind a newer server invents lands on the
            # catch-all as data to ignore. The handlers still take the body as
            # it arrived — the model decides WHICH one, never what a field
            # means.
            try:
                body = CHAT_RELAY_ADAPTER.validate_python(relay)
            except ValidationError as malformed:
                raise RelayRefusedError(
                    f"the body is not a {kind!r} relay: {malformed.error_count()} bad field(s)"
                ) from malformed
            if isinstance(body, (PromptRelay, AnswerRelay)):
                await self._on_prompt(relay)
            elif isinstance(body, RunQueryRelay):
                await self._on_run_query(relay)
            elif isinstance(body, PromoteRelay):
                await self._on_promote(relay)
            else:
                logger.info("mirror %s: ignoring relay kind %r", self._chat_id, kind)
        except RelayRefusedError as refused:
            self._ignored_relays += 1
            logger.warning(
                "mirror %s: %s relay ignored: %s",
                self._chat_id,
                kind,
                refused,
                extra={"chat_id": self._chat_id, "relay_kind": kind, "reason": str(refused)},
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("mirror %s: relay %r failed", self._chat_id, kind)

    def _refuses_the_write(self, request: PermissionRequest) -> bool:
        """Whether an ``allow`` for ``request`` must be ignored.

        Two independent reasons, and the second is not a mode:

        * the chat is in an analyst's mode (``read_only`` / ``plan``), where no
          write is approved at all — a relayed ``allow`` cannot walk around what
          the mode already decided;
        * the write would land outside the chat's own folder, which is refused
          whatever the mode. ``_resolve_permission`` already turned such an ask
          away before it was parked, so reaching this is a second line — and a
          second line is worth having on the one path a browser can drive.
        """
        if not _authorizes_a_write(request):
            return False
        if self._mode in NO_WRITE_MODES:
            return True
        return self.session_fence.judge_ask(request, writing=True).escaped

    async def _on_mode(self, relay: Mapping[str, Any]) -> None:
        """The reader moved this chat's permission mode.

        What the mode decides is whether a write inside the chat's own folder
        prompts or runs; it never decides where a write may land. A mode the box
        does not know is refused rather than guessed at — the alternative is a
        reader who believes they turned a boundary off and a box that read the
        word as something else.
        """
        wanted = relay.get("mode")
        if not isinstance(wanted, str) or wanted not in CLOUD_PERMISSION_MODES:
            raise RelayRefusedError(f"{wanted!r} is not a permission mode a cloud chat runs in")
        self.adopt_mode(cast(PermissionMode, wanted))

    def adopt_mode(self, mode: PermissionMode) -> None:
        """Run this chat in ``mode`` from the next permission ask and the next
        prompt on — the one way the stance moves on a live mirror.

        Two callers, one function: the reader's relay (how a box running the
        chat right now hears the switch) and the chat row on every read of it
        (how a box that missed the relay — its document socket down for the
        moment the reader flipped the chip — still follows the record). The
        harness session is what tells the model: its per-turn steering names
        the live mode, and the first prompt after a change carries the switch
        notice, exactly as the editor's daemon does it.
        """
        if mode == self._mode:
            return
        self._mode = mode
        if self._session is not None:
            self._session.set_permission_mode(mode)
        logger.info("mirror %s: the permission mode is now %s", self._chat_id, mode)

    async def _on_model(self, relay: Mapping[str, Any]) -> None:
        """The reader moved this chat onto a different model.

        The route resolved the pick against the catalog and wrote it on the
        chat row first, so the pin is the dict the mirror reads off the row, and
        it takes the SAME translation: a chat started on a model and one
        switched onto it run identically.

        A pin this box cannot translate, or one the chat may not move to (the
        box re-checks the server's verdict), is refused, never applied
        half-way, and remembered against the row. It runs from the NEXT turn.
        """
        pin = relay.get("pin")
        selection = pinned_model(dict(pin)) if isinstance(pin, Mapping) else None
        if selection is None:
            raise RelayRefusedError(f"{pin!r} is not a model this box can run a chat on")
        if (refusal := self._relay_refusal(relay)) is not None:
            raise RelayRefusedError(refusal)
        await self._adopt_selection(selection)

    async def adopt_model(self, pin: Any) -> None:
        """Follow the chat row's pin (a switch whose relay reached nobody)."""
        if (selection := pinned_model(pin)) is not None and self._follows_row(pin):
            await self._adopt_selection(selection)

    async def _on_stop(self) -> None:
        """The reader ended the turn this box is running.

        Nothing in the body is read: who pressed Stop is recorded by the server
        on the transcript, and a stop refused over an attribution this box could
        not parse would leave the turn running with no second control to press.

        A box that is not running this chat has no turn to end and does nothing
        — the same rule a mode switch follows, and the reason it matters is the
        inverse of the one above: a relay reaches every box subscribed to the
        chat, and acting on one for a session this box does not hold would
        abort whatever else it was doing.

        Stop also empties the lane, because ending a turn is what frees it:
        anything queued behind the turn would start the moment it ends, so a
        reader who pressed Stop watched the agent carry straight on into a
        message they sent long before and had no way to see was waiting. The
        queue is therefore taken out FIRST, before the cancel is even sent, and
        each message in it is reported cancelled so it reads as not sent rather
        than disappearing.

        "The lane" includes the message the lane is in the middle of preparing.
        A message that is past the queue is not yet the harness's: fetching its
        attachments and composing the turn both take awaits, and a turn a box
        was slow to start is exactly when a reader presses Stop. So the drain
        takes that one too, and the stop is stamped before anything is awaited
        — a prompt already on its way to the harness when the stamp moves has
        its turn cancelled the moment it is in flight, rather than running on
        past the stop with nothing left to press.
        """
        self._stops += 1
        if self._session is None:
            return
        dropped = self._drain_prompts()
        # Whether a turn was under way is read BEFORE the cancel: after it, an
        # adapter that settles synchronously has already stamped the chat idle
        # and the turn the reader ended would look like one that was not there.
        under_way = not self._idle.is_set()
        # Gagged BEFORE the cancel is awaited, not after. Cancelling is a round
        # trip to the agent, and the turn already in flight goes on publishing
        # across it: its `running`, its echo of the message, the shell of the
        # answer it was about to write — all of it landed on the chat while the
        # stop was still waiting for the adapter, and then the reader was shown
        # a message marked "not sent" with the start of an answer under it.
        if under_way:
            self._gag_stopped_turn()
        await self._session.cancel()
        if under_way:
            await self._close_stopped_turn()
        logger.info(
            "mirror %s: the running turn was stopped by a reader; %d queued message(s) dropped",
            self._chat_id,
            len(dropped),
            extra={"chat_id": self._chat_id, "dropped": len(dropped)},
        )
        for relay in dropped:
            await self._report_prompt_cancelled(relay)

    def _gag_stopped_turn(self) -> None:
        """Stop publishing for the turn the reader just ended — at once, with
        nothing awaited in between.

        Every await from here to the end of the stop is time the harness keeps
        the wire, so this is what has to happen first: the decision that this
        turn no longer speaks for the chat costs nothing and cannot be raced.
        """
        self._ending_turn = True
        self._ending_cancels = 0

    async def _close_stopped_turn(self) -> None:
        """End the turn the reader stopped, on the transcript, now.

        The harness is the only thing that ends a turn on its own, and a
        cancelled one ends whenever its agent loop gives up — after a tool
        result it now has to think about, after another model step. A reader
        who pressed Stop is owed the end of the turn, not the end of the
        winding down, so the mirror stamps it: the terminal goes on the
        transcript, the chat reads idle, and what the harness says for this
        turn afterwards is dropped unless it closes something (see
        :meth:`_quell`).

        Once per stopped turn, and the id says so rather than the barrier
        below: two paths reach this in the same window (the relay's own handler
        and the send that was in flight when the relay landed), and a box that
        was restarted mid-stop or a chat re-provisioned onto a second box
        reaches it with no memory of the first. That second kind is why the
        name of the turn is recovered from the transcript on catch-up: a
        process holds no memory, and a name only this process could spell would
        be a minted id under another word — a second terminal for one turn on
        every reader's transcript.

        Nothing names the turn after the CHAT. A per-chat constant would make
        every stop that fell through to it spell one id, and the record drops a
        row it already holds — so the second turn a reader stopped would never
        end on their screen. When neither the record nor the harness names the
        turn there is no id two boxes would agree on, and the turn is ended
        without a terminal rather than under an invented one: the chat still
        reads idle, which is what settles a replayed turn.

        It carries no ``detail``. A detail is a sentence a reader is shown
        beside the turn, and the sentence for this one is already on the
        transcript — the server writes "Stopped by <who>." when it records the
        stop, and only the server knows the name. A second sentence here would
        be the same fact twice, in worse words.
        """
        if self._stamped_stop:
            return
        self._stamped_stop = True
        self._gag_stopped_turn()
        named = self._turn_mark or self._current_turn_id
        if named is None:
            logger.info(
                "mirror %s: the stopped turn has no name on the record or in the harness; "
                "the chat is ended without a terminal rather than under an invented id",
                self._chat_id,
                extra={"chat_id": self._chat_id},
            )
            self._end_turn()
            return
        await self._publish_own(
            SessionStatusChanged(
                event_id=stopped_turn_terminal_id(named),
                time=datetime.now(UTC),
                session_id=self._chat_id,
                status="aborted",
                phase="idle",
                turn_id=self._current_turn_id,
            )
        )
        self._end_turn()

    def _drain_prompts(self) -> list[dict[str, Any]]:
        """Every message the harness has not been handed, taken out of the lane
        — the ones queued behind the running turn and the one being prepared.
        Synchronous on purpose: nothing may start between the reader asking for
        the turn to end and the lane being empty."""
        dropped: list[dict[str, Any]] = []
        if self._holding is not None:
            dropped.append(self._holding)
            self._holding = None
        while True:
            try:
                dropped.append(self._prompts.get_nowait())
            except asyncio.QueueEmpty:
                return dropped

    async def _report_prompt_cancelled(
        self, relay: Mapping[str, Any], *, reason: str = PROMPT_CANCELLED_STOPPED
    ) -> None:
        """Say that a message the lane was holding will never be run.

        The words are already on screen — the server makes a message durable
        the moment it takes it — so this names that message rather than
        repeating it, by the transcript id every reader of the chat already
        knows it under.

        ``reason`` is why, and it is the reader's only clue: a chat whose reply
        is missing looks the same whether a person ended it or the box could
        not carry it, and only the box can tell them apart.
        """
        client_id = relay.get("client_id")
        client_id = client_id if isinstance(client_id, str) else ""
        named = _prompt_mark(relay)
        if not named:
            return
        await self._publish_own(
            PromptCancelled(
                event_id=prompt_cancelled_event_id(named),
                time=datetime.now(UTC),
                session_id=self._chat_id,
                message_id=named,
                client_id=client_id,
                reason=reason,
            )
        )

    async def _admission_refusal(self, relay: Mapping[str, Any]) -> str | None:
        """Whether the member a relay names may still drive this chat
        (``cloud/turn_admission.py``); a relay naming nobody is asked about
        as the chat's owner."""
        return await turn_refusal(
            self._machine_rest,
            self._chat_id,
            relay,
            owner_user_id=self._owner_user_id,
            sleep=self._sleep,
        )

    async def _on_prompt(self, relay: dict[str, Any]) -> None:
        interrupt_id = relay.get("interrupt_id")
        if isinstance(interrupt_id, str) and interrupt_id:
            # An answer to an ask the running turn is BLOCKED on. It must never
            # queue behind that turn — the turn is waiting for it. Its author
            # must still be one who may drive the chat.
            if (refused := await self._admission_refusal(relay)) is not None:
                await self._note(refused)
                return
            self._answer_interrupt(interrupt_id, relay)
            return
        # A question waits its turn. Handing one to the harness over a live turn
        # SUPERSEDES it, so two typed a second apart answered only the second.
        self._prompts.put_nowait(relay)

    async def _prompt_lane(self) -> None:
        """Hand the chat's questions to the harness, one at a time, in order.

        The one it has taken is held where a stop can reach it — off the queue,
        it would otherwise be the single message a drain could not take back —
        and it is held for the WHOLE time it is not the harness's: waiting out
        the running turn, and then being prepared. Preparing takes awaits (the
        files the message names, the schema notice), so a message released at
        the start of that was one a Stop pressed during it could not stop.
        """
        while True:
            relay = await self._prompts.get()
            self._holding = relay
            try:
                await self._await_idle()
                if self._holding is not relay:
                    continue
                self._asking = True
                try:
                    await self._ask(relay)
                finally:
                    self._asking = False
            except asyncio.CancelledError:
                raise
            except Exception:
                # The reader is told the message failed (the trace is in the
                # box's log), and a turn it began ends here: the agent never got
                # it, so nothing else would, and the chat read "Working" with
                # its lane shut behind the turn until the box restarted.
                logger.exception("mirror %s: a queued question failed", self._chat_id)
                began = self._meter.running
                if self._holding is relay or began:
                    with contextlib.suppress(Exception):
                        await self._report_prompt_cancelled(relay, reason=PROMPT_CANCELLED_FAILED)
                if began:
                    self._end_turn()
            finally:
                if self._holding is relay:
                    self._holding = None

    async def _ask(self, relay: dict[str, Any]) -> None:
        text = relay.get("text")
        if not isinstance(text, str) or not text.strip() or self._session is None:
            return
        # Everything from here to the harness waits — the files the message
        # names, and the send itself — and a reader who
        # presses Stop during any of it ended the turn this is about to start.
        # So the stop count is taken now and read again at the two points it
        # can still be acted on: before the turn begins, and once the send has
        # begun one that arrived too late to prevent.
        stops = self._stops
        # A turn never starts on a gateway token about to lapse under it.
        await self._refresh_gateway_token()
        if self._session is None:
            return
        # Nor for an author who lost the right to drive the chat since the
        # message was accepted: the server is asked again, by its send rule.
        if (refused := await self._admission_refusal(relay)) is not None:
            await self._note(refused)
            return
        # The files the question names are fetched BEFORE the turn: the harness
        # is handed absolute paths, so it reads them with its ordinary read
        # tool. This never refuses the question — a node the box could not read
        # comes back as a notice, and the turn runs with what did arrive.
        materialized = MaterializedAttachments()
        if self._prepare_attachments is not None:
            materialized = await self._prepare_attachments(self._chat_id, relay)
        for notice in materialized.notices:
            await self._note(notice)
        # A stop that arrived while the message was being prepared: it is not
        # handed over. The drain has already taken it out of the lane and said
        # it was never sent, so nothing more is owed here.
        if self._stops != stops:
            return
        async with self._pin_lock:
            # Nor on an agent spawned without the model the chat is pinned to:
            # checked after every wait above (a switch the row or a relay
            # brought in meanwhile is on the pin by now), and held until the
            # turn is handed over, so no repin lands between the two.
            await self._respawn_for_pinned_model()
            if self._session is None or self._stops != stops:
                return
            if not await self._start_turn(relay, text, materialized, stops):
                return
        if self._stops != stops:
            # Sending is itself a wait — the system prompt and the tool
            # registry are composed inside it — and a stop that arrived during
            # it found no turn to cancel, because this one had not started yet.
            # It has now, so it is cancelled here: the reader pressed Stop, and
            # a turn that outlives the stop that ended it leaves them nothing
            # left to press.
            logger.info(
                "mirror %s: the turn was stopped while it was starting; cancelling it",
                self._chat_id,
                extra={"chat_id": self._chat_id},
            )
            self._gag_stopped_turn()
            await self._session.cancel()
            # Same as a stop that landed a moment later: the turn is over, and
            # what the harness makes of the cancel is not this chat's record.
            await self._close_stopped_turn()

    async def _start_turn(
        self,
        relay: dict[str, Any],
        text: str,
        materialized: MaterializedAttachments,
        stops: int,
    ) -> bool:
        """Begin the turn ``relay`` asked for and hand its message to the
        harness. The caller holds the pin lock. ``False`` when no agent took
        it (the one it drove died and none came back, or a stop arrived)."""
        # Past recall from the lane's point of view: a later stop ends the turn
        # rather than un-sending the message that started it. This is also the
        # one place the drop on a stopped turn is lifted: a NEW message is a
        # new turn, and nothing the harness says about the old one — not its
        # terminal, not another ``running`` — may speak for this chat again.
        if self._holding is relay:
            self._holding = None
        self._ending_turn = False
        self._ending_cancels = 0
        self._stamped_stop = False
        # And the cards the last turn drew stop being this turn's: a call named
        # from here on belongs to the message about to be sent.
        self._live_calls.clear()
        # The turn runs for the member who sent the prompt — the relay names
        # them — never for the operator whose token the box holds.
        self._attribution.begin(relaying_user_of(relay))
        # Named after the message that started it, by the transcript id every
        # reader already holds it under — the one name for this turn a box that
        # comes back to the chat can read off the record.
        self._begin_turn(None, mark=_prompt_mark(relay))
        # What the model reads before the words without it being the words:
        # the chat template's brief, which rides on the FIRST question and
        # never again (it is what this chat was started from, and repeating it
        # on every turn would spend the context window re-stating text the
        # agent already has and the user never wrote), then whatever context
        # the server recorded on this message (a
        # Slack thread's briefing and history, who is speaking). Both travel on
        # the hidden per-turn channel, so the harness's echo of the prompt —
        # the person's bubble in every reader — is exactly their words.
        brief, self._source_brief = self._source_brief, None
        recorded = relay.get("context")
        # And what the agent's last reply got wrong that no reader can fix: the
        # files it named that are not in the chat.
        missing = self._missing_files_context()
        # The model and effort ride the turn from the manifest (turn_model).
        turn = hidden_turn(
            self._machine_note.unsaid(),
            brief,
            recorded if isinstance(recorded, str) else None,
            missing,
        )
        prompt = prompt_with_attachments(text, materialized)
        mark = _prompt_mark(relay)
        restart = mark in self._restart_marks
        self._restart_marks.discard(mark)
        try:
            await self._hand_over(text, prompt, restart=restart, **turn)
        except HarnessGoneError:
            # The agent this session drove is gone — killed for memory, exited,
            # stopped answering — and the crash that took it already ended its
            # turn on the record. The message in hand is a NEW turn and gets a
            # fresh agent: the session is opened again on a fresh gateway token
            # (the same conversation resumes from the manifest's pin) and the
            # message is handed to it. Without this every later message fell
            # into the dead session and was never answered.
            logger.warning(
                "mirror %s: the agent is gone; starting a fresh one for the next message",
                self._chat_id,
                extra={"chat_id": self._chat_id},
            )
            await self._revive()
            if self._session is None or self._stops != stops:
                return False
            await self._hand_over(text, prompt, restart=restart, **turn)
        return True

    async def _hand_over(self, text: str, prompt: str, *, restart: bool, **turn: Any) -> None:
        """Hand one message to the harness: as the restart of an interrupted
        turn when the catch-up marked it so, else as a new turn."""
        assert self._session is not None
        if restart:
            await self._session.restart_interrupted_turn(text, prompt=prompt, **turn)
        else:
            await self._session.send_prompt(prompt, **turn)

    async def _revive(self) -> None:
        """Open this chat's session again after its agent died, on a fresh
        gateway token where one can be minted and on the one the dead agent
        held where the mint merely did not arrive. A mint the backend refuses
        is final: the chat is not revived on a credential that would be
        refused too."""
        assert self._broker is not None
        try:
            token = await self._mint_gateway_token()
        except CloudApiError as exc:
            if exc.status in (401, 403, 404) or not self._gateway_token:
                raise
            token = self._gateway_token
        except (httpx.HTTPError, OSError):
            if not self._gateway_token:
                raise
            token = self._gateway_token
        await self._reopen_on(token)

    def _note_ask_published(self, event: Event) -> bool:
        """Remember which asks the cloud has been shown, so a relay can be told
        apart: one for an ask on the wire but not yet parked is held; one for an
        id nothing published is the stray the relay guard counts. A resolution
        the harness publishes closes the ask for both.

        Returns whether ``event`` may go on the wire at all. A resolved ask is
        closed on the server: a request for its id published after the
        resolution — a re-announce, a re-raise with its subject, a replay —
        draws a card the server refuses every answer to, and the chat is stuck
        on it. So no request goes out for an id a resolution has closed, and a
        resolution goes out once.
        """
        if isinstance(event, PermissionRequest | QuestionRequest):
            if event.request_id in self._resolved_asks:
                logger.info(
                    "mirror %s: ask %s not published: already resolved",
                    self._chat_id,
                    event.request_id,
                )
                return False
            self._published_asks[event.request_id] = event
            while len(self._published_asks) > relay_memory():
                self._published_asks.popitem(last=False)
            return True
        if isinstance(event, PermissionResolved | QuestionAnswered | QuestionRejected):
            self._published_asks.pop(event.request_id, None)
            self._early_answers.pop(event.request_id, None)
            if event.request_id in self._resolved_asks:
                return False
            self._remember_resolved([event.request_id])
        return True

    def _remember_resolved(self, request_ids: Iterable[str]) -> None:
        for request_id in request_ids:
            self._resolved_asks[request_id] = None
        while len(self._resolved_asks) > relay_memory():
            self._resolved_asks.popitem(last=False)

    def _permission_answer(
        self, request: PermissionRequest, interrupt_id: str, relay: dict[str, Any]
    ) -> str | None:
        """The option a relay gives (an "Always" not the owner's binds this ask
        only), or None once refused and counted. Run as the relay lands."""
        option = relay.get("option_id")
        if not isinstance(option, str):
            option = relay.get("text")
        if not isinstance(option, str) or option not in _PERMISSION_OPTIONS:
            logger.info("mirror %s: bad permission answer %r", self._chat_id, option)
            return None
        offered = {choice.option_id for choice in request.options}
        if option not in offered:
            self._ignored_relays += 1
            logger.warning(
                "mirror %s: answer %r for %s ignored: the ask offered %s",
                self._chat_id,
                option,
                interrupt_id,
                sorted(offered) or "nothing",
            )
            return None
        option = as_answered_by(option, answered_by=relay.get("user_id"), owner=self._owner_user_id)
        if option.startswith("allow") and self._refuses_the_write(request):
            self._ignored_relays += 1
            logger.warning(
                "mirror %s: answer %r for %s ignored: a cloud session never approves a %s ask",
                self._chat_id,
                option,
                interrupt_id,
                request.canonical_kind,
            )
            return None
        return option

    def _answer_interrupt(self, interrupt_id: str, relay: dict[str, Any]) -> None:
        interrupt = self._interrupts.get(interrupt_id)
        if interrupt is None:
            if interrupt_id in self._settled_asks:
                # The same answer twice — the relay after the record it made,
                # or the reverse. One of them settled the ask; the other is
                # not a stray, and it is applied by nobody.
                logger.info("mirror %s: answer for %s already applied", self._chat_id, interrupt_id)
                return
            published = self._published_asks.get(interrupt_id)
            if published is None:
                self._ignored_relays += 1
                logger.warning(
                    "mirror %s: answer for %s ignored: no such ask is outstanding",
                    self._chat_id,
                    interrupt_id,
                )
                return
            if (
                isinstance(published, PermissionRequest)
                and self._permission_answer(published, interrupt_id, relay) is None
            ):
                return
            self._early_answers[interrupt_id] = relay
            while len(self._early_answers) > relay_memory():
                self._early_answers.popitem(last=False)
            logger.info(
                "mirror %s: answer for %s held until the ask is parked",
                self._chat_id,
                interrupt_id,
            )
            return
        if interrupt.kind == "permission":
            request = interrupt.request
            assert isinstance(request, PermissionRequest)
            option = self._permission_answer(request, interrupt_id, relay)
            if option is not None:
                self._settle_interrupt(interrupt, value=option)
            return
        if relay.get("reject") is True:
            reason = relay.get("reason")
            self._settle_interrupt(
                interrupt, value=("reject", reason if isinstance(reason, str) else None)
            )
            return
        answers_raw = relay.get("answers")
        answers: list[list[str]] = []
        if isinstance(answers_raw, list):
            for inner in answers_raw:
                if isinstance(inner, list):
                    answers.append([str(x) for x in inner if isinstance(x, str)])
                elif isinstance(inner, str):
                    answers.append([inner])
        elif isinstance(relay.get("text"), str):
            answers = [[str(relay["text"])]]
        if not answers:
            logger.info("mirror %s: empty question answer", self._chat_id)
            return
        request = interrupt.request
        note = relay.get("note")
        if isinstance(request, QuestionRequest) and request.kind == "plan_approval":
            answers = plan_answers_with_note(answers, note if isinstance(note, str) else None)
        self._settle_interrupt(interrupt, value=("answer", answers))

    def _apply_early_answer(self, request_id: str) -> None:
        """Settle a just-parked ask with the answer a reader gave before it was parked."""
        relay = self._early_answers.pop(request_id, None)
        if relay is not None:
            self._answer_interrupt(request_id, relay)

    def _settle_interrupt(
        self, interrupt: _Interrupt, *, value: Any = None, cancelled: bool = False
    ) -> None:
        if interrupt.future.done():
            return
        if cancelled:
            value = "cancelled" if interrupt.kind == "permission" else ("reject", "cancelled")
        else:
            self._settled_asks[interrupt.request.request_id] = None
            while len(self._settled_asks) > relay_memory():
                self._settled_asks.popitem(last=False)
        interrupt.future.set_result(value)

    def _fence_reason(self, request: PermissionRequest, escape: str) -> str:
        """The sentence the model gets for a location this chat may not touch.

        Which boundary it is told about follows the operation, not the fence
        that caught it: a cloud chat reads the workspace and writes only its
        own folder, so a refused WRITE that happens to have escaped the read
        fence first must still be pointed at the sandbox — telling it to write
        inside the workspace would send it straight into the next refusal.
        """
        writing = self.session_fence.refuses_a_write(request, escape, _authorizes_a_write(request))
        if not escape:
            # No location to quote: the fence could not read the command at all,
            # and a person turned it down.
            subject = request.subject if isinstance(request.subject, Mapping) else {}
            raw = subject.get("raw")
            return self.session_fence.explain(
                fence.FenceVerdict("unknown", raw if isinstance(raw, str) else "", writing)
            )
        # The one directory the model can type, spelled the way it sees it.
        return fence_reason(
            writing=writing, target=escape, boundary=self.session_fence.spell(self.working_dir)
        )

    async def _resolve_permission(self, request: PermissionRequest) -> PermissionOptionId:
        # What the session may read is the workspace: an ask naming a location
        # outside it is refused here, in plain language, never parked on a reader.
        # Either refusal is the FENCE's decision, not a reader's: it is raised as
        # one, so the session records it as policy, the transcript's
        # ``permission.resolved`` does not say a person chose, and the model is
        # handed the sentence — which keeps the turn going, where a reader's
        # bare reject would end it.
        # And what it may WRITE is this chat's own folder — in every mode. The
        # mode decides whether a write inside it prompts; it never widens where
        # a write may land, so a reader who turns prompting off has not turned
        # the other chats on this box into writable directories. One judge
        # answers both bounds, the same one the harness chokepoint and the
        # in-tool shell gate ask; an ask it cannot vouch for is the reader's.
        writing = _authorizes_a_write(request)
        verdict = self.session_fence.judge_ask(request, writing=writing)
        if verdict.escaped:
            target = verdict.target or ""
            # The reader is told which bound caught it; the model is told which
            # operation was refused and where it may go instead.
            if verdict.bound == "write":
                await self._say_refusal(RefusalNote("outside_chat_folder", quote_statement(target)))
            else:
                await self._say_refusal(RefusalNote("outside_workspace", quote_statement(target)))
            raise ResolverRefusedError(self._fence_reason(request, target), decided_by="fence")
        granted = self._standing_approvals.pop(_ask_key(request), None)
        if granted is not None:
            # The person already allowed exactly this action, on the ask the
            # harness was holding when the chat slept or the box restarted; the
            # continuation made the agent raise it again. Asking twice for one
            # allow would be the orphan the reader just resolved, re-orphaned.
            logger.info(
                "mirror %s: %s granted from the reader's earlier answer",
                self._chat_id,
                request.request_id,
            )
            return granted
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._interrupts[request.request_id] = _Interrupt("permission", future, request)
        self._announce_ask(request)
        self._apply_early_answer(request.request_id)
        self._meter.hold()
        try:
            return cast(PermissionOptionId, await future)
        finally:
            self._meter.release()
            self._interrupts.pop(request.request_id, None)

    def _announce_ask(self, request: PermissionRequest) -> None:
        """Tell the readers this ask is theirs to answer.

        The harness appends every ``permission.request`` BEFORE its policy has
        run, so from that entry alone a browser cannot tell an ask a person is
        being asked from one the policy is about to answer — it renders the
        decision controls only for an ask tagged ``prompting``, exactly as the
        editor does once the daemon's broker reaches it. On the box the mirror
        IS that broker's human channel, so the tag goes on the wire here, the
        moment the ask is parked on a reader and only then: an ask the fence
        turned away above was never anyone's to answer and earns no tag. Left
        untagged, a ``default``-mode write would show its diff with no Allow.

        The copy is put on the outbound lane directly, never on the session's
        event bus: a second ``PermissionRequest`` on the bus would reach the
        policy watcher again and raise the same ask twice. Its own event id
        keeps the server's dedupe from dropping it, and the browser folds it
        onto the part the harness's entry created — or creates the part from
        it, should it land first.
        """
        announced = PermissionRequest.model_validate(
            {
                **request.model_dump(mode="json"),
                "event_id": f"{request.event_id}-prompting",
                "prompting": True,
            }
        )
        if not self._note_ask_published(announced):
            return
        self._outbound.put_nowait(
            append_entry(announced, self._roles, stopping=self._state == "stopped")
        )

    async def _resolve_question(self, request: QuestionRequest) -> QuestionResolution:
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._interrupts[request.request_id] = _Interrupt("question", future, request)
        self._apply_early_answer(request.request_id)
        self._meter.hold()
        try:
            return cast(QuestionResolution, await future)
        finally:
            self._meter.release()
            self._interrupts.pop(request.request_id, None)

    # -- an ask the transcript holds, re-offered ------------------------------------

    def _rearm_asks(self, read: _CatchUpRead) -> list[str]:
        """Re-offer every ask the walk left open — the ones nobody has answered.

        A pending ask is state of the CHAT, not of the process that raised it:
        the harness appended its ``permission.request`` / ``question.request``
        to the transcript, and its resolution — when there is one — sits there
        too. A mirror that opens the chat after a sleep, a restart or a move
        reads both back and parks the unanswered ones again, under the same
        ids, so the card a reader sees on a cold open is answerable and the
        answer they give resolves it. An ask the harness raised in THIS
        process is already parked and is left alone, as is one this mirror
        settled a moment ago whose echo has not landed yet. Returns the ids
        re-armed.

        A resolution in the transcript comes in two kinds, told apart by the
        row's role. One the machine published (its own role) means a harness
        already acted on it — the ask is done. One a READER recorded through
        the server (``RECORDED_ANSWER_ROLE``) is an answer given while no box
        was holding the ask — the chat asleep, the box gone, the relay lost —
        and nobody has acted on it yet: that answer WINS over re-offering the
        ask. It is applied here as the relay would have been (the same checks,
        the same continuation or end of turn), and the machine's own echo of
        the resolution is what marks it done for every later open. An ask
        this box is holding whose answer arrives as a row rather than a relay
        is settled the same way.

        The walk has already folded the transcript down to what is open (see
        :func:`_fold_ask`): the asks nobody answered, the answers a reader
        recorded, and — by tool call id — the calls a permission ask may name
        (a call's name, the input from the last frame that carried one, and the
        provider ids that alias onto the transcript's key).
        """
        asks = read.asks
        announced = read.announced
        recorded = read.recorded
        calls = read.calls
        aliases = read.aliases
        rearmed: list[str] = []
        applied: list[str] = []
        for request_id in [*asks, *(rid for rid in recorded if rid not in asks)]:
            if request_id in self._settled_asks:
                continue
            decision = recorded.get(request_id)
            if request_id in self._interrupts:
                if decision is not None:
                    self._answer_interrupt(request_id, _relay_of_recorded(decision))
                    applied.append(request_id)
                continue
            if request_id in self._live_asks:
                # This process's harness raised it and has not parked it: its
                # policy answered it, or is about to answer or park it. A read
                # that saw the request before the resolution is no reason to
                # hand it to a reader. A reader's recorded answer is held for
                # the park, exactly as the same answer relayed would be.
                if decision is not None and request_id not in self._resolved_asks:
                    self._answer_interrupt(request_id, _relay_of_recorded(decision))
                continue
            entry = asks.get(request_id)
            if entry is None:
                continue
            kind, event = entry
            request: PermissionRequest | QuestionRequest
            try:
                if kind == "permission":
                    request = PermissionRequest.model_validate(
                        {k: v for k, v in event.items() if k != "prompting"}
                    )
                else:
                    request = QuestionRequest.model_validate(event)
            except ValidationError as malformed:
                logger.warning(
                    "mirror %s: ask %s in the transcript could not be read back (%d bad field(s))",
                    self._chat_id,
                    request_id,
                    malformed.error_count(),
                )
                continue
            future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            tool, tool_refusal = self._tool_call_of(request, calls, aliases)
            interrupt = _Interrupt(
                kind,
                future,
                request,
                restored=True,
                tool=tool,
                tool_refusal=tool_refusal,
            )
            self._interrupts[request_id] = interrupt
            if decision is None:
                if isinstance(request, PermissionRequest) and request_id not in announced:
                    self._announce_ask(request)
                self._spawn(self._await_restored(interrupt), ask_waiter=True)
                rearmed.append(request_id)
                continue
            self._answer_interrupt(request_id, _relay_of_recorded(decision))
            if future.done():
                self._spawn(self._await_restored(interrupt), ask_waiter=True)
            else:
                # The record names an answer this box may not apply (an allow
                # for a write the chat's mode refuses, an option it cannot
                # read): the ask is not left parked on a card nobody sees.
                self._interrupts.pop(request_id, None)
                self._spawn(self._refuse_recorded(interrupt))
            applied.append(request_id)
        if rearmed or applied:
            # The turn this box came up in the middle of is parked on a reader,
            # or continued here from their answer: not lost, and not said to be.
            self._lost_turn_unexplained = False
        if rearmed:
            logger.info(
                "mirror %s: %d ask(s) in the transcript still wait on a reader; re-offered",
                self._chat_id,
                len(rearmed),
                extra={"chat_id": self._chat_id, "asks": rearmed},
            )
        if applied:
            logger.info(
                "mirror %s: %d ask(s) answered while no box held them; acting on the record",
                self._chat_id,
                len(applied),
                extra={"chat_id": self._chat_id, "asks": applied},
            )
        return rearmed

    def _spawn(self, coro: Coroutine[Any, Any, None], *, ask_waiter: bool = False) -> None:
        """Run ``coro`` as work this chat owes. ``ask_waiter`` marks one that
        does nothing but wait for an ask to be answered — work that goes cold
        with the ask it is waiting on rather than keeping the chat awake."""
        task = asyncio.get_running_loop().create_task(coro)
        self._relay_tasks.add(task)
        task.add_done_callback(self._relay_tasks.discard)
        if ask_waiter:
            self._ask_waiters.add(task)
            task.add_done_callback(self._ask_waiters.discard)

    async def _refuse_recorded(self, interrupt: _Interrupt) -> None:
        """Close a recorded answer this box may not apply as the policy's
        decision — on the transcript, so no later open tries it again — and
        give the composer back: nothing is running, and the person's allow
        did not become a write the chat's mode forbids."""
        request_id = interrupt.request.request_id
        self._settled_asks[request_id] = None
        while len(self._settled_asks) > relay_memory():
            self._settled_asks.popitem(last=False)
        now = datetime.now(UTC)
        if interrupt.kind == "permission":
            await self._publish_own(
                PermissionResolved(
                    event_id=f"restored-{request_id}-refused",
                    time=now,
                    session_id=self._chat_id,
                    request_id=request_id,
                    option_id="reject_once",
                    decided_by="policy",
                )
            )
        else:
            await self._publish_own(
                QuestionRejected(
                    event_id=f"restored-{request_id}-refused",
                    time=now,
                    session_id=self._chat_id,
                    request_id=request_id,
                    reason="the recorded answer could not be applied on this machine",
                )
            )
        self._settle_restored_turn()

    async def _await_restored(self, interrupt: _Interrupt) -> None:
        """Act on the answer to an ask nothing in this process was holding.

        The decision is put on the transcript first, so the card retires on
        every reader's screen and no later open re-offers it. An allow then
        continues the turn on the restored harness session: the agent is told
        what was allowed and to carry on, and the identical ask it raises to do
        so is granted from the answer already given. A reject ends the turn
        where it stood — nothing is running, so ending it is saying so.
        """
        request_id = interrupt.request.request_id
        # What every row this ask leaves behind is named from, the turn it
        # restarts included.
        restored = f"restored-{request_id}"
        try:
            value = await interrupt.future
        finally:
            if self._interrupts.get(request_id) is interrupt:
                self._interrupts.pop(request_id, None)
        now = datetime.now(UTC)
        if interrupt.kind == "permission":
            request = interrupt.request
            assert isinstance(request, PermissionRequest)
            option = cast(PermissionOptionId, value)
            await self._publish_own(
                PermissionResolved(
                    event_id=f"restored-{request_id}-resolved",
                    time=now,
                    session_id=self._chat_id,
                    request_id=request_id,
                    option_id=option,
                    decided_by="user",
                )
            )
            if option.startswith("allow"):
                self._standing_approvals[_ask_key(request)] = option
                # The call the ask gated is performed HERE, under the reader's
                # decision, and its real result closes the original call before
                # the agent is told anything — so what it narrates is what
                # happened, not what it hoped.
                outcome = await self._replay_gated_call(interrupt, request)
                await self._continue_after(
                    continuation_after_allow(request, outcome), mark=restored
                )
            else:
                await self._close_gated_call(interrupt, request, error=DENIED_BY_USER)
                self._settle_restored_turn()
            return
        question = interrupt.request
        assert isinstance(question, QuestionRequest)
        verdict, detail = cast(tuple[str, Any], value)
        if verdict == "answer":
            answers = cast(list[list[str]], detail)
            # A plan's note rides its answer to the harness; on the record and
            # in what the agent is told it stands on its own.
            note = plan_note_of(answers) if question.kind == "plan_approval" else None
            if note is not None:
                answers = [answers[0][:1], *answers[1:]]
            await self._publish_own(
                QuestionAnswered(
                    event_id=f"restored-{request_id}-answered",
                    time=now,
                    session_id=self._chat_id,
                    request_id=request_id,
                    answers=answers,
                    decided_by="user",
                    note=note,
                )
            )
            await self._continue_after(
                with_plan_note(continuation_after_answer(question, answers), note),
                mark=restored,
            )
            return
        await self._publish_own(
            QuestionRejected(
                event_id=f"restored-{request_id}-rejected",
                time=now,
                session_id=self._chat_id,
                request_id=request_id,
                reason=detail if isinstance(detail, str) else None,
            )
        )
        self._settle_restored_turn()

    def _tool_call_of(
        self,
        request: PermissionRequest | QuestionRequest,
        calls: Mapping[str, tuple[str, dict[str, Any]]],
        aliases: Mapping[str, str],
    ) -> tuple[tuple[str, str, dict[str, Any]] | None, str | None]:
        """The call a restored permission ask gated — the transcript's key for
        it, its tool name and its input — from the transcript rows read back,
        or from this box's own copy of the chat when the rows read did not
        reach back to the call; and, when the call could not be taken from
        that copy, the reason the reader is owed.

        The ask is matched under whichever id it carries: the transcript's key
        (``tool_call_id``), the provider's (``provider_call_id``), or a key the
        rows alias to either — a harness may raise the ask under the provider's
        id before its own part for the call was ever seen.

        The box's own copy is read only once it has passed the check against
        the digest pinned beside it. What is rebuilt from it is a tool call
        this box then RUNS, and the copy came down the drive, where a person
        with edit on a shared chat once could rewrite it: a transcript that no
        longer starts with the bytes its digest pinned is refused as the
        source of anything this box does, and the ask closes as not run with
        that reason rather than with an input somebody else wrote.
        """
        if not isinstance(request, PermissionRequest):
            return None, None
        wanted = [
            cid for cid in dict.fromkeys((request.tool_call_id, request.provider_call_id)) if cid
        ]
        if not wanted:
            return None, None
        call_id: str | None = None
        for candidate in wanted:
            if candidate in calls:
                call_id = candidate
                break
            aliased = aliases.get(candidate)
            if aliased is not None and aliased in calls:
                call_id = aliased
                break
        found = calls.get(call_id) if call_id is not None else None
        if found is not None and found[0] and call_id is not None:
            return (call_id, found[0], found[1]), None
        name = found[0] if found is not None else ""
        tool_input = dict(found[1]) if found is not None else {}
        if self._session is not None:
            if verify_trace_file(self.chat_folder, TRANSCRIPT_FILENAME) is TraceVerdict.TAMPERED:
                logger.error(
                    "mirror %s: transcript integrity check failed — %s on this box does not "
                    "start with the bytes its pinned digest covers; call %s is not rebuilt from it",
                    self._chat_id,
                    TRANSCRIPT_FILENAME,
                    wanted[0],
                )
                return None, TRANSCRIPT_MISMATCH
            try:
                for event in self._session.events():
                    if isinstance(event, ToolCall) and (
                        event.tool_call_id == call_id
                        or event.tool_call_id in wanted
                        or event.provider_call_id in wanted
                    ):
                        call_id = event.tool_call_id
                        name = event.tool_name or name
                        if event.input:
                            tool_input = dict(event.input)
                    elif (
                        isinstance(event, ToolCallUpdate)
                        and (event.tool_call_id == call_id or event.tool_call_id in wanted)
                        and isinstance(event.input, dict)
                        and event.input
                    ):
                        call_id = event.tool_call_id
                        tool_input = dict(event.input)
            except Exception:
                logger.debug(
                    "mirror %s: could not read the chat back for call %s",
                    self._chat_id,
                    wanted[0],
                    exc_info=True,
                )
        return ((call_id, name, tool_input) if name and call_id is not None else None), None

    async def _replay_gated_call(
        self, interrupt: _Interrupt, request: PermissionRequest
    ) -> ReplayResult | None:
        """Perform the call an allowed, restored ask gated, and close the
        original call with what happened.

        A file write or edit is re-run from the input the transcript holds,
        inside this chat's folder, and the ORIGINAL call closes with the real
        result — completed, or failed with the reason. A call the workspace
        cannot re-run from its input (a shell command, a fetch, a subagent; a
        call the transcript no longer has) closes as not run, so the card
        never shows a result nobody produced, and the agent is told to retry
        it. ``None`` means the agent has to do it.
        """
        call_id = _gated_call_id(interrupt, request)
        if not call_id:
            return None
        outcome: ReplayResult | None = None
        tool_input: dict[str, Any] | None = None
        if interrupt.tool is not None:
            _, name, tool_input = interrupt.tool
            outcome = replay_file_tool(
                name, tool_input, folder=self.working_dir, base=self.working_dir
            )
        if outcome is None:
            await self._close_gated_call(
                interrupt, request, error=interrupt.tool_refusal or NOT_RUN_BEFORE_RESTART
            )
            return None
        self._replayed_calls[call_id] = None
        while len(self._replayed_calls) > relay_memory():
            self._replayed_calls.popitem(last=False)
        logger.info(
            "mirror %s: re-ran the %s call %s the reader allowed: %s",
            self._chat_id,
            outcome.tool,
            call_id,
            outcome.summary,
        )
        await self._publish_own(
            ToolCallUpdate(
                event_id=f"restored-{request.request_id}-call",
                time=datetime.now(UTC),
                session_id=self._chat_id,
                tool_call_id=call_id,
                status="completed" if outcome.ok else "error",
                input=tool_input,
                output=outcome.output,
                error_text=None if outcome.ok else outcome.error,
            )
        )
        return outcome

    async def _close_gated_call(
        self, interrupt: _Interrupt, request: PermissionRequest, *, error: str
    ) -> None:
        """Close the call a restored ask gated as not done, with the reason —
        the reader refused it, or nothing could run it."""
        call_id = _gated_call_id(interrupt, request)
        if not call_id:
            return
        tool_input = interrupt.tool[2] if interrupt.tool is not None else None
        await self._publish_own(
            ToolCallUpdate(
                event_id=f"restored-{request.request_id}-call",
                time=datetime.now(UTC),
                session_id=self._chat_id,
                tool_call_id=call_id,
                status="error",
                input=tool_input,
                output={"error": error},
                error_text=error,
            )
        )

    def _settle_restored_turn(self) -> None:
        """A rejected restored ask ends the turn it belonged to — which no
        process is running any more — so the composer is the reader's again."""
        if self._meter.running:
            self._end_turn()
        else:
            self._queue_meta({"state": "idle"})

    async def _continue_after(self, continuation: str, *, mark: str) -> None:
        """Continue a turn the harness was no longer running: the answer goes
        to the agent as a steering part on the restored session (the
        synthetic-part convention, hidden from readers), and the turn runs as
        any prompt does — metered and attributed.

        ``mark`` names it after the ask that restarted it, because no message
        of the reader's did: what this turn is on the record is "what came of
        answering that ask", and a row about it has to spell the same id
        wherever it is written.
        """
        if self._session is None:
            return
        self._begin_turn(None, mark=mark)
        await self._session.send_prompt(CONTINUE_TEXT, system_addendum=continuation)

    # -- relay checks ------------------------------------------------------------------

    async def _object_for(self, relay: dict[str, Any], *, kind: str) -> tuple[str, dict[str, Any]]:
        """The object a relay names, read back through REST as this chat's
        agent, once its id is a UUID and the record is bound to this chat and
        (when it says) owned by the chat's owner."""
        object_id = _uuid_field(relay, "object_id")
        try:
            obj = await self._rest.get_object(object_id)
        except CloudApiError as exc:
            raise RelayRefusedError(f"object {object_id} could not be read ({exc.status})") from exc
        if obj.get("type") != kind:
            raise RelayRefusedError(f"object {object_id} is not a {kind}")
        spec = obj.get("spec")
        spec = spec if isinstance(spec, dict) else {}
        bound_chat = _first_str(spec, *CHAT_BINDING_KEYS) or _first_str(obj, *CHAT_BINDING_KEYS)
        if bound_chat is None:
            raise RelayRefusedError(f"object {object_id} names no chat")
        if bound_chat != self._chat_id:
            raise RelayRefusedError(f"object {object_id} belongs to another chat")
        owner = _first_str(obj, "owner_user_id") or _first_str(spec, "owner_user_id")
        if owner is not None:
            if not self._owner_user_id:
                raise RelayRefusedError(
                    f"object {object_id} has an owner and this mirror does not know the chat's"
                )
            if owner != self._owner_user_id:
                raise RelayRefusedError(f"object {object_id} is not owned by the chat's owner")
        return object_id, obj

    # -- run_query -----------------------------------------------------------------------

    async def _on_run_query(self, relay: dict[str, Any]) -> None:
        run_id = _uuid_field(relay, "run_id")
        params = relay.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict) or not all(isinstance(k, str) for k in params):
            raise RelayRefusedError("params is not an object")
        object_id, obj = await self._object_for(relay, kind="query")
        if self._session is None:
            return
        if (refused := await self._admission_refusal(relay)) is not None:
            await self._note(refused)
            return
        started = datetime.now(UTC)
        call_id = f"run-{run_id}"
        # A re-run is asked for by whoever the relay names (the route stamps
        # its caller); its result is theirs on the receipt. Both keys, because
        # a promote names the tool CALL id (the one identity of a result across
        # that seam) while the published part carries the `-result` id — and a
        # key the promote cannot find falls back to the box's own account, so
        # the member's re-run would come back attributed to the operator.
        member = relaying_user_of(relay)
        self._attribution.attribute(call_id, member)
        self._attribution.attribute(f"{call_id}-result", member)
        tool_input: dict[str, Any] = {"run_id": run_id, "object_id": object_id, "params": params}
        tool_args: dict[str, Any]
        try:
            raw_spec = obj.get("spec")
            spec: dict[str, Any] = dict(raw_spec) if isinstance(raw_spec, dict) else {}
            template = _first_str(spec, "sql_template", "sql", "template", "query")
            connection = _first_str(spec, "connection", "connection_name", "connection_id")
            engine = _first_str(spec, "engine")
            if template is None or connection is None:
                raise QueryCompileError("the query object names no sql template or connection")
            if engine is None:
                raise QueryCompileError("the query object names no engine")
            declared = spec.get("params")
            bound = compile_query(
                template,
                params,
                engine,
                declarations=[d for d in declared if isinstance(d, dict)]
                if isinstance(declared, list)
                else (),
            )
            limit = spec.get("limit")
            tool_args = {
                "connection": connection,
                "sql": bound.sql,
                "params": bound.params,
                "limit": limit if isinstance(limit, int) and limit > 0 else rerun_limit(),
                "result_name": str(obj.get("title") or "")[:200],
            }
            tool_input.update(
                {
                    "connection": connection,
                    "sql": bound.sql,
                    "engine": engine,
                    "limit": tool_args["limit"],
                    "result_name": tool_args["result_name"],
                }
            )
        except (QueryCompileError, ValueError) as exc:
            await self._publish_tool_result(
                call_id, tool_input, output=None, error=f"{type(exc).__name__}: {exc}", at=started
            )
            return
        # The spec names its connection by the CLOUD record id — that is what
        # `sql.query` stamps on `provenance.connection_id`, and what the browser
        # saved. The registry resolves a connection by the LOCAL handle the box
        # leased it under, so the id has to be mapped to that handle or every
        # re-run refuses as an unknown connection. A spec that already names a
        # handle (an older save, a hand-written object) is left alone.
        unresolved: str | None = None
        binding = self._session.tool_binding
        if binding is not None:
            leased = next(
                (c for c in binding.registry.connections() if team_connection_id(c) == connection),
                None,
            )
            if leased is not None:
                connection = leased.handle
                tool_args["connection"] = connection
                tool_input["connection"] = connection
            elif binding.registry.connection_for(connection) is None:
                unresolved = (
                    f"this machine has no connection {connection!r} — the saved query "
                    "names a connection that is not leased here"
                )
        await self._publish_own(
            ToolCall(
                event_id=f"{call_id}-call",
                time=started,
                session_id=self._chat_id,
                tool_call_id=call_id,
                message_id=call_id,
                tool_name=SQL_TOOL,
                tool_kind="read",
                input=tool_input,
                status="running",
            )
        )
        if unresolved is not None:
            # The card shows the call and then the refusal, in plain words —
            # never a silent drop, and never a result with no call above it.
            await self._publish_tool_result(
                call_id, tool_input, output=None, error=unresolved, at=started
            )
            return
        result = await self._dispatch_sql(tool_args)
        error = result.get("error") if isinstance(result, dict) else None
        await self._publish_tool_result(
            call_id,
            tool_input,
            output=result,
            error=str(error) if isinstance(error, str) and error else None,
            at=started,
        )

    async def _dispatch_sql(self, args: dict[str, Any]) -> dict[str, Any]:
        assert self._session is not None
        binding = self._session.tool_binding
        if binding is None:
            return {"error": "this chat has no tool binding", "tool": SQL_TOOL}
        # The same kwargs the session's own MCP transport passes, so a cloud
        # re-run is gated and audited exactly like the model calling the tool.
        return await (await binding.current_registry()).dispatch(
            SQL_TOOL,
            args,
            broker=binding.broker,
            permission_mode=binding.permission_mode,
            permissions=binding.permissions,
            session_id=binding.session_id,
            spawn=binding.spawn,
            tool_scope=binding.tool_scope,
            decision_sink=binding.decision_sink,
            judge=binding.judge,
            task_goal=binding.task_goal,
            alkera_dir=binding.alkera_dir,
            task_store=binding.task_store,
            sandbox_dir=binding.sandbox_dir,
            background=binding.background,
            abort=binding.abort,
            fence=binding.fence,
        )

    async def _publish_tool_result(
        self,
        call_id: str,
        tool_input: dict[str, Any],
        *,
        output: dict[str, Any] | None,
        error: str | None,
        at: datetime,
    ) -> None:
        if self._session is None:
            return
        await self._publish_own(
            PartCreated(
                event_id=f"{call_id}-result",
                time=datetime.now(UTC),
                session_id=self._chat_id,
                part=ToolCallPart(
                    part_id=f"{call_id}-part",
                    message_id=call_id,
                    call_id=call_id,
                    name=SQL_TOOL,
                    input=tool_input,
                    state="error" if error else "completed",
                    output=output,
                    error_text=error,
                ),
            )
        )

    # -- promote -------------------------------------------------------------------------

    async def _on_promote(self, relay: dict[str, Any]) -> None:
        event_id = relay.get("event_id")
        if not isinstance(event_id, str) or not _EVENT_ID.match(event_id):
            raise RelayRefusedError("event_id is not a valid event id")
        object_id, obj = await self._object_for(relay, kind="result")
        if obj.get("status") != "pending_upload":
            raise RelayRefusedError(f"object {object_id} is not awaiting a payload")
        spec = obj.get("spec")
        spec = spec if isinstance(spec, dict) else {}
        bound_event = _first_str(spec, *EVENT_BINDING_KEYS) or _first_str(obj, *EVENT_BINDING_KEYS)
        if bound_event is None:
            raise RelayRefusedError(f"object {object_id} names no transcript event")
        if bound_event != event_id:
            raise RelayRefusedError(f"object {object_id} was promoted from another event")
        if self._session is None:
            return
        if (refused := await self._admission_refusal(relay)) is not None:
            await self._refuse_promote(object_id, refused)
            return
        assert self._session is not None
        found = find_tool_result(self._session.events(), event_id)
        if found is None:
            await self._refuse_promote(object_id, f"no tool result with event id {event_id}")
            return
        part, at, call_started = found
        output = part.output if isinstance(part.output, dict) else {}
        try:
            envelope = result_envelope(self._runtime.project.blobs, output)
        except (FileNotFoundError, ValueError) as exc:
            await self._refuse_promote(object_id, f"the result payload is unavailable ({exc})")
            return
        receipt = self._receipt_for(part, output, event_id=event_id, at=at, started=call_started)
        try:
            await self._rest.upload_payload(
                object_id,
                envelope=envelope.model_dump(mode="json"),
                receipt=receipt.model_dump(mode="json"),
            )
        except CloudApiError as exc:
            await self._refuse_promote(object_id, f"the upload was refused ({exc.status})")
            return
        await self._note(f"Saved {envelope.total} rows to result {object_id}.", ok=True)

    async def _refuse_promote(self, object_id: str, reason: str) -> None:
        """A promote this machine cannot honour, said in BOTH places it is read.

        The reader who pressed save is usually on the object's own page by now,
        so a note in the chat reaches nobody: the object is failed with the
        reason, which is what its page and the results list render. The note
        stays for the reader who did not leave."""
        await self._note(f"Could not promote: {reason}.")
        try:
            await self._rest.fail_payload(object_id, reason=reason)
        except CloudApiError as exc:
            logger.warning(
                "mirror %s: could not fail object %s (%s)", self._chat_id, object_id, exc.status
            )

    def _receipt_for(
        self,
        part: ToolCallPart,
        output: dict[str, Any],
        *,
        event_id: str,
        at: datetime,
        started: datetime | None,
    ) -> ResultReceipt:
        return receipt_for_tool_result(
            part.input,
            output,
            event_id=event_id,
            at=at,
            started=started,
            principal=self._principal_for(event_id),
        )

    async def _say_refusal(self, refusal: RefusalNote) -> None:
        """Put a refusal on the transcript in plain language.

        The harness already told the MODEL why it was refused, and that message
        keeps the turn going — but the reader sees only a failed tool call, which
        reads as a broken product rather than a working one. This is the sentence
        they get instead: the workspace's own, with the statement it would not
        run quoted beside it, and nothing for them to go and change."""
        await self._note(refusal.as_note(), ok=True)

    def _principal_for(self, event_id: str) -> ReceiptPrincipal:
        """Who a result was produced for: the member whose turn produced the
        event, with the machine (or, before it registered, the chat) acting
        for them."""
        return receipt_principal(
            user_id=self._attribution.user_for(event_id),
            agent_id=self._machine_id or self._chat_id,
        )

    async def _load_source_brief(self) -> None:
        """Put the chat template this chat was started from in front of the agent.

        The brief itself comes off this chat's OWN record, where it was copied
        when the chat was created, never off the template: the template belongs
        to whoever saved it, and the operator whose credential this box holds
        may hold no rung on a colleague's private one. The template's record is
        read only to learn its NAME, and only best-effort — a read the drive
        refuses costs the name, not the brief.

        The author's notes come off the copied files, which are already on this
        box by the time the chat opens. Everything here is read on every open
        rather than carried across a sleep, so a chat that woke on another box
        is handed the same first turn as the one that opened it.
        """
        if not self._source_object_id:
            return
        brief = await self._template_brief()
        notes = template_notes(self.working_dir)
        if not brief and notes is None:
            return
        document: Mapping[str, Any] | None = None
        try:
            record = await self._rest.get_object(self._source_object_id)
        except Exception:
            logger.info(
                "mirror %s: could not read the chat template %s to name it",
                self._chat_id,
                self._source_object_id,
                exc_info=True,
            )
        else:
            document = source_document(record)
        self._source_brief = source_brief(document, brief=brief, notes=notes)

    async def _template_brief(self) -> str:
        """The template's prose as it was copied onto this chat, or ``""``.

        Read off the chat's own object, which the box may always read: it is
        the chat it publishes. A chat whose record carries none — one started
        before templates, or from something that is not one — has no brief,
        which reads as a chat started from nothing.
        """
        try:
            record = await self._rest.get_object(self._chat_id)
        except Exception:
            logger.warning(
                "mirror %s: could not read its own record for the template brief",
                self._chat_id,
                exc_info=True,
            )
            return ""
        spec = record.get("spec")
        metadata = spec.get("metadata") if isinstance(spec, Mapping) else None
        brief = metadata.get("template_brief") if isinstance(metadata, Mapping) else None
        return brief if isinstance(brief, str) else ""

    async def note_pressure_sleep(self, sentence: str, *, within: float = 10.0) -> bool:
        """Tell the chat, before it is put to sleep for memory or disk, which
        processes the sleep stops (``cloud/pressure_notice.py``). Owed in the
        chat's records first, and settled once the note is published (waited
        on for up to ``within`` seconds); a note still owed is posted at the
        chat's next start. ``True`` when it was published here."""
        await asyncio.to_thread(owe_note, self.chat_folder, sentence)
        before = self._published_count
        events = note_events(self._chat_id, sentence, answers_nothing=True)
        if self._session is None:
            return False
        for event in events:
            await self._publish_own(event)
        deadline = asyncio.get_running_loop().time() + within
        while asyncio.get_running_loop().time() < deadline:
            if self._published_count >= before + len(events) and not self._unpublished:
                await asyncio.to_thread(settle_notes, self.chat_folder)
                return True
            await asyncio.sleep(0.05)
        logger.warning(
            "mirror %s: the note about a sleep for room was not published before the "
            "release; it is posted when the chat next starts",
            self._chat_id,
            extra={"chat_id": self._chat_id},
        )
        return False

    async def post_owed_notes(self) -> None:
        """Post the notes a sleep for room owed the chat and could not publish
        before its release."""
        owed = await asyncio.to_thread(pending_notes, self.chat_folder)
        for sentence in owed:
            await self._note(sentence, answers_nothing=True)
        if owed:
            await asyncio.to_thread(settle_notes, self.chat_folder)

    async def _note(self, message: str, *, ok: bool = False, answers_nothing: bool = False) -> None:
        """A visible line in the transcript for something the cloud asked the
        daemon to do outside a turn: a system message of its own, with its
        own event ids, that no turn — the harness's or the meter's — is
        settled or counted by.

        ``answers_nothing`` marks a note the mirror writes about ITSELF, so the
        catch-up watermark keeps looking past it for a question still waiting.
        It is spelled into the note's ID rather than remembered here, because
        the reader that most needs to know is the box that comes back AFTER the
        restart this note reports — and the server, which has never had this
        process's memory at all.
        """
        if self._session is None:
            return
        for event in note_events(self._chat_id, message, ok=ok, answers_nothing=answers_nothing):
            await self._publish_own(event)


def _authorizes_a_write(request: PermissionRequest) -> bool:
    """Whether allowing ``request`` would let a write-class action run.

    The classifier's verdict is read FIRST, because the kind alone cannot
    answer it: ``shell`` is the canonical kind of every command opencode asks
    about, so a kind-first answer made ``ls`` as much a write as ``rm``. The
    write fence then judged a read by where it would land bytes, and refused —
    with no card, and with the sentence for a write — every read whose
    destinations its parser declines to guess at: a nested shell, a command
    behind ``env``/``xargs``/``sudo``, an unbalanced quote. The classifier reads
    inside all of those and calls each of them a write when it is one, so the
    two disagree only where it positively says READ.

    Only a positive ``read`` excuses an ask. A verdict that is missing, empty or
    a word this box does not know leaves the kind to answer, which is the
    fail-closed side: an unclassified shell ask still meets the fence.
    """
    subject = request.subject
    if isinstance(subject, Mapping):
        effect = subject.get("effect")
        if isinstance(effect, str) and effect:
            return effect != "read"
    return request.canonical_kind in WRITE_CLASS_KINDS


def _prompt_mark(relay: Mapping[str, Any]) -> str:
    """The transcript id a relayed message is known by on every reader's
    screen: the id minted from the sender's own client id, falling back to the
    row when a message arrived without one.

    One spelling, because two rows are named from it — what became of the
    message ("never run") and what became of the turn it started ("stopped") —
    and a reader that met them under different names would hold two facts about
    one message.
    """
    client_id = relay.get("client_id")
    message_id = relay.get("message_id")
    if isinstance(client_id, str) and client_id:
        return prompt_entry_id(client_id)
    return message_id if isinstance(message_id, str) else ""


def _ask_key(request: PermissionRequest) -> str:
    """What makes two asks the same action: the kind and what it names."""
    subject = request.subject if isinstance(request.subject, Mapping) else {}
    raw = subject.get("raw")
    return json.dumps(
        [
            request.canonical_kind,
            raw if isinstance(raw, str) else None,
            sorted(request.patterns),
            subject.get("targets"),
        ],
        sort_keys=True,
        default=str,
    )


#: A message's finish reasons that mean the answer was complete, as against a
#: step that stopped to call a tool.
_FINAL_FINISH_REASONS: frozenset[str] = frozenset({"stop", "end_turn", "length", "max_tokens"})


def _ends_a_turn(event: Mapping[str, Any]) -> bool:
    """Whether a published harness event closes the turn it belongs to."""
    kind = event.get("event_type")
    if kind == "turn.finished":
        return True
    if kind == "message.completed":
        return event.get("finish_reason") in _FINAL_FINISH_REASONS and not event.get("error")
    return kind == "session.status_changed" and event.get("status") == "idle"


def sandbox_fields_of(row: Mapping[str, Any]) -> dict[str, Any]:
    """The chat row's sandbox figures, as the harness bag carries them: the
    org's vCPU and memory limits (a positive int each). A field the row lacks,
    nulls or mistypes is left out, and the box default applies. The sandbox
    MODE is a node property, not a chat's — it comes from the box's own env
    (``ALKERA_SANDBOX_MODE``), and which chats a non-gVisor box may serve is the
    server's placement guard, not a per-chat field."""
    fields: dict[str, Any] = {}
    for name in ("sandbox_vcpu", "sandbox_memory_mb"):
        value = row.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            fields[name] = value
    return fields


def _harness_event_of(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """The harness event a transcript row carries, or ``None``.

    A machine row's ``payload`` is the entry the mirror published —
    ``{event_id, role, kind, payload}`` — so the event is one level in; a row
    written flat names its ``event_type`` at the top.
    """
    entry = row.get("payload")
    if not isinstance(entry, dict):
        return None
    inner = entry.get("payload")
    if isinstance(inner, dict) and isinstance(inner.get("event_type"), str):
        return inner
    if isinstance(entry.get("event_type"), str):
        return entry
    return None


def _gated_call_id(interrupt: _Interrupt, request: PermissionRequest) -> str | None:
    """The id the gated call's closure goes on: the transcript's key when the
    call was found there (the card the reader sees), else what the ask named."""
    if interrupt.tool is not None:
        return interrupt.tool[0]
    return request.tool_call_id or request.provider_call_id


def _fold_ask(read: _CatchUpRead, row: Mapping[str, Any], event: dict[str, Any]) -> None:
    """Fold one ask-shaped row into the read, and drop what it closes.

    This is what keeps a catch-up's memory the size of the chat's OPEN
    questions rather than the size of the turn. A turn runs unbounded — days,
    hundreds of thousands of tool calls, each row as large as the entry cap
    allows — and every one of those rows is ask-shaped, so holding them was
    holding the transcript under another name, on every mirror at once, at the
    moment a box resumes a long turn.

    So nothing is held past its usefulness: a resolution the MACHINE published
    means its harness already acted, which retires the ask, the answer anybody
    recorded for it and the card it was announced on; a tool call that reached
    a terminal state is not gating a permission ask any more, which retires the
    call and the provider ids that pointed at it. What is left is what a reader
    still owes an answer to.
    """
    kind = event.get("event_type")
    if kind in ("tool.call", "tool.call_update"):
        _note_call(read.calls, read.aliases, event)
        if event.get("status") in _SETTLED_CALL_STATES:
            _forget_call(read, event.get("tool_call_id"))
        return
    request_id = event.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return
    if kind == "permission.request":
        if event.get("prompting") is True:
            read.announced.add(request_id)
        read.asks.setdefault(request_id, ("permission", event))
    elif kind == "question.request":
        read.asks.setdefault(request_id, ("question", event))
    elif kind in RESOLUTION_KINDS:
        if _recorded_by_a_reader(row):
            # An answer given while no box was holding the ask: nobody has
            # acted on it, so it is carried to the end of the walk and applied
            # — unless a turn the machine ended after it says otherwise.
            read.recorded.setdefault(request_id, event)
            seq = row.get("seq")
            if isinstance(seq, int):
                read.recorded_at.setdefault(request_id, seq)
        else:
            read.asks.pop(request_id, None)
            read.recorded.pop(request_id, None)
            read.announced.discard(request_id)
            read.resolved.add(request_id)


def _retire_answers_before(read: _CatchUpRead, seq: int) -> None:
    """A turn the machine ended at ``seq`` was carried past every answer a
    reader recorded before it: the harness got those answers live and acted on
    them. Applying one again on a later open re-runs the call it allowed and
    continues a turn that is already over, so they are retired with their asks.
    """
    for request_id, at in list(read.recorded_at.items()):
        if at >= seq:
            continue
        read.recorded_at.pop(request_id, None)
        read.recorded.pop(request_id, None)
        read.asks.pop(request_id, None)
        read.announced.discard(request_id)
        read.resolved.add(request_id)


def _is_prompt_row(row: Mapping[str, Any]) -> bool:
    """Whether a reader's row is a message they sent (not a recorded answer)."""
    payload = row.get("payload")
    kind = (payload.get("kind") if isinstance(payload, Mapping) else None) or row.get("kind")
    return reads_as_prompt(role="user", kind=kind)


def _forget_call(read: _CatchUpRead, call_id: Any) -> None:
    """Drop a finished call from the read, aliases included."""
    if not isinstance(call_id, str) or not call_id:
        return
    read.calls.pop(call_id, None)
    for provider in [key for key, target in read.aliases.items() if target == call_id]:
        read.aliases.pop(provider, None)


def _note_call(
    calls: dict[str, tuple[str, dict[str, Any]]],
    aliases: dict[str, str],
    event: Mapping[str, Any],
) -> None:
    """Fold one ``tool.call`` / ``tool.call_update`` into ``calls``: the name
    comes from the call, the input from the last frame that carried one. A
    provider call id the call carries is kept in ``aliases`` onto the
    transcript's key, so an ask naming the call the provider's way resolves."""
    call_id = event.get("tool_call_id")
    if not isinstance(call_id, str) or not call_id:
        return
    name, tool_input = calls.get(call_id, ("", {}))
    if event.get("event_type") == "tool.call":
        given = event.get("tool_name")
        if isinstance(given, str) and given:
            name = given
        provider = event.get("provider_call_id")
        if isinstance(provider, str) and provider and provider != call_id:
            aliases[provider] = call_id
    fresh = event.get("input")
    if isinstance(fresh, dict) and fresh:
        tool_input = dict(fresh)
    calls[call_id] = (name, tool_input)


def _recorded_by_a_reader(row: Mapping[str, Any]) -> bool:
    """Whether a resolution row is one the SERVER wrote for a reader's answer
    (``RECORDED_ANSWER_ROLE``), as against one the machine published after its
    harness settled the ask. The column the server indexed says so; a row
    served without it falls back to the envelope's own role."""
    role = row.get("role")
    if isinstance(role, str) and role:
        return role == RECORDED_ANSWER_ROLE
    entry = row.get("payload")
    return isinstance(entry, dict) and entry.get("role") == RECORDED_ANSWER_ROLE


def _relay_of_recorded(event: Mapping[str, Any]) -> dict[str, Any]:
    """A recorded resolution, as the answer relay it stands for — so the record
    goes through exactly the checks a relayed answer does."""
    kind = event.get("event_type")
    if kind == "permission.resolved":
        return {"option_id": event.get("option_id"), "user_id": event.get("decided_by_user_id")}
    if kind == "question.answered":
        return {"answers": event.get("answers"), "note": event.get("note")}
    return {"reject": True, "reason": event.get("reason")}


def _stamp_of(value: object) -> datetime | None:
    """A row's ``created_at`` as the server serialised it, or ``None`` for
    anything that is not one."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _last_prompt_relay(
    users: Sequence[Mapping[str, Any]], *, chat_id: str = ""
) -> dict[str, Any] | None:
    """The person's last message in ``users`` as a relay, or ``None``."""
    for row in reversed(list(users)):
        relay = _relay_from_transcript(row, chat_id=chat_id)
        if relay is not None and relay.get("text"):
            return relay
    return None


def _relay_from_transcript(row: Mapping[str, Any], *, chat_id: str = "") -> dict[str, Any] | None:
    """A stored transcript entry, read back as the relay it was — or ``None``
    when the row is not a person asking something.

    Deliberately the SAME shape the socket delivers — the message's own id and
    sequence from the row, the text, the browser's client id and the user the
    server stamped on it from the payload — so a caught-up message runs through
    exactly the path a live one does, attribution and all.

    A user-role row is not by itself a question. The machine publishes the
    harness's echo of the message it accepted as an entry of its own, still
    attributed to the person who spoke, and that echo is the LAST row of the
    chat whenever a box dies between taking a question and publishing the first
    part of its answer. It is not a prompt record, so it is not a relay.
    """
    payload = row.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    # A row that positively names another kind — in the payload the producer
    # wrote or in the column the server indexed — is somebody else's entry.
    # Silence is not: a row written before either carried a kind still reads
    # back as the question it was.
    kind = payload.get("kind") or row.get("kind")
    if not reads_as_prompt(role="user", kind=kind):
        logger.info(
            "mirror %s: transcript row %s is a %r entry, not a question; nothing to run",
            chat_id,
            row.get("seq"),
            kind,
            extra={"chat_id": chat_id, "seq": row.get("seq"), "entry_kind": kind},
        )
        return None
    try:
        record = ChatPromptRecord.model_validate(payload)
    except ValidationError as malformed:
        logger.warning(
            "mirror %s: transcript row %s could not be read as a question "
            "(%d bad field(s)); skipping it",
            chat_id,
            row.get("seq"),
            malformed.error_count(),
            extra={"chat_id": chat_id, "seq": row.get("seq")},
        )
        return None
    seq = row.get("seq")
    created_at = _stamp_of(row.get("created_at"))
    return PromptRelay(
        message_id=str(row.get("id") or ""),
        seq=seq if isinstance(seq, int) else 0,
        text=record.text,
        client_id=record.client_id,
        user_id=record.user_id,
        # The row's own stamp, as the live relay carries it.
        at=created_at,
        # A message the box catches up on names the same files the live relay
        # would have: dropping them here would answer the question without the
        # attachments it was asked about.
        attachments=record.attachments,
        # And the same context: a caught-up Slack first turn is briefed exactly
        # as a live one.
        context=record.context,
    ).model_dump(mode="json")


def _uuid_field(relay: dict[str, Any], key: str) -> str:
    value = relay.get(key)
    if not isinstance(value, str):
        raise RelayRefusedError(f"{key} is missing")
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise RelayRefusedError(f"{key} is not a UUID") from None


def _first_str(spec: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = spec.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def chunk_size(events: list[dict[str, Any]]) -> int:
    """The bytes the server measures a chunk's events at: compact JSON with
    non-ASCII escaped, as ``pg_notify`` receives it."""
    return len(json.dumps(events, separators=(",", ":"), ensure_ascii=True))


def _split_chunk(event: dict[str, Any], max_bytes: int) -> list[dict[str, Any]]:
    """One coalesced chunk as pieces of at most ``max_bytes`` each, measured in
    the server's bytes — a CJK or emoji character costs six, not one. The
    socket's :attr:`~alkera_cli.cloud.transport.CloudSocket.sizes` names the
    budget the server's notify cap leaves."""
    if chunk_size([event]) <= max_bytes:
        return [event]
    text = event.get("text")
    if not isinstance(text, str) or not text:
        return [event]
    shell = dict(event)
    shell["text"] = ""
    shell["is_final"] = False
    budget = max_bytes - chunk_size([shell])
    if budget <= 0:
        return [event]
    segments: list[str] = []
    segment: list[str] = []
    used = 0
    for char in text:
        cost = len(json.dumps(char, ensure_ascii=True)) - 2
        if used + cost > budget and segment:
            segments.append("".join(segment))
            segment, used = [], 0
        segment.append(char)
        used += cost
    if segment:
        segments.append("".join(segment))
    out: list[dict[str, Any]] = []
    for index, chunk_text in enumerate(segments):
        piece = dict(event)
        piece["text"] = chunk_text
        if index < len(segments) - 1:
            piece["is_final"] = False
        out.append(piece)
    return out


__all__ = [
    "CHAT_BINDING_KEYS",
    "EVENT_BINDING_KEYS",
    "NOT_PUBLISHER",
    "SQL_TOOL",
    "WRITE_CLASS_KINDS",
    "ChatMirror",
    "ChatMirrorRefusedError",
    "MirrorState",
    "RelayRefusedError",
    "chunk_size",
]
