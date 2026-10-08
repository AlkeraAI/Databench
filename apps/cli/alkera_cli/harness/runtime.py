"""Workspace-scoped harness runtime and chat-session orchestration.

Both the CLI and the daemon construct `HarnessRuntime`; daemon JSON-RPC
methods only translate calls into this library. The runtime owns chat locks,
adapters, live event fan-out, persistence and scheduled background work. A
`ChatSession` owns one chat, its adapter, its credential and its pumps.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast, get_args

from alkera_core.process import SpawnSpec, kill_tree_async, spawn_async
from alkera_core.project.directory import SANDBOX_SUBDIR, ProjectDirectory
from alkera_core.project.locking import live_holder
from alkera_core.schemas.chat import (
    ChatManifest,
    Event,
    MessageCompleted,
    PartCreated,
    PermissionOptionId,
    PermissionRequest,
    PermissionResolved,
    QuestionRequest,
    SessionStatusChanged,
    SessionUpdated,
    SubagentCompleted,
    SubagentStarted,
    ToolCall,
    ToolCallStatus,
    ToolCallUpdate,
    TurnFinished,
)
from pydantic import BaseModel

from alkera_cli.account.binding import ChatCredential, FixedCredential, ProfileBinding, bind_session
from alkera_cli.cloud_sync.team_actions import team_connection_actions
from alkera_cli.environment import CAPTURE_TOOL_NAME, EnvironmentService, environment_blocks
from alkera_cli.files.chat_fs import ChatTree
from alkera_cli.harness.adapter import (
    MCP_NEVER_TIMEOUT_MS,
    HarnessAdapter,
    HarnessNotReadyError,
    HarnessUnavailableError,
    PathFence,
    PromptInput,
    SessionConfig,
)
from alkera_cli.harness.adapter_factory import AdapterFactory
from alkera_cli.harness.background import BackgroundJob, BackgroundJobRegistry
from alkera_cli.harness.claude_gateway import ClaudeEnvBuilder
from alkera_cli.harness.empty_answer import EmptyAnswerWatch
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.extension_points import (
    HARNESS_CONTEXT_PROVIDERS,
    BriefRequest,
    InstructionRequest,
    connection_records,
    session_spend,
    workspace_seeder,
)
from alkera_cli.harness.gateway_session import GatewayConfigBuilder, bound_to, chat_token
from alkera_cli.harness.interrupted_jobs import as_finished_job, interrupted_background_jobs
from alkera_cli.harness.mcp_server import (
    WEB_MCP_MOUNT,
    AlkeraToolServer,
    SessionToolBinding,
)
from alkera_cli.harness.model_retry import (
    DEFAULT_MAX_MODEL_RETRIES,
    DEFAULT_MODEL_RETRY_WINDOW_SECONDS,
)
from alkera_cli.harness.permission_broker import PermissionBroker
from alkera_cli.harness.permission_mode import (
    PermissionMode,
    denial_message,
    mode_change_note,
    mode_switch_reminder,
    mode_system_prompt,
    mode_to_agent,
    parse_mode,
    refusal_feedback,
    write_free_root,
)
from alkera_cli.harness.permission_policy import RequestDecision, request_auto_decision
from alkera_cli.harness.question_broker import QuestionBroker
from alkera_cli.harness.registry import CLAUDE_HARNESS, OPENCODE_HARNESS
from alkera_cli.harness.resume_reconcile import reconcile_interrupted_turn
from alkera_cli.harness.runtime_scheduling import _SchedulingMixin, protected_kinds
from alkera_cli.harness.sandbox import RUNTIME_STATE_SUBDIR, owner_session, session_default_env
from alkera_cli.harness.session_launches import forget_session_launches
from alkera_cli.harness.subagent_routing import SubagentModelResolver
from alkera_cli.harness.system_prompt import (
    VERIFICATION_TURN_PROMPT,
    compose_main_agent_guidance,
    render_root_folder_brief,
    render_task_reminder,
)
from alkera_cli.harness.tasks import stop_task
from alkera_cli.harness.turn_briefs import brief_texts, turn_briefs
from alkera_cli.harness.turn_lifecycle import (
    ReplayPrompt,
    TurnLifecycle,
    TurnLifecycleConfig,
    ends_attempt,
)
from alkera_cli.harness.turn_model import pinned_model_key, turn_model, with_reasoning_history
from alkera_cli.harness.turn_restart import (
    TURN_RESTART_FAILED,
    TURN_RESTART_LIMIT,
    TURN_RESTART_NOTE,
    RestartLedger,
    TurnRestart,
    plan_turn_restart,
)
from alkera_cli.harness.web_flags import WebToolFlags
from alkera_cli.host.launcher import LauncherNotFoundError, running_launcher
from alkera_cli.host.limits import env_count, env_seconds
from alkera_cli.host.parent_watchdog import SEED_PARENT_PID_ENV
from alkera_cli.notebooks import session as notebooks
from alkera_cli.plugins.plugin_base.agent_result import AgentUsageStats, SubagentRunResult
from alkera_cli.plugins.plugin_base.artifacts import artifact_mtime
from alkera_cli.plugins.plugin_base.delivery import deliver_generic
from alkera_cli.plugins.plugin_base.permissions.audit import (
    AuditUnavailableError,
    NotedDecisionSink,
)
from alkera_cli.plugins.plugin_base.permissions.policy import AutoDecision
from alkera_cli.plugins.plugin_base.permissions.resolve import (
    AUDIT_FAILED_REASON,
    AskReconsideredError,
    bind_to_this_ask,
    standing_answer_recordable,
    without_standing_grant,
)
from alkera_cli.plugins.plugin_base.wire import (
    is_tool_error_result,
    model_facing_text,
    serialize_tool_result,
)
from alkera_cli.preferences.instructions import cap_global_instructions, load_global_instructions

if TYPE_CHECKING:
    from alkera_core.project.chats.chat import Chat

    from alkera_cli.contracts.tool_types import ActionDescriptor
    from alkera_cli.harness.file_watcher import ProjectFileWatcher
    from alkera_cli.harness.safety_judge import SafetyJudge
    from alkera_cli.observability.audit_report import AuditReporter
    from alkera_cli.observability.otel_export import OtelExporter
    from alkera_cli.plugins.plugin_base import (
        Connection,
        ConnectionFormSchema,
        PluginRegistry,
        ToolRegistry,
    )
    from alkera_cli.plugins.plugin_base.permissions import DecisionSink
    from alkera_cli.plugins.plugin_base.scheduler import Scheduler
    from alkera_cli.plugins.plugin_base.tool import ToolScope

logger = logging.getLogger(__name__)

# Persistence subscription is UNBOUNDED. chat.jsonl is the durable audit
# log of the conversation; under back-pressure (slow disk, NFS, full
# disk) we must NEVER drop. Memory growth is the right tradeoff —
# the steady-state event rate is low (chunks/heartbeats are filtered
# at the bus level via `NON_PERSISTED_EVENT_TYPES`), so even a long
# stall buffers in the low single-digit MB. The renderer's subscription
# stays bounded (chunks are recoverable from finalized parts).
PERSIST_QUEUE_MAXSIZE = 0

#: How long closing or clearing a chat waits for that unbounded queue to drain
#: before teardown proceeds anyway. Every event past the cut is lost from
#: chat.jsonl, so the bound exists only against a pump that has wedged. 0 waits
#: for the drain however long the disk takes, which is what a deployment on a
#: slow or networked disk should set.
ENV_PERSIST_DRAIN_TIMEOUT = "ALKERA_PERSIST_DRAIN_TIMEOUT"
PERSIST_DRAIN_SECONDS = env_seconds(os.environ.get(ENV_PERSIST_DRAIN_TIMEOUT), default=5.0)

#: A subagent carries no wall-clock budget of its own; if an operator sets one,
#: hitting it does NOT kill the child — we interrupt it and force a final summary
#: turn. There is no fan-out cap either: cost is bounded by the metered gateway +
#: cost caps, not an arbitrary count.
#: The provenances that hand the turn back to the person: their own answer, a
#: prompt that ran out on them, and a broker that could not reach them. A
#: reject from any of these carries no reason (opencode ends the turn on that),
#: and every other provenance is an automatic decision the turn survives.
_HANDED_BACK = frozenset({"human", "timeout", "broker"})

#: What the model is told when the session's own boundary judge could not run.
#: It carries a reason, so the refusal is the action's and not the turn's — the
#: model reads it and tries something else, instead of the chat stopping.
FENCE_UNCHECKED_REASON = (
    "The workspace boundary could not be checked for this action, so it was refused and "
    "nothing ran. Use the read/edit tools, or a command whose paths are spelled out plainly."
)


def _event_provenance(decided_by: str) -> Literal["user", "policy", "timeout"]:
    """The transcript's word for who decided: ``user`` only for a person's
    answer, ``timeout`` when the prompt ran out, ``policy`` for everything
    the session decided itself (mode, rule, floor, fence, sandbox, judge…)."""
    if decided_by == "human":
        return "user"
    if decided_by == "timeout":
        return "timeout"
    return "policy"


#: Context-overflow recovery (ROOT only): how many times a single turn may be
#: auto-compacted + re-sent before the overflow is surfaced. One-shot by default —
#: a second overflow after a compaction usually means compaction couldn't free
#: enough space, so retrying again would just loop; give up with an actionable
#: notice instead. 0 removes the bound, for a deployment that would rather keep
#: compacting for as long as each compaction keeps freeing space.
ENV_MAX_OVERFLOW_RETRIES = "ALKERA_MAX_OVERFLOW_RETRIES"
MAX_OVERFLOW_RETRIES = env_count(os.environ.get(ENV_MAX_OVERFLOW_RETRIES), default=1)


#: The orthogonal analysis selector: ``analyst`` makes the harness issue one
#: verification turn after an answer computed from data. Off changes nothing.
AnalysisPipeline = Literal["off", "analyst"]

#: The tools whose COMPLETED, non-error result marks a turn as "answered from
#: data" and earns a verification turn. Discovered tools arrive through the generic
#: dispatcher, so the name is read off the dispatch input as well as the tool name.
_DATA_READ_TOOLS = frozenset({"sql.query", "data.join", "blob.query"})

#: How the last verification turn ended: ``verified`` means a clean idle, so its
#: restatement is the delivery; ``failed: <reason>`` means the first answer stands.
VerificationOutcome = str

#: Stop reasons that mean the turn settled with a finished answer.
SETTLED_STOP_REASONS = frozenset({"end_turn", "completed"})

#: Session statuses that end a live turn.
TURN_ENDING_STATUSES = frozenset({"idle", "error", "completed", "aborted"})

#: Finish reasons that mean the output cap cut the message (opencode reports
#: ``length``, claude ``max_tokens``); opencode then idles with no ``TurnFinished``.
TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens"})

#: A background job's START card (``bgjob:<id>``) and FINISH card (``bgdone:<id>``)
#: report work the turn did not do; the reader and the emitters share these keys.
_BACKGROUND_START_CARD = "bgjob:"
_BACKGROUND_FINISH_CARD = "bgdone:"


def _is_background_card(tool_call_id: str) -> bool:
    return tool_call_id.startswith((_BACKGROUND_START_CARD, _BACKGROUND_FINISH_CARD))


#: Who issued a turn. Only a caller turn can earn a verification.
TurnOrigin = Literal["caller", "wake", "verification"]


@dataclass
class _Turn:
    """The turn in flight, as the analyst policy sees it."""

    seq: int
    origin: TurnOrigin
    read_data: bool = False
    failed: bool = False
    pending_calls: set[str] = field(default_factory=set)


@dataclass
class _Verification:
    """The verification owed to the current caller prompt: ``seq`` once it fired,
    ``outcome`` once it ended (or the fire failed)."""

    seq: int | None = None
    outcome: VerificationOutcome | None = None

    @property
    def pending(self) -> bool:
        return self.outcome is None


#: Bound on the recovery compaction going SILENT (each event it publishes buys the
#: window again; it ends on a `CompactionApplied`, or a fresh error = compaction
#: failed). Summarizing a near-full context on a slow model is a long LLM call that
#: a total deadline abandons mid-flight, losing the reader's message; only a
#: compaction that has stopped saying anything is wedged. 0 removes the bound.
ENV_OVERFLOW_COMPACTION_TIMEOUT = "ALKERA_OVERFLOW_COMPACTION_TIMEOUT"
OVERFLOW_COMPACTION_TIMEOUT_SECONDS = env_seconds(
    os.environ.get(ENV_OVERFLOW_COMPACTION_TIMEOUT), default=1800.0
)
#: A turn whose model call keeps failing (the gateway unreachable, the provider
#: answering 5xx or overloaded) is stopped and ends failed after this many
#: consecutive retries, or after this long spent retrying, whichever comes
#: first. The agent itself retries with no cap. 0 removes a bound.
ENV_MAX_MODEL_RETRIES = "ALKERA_MAX_MODEL_RETRIES"
MAX_MODEL_RETRIES = env_count(
    os.environ.get(ENV_MAX_MODEL_RETRIES), default=DEFAULT_MAX_MODEL_RETRIES
)
ENV_MODEL_RETRY_WINDOW = "ALKERA_MODEL_RETRY_WINDOW"
MODEL_RETRY_WINDOW_SECONDS = env_seconds(
    os.environ.get(ENV_MODEL_RETRY_WINDOW), default=DEFAULT_MODEL_RETRY_WINDOW_SECONDS
)
#: The clock the retry window is measured on (monotonic seconds); a module
#: seam so a test can step it without freezing the event loop's own clock.
MODEL_RETRY_CLOCK: Callable[[], float] = time.monotonic
#: Settled-turn history kept per session; old entries prune. A memory bound, not
#: a product one — every consumer reads its own turn's outcome within that turn,
#: so the only thing a bigger number buys is a longer forensic tail. 0 keeps
#: every settlement for the life of the session.
ENV_SETTLEMENT_HISTORY = "ALKERA_SETTLEMENT_HISTORY"
_SETTLEMENT_HISTORY = env_count(os.environ.get(ENV_SETTLEMENT_HISTORY), default=256)
ENV_SUBAGENT_TURN_BUDGET = "ALKERA_SUBAGENT_TURN_BUDGET"
#: How long a spawned worker may explore before it is interrupted and asked for
#: its report. No budget at all by default: a worker that reads a large
#: repository, profiles a warehouse or runs a build is doing the work it was
#: spawned for, and a clock that fires first hands the parent a partial answer
#: instead of an answer — a wrong answer nobody can see is wrong. A worker stays
#: bounded by things that are real: its own process dying, a tool's own timeout,
#: and the parent's cancel. An operator who wants a leash sets one here.
_SUBAGENT_TURN_BUDGET_DEFAULT: float | None = None


def subagent_turn_budget(raw: str | None) -> float | None:
    """The exploration budget in seconds, or ``None`` for no budget at all.

    Read exactly like its siblings: unset, blank or unreadable is the default —
    which is no budget, so a typo can never invent one — and a non-positive
    value says "unbounded" explicitly.
    """
    return env_seconds(raw, default=_SUBAGENT_TURN_BUDGET_DEFAULT)


SUBAGENT_TURN_BUDGET_SECONDS = subagent_turn_budget(os.environ.get(ENV_SUBAGENT_TURN_BUDGET))
#: Said in the report itself when a budget an operator set cut the exploration
#: short. The parent reads the summary and nothing else, so a partial report that
#: does not say it is partial is read as a complete one. With no budget in force
#: nothing cuts the work, and the note never rides.
_BUDGET_CUT_NOTE = (
    "[Note: this agent was stopped at its {minutes:.0f}-minute research budget and asked to "
    "report what it had. Anything it had not reached yet is missing from the report above.]"
)
#: A ceiling on the force-summary (reporting) turn. Unset by default: a thinking
#: model writing up hours of research takes as long as it takes, and a clock that
#: cuts it hands the parent a report that stops mid-sentence and reads as
#: findings. The parent is still released by things that are real — the adapter
#: synthesizes the attempt's terminal when the child dies, and the parent's own
#: cancel ends the wait. An operator who wants a ceiling sets one; on hit:
#: best-effort partial summary, or a clean error.
ENV_SUBAGENT_REPORT_CEILING = "ALKERA_SUBAGENT_REPORT_CEILING"
SUBAGENT_REPORT_CEILING_DEFAULT: float | None = None
SUBAGENT_REPORT_CEILING_SECONDS = env_seconds(
    os.environ.get(ENV_SUBAGENT_REPORT_CEILING), default=SUBAGENT_REPORT_CEILING_DEFAULT
)
#: Absolute ceiling — not an idle gap — on draining the cancel-synthesized events
#: (flushed partials + the ``aborted`` status) before the force-summary turn. The
#: synth is local + immediate, so this only fires for a wedged child (then we bail
#: the drain). 0 waits for the child's own terminal instead.
ENV_SUBAGENT_DRAIN_CEILING = "ALKERA_SUBAGENT_DRAIN_CEILING"
_DRAIN_CEILING_SECONDS = env_seconds(os.environ.get(ENV_SUBAGENT_DRAIN_CEILING), default=5.0)
_CHAT_SESSION_CLOSED_MESSAGE = "ChatSession is closed"


#: Hard cap on one seed subprocess (lineage table/column OR the KB index). Set VERY high (8h)
#: on purpose: a spellbook-scale FIRST seed (hundreds of MB of manifests → tens of thousands of
#: cards + GB of embeddings, or the global column drain) can legitimately run for hours, and a
#: cap that kills it mid-pass would re-start it from the top every run and never converge.
#:
#: Long runs are SAFE, not just tolerated: the store is durable (the worker commits as it goes)
#: and every seed is incremental + resumable (parse marks / source fingerprints / pending-vector
#: backfill), so a kill loses nothing — the next run continues. And while a worker runs the
#: scheduler heartbeats its lease every ~20s; because the seed is OUT of process the daemon loop
#: stays free to do so, so a live run is never seen as stale → never reset, re-claimed, or
#: double-run (only a DEAD daemon's lease goes stale and gets reclaimed). The cap remains only as
#: a backstop against a truly wedged worker pinning a slot forever. Override (seconds) with
#: ``ALKERA_SEED_SUBPROCESS_TIMEOUT``.
try:
    _SEED_SUBPROCESS_TIMEOUT = float(os.environ["ALKERA_SEED_SUBPROCESS_TIMEOUT"])
except (KeyError, ValueError):
    _SEED_SUBPROCESS_TIMEOUT = 8 * 3600.0  # 8 hours

#: Bound on reaping a KILLED seed worker. The worker holds the caller's ``_seed_lock``
#: (the next lineage job blocks on it), so a SIGKILLed child that doesn't die promptly
#: (uninterruptible IO, surviving pool children) must NOT pin the lock — the next job
#: would sit in "Waiting for another lineage job to finish…" forever. We bail after this
#: and let the OS reap the orphan; the lock releases either way. 0 waits for the
#: child, which a deployment with no second lineage job to run may prefer.
ENV_SEED_KILL_TIMEOUT = "ALKERA_SEED_KILL_TIMEOUT"
_SEED_KILL_TIMEOUT = env_seconds(os.environ.get(ENV_SEED_KILL_TIMEOUT), default=5.0)
#: Min seconds between connection RE-discoveries on the file-watcher's per-edit callback. New
#: connections only come from file add/remove, and discovery walks the tree — so don't repeat it
#: on every keystroke-save; a newly-dropped artifact still surfaces within this window.
#: 0 re-discovers on every save, for a workspace small enough that the walk is free.
ENV_REDISCOVER_MIN_INTERVAL = "ALKERA_CONNECTION_REDISCOVER_INTERVAL"
_REDISCOVER_MIN_INTERVAL_S = (
    env_seconds(os.environ.get(ENV_REDISCOVER_MIN_INTERVAL), default=5.0) or 0.0
)
#: How long ``close_all`` waits for the cancelled scheduler beat to end before abandoning it
#: by name. A beat ends at its next await, so on a healthy runtime the wait is milliseconds;
#: the bound is for a tick wedged in a call that never returns, which must not hold a daemon
#: shutdown or a chat's exit open with nothing said about why.
BEAT_STOP_TIMEOUT_S = 5.0


def _alkera_entrypoint() -> str:
    """The ``alkera`` executable to re-invoke for a seed subprocess. ``sys.argv[0]`` is
    the right entrypoint when the process was launched as ``alkera ...`` (dev
    ``.venv/bin/alkera``, ``uv run alkera``, or the compiled binary). But if we were
    launched via a different wrapper (e.g. ``pytest``), argv[0] would be that wrapper —
    so fall back to ``alkera`` on PATH when argv[0] doesn't look like our binary.
    Named by its absolute path (:mod:`alkera_cli.host.launcher`), so a box with
    the build off ``PATH`` still finds it; bare ``alkera`` only as a last resort."""
    try:
        return str(running_launcher(only_alkera_argv0=True))
    except LauncherNotFoundError:
        return "alkera"


#: Tool names (by ``tool_name``, case-insensitive) whose input names a file/path
#: we tally into ``files_read``. Vendor read tools; ``tool_kind`` is unreliable
#: (opencode ships none), so match on name.
_READ_FILE_TOOLS = {"read", "grep", "glob", "list"}
#: Input keys a read tool uses to name its target (best-effort across backends).
_FILE_INPUT_KEYS = ("filePath", "file_path", "path", "pattern")
#: Injected as the child's prompt when an operator's exploration budget fires:
#: interrupt-then-force-summary, NOT a kill.
_FORCE_SUMMARY_PROMPT = (
    "You've hit your time budget — STOP exploring now and return your FINAL "
    "summary of what you found so far. This is your last turn. Include positive "
    "AND negative findings with file:line citations, and state anything you "
    "could not finish checking."
)
#: Injected instead when the child simply ended without a usable report. Nothing
#: interrupted it, so telling it that a clock did would put a cut-short story in
#: front of a worker that ran to completion — and the parent reads what it writes.
_ASK_FOR_REPORT_PROMPT = (
    "Your exploration has ended without a report. Return your FINAL summary now. "
    "This is your last turn. Include positive AND negative findings with file:line "
    "citations, and state anything you could not check."
)


class SubagentError(RuntimeError):
    """A subagent could not be spawned or its run failed."""


@dataclass(slots=True)
class _SubagentTurnCollector:
    """Text, tool, and file accounting for one child turn."""

    texts: list[str] = field(default_factory=list)
    by_tool: dict[str, int] = field(default_factory=dict)
    name_by_id: dict[str, str] = field(default_factory=dict)
    files: set[str] = field(default_factory=set)
    last_was_text: bool = False
    saw_tool_error: bool = False

    @property
    def needs_forced_report(self) -> bool:
        """Whether the child ended without usable report text."""
        return not self.texts or (not self.last_was_text and self.saw_tool_error)

    def consume(self, ev: Event) -> None:
        """Fold one child event into report/statistics state."""
        if isinstance(ev, PartCreated):
            self._consume_part(ev)
            return
        if isinstance(ev, ToolCall):
            self._consume_tool_call(ev)
            return
        if isinstance(ev, ToolCallUpdate):
            self._consume_tool_update(ev)

    def _consume_part(self, ev: PartCreated) -> None:
        part = ev.part
        if getattr(part, "type", None) != "text":
            return
        text = getattr(part, "text", "")
        if text:
            self.texts.append(text)
            self.last_was_text = True

    def _consume_tool_call(self, ev: ToolCall) -> None:
        name = ev.tool_name or "tool"
        self.by_tool[name] = self.by_tool.get(name, 0) + 1
        self.name_by_id[ev.tool_call_id] = name
        self._maybe_file(name, ev.input)
        self.last_was_text = False

    def _consume_tool_update(self, ev: ToolCallUpdate) -> None:
        if ev.input:
            self._maybe_file(self.name_by_id.get(ev.tool_call_id, ""), ev.input)
        if ev.status == "error":
            self.saw_tool_error = True

    def _maybe_file(self, name: str, inp: dict[str, Any] | None) -> None:
        if not inp or name.lower() not in _READ_FILE_TOOLS:
            return
        for key in _FILE_INPUT_KEYS:
            value = inp.get(key)
            if isinstance(value, str) and value:
                self.files.add(value)
                return


def _child_permission_mode(parent_mode: PermissionMode, agent_def: Any) -> PermissionMode:
    """The child subagent's permission mode.

    A read-only agent — a ``read_only`` ``tool_scope`` or an ``explore`` ``mode``
    definition, which is the built-in ``explore`` and ``review`` subagents (and any
    plugin agent that opts in) — is ALWAYS ``read_only``, regardless of the parent's
    mode. It must never mutate, even under a ``default`` / ``auto`` / ``bypass`` parent.

    Any other agent inherits the parent's mode (tighten-only), except a ``plan``
    ceiling becomes ``read_only`` — a subagent never plans. Plan mode is the ROOT
    session's human-in-the-loop planning contract (present a plan for approval, ask the
    user clarifying questions, fan out Explore agents), none of which a headless
    subagent can do: it has no human watching it, must not present a plan, and can't
    recurse. ``read_only`` keeps the same no-mutation guarantee (it auto-rejects writes
    rather than prompting a human who isn't watching) and drops the plan/ask steering
    that would otherwise point a subagent at interactive tools it isn't given.
    """
    if agent_def is not None and (
        getattr(agent_def, "tool_scope", None) == "read_only"
        or getattr(agent_def, "mode", None) == "explore"
    ):
        return "read_only"
    if parent_mode == "plan":
        return "read_only"
    return parent_mode


async def _detached_child_auto_reject(_request: PermissionRequest) -> PermissionOptionId:
    """The permission resolver for a DETACHED (background) subagent's child session.

    A background subagent runs with no human watching, so it must never block on a
    permission prompt. The ``read_only`` MODE clamp is only the policy BASE — a project
    ``.alkera/permissions.yml`` ``ask`` rule overrides it to PROMPT (the rule wins over
    the mode default), and the shared interactive broker has no timeout, so such a
    prompt would await a human forever and hang the job (pinning a background slot).
    Giving the detached child this non-interactive resolver turns any residual prompt —
    from an ``ask`` rule on a write OR a read, or any future mode/rule interaction —
    into a clean ``reject_once`` denial, fail-closed, so the child can never stall.
    """
    return "reject_once"


def _compose_system(*blocks: str | None) -> str | None:
    """Join the non-empty per-turn system blocks (mode steering + a caller
    addendum) into ONE string, or ``None``. Both backends inject the composed
    result as a single leading ``<system-reminder>``."""
    parts = [b.strip() for b in blocks if b and b.strip()]
    return "\n\n".join(parts) if parts else None


def _replay_prompt_from_kwargs(text: str, kwargs: Mapping[str, Any]) -> tuple[ReplayPrompt, bool]:
    """Parse the historical `send_prompt` keyword surface into a replayable prompt."""
    values = dict(kwargs)
    known = {"model", "variant", "parts", "system_addendum", "context", "_overflow_retry"}
    unknown = values.keys() - known
    if unknown:
        name = next(iter(unknown))
        raise TypeError(f"send_prompt() got an unexpected keyword argument {name!r}")
    overflow_retry = bool(values.pop("_overflow_retry", False))
    prompt = ReplayPrompt(
        text=text,
        model=values.pop("model", None),
        variant=values.pop("variant", None),
        parts=values.pop("parts", None),
        system_addendum=values.pop("system_addendum", None),
        context=values.pop("context", None),
    )
    return prompt, overflow_retry


#: The model-facing tool name for each background job kind (for the wake envelope).
_BACKGROUND_TOOL_NAME: dict[str, str] = {
    "agent": "spawn_agent",
    "sql": "sql.query",
    "bash": "bash",
    "integration_sdk": "call_integration_sdk",
}
#: Background job kinds that render as a SELF-CONTAINED tool card (a bgjob: running card +
#: a bgdone: finish card the UIs route by tool name) — every non-agent kind. The agent kind
#: instead folds its report onto the spawn card (SubagentCompleted).
_TOOL_CARD_BACKGROUND_KINDS = ("sql", "bash", "integration_sdk")
#: State → past-tense verb for the wake summary line.
_BACKGROUND_VERB: dict[str, str] = {
    "completed": "completed",
    "error": "failed",
    "cancelled": "cancelled",
}


def _background_tool_name(kind: str) -> str:
    """The model-facing tool name a background job renders under."""
    return _BACKGROUND_TOOL_NAME.get(kind, kind)


def _render_background_wake(
    jobs: list[tuple[BackgroundJob, str]],
    render_result: Callable[[BackgroundJob, str], str],
) -> str:
    """Compose the "Backgrounded Tool Finished" model wake for one or more finished
    jobs, each paired with the delivered bytes its finish card already rendered
    ("" when no card rendered). Each entry wraps the tool's NATIVE result (via
    ``render_result``) in a uniform envelope so the model treats it as the tool
    having finished async — NOT a flattened text dump. Pure (no I/O) so it's
    unit-testable in isolation."""

    def _one(job: BackgroundJob, delivered: str) -> str:
        tool = _background_tool_name(job.kind)
        verb = _BACKGROUND_VERB.get(job.state, job.state)
        open_tag = (
            f'<backgrounded_tool_finished job_id="{job.job_id}" tool="{tool}" status="{job.state}">'
        )
        return "\n".join(
            [
                open_tag,
                f"<summary>Background {tool} {verb}: {job.title}</summary>",
                "<result>",
                render_result(job, delivered),
                "</result>",
                "</backgrounded_tool_finished>",
            ]
        )

    if len(jobs) == 1:
        return _one(*jobs[0])
    lead = (
        "Background jobs finished while you were working — each with its own result "
        "and status below. Read each, fold its result into your work, and do not "
        "re-run any of it."
    )
    inner = "\n".join(_one(j, d) for j, d in jobs)
    return f"<backgrounded_tools_finished>\n{lead}\n{inner}\n</backgrounded_tools_finished>"


def _context_seeds_on_activation(provider: object) -> bool:
    """Whether a ``ContextProvider`` opts into offline activation-seeding. The
    ``ContextProvider`` Protocol deliberately has no ``seeds_on_activation`` method —
    adding one would make every provider that lacks it fail the runtime-checkable
    ``isinstance`` lookup and silently drop off the refresh path. So it's a structural
    opt-in: a local/offline provider EXPOSES ``seeds_on_activation()`` to be seeded the
    moment its plugin activates; a warehouse context provider doesn't, and stays on the
    refresh path (so activation never fires a billable query)."""
    fn = getattr(provider, "seeds_on_activation", None)
    return bool(callable(fn) and fn())


def _connection_offline_seed(conn: Any) -> bool:
    """Whether THIS connection opts into offline activation-seeding regardless of its
    provider's default — a fully-offline file connection (a ``.twb``/``.twbx``
    workbook, a static ``dags/`` folder) of an otherwise-live plugin. Canonical
    definition lives on :meth:`Connection.is_offline_seed`; this thin wrapper keeps
    the runtime's ``Any``-typed call sites readable."""
    return bool(conn.is_offline_seed())


def _model_slug(model: dict[str, Any] | None) -> str | None:
    """A short, human-readable slug for a resolved gateway model dict — for
    ``AgentUsageStats.model``. Prefers ``display_name``, else ``provider/model``."""
    if not model:
        return None
    display = model.get("display_name")
    if isinstance(display, str) and display:
        return display
    provider = str(model.get("provider_id", "")).strip()
    model_id = str(model.get("model_id", "")).strip()
    if provider and model_id:
        return f"{provider}/{model_id}"
    return model_id or None


# ---------------------------------------------------------------------------
# HarnessRuntime
# ---------------------------------------------------------------------------


def _alkera_mcp_block(
    url: str, headers: dict[str, str], *, claude: bool, web_url: str | None = None
) -> dict[str, Any]:
    """The MCP config block pointing a backend at the parent-hosted loopback MCP
    server the parent hosts on loopback. Same ``url`` + bearer ``headers`` for both
    backends — only the transport discriminator differs: Claude wants
    ``type:"http"`` (``McpHttpServerConfig``), OpenCode wants ``type:"remote"``.
    Returned as a ``{name: config}`` map: Claude merges it into ``mcp_servers``,
    OpenCode into its config ``mcp`` map.

    ``web_url`` (OpenCode only) adds the dedicated ``web`` mount serving the web
    tools under bare names, so the composed model-facing names are
    ``web_search``/``web_fetch`` — not ``alkera_``-prefixed. Claude never gets it:
    its native WebSearch/WebFetch already cover the web (broker-gated)."""

    def _entry(entry_url: str) -> dict[str, Any]:
        block: dict[str, Any] = {
            "type": "http" if claude else "remote",
            "url": entry_url,
            "headers": dict(headers),
        }
        if not claude:
            block["enabled"] = True
            # Without this, opencode's MCP client falls back to the SDK's 60s
            # request timeout and kills any tool call held open by a permission
            # prompt. (Claude has no per-server field — its knob is the
            # MCP_TOOL_TIMEOUT env the adapter sets, claude_agent._build_env.)
            block["timeout"] = MCP_NEVER_TIMEOUT_MS
        return block

    out = {"alkera": _entry(url)}
    if web_url is not None and not claude:
        out[WEB_MCP_MOUNT] = _entry(web_url)
    return out


@dataclass(frozen=True)
class ConnectionAnnouncement:
    """One connection changed. Carries the identity, not the row: the daemon
    rebuilds the row the way ``connection.list`` would, so a pushed badge and a
    listed badge cannot be two different derivations."""

    key: str
    plugin: str = ""
    handle: str = ""
    record_id: str = ""
    origin: str = "local"
    removed: bool = False


@dataclass(frozen=True)
class OAuthStartedAnnouncement:
    """A browser sign-in wants a window opened. The host that can open one
    (the editor) takes it; nobody else is asked to guess."""

    plugin: str
    handle: str
    url: str
    record_id: str = ""


ConnectionChangeListener = Callable[[ConnectionAnnouncement], None]
OAuthStartedListener = Callable[[OAuthStartedAnnouncement], None]


def _as_web_tool_flags(value: bool | WebToolFlags) -> WebToolFlags:
    """Normalise whatever a caller (or its flag provider) handed us.

    A plain bool predates the per-tool split and means BOTH web tools, which is
    what it got before the deployment `web.fetch` switch existed — so an older
    embedder or provider keeps working unchanged. Anything else fails closed."""
    if isinstance(value, WebToolFlags):
        return value
    if isinstance(value, bool):
        return WebToolFlags(search=value, fetch=value)
    return WebToolFlags()


class HarnessRuntime(_SchedulingMixin):
    """One per process. Manages a registry of open `ChatSession`s.

    Construction is cheap — nothing spawns. Spawning happens lazily on
    `open_chat()`. Job scheduling lives on the mixin (`runtime_scheduling`).
    """

    def __init__(
        self,
        project: ProjectDirectory,
        *,
        adapter_factory: AdapterFactory | None = None,
        gateway_config_builder: GatewayConfigBuilder | None = None,
        claude_env_builder: ClaudeEnvBuilder | None = None,
        subagent_model_resolver: SubagentModelResolver | None = None,
        safety_judge: SafetyJudge | None = None,
        subprocess_seed: bool = False,
        web_search_enabled: bool
        | WebToolFlags
        | Callable[[], Awaitable[bool | WebToolFlags]] = False,
        subagents_enabled: bool | None = None,
        audit_reporter: AuditReporter | None = None,
        otel_exporter: OtelExporter | None = None,
        system_block_overrides: Mapping[str, str] | None = None,
        connection_scope: Callable[[str], Awaitable[Iterable[str]]] | None = None,
        notebook_host_factory: notebooks.HostFactory | None = None,
    ) -> None:
        self._project = project
        self.notebooks = notebooks.service_for(notebook_host_factory, write_free_root, parse_mode)
        # Team connections a chat may see, asked per chat at open and each turn: None is
        # one person's store; a box reads per chat (its store is every org's union).
        self._connection_scope = connection_scope
        # Whether this machine's chats may delegate. None reads the setting
        # (``ALKERA_SUBAGENTS_ENABLED``, default on) at registry-build time;
        # an explicit bool is the injection seam a test or an embedding host
        # uses without touching process env.
        self._subagents_enabled = subagents_enabled
        self._chats_store = project.chats()
        self._adapter_factory = adapter_factory or AdapterFactory()
        # The org-audit reporter. None (tests, logged out, non-chat commands)
        # reports nothing. The reporter owns the decision-observer install;
        # the runtime only emits session lifecycle events.
        self._audit_reporter = audit_reporter
        # The opt-in OTel exporter (otel_export.py). Fed the same session
        # lifecycle plus, via the persist tap, every finalized tool part.
        self._otel = otel_exporter
        self._session_opened_at: dict[str, float] = {}
        self._trace_sweep_done = False
        # The auto-mode grounded safety judge (a metered gateway call). CLI +
        # daemon inject a `GatewaySafetyJudge`; None (tests / offline) → auto mode
        # degrades to the deterministic MVP (allow the recoverable write middle).
        self._safety_judge = safety_judge
        # Maps a spawned subagent's AgentDefinition → a (cheaper) child model.
        # None (tests / no gateway) → the child inherits the parent's model.
        self._subagent_model_resolver = subagent_model_resolver
        # When set (CLI + daemon pass the default builder), the runtime builds a
        # fresh gateway agent_config from the chat's pinned model + the current
        # auth token at every open, and injects it WITHOUT persisting (the token
        # must not hit the manifest). None (tests) → opencode's default config.
        self._gateway_config_builder = gateway_config_builder
        # Same discipline for the Claude Code harness: a fresh `claude_env`
        # (ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY / ANTHROPIC_MODEL) built per-open
        # from the pinned model + current token. None (tests) → the adapter's
        # lockdown defaults (no gateway routing).
        self._claude_env_builder = claude_env_builder
        # Daemon mode: run the heavy offline seed in a SUBPROCESS (`alkera lineage seed`)
        # so a spellbook-scale manifest's GIL-holding parse never blocks the daemon's
        # event loop. The CLI + the seed-subprocess worker itself leave this False (the
        # worker runs the seed in-process — that's its whole job — which also breaks the
        # would-be spawn recursion).
        self._subprocess_seed = subprocess_seed
        # The org's web-tools toggle (gates registering `web.search`/`web.fetch`).
        # A plain bool when the caller fetched the catalog up-front (CLI chat /
        # headless); an async provider when the flag isn't knowable at
        # construction (the daemon builds runtimes before auth) — consulted, and
        # memoized on success, when the tool registry is built. Default False:
        # fail-closed for callers that never see a gateway (tests, lineage/context
        # commands).
        self._web_search_enabled = web_search_enabled
        # Live seed subprocesses, tracked so close_all can terminate them.
        self._seed_procs: set[asyncio.subprocess.Process] = set()
        self._sessions: dict[str, ChatSession] = {}
        #: The interrupted turn each chat's last open found and will restart,
        #: by session id — read by the session the open builds.
        self._turn_restarts: dict[str, TurnRestart] = {}
        #: The chats whose last open found a turn this disk's log left open —
        #: the record's own mark that a turn was interrupted here.
        self._interrupted_opens: set[str] = set()
        # The per-project plugin framework. Built + activated
        # lazily on first access (discovery + activation are async); persistent
        # plugin state lives in `.alkera/`, never on this object.
        self._plugin_registry: PluginRegistry | None = None
        self._tool_registry: ToolRegistry | None = None
        self.environment = EnvironmentService()
        self._registry_init_lock = asyncio.Lock()
        # (plugin, connection) -> the artifact mtime at last seed, so an offline
        # producer (dbt manifest / DuckDB file) seeds the graph + KB cards once, then
        # RE-seeds when its artifact changes (e.g. a fresh `dbt parse`/`build`) — but
        # not on every re-activation when nothing changed.
        self._activation_seeded: dict[tuple[str, str], float] = {}
        # Serializes the (potentially heavy) offline seed so two triggers — a chat
        # open's refresh_lineage and a plugin.list — can't run two seed threads that
        # both write the lineage store at once. This is the TABLE-grain lane (+ the KB
        # cards/embed path).
        self._seed_lock = asyncio.Lock()
        # The COLUMN-grain lane — a SEPARATE lock so the global ``lineage_column`` drain
        # runs ALONGSIDE a table refresh (one of each at a time, never two of either)
        # instead of queuing behind it. The point of the table/column split: table lineage
        # lands fast while the slow sqlglot column pass churns in the background.
        self._lineage_column_lock = asyncio.Lock()
        # Background offline-seed tasks kicked by chat-open (kept referenced so they
        # aren't GC'd mid-run; cancelled on close). chat-open never AWAITS the seed —
        # the graph fills in while the user chats.
        self._seed_tasks: set[asyncio.Task[None]] = set()
        # The per-project daemon scheduler. Built lazily; its beat is
        # driven by the daemon startup hook (one tick/sec across all runtimes).
        self._scheduler: Scheduler | None = None
        # The CLI/TUI scheduler beat task (the daemon drives the beat itself, so this stays
        # None there). Started by ``start_scheduler_beat`` on chat open; cancelled in close_all.
        self._beat_task: asyncio.Task[None] | None = None
        # The project file watcher (event-driven lineage/KB refresh). Rebuilt + restarted when
        # the connection set changes; stopped in close_all. None until there's something to watch.
        self._file_watcher: ProjectFileWatcher | None = None
        # A restart stops the current watcher before it starts the next one. Two restarts
        # in flight at once — the beat's first pass and a connection mutation — would each
        # find no watcher to stop, start one each, and keep only the last: the other watches
        # on with nobody holding it, and holds its worker thread for the rest of the process.
        self._file_watcher_lock = asyncio.Lock()
        # Throttle connection RE-discovery (a workspace tree-walk) on the file-watcher's
        # per-edit callback: new connections only come from file add/remove, so re-walking on
        # every keystroke-save is wasteful — a newly-dropped artifact still surfaces within
        # ``_REDISCOVER_MIN_INTERVAL_S``.
        self._last_rediscover_at: float = 0.0
        # The append-only permission-decision audit log, built lazily + shared.
        self._decision_sink: DecisionSink | None = None
        # Per-name replacements for the main-agent guidance blocks, applied at every
        # root turn's compose. None (every production caller) → the shipped text. A
        # prompt-evolution benchmark substitutes one block per run through this.
        self._system_block_overrides = system_block_overrides
        # Who to tell when a connection's row changes. Empty for the CLI, one
        # forwarder per project for the daemon; the store's own writers (a query
        # that failed, the credential sweep) reach these through the state
        # store's listener rather than through a runtime they do not have.
        self._connection_listeners: list[ConnectionChangeListener] = []
        self._oauth_listeners: list[OAuthStartedListener] = []
        self._state_unsubscribe: Callable[[], None] | None = None
        # One mutation, one announcement per row. A mutation both writes the
        # state store (which announces on its own, from whatever thread wrote it)
        # and announces the row it produced, and an editor told twice that the
        # same row changed renders it twice — the second time from a read it did
        # not need. While the depth is non-zero, announcements collect here and
        # are emitted once each when the mutation finishes.
        self._announce_depth = 0
        self._announce_pending: dict[str, bool] = {}

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def project(self) -> ProjectDirectory:
        return self._project

    @property
    def system_block_overrides(self) -> Mapping[str, str] | None:
        """Per-name replacements for the main-agent guidance blocks, or None."""
        return self._system_block_overrides

    @property
    def decision_sink(self) -> DecisionSink:
        """Project-level decision log (``.alkera/decisions.jsonl``) — the fallback
        for decisions made OUTSIDE a chat (e.g. a non-session ``tool.call``). A
        chat's own decisions go to its per-chat log (``ChatSession.decision_sink``)."""
        if self._decision_sink is None:
            from alkera_cli.plugins.plugin_base.permissions import DecisionSink

            self._decision_sink = DecisionSink(self._project.path)
        return self._decision_sink

    @property
    def workspace_root(self) -> Path:
        """The workspace dir that contains `.alkera/` — what plugins scan for
        activation signals."""
        return self._project.path.parent

    # ------------------------------------------------------------------
    # Plugin framework
    # ------------------------------------------------------------------

    async def plugin_registry(self) -> PluginRegistry:
        """The per-project `PluginRegistry`, discovered + activated once."""
        # Lock + re-check: discovery awaits, so two concurrent daemon RPCs
        # (open_chat + tool.call) would otherwise both see None and run a full
        # discovery/activation each — with an earlier caller left dispatching
        # through the losing registry instance.
        async with self._registry_init_lock:
            if self._plugin_registry is None:
                from alkera_cli.plugins.plugin_base import PluginRegistry as _Registry
                from alkera_cli.plugins.plugin_base.plugin import WorkspaceEvent

                registry = _Registry(self._project, self.workspace_root)
                await registry.discover()
                # Activation walks the workspace tree (filesystem) — run it OFF the
                # event loop so a large workspace can't starve the daemon heartbeat
                # (the same reason the offline seed runs in a worker thread).
                await asyncio.to_thread(
                    self._evaluate_activation_blocking,
                    registry,
                    WorkspaceEvent(kind="open", workspace_root=self.workspace_root),
                )
                self._plugin_registry = registry
                self._tool_registry = None  # rebuild the tool view after (re)discovery
                # NOTE: the offline lineage/context SEED is deliberately NOT run here —
                # building the registry must stay fast (it's on the chat-open path). The
                # seed runs in the BACKGROUND via the scheduler beat's refresh jobs (+ the
                # file watcher on edits), so a huge project never blocks the first chat.
                # Register those refresh jobs now (cheap); pass the local registry to
                # avoid re-entering plugin_registry() under the init lock.
                with contextlib.suppress(Exception):
                    await self.schedule_refresh(registry)
                # With this project's refresh runners now registered, drop any persisted
                # job whose kind no longer has one (e.g. after a kind rename) so it stops
                # cluttering the Jobs UI. The KB kinds are
                # protected — they register on the daemon's context-jobs path (or another
                # process), so absence of a runner here isn't proof they're orphaned.
                with contextlib.suppress(Exception):
                    self.scheduler().prune_orphans(protected=set(protected_kinds()))
            return self._plugin_registry

    @staticmethod
    def _evaluate_activation_blocking(registry: PluginRegistry, event: Any) -> list[Any]:
        """Run plugin activation in a worker thread, driving the async pass with its
        own loop so the workspace tree-walk inside it never blocks the daemon event
        loop. The providers it calls do only synchronous local work."""
        return asyncio.run(registry.evaluate_activation(event))

    async def team_connections_changed(self) -> None:
        """The team-connections store moved under this runtime: a sync this
        runtime did not drive (the cloud box's schema-card loader, the daemon's
        cloud-sync beat) rewrote it. Bring the agent's surface up to date.

        The project's registry is built once per runtime and every open chat
        views it, so a row that arrived after a chat opened was invisible to it
        for the rest of its life — a box that boots and opens its chats before
        the first sync lands served every one of them off an EMPTY store. Re-run
        activation (a plugin whose only connection is the team's becomes active
        here), rebuild the registry, and point every open session's binding at
        its own view of the rebuilt one — derived from the flags that session
        last resolved as its chat, never from a flag read as the machine: the
        loopback MCP server reads the binding on each call, so the next turn
        lists and dispatches the new set with no reopen.
        """
        from alkera_cli.plugins.plugin_base.plugin import WorkspaceEvent

        registry = await self.plugin_registry()
        await asyncio.to_thread(
            self._evaluate_activation_blocking,
            registry,
            WorkspaceEvent(kind="file_change", workspace_root=self.workspace_root),
        )
        self._tool_registry = None
        await self._resync_jobs_after_mutation()
        for session in list(self._sessions.values()):
            binding = session.tool_binding
            if binding is not None:
                binding.registry = await self.tool_registry_for(
                    binding.web_tools,
                    connection_ids=binding.connection_ids,
                    knowledge_owner=binding.knowledge_owner,
                )

    async def _resolve_connection_scope(self, chat_id: str | None) -> frozenset[str] | None:
        """The team-record ids ``chat_id`` may see, resolved fresh on EVERY
        call, or ``None`` on a runtime with no scope (every connection in the
        store is the one person's). With a scope and no chat — a session-less
        read — nothing is in it: a connection is only ever a chat's. A read
        that fails is an empty scope for this call: a chat must open and a
        turn must run when the API is down, and fail-closed is the direction
        for a gate on which org's warehouses a chat may name; the next read
        that succeeds restores the set, and nothing wider is remembered."""
        scope = self._connection_scope
        if scope is None:
            return None
        if not chat_id:
            return frozenset()
        try:
            return frozenset(str(record_id) for record_id in await scope(chat_id))
        except Exception:
            logger.debug(
                "connection scope for chat %s could not be read; none for now",
                chat_id,
                exc_info=True,
            )
            return frozenset()

    def _bind_chat(self) -> ProfileBinding | None:
        """The sign-in a local chat opening now keeps for its life (None when
        signed out). Refuses (``ProfileResolutionError``) when this project is
        pinned to another org than every usable profile."""
        return ProfileBinding.bind(project=self._project)

    async def _resolve_web_search_flag(self, credential: str | None = None) -> WebToolFlags:
        """The org's web-tools toggle as ``credential`` sees it, resolved fresh
        on EVERY call. A bool passes through; a provider (the gateway read) is
        awaited each time and NOT cached — a chat resolves it as it opens and
        at the start of each turn, as its own credential, so on a box that
        serves several orgs no chat ever reads another org's answer, and an
        admin who disables web access sees it drop on the next turn rather than
        at the process's next restart. Any provider failure reads as disabled:
        a chat must open and a turn must run even when the gateway is down, and
        fail-closed is the safe direction for a capability gate."""
        flag = self._web_search_enabled
        if not callable(flag):
            return _as_web_tool_flags(flag)
        try:
            return _as_web_tool_flags(await bound_to(flag, credential)())
        except Exception:
            logger.debug("web-search flag provider failed; leaving web tools off", exc_info=True)
            return WebToolFlags()

    @property
    def subagents_enabled(self) -> bool:
        """Whether chats on this runtime may spawn subagents. An explicit
        constructor value wins; otherwise the CLI setting decides, read here
        rather than at construction so the daemon picks up the env it was
        launched with even when it built the runtime first."""
        if self._subagents_enabled is not None:
            return self._subagents_enabled
        from alkera_cli.host.config import get_settings

        return get_settings().alkera_subagents_enabled

    async def tool_registry(
        self, *, credential: str | None = None, chat_id: str | None = None
    ) -> ToolRegistry:
        """The active project's `ToolRegistry` (hot meta-tools + searchable
        catalog) as ``credential`` may see it. ``credential`` is the chat's own
        token (its bound profile's, or a cloud chat's); with none the flags
        read nothing. The org web-tool flags are read as it, on this call,
        and the registry returned is a view with what that org withholds taken
        out. ``chat_id`` is the chat whose connection scope the view carries;
        on a runtime with a scope and no chat named, the view holds no
        connection. The build behind the view is shared by the runtime and
        knows no org (:meth:`_shared_tool_registry`), so nothing one caller
        resolved is ever cached for the next."""
        return await self.tool_registry_for(
            await self._resolve_web_search_flag(credential),
            connection_ids=await self._resolve_connection_scope(chat_id),
        )

    async def tool_registry_for(
        self,
        web_tools: WebToolFlags,
        *,
        connection_ids: frozenset[str] | None = None,
        knowledge_owner: str = "",
    ) -> ToolRegistry:
        """The project's registry as a chat whose org flags read ``web_tools``
        and whose connection scope is ``connection_ids`` sees it: the shared
        build, less the web tools those flags withhold, with only the team
        connections in scope (every connection, when the scope is ``None``),
        and the knowledge store read as ``knowledge_owner``'s chat."""
        from alkera_cli.plugins.plugin_base.web_tools import withheld_web_tools

        base = await self._shared_tool_registry()
        return base.restricted(
            withheld_web_tools(search=web_tools.search, fetch=web_tools.fetch),
            connection_ids=connection_ids,
            knowledge_owner=knowledge_owner,
        )

    async def _shared_tool_registry(self) -> ToolRegistry:
        """The one registry this runtime builds from the activated plugins, with
        every tool any chat on it may be given — the web tools included, whatever
        any org's toggle says. It is built once and shared, which is exactly why
        it must carry nothing that is one org's: a chat's org decides what is
        withheld from that chat's VIEW (:meth:`tool_registry_for`), never what
        the shared build contains."""
        if self._tool_registry is None:
            registry = await self.plugin_registry()
            if self._tool_registry is None:  # may have been built while awaiting
                # Hand the lineage tools a live reader of this runtime's memoized
                # scheduler jobs so they can wait out an in-flight refresh before
                # querying a possibly-stale graph (list_jobs re-reads disk each call).
                jobs_snapshot = self.scheduler().list_jobs
                # Parsing every plugin's snapshot files stays off the event loop.
                self._tool_registry = await asyncio.to_thread(
                    registry.tool_registry,
                    lineage_jobs_snapshot=jobs_snapshot,
                    web_search_enabled=True,
                    web_fetch_enabled=True,
                    subagents_enabled=self.subagents_enabled,
                )
                self._tool_registry.environment = self.environment  # every view carries it
                notebooks.install(self._tool_registry, self.notebooks)
        return self._tool_registry

    async def refresh_lineage(
        self,
        only: str | None = None,
        *,
        only_plugin: str | None = None,
        force: bool = False,
        grain: str = "all",
        tier: str = "all",
    ) -> None:
        """Re-seed the offline lineage from the current artifacts — picks up a regenerated dbt
        manifest / a changed ``.duckdb`` without a daemon restart. Mtime-gated, so it's a cheap
        no-op when nothing changed. Best-effort. ``only`` restricts the seed to a single
        connection handle. ``force`` (an explicit Run-now / ``--force``) re-seeds even an
        unchanged artifact so a manual trigger does visible work.

        ``grain`` selects the lineage grain (the ``alkera lineage seed --grain`` worker passes
        it): ``table`` (fast, per-connection emit + reconcile), ``column`` (the slow GLOBAL
        sqlglot drain — ignores ``only``), or ``all`` (both — the CLI one-shot)."""
        try:
            await self._seed_on_activation(
                await self.plugin_registry(),
                only=only,
                only_plugin=only_plugin,
                force=force,
                grain=grain,
                tier=tier,
                refresh=True,
            )
        except Exception as exc:  # non-fatal by contract, but never silent
            # Swallowing this whole made an empty graph indistinguishable from a
            # successful one; the seed stays best-effort, the reason does not.
            hints = self.lineage_hints()
            logger.warning(
                "lineage refresh failed (only=%s plugin=%s grain=%s tier=%s): %s%s",
                only,
                only_plugin,
                grain,
                tier,
                exc,
                "".join(f"; {hint}" for hint in hints),
                exc_info=True,
            )

    def lineage_hints(self) -> list[str]:
        """One line per shared connection this workspace cannot read yet.

        A seed that found no live connection reports zero dependencies and zero
        column edges, which is also what a genuinely empty graph reports. When
        the cause is a shared connection the member has not accepted (or not
        finished signing in), these lines say so — and name the handle — so the
        caller can print the difference instead of an unqualified success."""
        records = connection_records()
        return records.pending_hints(self._project) if records is not None else []

    async def _on_workspace_change(self, handles: set[str], kb_dirty: bool) -> None:
        """The file-watcher's callback. FIRST re-discover connections so a newly-dropped artifact
        (a Tableau workbook, a ``.duckdb``, a dbt project) goes live WITHOUT an IDE restart — then
        the usual gated per-connection lineage + repo-wide KB refresh. Re-discovery is throttled
        (it walks the tree) and runs on the loop (NOT a thread — a worker mutating the registry's
        sets while RPCs read them would race; a throttled bounded walk is the safer trade). Never
        raises into the watcher loop."""
        from alkera_cli.plugins.plugin_base.plugin import WorkspaceEvent

        now = time.monotonic()
        if now - self._last_rediscover_at >= _REDISCOVER_MIN_INTERVAL_S:
            self._last_rediscover_at = now
            with contextlib.suppress(Exception):
                registry = await self.plugin_registry()
                ev = WorkspaceEvent(kind="file_change", workspace_root=self.workspace_root)
                # The registry publishes its new candidate map in one assignment
                # when the walk is done, so an RPC reading the set mid-walk sees
                # the old one whole rather than a half-replaced one.
                if await registry.rediscover(ev):  # a connection appeared/vanished
                    self._tool_registry = None  # rebuild the agent's tool surface
                    with contextlib.suppress(Exception):
                        await self.schedule_refresh()  # register the new connection's refresh job
                    with contextlib.suppress(Exception):
                        # A connection that appears mid-session gets its cards indexed.
                        self.nudge_standing_jobs("connections_changed")
        await self.trigger_connection_refresh(handles, kb_dirty)

    async def trigger_connection_refresh(self, handles: set[str], kb_dirty: bool) -> None:
        """A watched file changed — nudge the affected refresh(es) via the GATED path
        (``reschedule_soon``, NOT forced): an unchanged source is a cheap no-op, a real change
        re-parses/re-ingests. The beat (daemon subprocess / CLI in-process) runs them.

        ``handles`` = connections whose own source file changed → refresh their LINEAGE (ensures
        each refresh job exists first; ``schedule_refresh`` is idempotent, so a not-yet-scheduled
        connection is SPAWNED). ``kb_dirty`` = a repo-tracked file changed → re-seed the KB, which
        indexes the WHOLE repo (not just connection files) — so a plain source edit refreshes the
        KB even with no connection involved (the per-source fingerprint gate skips unchanged cards).

        A change arriving mid-run leaves that job running (``reschedule_soon`` → None); it's picked
        up by the next file event or the cadence — the watcher debounces bursts, so this is the
        rare single-edit-during-a-run case."""
        if not handles and not kb_dirty:
            return
        sched = self.scheduler()
        if handles:
            # Spawn any missing connection refresh jobs (idempotent; cheap once the registry is
            # built) so a connection the user just added is covered too.
            with contextlib.suppress(Exception):
                await self.schedule_refresh()
            sched = self.scheduler()
            for job in sched.list_jobs():
                if job.payload.get("connection") in handles:
                    sched.reschedule_soon(job.job_id)  # this connection's table-grain refresh
            # The GLOBAL column drain is NOT nudged here: that would race the table refresh and
            # parse a half-emitted relation set. It's kicked from the refresh's on_seeded callback
            # instead — once ALL table refreshes finish — so it always sees the complete SQL.
        if kb_dirty:
            self.nudge_standing_jobs("files_changed")

    async def restart_file_watcher(self) -> None:
        """(Re)build the project file watcher from the CURRENT connections + start it: a change to
        a connection's source file triggers its GATED lineage refresh, and a change to ANY
        repo-tracked file triggers the whole-repo KB re-seed (the watcher watches the workspace
        root for that). Restarted on every connector mutation because the watched dirs move when
        connections are added/removed. Best-effort — a watcher failure never blocks seeding. Same
        in the daemon and the CLI; must run within an event loop."""
        from alkera_cli.harness.file_watcher import ProjectFileWatcher

        async with self._file_watcher_lock:
            if self._file_watcher is not None:
                await self._file_watcher.stop()
                self._file_watcher = None
            with contextlib.suppress(Exception):
                registry = await self.plugin_registry()
                conns = [*registry.detected_connections(), *registry.active_connections()]
                watcher = ProjectFileWatcher(
                    conns, self._on_workspace_change, workspace_root=self.workspace_root
                )
                if watcher.start_async() is not None:  # None only if there's nothing to watch
                    self._file_watcher = watcher

    async def _spawn_seed_subprocess(
        self,
        plugin: str,
        conn_handle: str,
        job_id: str,
        forced: bool = False,
        metered: bool = False,
    ) -> None:
        """Build ONE connection's TABLE-grain lineage in a separate ``alkera lineage seed
        --grain table`` process so the manifest parse never touches the daemon's interpreter.
        The caller holds the table-grain lock, so at most one table worker runs at a time
        (bounded memory) — but a column drain runs alongside it on its own lane. ``forced`` (an
        explicit Run-now) passes ``--force`` so the worker re-emits even when unchanged.
        ``metered`` selects the cost tier: the automatic beat runs ``--tier free`` (never a billed
        warehouse query); the manual metered job runs ``--tier metered``.

        ``--plugin`` travels with ``--connection`` so the worker re-resolves by the FULL
        (plugin, handle) identity: two plugins can share a handle (a ``.duckdb`` file + a
        same-named warehouse), and a bare-handle re-resolve in the worker could pick the WAREHOUSE
        and fire a billed query on this FREE/offline beat (money-safety)."""
        cmd = ["lineage", "seed", "--grain", "table", "--project", str(self.workspace_root)]
        cmd += ["--tier", "metered" if metered else "free"]
        if conn_handle:
            cmd += ["--connection", conn_handle]
        if plugin:
            cmd += ["--plugin", plugin]
        if forced:
            cmd.append("--force")
        await self._run_seed_worker(cmd, label=f"lineage-table/{conn_handle}", job_id=job_id)

    async def _spawn_column_subprocess(self, job_id: str) -> None:
        """Drain the GLOBAL column grain in a separate ``alkera lineage seed --grain column``
        process so the spellbook-scale sqlglot pass never blocks the daemon interpreter. The
        caller (the ``lineage_column`` runner) holds the COLUMN lane, separate from the table
        lane, so this runs alongside a table refresh. No ``--connection`` (the parse is global)
        and no ``--force`` (the parse marks already make it incremental)."""
        cmd = ["lineage", "seed", "--grain", "column", "--project", str(self.workspace_root)]
        await self._run_seed_worker(cmd, label="lineage-column", job_id=job_id)

    async def _spawn_kb_subprocess(self, job_id: str, forced: bool = False) -> None:
        """Index the codebase KB in a separate ``alkera context seed`` process so a huge
        repo's walk + embed never blocks the daemon's interpreter. ``forced`` (an explicit
        Run-now) passes ``--force`` so the provider context re-ingests even when unchanged.

        DELIBERATELY not under the lineage ``_seed_lock``: the KB and lineage seeds write
        DISJOINT stores, so the worst case is one KB worker + one lineage worker at once
        (~bounded memory) — and sharing the lock would head-of-line-block lineage behind a
        long KB embed (or vice versa). The scheduler runs at most one kb_seed at a time,
        and the lineage workers serialize among themselves, so the fleet stays bounded."""
        cmd = ["context", "seed", "--project", str(self.workspace_root)]
        if forced:
            cmd.append("--force")
        await self._run_seed_worker(cmd, label="kb", job_id=job_id)

    async def _run_seed_worker(
        self, sub_cmd: list[str], *, label: str, job_id: str | None = None
    ) -> None:
        """Run a seed in a child ``alkera`` process — the daemon's escape hatch so a
        spellbook-scale parse/embed never touches its interpreter (the SQLite + KB stores
        are process-safe). Re-invokes THIS same entrypoint (``sys.argv[0]``); the worker
        runs in-process (``subprocess_seed`` False), so there's no spawn recursion.
        Best-effort: a failed/timed-out worker is logged, never raised into the beat. The
        store is durable, so a killed worker just resumes next run."""
        cmd = [_alkera_entrypoint(), *sub_cmd]
        # Tie the worker's life to ours: macOS has no OS parent-death signal, so the worker
        # watches this PID and exits if we die (else it would keep parsing as an orphan and
        # race the next daemon's seed of the same store). See alkera_cli.host.parent_watchdog.
        env = {**os.environ, SEED_PARENT_PID_ENV: str(os.getpid())}
        # Deterministic column lineage: sqlglot's ambiguous-column resolution is
        # hash-seed-dependent, so pin a fixed PYTHONHASHSEED for the seed worker AND the
        # parse subprocesses it spawns (they inherit this env) — otherwise each run, and
        # each parallel worker, resolves a few column edges differently and the graph stops
        # being reproducible. Respects an explicitly-set PYTHONHASHSEED.
        env.setdefault("PYTHONHASHSEED", "0")
        # The worker reports progress to this job's sidecar, which scheduler.list merges.
        if job_id is not None:
            from alkera_cli.plugins.plugin_base.progress import PROGRESS_FILE_ENV
            from alkera_cli.plugins.plugin_base.scheduler import progress_sidecar_path

            env[PROGRESS_FILE_ENV] = str(
                progress_sidecar_path(self._project.scheduler_path, job_id)
            )
        try:
            proc = await spawn_async(SpawnSpec(argv=cmd, env=env, stdout="devnull", stderr="pipe"))
        except Exception:
            logger.warning("seed subprocess (%s) failed to spawn: %s", label, cmd, exc_info=True)
            return
        self._seed_procs.add(proc)
        try:
            # Bounded so a hung worker can't pin a slot forever; the store is durable, so
            # killing it loses nothing (the next run resumes from the marks).
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_SEED_SUBPROCESS_TIMEOUT)
            if proc.returncode != 0:
                tail = (stderr or b"").decode("utf-8", "replace")[-2000:]
                logger.warning("seed subprocess (%s) exited %s: %s", label, proc.returncode, tail)
        except TimeoutError:
            logger.warning("seed subprocess (%s) timed out — killing", label)
        finally:
            # Drop the tracked handle FIRST so it's released even if the kill/wait below is
            # itself cancelled (a cancelled run task re-raises CancelledError out of
            # ``await proc.wait()``) — otherwise the set would leak a dead proc forever.
            self._seed_procs.discard(proc)
            # Kill the worker if it's still alive on ANY exit (timeout, or the run task was
            # cancelled — e.g. the connection was removed / plugin disabled mid-seed) so it
            # never orphans. A normal completion already has a returncode → no-op.
            if proc.returncode is None:
                # The worker and the parse processes it started. BOUND the reap: an
                # unbounded wait on a tree that won't die promptly would hold the
                # caller's seed lock indefinitely, leaving the next lineage job stuck on
                # "Waiting for another lineage job to finish…". After the bound we give
                # up waiting (the OS reaps the orphan) so the lock is released, losing
                # nothing, since the store is durable.
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(
                        kill_tree_async(proc, grace=0), timeout=_SEED_KILL_TIMEOUT
                    )

    async def _seed_on_activation(
        self,
        registry: PluginRegistry,
        *,
        only: str | None = None,
        only_plugin: str | None = None,
        do_lineage: bool = True,
        do_context: bool = False,
        force: bool = False,
        grain: str = "all",
        tier: str = "free",
        refresh: bool = False,
    ) -> None:
        """Offline-safe seed of the lineage GRAPH and/or the KB context CARDS. Only
        providers that opt into activation-seeding run: the offline/local ones (dbt
        manifest, DuckDB file), never a warehouse query on mere activation.

        ``do_lineage`` / ``do_context`` select the engine — they run as SEPARATE workers
        (the lineage worker never loads the embedder, so the graph fills fast regardless of
        KB size). Each (plugin, connection, engine) seeds once per ARTIFACT VERSION: a
        re-call re-seeds only the connections whose dbt manifest / ``.duckdb`` changed
        since, and is otherwise a cheap no-op. Best-effort. ``only`` restricts the pass to
        a single connection handle (the per-connection subprocess worker).

        ``grain`` (lineage only): ``table`` builds the per-connection table grain on the table
        lane; ``column`` drains the GLOBAL sqlglot column grain on the column lane (no
        per-connection work-list — it parses the whole store); ``all`` does both."""
        from alkera_cli.plugins.plugin_base.surfaces import (
            ContextProvider,
            LineageProvider,
            provider_metered,
        )

        seeds = workspace_seeder()  # a build with no seeder has nothing to seed
        if seeds is None:
            return

        # Cost tier: the automatic paths pass "free" so a billed source (Snowflake
        # ACCESS_HISTORY, BigQuery JOBS, Databricks system.access) is NEVER queried on the
        # cadence; "metered" is the explicit manual trigger; "all" the CLI one-shot.
        def _in_tier(provider: object) -> bool:
            if tier == "all":
                return True
            is_metered = provider_metered(provider)
            return is_metered if tier == "metered" else not is_metered

        # Column grain is GLOBAL (the parse reads the whole store, not one connection), so it
        # skips the per-connection work-list / mtime gate entirely and drains on its own lane —
        # this is the ``lineage_column`` singleton's in-process path (+ ``--grain column``).
        if do_lineage and not do_context and grain == "column":
            await seeds.seed_column(self._project, lock=self._lineage_column_lock)
            return

        # Dedup detected + added by connection identity (Connection __eq__/__hash__),
        # preserving order — the same connection is never seeded twice. CRITICAL: a
        # DETECTED-but-not-ADDED LIVE warehouse (e.g. a ``redshift_env`` discovered from
        # ``REDSHIFT_*`` env vars) is EXCLUDED — seeding it (even on a refresh) would fire a
        # live, possibly billed query on a connection the human never opted into. Only ADDED
        # connections (explicit allow-list) and DETECTED OFFLINE files (dbt manifest, ``.duckdb``,
        # Tableau workbook, Airflow ``dags/``) are safe to seed automatically. Same human-in-the-
        # loop guard as the background refresh's ``_refreshable_connections``.
        # Membership keyed by the FULL (plugin, handle) identity — NOT the bare handle. A
        # bare-handle test is a MONEY-SAFETY hole: a merely-DETECTED LIVE warehouse that shares
        # a handle with an ADDED offline file (data.duckdb added + a data-named warehouse only
        # detected) would test "added" and get seeded — firing a billed query on a connection the
        # human never opted into. Same human-in-the-loop guard as the refresh runner.
        # ACTIVE = the member's own allow-list + the live team/org Preconfigured
        # lane — an accepted/authorized team connection seeds like any add (the
        # cost TIER gate above still keeps metered providers off the free beat).
        added_ids = {(c.plugin, c.handle) for c in registry.active_connections()}
        candidates = dict.fromkeys(
            c
            for c in [*registry.detected_connections(), *registry.active_connections()]
            if (c.plugin, c.handle) in added_ids or _connection_offline_seed(c)
        )
        if only is not None:
            # Narrow to the one connection the worker was spawned for — by the FULL (plugin, handle)
            # identity when the plugin is known (the daemon's --plugin), NOT the bare handle. A
            # bare-handle narrow is a MONEY-SAFETY hole: an OFFLINE file's free beat would also keep
            # a same-handle LIVE warehouse here and seed it (a billed query). With no plugin (a
            # legacy/CLI call), fall back to the handle ONLY when unambiguous — refuse to guess
            # (and possibly bill) when several connections share it.
            if only_plugin:
                candidates = {
                    c: None for c in candidates if c.plugin == only_plugin and c.handle == only
                }
            else:
                matches = [c for c in candidates if c.handle == only]
                candidates = {matches[0]: None} if len(matches) == 1 else {}

        # The mtime gate is PER-ENGINE: lineage and context seed the same artifact into
        # different stores, so one mustn't gate the other.
        engine = f"{'L' if do_lineage else ''}{'C' if do_context else ''}"
        # First, on the event loop and cheap: decide WHICH connections to seed —
        # mtime-gate only (re-seed a changed artifact). No size cap: a project of ANY
        # size seeds; the heavy work is off-loop + the column re-parse is incremental, so
        # a huge manifest no longer freezes the daemon. No parsing/IO beyond a stat here.
        work: list[tuple[Any, list[Any], list[Any]]] = []
        for conn in candidates:
            key = (conn.plugin, f"{conn.handle}:{engine}")
            # Re-seed whenever the connection's artifact mtime DIFFERS from the last seed
            # (or it was never seeded). Compare by inequality, NOT ``>=``: a `git checkout`
            # or a restore-from-backup can move an artifact's mtime BACKWARD while changing
            # its content, and a ``>=`` gate would wrongly skip that as "already seen". A
            # no-artifact connection has mtime 0.0 → seeds once (0.0 == 0.0 thereafter).
            # ``force`` (an explicit Run-now) bypasses this in-memory mtime gate so a manual
            # trigger re-seeds even an unchanged artifact (the persistent fingerprint gate
            # downstream is bypassed too, via drive_seed(force=...)).
            mtime = artifact_mtime(conn)
            if not force and self._activation_seeded.get(key) == mtime:
                continue
            # A provider seeds on activation when its plugin opts in OR when THIS
            # connection does (an offline file connection of an otherwise-live plugin,
            # e.g. a Tableau .twb/.twbx workbook). Protocols passed for structural
            # isinstance lookup, not instantiation.
            conn_offline = _connection_offline_seed(conn)
            # A REFRESH (cadence / manual / CLI) runs ALL of this connection's providers in its
            # tier — including a warehouse's view-def lineage that doesn't seed on mere
            # activation. ACTIVATION (refresh=False) stays gated to the offline/activation
            # providers so enabling a plugin never queries a live warehouse. The tier filter
            # (free vs metered) sits on top of both, so a billed source only ever runs in the
            # explicit metered pass.
            lineage_providers = [
                p
                for p in registry.plugin_providers(conn.plugin, LineageProvider)  # type: ignore[type-abstract]
                if (refresh or p.seeds_on_activation() or conn_offline) and _in_tier(p)
            ]
            context_providers = [
                p
                for p in registry.plugin_providers(conn.plugin, ContextProvider)  # type: ignore[type-abstract]
                if (refresh or _context_seeds_on_activation(p) or conn_offline) and _in_tier(p)
            ]
            if not lineage_providers and not context_providers:
                continue
            self._activation_seeded[key] = mtime
            work.append((conn, lineage_providers, context_providers))

        if not work:
            return
        # Then, OFF the event loop and serialized: the heavy work. ``drive_seed`` holds
        # ``self._seed_lock`` (shared with the refresh beat so seeds never overlap), writes
        # each connection's facts one worker-thread hop at a time, then drains the budgeted
        # derived layer (column grain for lineage; vector backfill for context) — yielding
        # to the loop between each so a huge project never starves the daemon heartbeat.
        if do_lineage and not do_context and grain == "table":
            # Table grain only (the per-connection ``--grain table`` worker): fast emit +
            # reconcile on the table lane; the column singleton fills column grain after.
            await seeds.seed_table(registry, self._project, work, lock=self._seed_lock, force=force)
        else:
            # ``grain == "all"`` (CLI one-shot lineage) or the KB context path. Pass BOTH lanes
            # so the column phase uses its own lock and the table phase stays unblocked.
            await seeds.seed(
                registry,
                self._project,
                work,
                lock=self._seed_lock,
                column_lock=self._lineage_column_lock,
                do_lineage=do_lineage,
                do_context=do_context,
                force=force,
            )

    async def refresh_context(self, *, force: bool = False) -> None:
        """Seed the KB (the CONTEXT engine) — separate from lineage so a huge embed never
        blocks the graph. Writes the file-derived cards (``seed_from_codebase``) AND the
        plugin context-provider cards with the embedding DEFERRED (text usable now), then
        backfills the vectors incrementally. Runs in the KB worker (``alkera context seed``
        / the daemon's kb_seed subprocess), never on the lineage path. Best-effort. ``force``
        (an explicit Run-now / ``--force``) re-ingests the provider cards even when the
        source is unchanged so a manual trigger does visible work."""
        for provider in HARNESS_CONTEXT_PROVIDERS.items():
            if provider.seed_workspace is None:
                continue
            with contextlib.suppress(Exception):
                # File-derived KB cards — deferred embed (the vectors backfill below).
                await asyncio.to_thread(provider.seed_workspace, self._project)
        with contextlib.suppress(Exception):
            # Plugin context-provider cards (dbt models, DuckDB tables) + the budgeted
            # vector backfill for EVERY pending card (file + provider).
            await self._seed_on_activation(
                await self.plugin_registry(), do_lineage=False, do_context=True, force=force
            )
        with contextlib.suppress(Exception):
            # ALWAYS backfill pending vectors, even when nothing above changed: a worker
            # killed mid-embed (IDE closed) leaves cards written-but-unembedded, and the
            # seeds above are change-gated so they won't re-trigger. The drain is the
            # crash-resume seam — zero-vector == pending, so it resumes exactly where it
            # stopped and is a cheap no-op once every card is embedded.
            if (seeds := workspace_seeder()) is not None:
                await seeds.drain_embeddings(self._project)

    def scheduler(self) -> Scheduler:
        """The per-project daemon `Scheduler` over `.alkera/scheduler/`. Plugins
        register their job runners onto it (refresh providers); the daemon
        beat ticks it. Cheap + synchronous — no plugin discovery needed."""
        if self._scheduler is None:
            from alkera_cli.plugins.plugin_base.scheduler import Scheduler, SchedulerStore

            self._scheduler = Scheduler(SchedulerStore(self._project.scheduler_path))
        return self._scheduler

    async def beat_tick(self) -> list[str]:
        """One scheduler beat for THIS runtime: self-heal the KB runner registration (a
        one-off bind miss would otherwise skip a ``kb_*`` job forever), then claim + run every
        due job. The reusable per-runtime tick shared by the daemon's multi-runtime beat and
        the CLI's own single-runtime beat (so the CLI is just another frontend over the same
        scheduler). Returns the job ids started this tick."""
        with contextlib.suppress(Exception):
            self.ensure_context_runners()
        return await self.scheduler().tick()

    def start_scheduler_beat(self, *, interval_seconds: float = 1.0) -> None:
        """Run the scheduler BEAT for this runtime in the background — the CLI/TUI equivalent
        of the daemon's server-level beat, so the SAME machinery (the primed-due KB +
        connection-refresh jobs seed on open and stay fresh, and the file watcher's
        ``reschedule_soon`` nudges take effect) runs in-process here. The CLI is just another
        frontend over the scheduler.

        A NO-OP in daemon mode (``subprocess_seed``): the daemon already ticks every open
        runtime, so a per-runtime beat would double-tick. Idempotent — one beat per runtime.
        Must be called from within a running event loop (the TUI's ``on_mount``)."""
        if self._subprocess_seed:
            return
        if self._beat_task is not None and not self._beat_task.done():
            return
        # Register the standing jobs (KB + connection refresh) up front so the beat has
        # something to run; both prime themselves due-now → they seed on this open, then recur.
        with contextlib.suppress(Exception):
            self.schedule_context_jobs()

        async def _loop() -> None:
            with contextlib.suppress(Exception):
                await self.schedule_refresh()  # async: needs the plugin registry
            with contextlib.suppress(Exception):
                await self.restart_file_watcher()  # watch the connections' files (event-driven)
            while True:
                await asyncio.sleep(interval_seconds)
                with contextlib.suppress(Exception):
                    await self.beat_tick()

        self._beat_task = asyncio.create_task(_loop(), name="cli-scheduler-beat")

    # ------------------------------------------------------------------
    # Connections (detect-then-add)
    # ------------------------------------------------------------------

    def on_connection_changed(self, listener: ConnectionChangeListener) -> Callable[[], None]:
        """Call ``listener`` whenever a connection of this project changes.
        Returns the unsubscribe.

        The first listener also subscribes this runtime to the state store, so an
        outcome recorded by something holding no runtime — a query tool, a
        refresh job, the credential sweep — still reaches the editor.
        """
        self._connection_listeners.append(listener)
        records = connection_records()
        if self._state_unsubscribe is None and records is not None:
            root = self._project.path

            def _observe(project_path: Path, key: str) -> None:
                if project_path == root:
                    self._announce_connection(key)

            self._state_unsubscribe = records.on_state_written(_observe)

        def _off() -> None:
            with contextlib.suppress(ValueError):
                self._connection_listeners.remove(listener)

        return _off

    def on_oauth_started(self, listener: OAuthStartedListener) -> Callable[[], None]:
        """Call ``listener`` instead of opening a browser here. Returns the
        unsubscribe."""
        self._oauth_listeners.append(listener)

        def _off() -> None:
            with contextlib.suppress(ValueError):
                self._oauth_listeners.remove(listener)

        return _off

    def _announce_connection(self, key: str, *, removed: bool = False) -> None:
        """Tell every listener that ``key`` changed, or hold it for the mutation
        in flight so the row is announced exactly once."""
        if self._announce_depth > 0:
            # A removal outranks a plain change for the same key: the row that
            # ends the mutation gone is the row the editor has to hear about.
            self._announce_pending[key] = self._announce_pending.get(key, False) or removed
            return
        self._emit_connection(key, removed=removed)

    def _emit_connection(self, key: str, *, removed: bool) -> None:
        """Hand one announcement to every listener. Never raises into a caller
        whose real job was the mutation."""
        records = connection_records()
        if records is None:
            return  # only a connection record mints keys; with none there is no row to name
        origin, plugin, handle, record_id = records.identity_of(key)
        event = ConnectionAnnouncement(
            key=key,
            plugin=plugin,
            handle=handle,
            record_id=record_id,
            origin=origin,
            removed=removed,
        )
        for listener in list(self._connection_listeners):
            with contextlib.suppress(Exception):
                listener(event)

    @contextlib.contextmanager
    def _one_announcement(self) -> Iterator[None]:
        """Collect this mutation's announcements and emit each row once when it
        is over — including the ones the state store raised from a worker
        thread while it ran."""
        self._announce_depth += 1
        try:
            yield
        finally:
            self._announce_depth -= 1
            if self._announce_depth == 0:
                pending, self._announce_pending = self._announce_pending, {}
                for key, removed in pending.items():
                    self._emit_connection(key, removed=removed)

    def announce_local_connection(self, plugin: str, handle: str, *, removed: bool = False) -> None:
        """Announce one of the member's own connections. Public because the
        cloud-sync lanes change rows this runtime never touched."""
        records = connection_records()
        if records is not None:
            self._announce_connection(records.local_key(plugin, handle), removed=removed)

    def announce_team_connection(self, record_id: str, *, removed: bool = False) -> None:
        """Announce one team/org Preconfigured Connection row."""
        records = connection_records()
        if records is not None:
            self._announce_connection(records.team_key(record_id), removed=removed)

    async def _announce_connection_for(self, conn: Connection, *, removed: bool = False) -> None:
        """Announce whatever row ``conn`` is — local or team, by its own key."""
        records = connection_records()
        if records is not None:
            self._announce_connection(records.key_for(conn), removed=removed)

    def _open_browser(
        self, url: str, *, plugin: str = "", handle: str = "", record_id: str = ""
    ) -> None:
        """Get ``url`` in front of the person.

        Under a host that owns the window (the editor) this hands the URL over
        and returns — the daemon must never open a browser on the machine it
        happens to run on. Otherwise it opens one here and RAISES when it cannot:
        a swallowed failure left the CLI waiting on a loopback for a consent
        screen nobody was ever shown.
        """
        if self._oauth_listeners:
            event = OAuthStartedAnnouncement(
                plugin=plugin, handle=handle, url=url, record_id=record_id
            )
            for listener in list(self._oauth_listeners):
                with contextlib.suppress(Exception):
                    listener(event)
            return
        import webbrowser

        try:
            opened = webbrowser.open(url)
        except Exception as exc:
            raise RuntimeError(f"couldn't open a browser; open this link yourself: {url}") from exc
        if not opened:
            raise RuntimeError(f"couldn't open a browser; open this link yourself: {url}")

    async def detected_connections(self) -> list[Connection]:
        """Discovered candidate connections — what the user could add (not live)."""
        return (await self.plugin_registry()).detected_connections()

    async def added_connections(self) -> list[Connection]:
        """The explicitly-added, live connections."""
        return (await self.plugin_registry()).added_connections()

    async def add_connection(self, handle: str, *, plugin: str | None = None) -> Connection | None:
        """Promote a detected candidate to live, then rebuild the tool view so the next tool
        surface includes it. Returns None for an unknown handle. The candidate is PROBED
        first — a connection that can't actually connect raises ``ConnectionValidationError``
        (with the real error) and is NOT added, so it stays a suggestion. Re-syncs background
        jobs so the new connection seeds without manual interaction.

        ``plugin`` disambiguates a cross-plugin handle collision so the EXACT ``(plugin, handle)``
        candidate is probed AND promoted — without it, two plugins detecting the same handle
        could probe one and add the other."""
        with self._one_announcement():
            registry = await self.plugin_registry()
            candidate = next(
                (
                    c
                    for c in registry.detected_connections()
                    if c.handle == handle and (plugin is None or c.plugin == plugin)
                ),
                None,
            )
            if candidate is not None:
                # Raises ConnectionValidationError on probe failure → the caller keeps it suggested.
                await registry.validate_connection(candidate)
            conn = registry.add_connection(handle, plugin=plugin)
            if conn is not None:
                self._tool_registry = None
                await self._resync_jobs_after_mutation()
                await self._announce_connection_for(conn)
            return conn

    async def accept_team_connection(
        self, record_id: str, *, member_values: dict[str, str] | None = None
    ) -> Connection:
        """Accept a suggested team/org Preconfigured Connection (credential →
        local probe → live), then rebuild the tool view + resync jobs like any
        add. ``member_values`` carries this member's own field values for a
        per-user credential row. Raises with the user-facing reason when it
        can't proceed."""
        with self._one_announcement():
            registry = await self.plugin_registry()
            conn: Connection = await team_connection_actions().accept(
                self._project, registry, record_id, member_values=member_values
            )
            self._tool_registry = None
            await self._resync_jobs_after_mutation()
            self.announce_team_connection(record_id)
            return conn

    async def authorize_team_connection(self, record_id: str) -> Connection:
        """Complete a per-user team/org Preconfigured Connection with the
        member's own sign-in (browser + loopback → local bundle or backend
        relay), then rebuild the tool view + resync jobs. Raises with the
        user-facing reason when it can't proceed."""
        with self._one_announcement():
            registry = await self.plugin_registry()
            conn: Connection = await team_connection_actions().authorize(
                self._project, registry, record_id, open_browser=self._team_opener(record_id)
            )
            self._tool_registry = None
            await self._resync_jobs_after_mutation()
            self.announce_team_connection(record_id)
            return conn

    def _team_opener(self, record_id: str) -> Callable[[str], None]:
        """The browser opener a team sign-in uses — the same one the local flow
        gets, so the editor opens both and the daemon opens neither."""

        def _open(url: str) -> None:
            self._open_browser(url, record_id=record_id)

        return _open

    def _team_job_identity(self, record_id: str) -> tuple[str, str] | None:
        """The (plugin, local_handle) a team row's refresh jobs are keyed on —
        captured BEFORE a remove/dismiss mutates the store, so the row's jobs
        can be cancelled exactly like a local connection's."""
        records = connection_records()
        return records.team_row_identity(self._project, record_id) if records else None

    async def _finish_connection_mutation(
        self, *, cancel_identity: tuple[str, str] | None = None
    ) -> None:
        """Invalidate connector-owned runtime surfaces after a successful mutation."""
        self._tool_registry = None
        if cancel_identity is not None:
            plugin, handle = cancel_identity
            self._cancel_jobs(
                lambda j: j.plugin == plugin and j.payload.get("connection") == handle
            )
        await self._resync_jobs_after_mutation()

    async def _mutate_team_connection(
        self, record_id: str, mutate: Callable[[ProjectDirectory, str], bool]
    ) -> bool:
        """Apply a team-row deletion/tombstone and retire its refresh jobs if it changed."""
        with self._one_announcement():
            identity = self._team_job_identity(record_id)
            changed = mutate(self._project, record_id)
            if changed:
                await self._finish_connection_mutation(cancel_identity=identity)
                self.announce_team_connection(record_id, removed=True)
            return changed

    async def remove_team_connection(self, record_id: str) -> bool:
        """Remove a live team/org Preconfigured Connection from this workspace:
        a suggested-origin row returns to the SUGGESTED lane (re-addable), an
        auto-add one tombstones (else sync re-adds it). Rebuilds the tool view
        and cancels the row's refresh jobs (they'd otherwise linger and no-op
        against the deleted credential)."""
        return await self._mutate_team_connection(record_id, team_connection_actions().remove)

    async def dismiss_team_connection(self, record_id: str) -> bool:
        """Tombstone a team/org Preconfigured Connection for this member (and
        de-materialize it if live), then rebuild the tool view and cancel the
        row's refresh jobs."""
        return await self._mutate_team_connection(record_id, team_connection_actions().dismiss)

    async def _resync_jobs_after_mutation(self) -> None:
        """After a change to the active connector set (enable / add / configure / activate),
        re-sync the background jobs so the new data appears WITHOUT manual interaction:
        (re)register the connection refresh jobs (primed due-now → the beat runs them) AND
        re-arm the KB seed so the new connection's cards get indexed. Then rebuild the file
        watcher so the new connection's source files are watched (its watched dirs moved). The
        beat (daemon subprocess / CLI in-process) owns the actual seeding."""
        with contextlib.suppress(Exception):
            await self.schedule_refresh()
        with contextlib.suppress(Exception):
            # Re-run the KB worker for the new connection's cards.
            self.nudge_standing_jobs("connections_changed")
        with contextlib.suppress(Exception):
            await self.restart_file_watcher()  # watch the (possibly new) connection's files

    def _cancel_jobs(self, predicate: Callable[[Any], bool]) -> list[str]:
        """Cancel + remove every scheduler job matching ``predicate`` (used when a plugin is
        disabled or a connection removed, so its refresh job stops running and leaves the
        Jobs UI). Best-effort; returns the cancelled job ids."""
        sched = self.scheduler()
        cancelled: list[str] = []
        with contextlib.suppress(Exception):
            for job in sched.list_jobs():
                # remove (not stop): the connection/plugin is gone, so its job is an orphan
                # and should leave the Jobs UI — distinct from the user's "Cancel" (stop the
                # run, keep the job).
                if predicate(job) and sched.remove(job.job_id):
                    cancelled.append(job.job_id)
        return cancelled

    async def remove_connection(self, handle: str, *, plugin: str | None = None) -> bool:
        with self._one_announcement():
            owner = await (await self.plugin_registry()).remove_connection(handle, plugin=plugin)
            if owner is not None:
                self._tool_registry = None
                # Cancel the removed connection's refresh job so it stops and
                # leaves the Jobs UI. Match on the FULL (plugin, handle) identity,
                # not the bare handle: two plugins can share a handle (data.sqlite
                # + data.duckdb), and a bare-handle cancel would kill the SURVIVOR
                # plugin's still-live refresh job too.
                self._cancel_jobs(
                    lambda j: j.plugin == owner and j.payload.get("connection") == handle
                )
                self.announce_local_connection(owner, handle, removed=True)
            return owner is not None

    async def connection_form(self, plugin: str) -> ConnectionFormSchema | None:
        """The declarative connection form for ``plugin`` (None if it needs none —
        pure detect-then-add). Filtered to the LOCALLY-creatable auth methods:
        a method whose credential belongs to the org (BigQuery's OAuth client)
        is never offered in the workspace form — members must never paste it."""
        registry = await self.plugin_registry()
        target = next((p for p in registry.plugins() if p.manifest.name == plugin), None)
        schema = target.connection_form_schema() if target is not None else None
        records = connection_records()
        if schema is None or records is None:
            # With nothing to record a connection against, no form is offered
            # rather than one unfiltered for the auth methods a member may fill in.
            return None
        return records.local_form(schema)

    async def configure_connection(
        self, plugin: str, handle: str, auth_method: str, fields: dict[str, str]
    ) -> Connection:
        """Build + validate a connection from the UI form, store its secret via the
        CredentialManager (never on the Connection), add it live, and rebuild the tool
        view. Raises ValueError on invalid input (surfaced to the UI)."""
        registry = await self.plugin_registry()
        return await self._mutate_configured_connection(
            registry.configure_connection, plugin, handle, auth_method, fields
        )

    async def update_connection(
        self, plugin: str, handle: str, auth_method: str, fields: dict[str, str]
    ) -> Connection:
        """Edit an existing connection from the form (blank secret ⇒ keep the current
        one), re-probe, and atomically replace it. Rebuilds the tool view + re-syncs
        jobs so the edited config takes effect. Raises ``ValueError`` /
        ``ConnectionValidationError`` on bad input / a failed probe (surfaced to the UI)."""
        registry = await self.plugin_registry()
        return await self._mutate_configured_connection(
            registry.update_connection, plugin, handle, auth_method, fields
        )

    async def _mutate_configured_connection(
        self,
        operation: Callable[..., Awaitable[Connection]],
        plugin: str,
        handle: str,
        auth_method: str,
        fields: dict[str, str],
    ) -> Connection:
        """Apply a form-backed connection mutation and refresh derived runtime state."""
        with self._one_announcement():
            from alkera_cli.plugins.plugin_base import LocalCredentialManager

            conn = await operation(
                plugin, handle, auth_method, fields, credential_manager=LocalCredentialManager()
            )
            await self._finish_connection_mutation()
            await self._announce_connection_for(conn)
            return conn

    async def test_connection(self, plugin: str, handle: str) -> None:
        """Re-probe a live connection on demand. Raises ``ConnectionValidationError``
        (real error) if it no longer works, ``ValueError`` if it isn't added. Never
        mutates the connection store or the tool view; the verdict lands in the
        connection-state store so the badge reflects what the press just proved.

        A TEAM row is recorded too, under its record id. Only the member's own
        added set used to be, so a member who pressed Test on a preconfigured row
        and watched it fail kept reading the admin's stale "Connected"."""
        with self._one_announcement():
            records = connection_records()
            registry = await self.plugin_registry()
            conn = next(
                (
                    c
                    for c in registry.active_connections()
                    if c.plugin == plugin and c.handle == handle
                ),
                None,
            )
            if conn is None and records is not None:
                conn = records.team_connection(self._project, registry, plugin, handle)
            if conn is None or records is None:
                # Nothing here to record against; the registry's own error is the
                # whole answer.
                await registry.test_connection(plugin, handle)
                return
            async with records.records_verification(self._project, conn):
                await registry.test_connection(plugin, handle)
            await self._announce_connection_for(conn)

    async def set_connection_enabled(
        self, plugin: str, handle: str, enabled: bool
    ) -> Connection | None:
        """Mute / unmute one connection. Rebuilds the tool view so it drops off / returns
        to the surface; (re)syncs jobs so a re-enabled connection resumes refreshing and a
        muted one's refresh job is cancelled. Returns the updated connection or None.

        A team/org Preconfigured Connection routes to its member-local mute
        state instead (sync never writes the member's own store)."""
        with self._one_announcement():
            registry = await self.plugin_registry()
            conn = await registry.set_connection_enabled(plugin, handle, enabled)
            if conn is None:
                conn = self._set_team_connection_muted(registry, plugin, handle, muted=not enabled)
            if conn is not None:
                self._tool_registry = None
                if enabled:
                    await self._resync_jobs_after_mutation()
                else:
                    self._cancel_jobs(
                        lambda j: j.plugin == plugin and j.payload.get("connection") == handle
                    )
                await self._announce_connection_for(conn)
            return conn

    def _set_team_connection_muted(
        self, registry: PluginRegistry, plugin: str, handle: str, *, muted: bool
    ) -> Connection | None:
        from alkera_cli.plugins.plugin_base.team_connections import is_configured, to_connection

        match = next(
            (
                (record, member)
                for record, member in registry.team_connection_records()
                if record.plugin == plugin
                and (member.local_handle or record.handle) == handle
                and is_configured(record, member)
            ),
            None,
        )
        if match is None:
            return None
        record, member = match
        if not team_connection_actions().set_muted(self._project, record.id, muted):
            return None
        updated = member.model_copy(update={"muted": muted})
        conn = to_connection(record, updated, plugins_root=self._project.plugins_path)
        if conn is None:
            return None
        return conn.model_copy(update={"enabled": not muted})

    async def clone_connection(self, plugin: str, handle: str, new_handle: str) -> Connection:
        """Duplicate a connection's non-secret config under ``new_handle`` (muted, no
        secret). Rebuilds the tool view. Raises ``ValueError`` on a bad/duplicate handle."""
        with self._one_announcement():
            registry = await self.plugin_registry()
            conn = await registry.clone_connection(plugin, handle, new_handle)
            self._tool_registry = None
            await self._announce_connection_for(conn)
            return conn

    async def rename_connection(self, plugin: str, handle: str, new_handle: str) -> Connection:
        """Rename a connection's handle (migrating its credential dir). Rebuilds the tool
        view, cancels the OLD handle's refresh job (the new one reschedules on the next
        tick), and re-syncs jobs. Raises ``ValueError`` on a bad/duplicate handle."""
        with self._one_announcement():
            registry = await self.plugin_registry()
            conn = await registry.rename_connection(plugin, handle, new_handle)
            self._tool_registry = None
            self._cancel_jobs(
                lambda j: j.plugin == plugin and j.payload.get("connection") == handle
            )
            await self._resync_jobs_after_mutation()
            self.announce_local_connection(plugin, handle, removed=True)
            await self._announce_connection_for(conn)
            return conn

    async def start_oauth_connection(
        self,
        plugin: str,
        handle: str,
        auth_method: str,
        fields: dict[str, str],
        *,
        open_browser: Callable[[str], None] | None = None,
    ) -> Connection:
        """Add a user-delegated OAuth connection: run the browser handshake (loopback +
        PKCE), store the token bundle, probe as the user, and add it live. ``open_browser``
        defaults to the system browser (the loopback posture — the daemon/CLI is co-located
        with the browser); an embedded/hosted host injects a callback that opens the URL on
        the user's side. Rebuilds the tool view + re-syncs jobs on success. Raises
        ``ValueError`` / ``LoopbackError`` / ``OAuthError`` / ``ConnectionValidationError``
        (all surfaced to the UI)."""
        with self._one_announcement():

            def _open(url: str) -> None:
                if open_browser is not None:
                    open_browser(url)
                    return
                self._open_browser(url, plugin=plugin, handle=handle)

            registry = await self.plugin_registry()
            conn = await registry.configure_oauth_connection(
                plugin, handle, auth_method, fields, open_browser=_open
            )
            self._tool_registry = None
            await self._resync_jobs_after_mutation()
            await self._announce_connection_for(conn)
            return conn

    async def reauthorize_connection(self, plugin: str, handle: str) -> Connection:
        """Sign in again for an added OAuth connection, without the form.

        The row keeps its handle, its attributes and its place; only the token
        bundle is replaced, and only once a probe with the new one passes — a
        refused re-authorization leaves the previous bundle exactly as it was.
        The browser is opened the way every other sign-in opens one: handed to
        the host when there is one, opened here otherwise. Raises ``ValueError``
        for a row that has no browser sign-in to redo, the broker's own errors on
        a failed handshake, and ``ConnectionValidationError`` when the new token
        cannot connect."""
        with self._one_announcement():
            registry = await self.plugin_registry()

            def _open(url: str) -> None:
                self._open_browser(url, plugin=plugin, handle=handle)

            conn = await registry.reauthorize_oauth_connection(plugin, handle, open_browser=_open)
            self._tool_registry = None
            await self._announce_connection_for(conn)
            return conn

    async def enable_plugin(self, name: str) -> bool:
        """Re-enable a disabled plugin; rebuild the tool view so its surfaces return and
        re-sync jobs so the plugin's connections start refreshing again."""
        registry = await self.plugin_registry()
        registry.enable_plugin(name)
        self._tool_registry = None
        await self._resync_jobs_after_mutation()
        return True

    async def disable_plugin(self, name: str) -> bool:
        """Disable a plugin — its tools/providers/connections drop off the surface +
        ``on_disable`` runs; rebuild the tool view AND cancel its refresh jobs so a
        disabled plugin stops doing background work and leaves the Jobs UI."""
        registry = await self.plugin_registry()
        await registry.disable_plugin(name)
        self._tool_registry = None
        self._cancel_jobs(lambda j: j.plugin == name)
        return True

    @property
    def open_session_ids(self) -> list[str]:
        return list(self._sessions.keys())

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def list_chats(self) -> list[ChatManifest]:
        """Return every chat (root + subagent) in the project. UI is
        responsible for filtering by `parent_session_id`.

        Overlays the LIVE background-job count onto any chat that is currently
        open in this runtime with running jobs, so the chat list can show a chat
        as active-in-the-background even when its foreground turn is idle. The
        overlay lands on a manifest COPY — the on-disk manifest is never written
        with a nonzero count (jobs don't survive a restart, so a persisted value
        would be stale)."""
        summaries = self._chats_store.list_summaries()
        if not self._sessions:
            return summaries
        return [self._with_live_background_count(m) for m in summaries]

    def _with_live_background_count(self, manifest: ChatManifest) -> ChatManifest:
        session = self._sessions.get(manifest.session_id)
        running = session.background_jobs_running if session is not None else 0
        if running == 0:
            return manifest
        return manifest.model_copy(update={"background_jobs_running": running})

    def chat_list_status(
        self, session_id: str
    ) -> tuple[Literal["idle", "running"], Literal["permission", "question", "plan"] | None]:
        """``(status, pending_ask)`` for one listed chat, derived from LIVE state
        only — never persisted, so a crashed process can't leave a chat wearing a
        stale spinner or a phantom ask. Three honest sources, same as the TUI home:

        - open in THIS runtime → the session's turn latch + broker-pending asks;
        - locked by another LIVE process (CLI, second window) → ``running`` (the
          holder's ask, if any, isn't knowable from here);
        - nobody live holds it → ``idle`` (a dead process can't be running or
          asking).
        """
        session = self._sessions.get(session_id)
        if session is not None and not session.closed:
            status: Literal["idle", "running"] = "running" if session.turn_active else "idle"
            return status, session.pending_ask
        if live_holder(self._chats_store.path / session_id / ".lock") is not None:
            return "running", None
        return "idle", None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def is_harness_available(self, harness_type: str) -> bool:
        """Whether ``harness_type`` can run on this machine (its binary is
        installed/discoverable). Lets the CLI/daemon gate harness selection
        before creating a chat — see ``HarnessAdapter.is_available``."""
        return self._adapter_factory.is_available(harness_type)

    def _require_harness_available(self, harness_type: str) -> None:
        if not self._adapter_factory.is_available(harness_type):
            raise HarnessUnavailableError(
                f"harness {harness_type!r} is not available on this machine — its "
                "binary isn't installed/discoverable. (For Claude Code: install it, "
                "or set ALKERA_CLAUDE_BIN.)"
            )

    async def new_chat(
        self,
        *,
        harness_type: str = OPENCODE_HARNESS,
        title: str | None = None,
        parent_session_id: str | None = None,
        model: dict[str, Any] | None = None,
    ) -> ChatManifest:
        """Create a fresh chat WITHOUT opening it (no harness spawn).
        Returns the manifest; caller can subsequently `open_chat(sid)`.

        ``harness_type`` pins which harness backs this chat — IMMUTABLE for its
        lifetime, persisted to the manifest, and restored on resume. User-facing
        callers (CLI / daemon) MUST pass it explicitly; the default is a
        convenience for internal/test callers. `model` pins the chat's model
        selection (incl. gateway provider id + effort) for resume. Raises
        ``HarnessUnavailableError`` if that harness can't run here.
        """
        self._require_harness_available(harness_type)
        chat = self._chats_store.create(title=title, model=model, harness_type=harness_type)
        if parent_session_id is not None:
            chat.manifest.parent_session_id = parent_session_id
        manifest = chat.manifest.model_copy()
        chat.close()
        return manifest

    async def open_chat(
        self,
        session_id: str | None = None,
        *,
        create: bool = False,
        harness_type: str = OPENCODE_HARNESS,
        title: str | None = None,
        model: dict[str, Any] | None = None,
        permission_broker: PermissionBroker | None = None,
        question_broker: QuestionBroker | None = None,
        tool_scope: ToolScope = None,
        path_fence: PathFence | None = None,
        working_dir: Path | None = None,
        sandbox_dir: Path | None = None,
        gateway_token: str | None = None,
        knowledge_owner: str | None = None,
        credential: ChatCredential | None = None,
    ) -> ChatSession:
        """Acquire the chat's lock + spawn the harness adapter.

        ``knowledge_owner`` is the chat owner's user id on a machine whose one
        knowledge store serves several people's chats: the principal the
        session's notes are filed under and read back for. ``None`` on a
        person's own daemon, where the store is theirs alone.

        If ``session_id`` is None and ``create`` is True, creates a fresh chat
        first, pinning ``harness_type`` (which harness backs it — IMMUTABLE,
        persisted, restored on resume) + ``model`` (the gateway model selection).
        If ``session_id`` is None and ``create`` is False, raises `ValueError`.
        ``harness_type`` + ``model`` are IGNORED when resuming an existing chat —
        both are pinned in its manifest. Raises ``HarnessUnavailableError`` when
        creating with a harness that can't run here.

        ``path_fence`` bounds which filesystem locations the session's tools may
        name (a cloud box's session may read the project and nothing else). It
        is checked ahead of every allow, and inherited by the session's
        subagents. ``None`` — every local CLI / editor session — decides exactly
        as before.

        ``working_dir`` is where the agent RUNS — the base a relative path in a
        tool call resolves against. ``None`` means the project root, which is
        what every local session wants. A cloud chat passes its own folder: the
        one directory it may write, so the model's "create a file here" lands
        inside the write fence instead of in the customer's project, where it
        would be refused however the reader answered.

        ``sandbox_dir`` is the session's own scratch: the one directory a write
        is admitted into in every mode, so the model can keep its plan and
        working files even where project edits are refused. ``None`` means the
        chat's own ``<chat>/sandbox/`` under ``.alkera``, which is what a local
        session wants. A cloud chat passes its working directory — the child of
        its folder it also runs in, inside the write fence, where a file
        dropped onto the chat lands and what the person sees as the chat's
        files — so the box, the drive and the brief name one place.

        ``gateway_token`` is a cloud chat's own gateway token (refused by the API,
        billed as the box's session); it reaches the agent's config/environment
        only. ``credential`` is a parent chat's, inherited by its subagents.
        With neither, the chat binds the sign-in profile it resolves now and
        keeps it for its life (``ProfileBinding``): a later switch never moves it.

        Raises:
            LockHeldError: another client has the chat open.
            ChatNotFoundError: session_id doesn't exist (and create=False).
            HarnessStartError: the harness subprocess failed to start.
            ProfileResolutionError: the project is pinned to another org.
        """
        # Offline lineage/KB seeding is owned by the scheduler beat + file watcher,
        # so chat-open neither blocks on nor kicks the seed itself.
        if credential is None:
            credential = FixedCredential(gateway_token) if gateway_token else self._bind_chat()
        await self._sweep_traces_once()
        if session_id is None:
            if not create:
                raise ValueError("open_chat: session_id required when create=False")
            self._require_harness_available(harness_type)
            chat = self._chats_store.create(title=title, model=model, harness_type=harness_type)
        else:
            if session_id in self._sessions:
                raise RuntimeError(f"chat {session_id!r} is already open in this runtime")
            chat = self._chats_store.open(session_id)
        # From this point on: lock is held. Cleanup must release on failure.
        try:
            session = await self._build_and_start_session(
                chat,
                permission_broker=permission_broker,
                question_broker=question_broker,
                tool_scope=tool_scope,
                path_fence=path_fence,
                working_dir=working_dir,
                sandbox_dir=sandbox_dir,
                credential=credential,
                knowledge_owner=knowledge_owner,
            )
        except BaseException:
            with contextlib.suppress(Exception):
                chat.close()
            raise
        self._sessions[session.session_id] = session
        bind_session(session.session_id, credential)
        self._audit_session_started(session, resumed=session_id is not None)
        return session

    def open_session(self, session_id: str) -> ChatSession | None:
        """The live ``ChatSession`` for an id, or ``None`` when it isn't open in
        this runtime. The daemon's session-bound ``tool.call`` routes through it
        to reuse the session's dispatch binding; an observer uses it to attach to a
        RUNNING chat's live bus (e.g. a subagent the parent is driving)."""
        return self._sessions.get(session_id)

    def read_chat_events(self, session_id: str) -> Iterator[Event]:
        """Lock-free replay of a chat's persisted events by id — for an observer of
        a chat that is NOT open in this runtime (a completed subagent). Takes no
        per-chat lock, so it never blocks (or is blocked by) a writer."""
        return self._chats_store.read_events(session_id)

    async def mark_chat_seen(self, session_id: str) -> str | None:
        """Record that the user's client has rendered the chat: stamp the
        manifest's ``last_seen_event_id`` from ``last_event_id`` and flush.
        Returns the stamped id (``None`` for a chat with no events yet).

        A chat open in this runtime stamps through its live session; a cold
        chat takes the write lock briefly (off the loop) to stamp and release.
        Raises ``ChatNotFoundError`` for an unknown id and ``LockHeldError``
        when another live process owns the chat — the holder is the one
        rendering it, so the mark isn't ours to make."""
        session = self._sessions.get(session_id)
        if session is not None and not session.closed:
            session.mark_seen()
            return session.manifest.last_seen_event_id

        def _stamp() -> str | None:
            chat = self._chats_store.open(session_id)
            try:
                chat.manifest.last_seen_event_id = chat.manifest.last_event_id
                return chat.manifest.last_seen_event_id
            finally:
                chat.close()

        return await asyncio.to_thread(_stamp)

    async def close_chat(self, session_id: str) -> None:
        """Tear down a session: stop adapter, drain persistence,
        release lock. Idempotent."""
        session = self._sessions.pop(session_id, None)
        if session is None:
            return
        await session.close()
        # After close, cost is settled, so the finished event sees the
        # final ledger.
        await self._audit_session_finished(session_id)

    async def close_all(self) -> None:
        """Stop every open session in parallel. Used on daemon shutdown
        or when a CLI chat exits."""
        # Stop the CLI scheduler beat (the daemon drives its own beat; that field is None
        # there) BEFORE the file watcher: the beat's own first pass restarts the watcher, so a
        # beat still running would hand back a watcher this close has already stopped.
        if self._beat_task is not None:
            beat, self._beat_task = self._beat_task, None
            if not await stop_task(beat, grace=BEAT_STOP_TIMEOUT_S):
                logger.warning(
                    "the scheduler beat did not stop within %.0fs; abandoning it",
                    BEAT_STOP_TIMEOUT_S,
                )
        # Under the restart lock, so a restart still in flight has installed its watcher by
        # the time this reads the field. Clearing the field next to one would leave that
        # watcher running with nobody holding it — and holding a worker thread — for the rest
        # of the process. Waiting is bounded by the restart itself: a registry build and a
        # watcher stop that is bounded in its turn.
        async with self._file_watcher_lock:
            if self._file_watcher is not None:
                watcher, self._file_watcher = self._file_watcher, None
                with contextlib.suppress(Exception):
                    await watcher.stop()
        # Cancel any in-flight background seed (the work is idempotent + resumable from
        # the parse marks, so cancelling mid-seed loses nothing — the next open resumes).
        for task in list(self._seed_tasks):
            task.cancel()
        self._seed_tasks.clear()
        # Kill any running seed subprocess (daemon mode) for the same reason — the store
        # is durable, so a killed worker just resumes from the parse marks next time.
        for proc in list(self._seed_procs):
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        self._seed_procs.clear()
        sids = list(self._sessions.keys())
        if sids:
            await asyncio.gather(*(self.close_chat(sid) for sid in sids), return_exceptions=True)

    async def _sweep_traces_once(self) -> None:
        """Age out expired local traces, once per process, off the event loop."""
        if self._trace_sweep_done:
            return
        self._trace_sweep_done = True
        try:
            from alkera_cli.host.config import get_settings
            from alkera_cli.observability.trace_store import sweep_expired_traces

            await asyncio.to_thread(
                sweep_expired_traces,
                self._chats_store,
                retention_days=get_settings().trace_retention_days,
            )
        except Exception:
            logger.warning("trace retention sweep failed", exc_info=True)

    def _audit_session_started(self, session: ChatSession, *, resumed: bool) -> None:
        if self._audit_reporter is None and self._otel is None:
            return
        self._session_opened_at[session.session_id] = time.monotonic()
        try:
            manifest = session.manifest
            detail = {
                "harness": manifest.harness_type,
                "model": manifest.model,
                "permission_mode": manifest.permission_mode,
                "resumed": resumed,
            }
        except Exception:  # reporting never breaks a chat open
            logger.warning("audit session_started emit failed", exc_info=True)
            return
        self._report_session("started", session_id=session.session_id, detail=detail)

    async def _audit_session_finished(self, session_id: str) -> None:
        if self._audit_reporter is None and self._otel is None:
            return
        opened_at = self._session_opened_at.pop(session_id, None)
        try:
            # Off the loop: the detail build reads the cost ledger and hashes the
            # session's whole trace, which must not stall the daemon's other chats.
            detail = await asyncio.to_thread(self._session_finish_detail, session_id, opened_at)
        except Exception:  # reporting never breaks a chat close
            logger.warning("audit session_finished emit failed", exc_info=True)
            return
        self._report_session("finished", session_id=session_id, detail=detail)

    def _session_finish_detail(self, session_id: str, opened_at: float | None) -> dict[str, Any]:
        detail: dict[str, Any] = {"queries": self._report_session_cost(session_id)}
        if opened_at is not None:
            detail["duration_seconds"] = round(time.monotonic() - opened_at, 1)
        trace_hash = self._trace_digest(session_id)
        if trace_hash is not None:
            detail["trace_hash"] = trace_hash
        return detail

    def _report_session(
        self, phase: Literal["started", "finished"], *, session_id: str, detail: dict[str, Any]
    ) -> None:
        """One session lifecycle event to both consumers; a failing (or absent)
        consumer never affects the other."""
        project = self._project.path.parent.name
        for consumer in (self._audit_reporter, self._otel):
            if consumer is None:
                continue
            try:
                emit = consumer.session_started if phase == "started" else consumer.session_finished
                emit(session_id=session_id, project=project, detail=dict(detail))
            except Exception:
                logger.warning("audit session_%s emit failed", phase, exc_info=True)

    def _report_session_cost(self, session_id: str) -> int:
        """Settle the session's warehouse spend into an ``agent.cost`` event
        (org-audit only) and return the ledger's entry count."""
        spend = session_spend(self._project, session_id)
        by_connection = dict(spend.by_connection)
        if by_connection and self._audit_reporter is not None:
            self._audit_reporter.session_cost(
                session_id=session_id,
                connection_usd=by_connection,
                total_usd=sum(by_connection.values()),
            )
        return spend.queries

    def _otel_chat_event(self, session_id: str, event: Event) -> None:
        """The persist-loop tap: finalized tool parts become OTel
        ``alkera.tool_result`` events. Never raises into persistence."""
        if self._otel is None:
            return
        try:
            self._otel.on_chat_event(session_id, event)
        except Exception:
            logger.warning("otel chat event emit failed", exc_info=True)

    def _trace_digest(self, session_id: str) -> str | None:
        """Pin the session's trace files and return the combined hash."""
        try:
            from alkera_core.project.chats.trace import pin_trace

            return pin_trace(self._chats_store.path / session_id, session_id).combined or None
        except Exception:
            logger.warning("trace pin failed for %s", session_id, exc_info=True)
            return None

    async def delete_chat(self, session_id: str, *, recursive: bool = False) -> None:
        """Delete a chat (and optionally its subagent children). The
        chat must NOT be currently open in this runtime."""
        if session_id in self._sessions:
            # Refuse rather than auto-close — caller should explicitly
            # close first to confirm intent.
            raise RuntimeError(f"refuse to delete open chat {session_id!r}; close it first")
        self._chats_store.delete(session_id, recursive=recursive)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _plan_turn_restart(
        self, chat: Chat, events: list[Event], closures: list[Event]
    ) -> TurnRestart | None:
        """What the next turn does about an interrupted one: restart it from the
        same message, or — past :data:`TURN_RESTART_LIMIT` — nothing, with the
        failure appended to ``closures``. A log with nothing interrupted clears
        the ledger: the last restart finished, so the count starts over."""
        ledger = RestartLedger(chat.path)
        restarted, restarted_text = ledger.read()
        try:
            plan = plan_turn_restart(
                events,
                interrupted=bool(closures),
                restarted=restarted,
                restarted_text=restarted_text,
            )
        except Exception:
            logger.debug("turn restart: could not plan for %s", chat.session_id, exc_info=True)
            return None
        with contextlib.suppress(OSError):
            if not closures:
                # Nothing interrupted: whatever was restarted finished.
                ledger.clear()
            elif plan is not None and plan.give_up:
                # Kept at the limit, so a caller naming the same message on
                # this open is refused too; the next clean open clears it.
                ledger.record(plan)
        if plan is not None and plan.give_up:
            closures.append(
                SessionStatusChanged(
                    event_id=secrets.token_hex(10),
                    time=datetime.now(UTC),
                    session_id=chat.session_id,
                    status="error",
                    phase="error",
                    detail=TURN_RESTART_FAILED,
                )
            )
        return plan

    def _reconcile_interrupted_turn(self, chat: Chat) -> int:
        """Append synthetic close events for a turn left interrupted by a prior
        crash/restart (the adapter's in-process close-synthesis can't run once its
        process is gone). Returns the count appended (0 when already clean).

        Synchronous + bus-free: the closures land in ``chat.jsonl`` BEFORE the
        adapter starts and before ``open_chat`` returns, so the daemon's on-open
        replay sees a closed turn with no async race. Best-effort — neither a read
        NOR a write error may block the open: a user who just suffered a crash must
        still be able to reopen the chat. A partial write self-heals, because the
        next open re-runs this (reconcile is idempotent) and closes the remainder."""
        try:
            events = list(chat.events())
        except Exception:
            logger.debug(
                "resume reconcile: could not read chat %s events", chat.session_id, exc_info=True
            )
            return 0
        closures = reconcile_interrupted_turn(
            events,
            session_id=chat.session_id,
            now=datetime.now(UTC),
            next_id=lambda: secrets.token_hex(10),
        )
        if closures:
            self._interrupted_opens.add(chat.session_id)
        else:
            self._interrupted_opens.discard(chat.session_id)
        plan = self._plan_turn_restart(chat, events, closures)
        if plan is None:
            self._turn_restarts.pop(chat.session_id, None)
        else:
            self._turn_restarts[chat.session_id] = plan
        written = 0
        try:
            for event in closures:
                chat.append_event(event)
                written += 1
        except Exception:
            # A write failure (disk full, bad fd, transient I/O) must NOT fail the
            # open — proceed with whatever closures landed; the next open finishes
            # the rest (idempotent reconciliation).
            logger.debug(
                "resume reconcile: could not append all closures for chat %s (%d/%d written)",
                chat.session_id,
                written,
                len(closures),
                exc_info=True,
            )
        if written:
            logger.info(
                "resume reconcile: closed an interrupted turn for chat %s (+%d events)",
                chat.session_id,
                written,
            )
        return written

    async def _build_and_start_session(
        self,
        chat: Chat,
        *,
        permission_broker: PermissionBroker | None,
        question_broker: QuestionBroker | None,
        tool_scope: ToolScope = None,
        path_fence: PathFence | None = None,
        working_dir: Path | None = None,
        sandbox_dir: Path | None = None,
        credential: ChatCredential | None = None,
        knowledge_owner: str | None = None,
    ) -> ChatSession:
        gateway_token = chat_token(credential)
        # A prior daemon crash/restart can kill the adapter mid-turn, bypassing its
        # streaming-closure synthesis and leaving the persisted log open (a pending
        # tool call, a dangling subagent, no turn.finished, a `running` status).
        # Close any such interrupted turn BEFORE attaching a fresh adapter, so a
        # resumed chat shows it ended instead of spinning "working…" forever. Pure
        # log reconciliation → covers both backends, OS-agnostic; no-op for a clean
        # or brand-new chat.
        self._reconcile_interrupted_turn(chat)
        # The "project root" — opencode's cwd — is the parent of `.alkera/`.
        project_root = self._project.path.parent

        bus = EventBus()
        # Adapter-pinned durable handles (the harness session id) plus, for
        # gateway-routed chats, a freshly built agent_config carrying the chat's
        # credential: a RUNTIME-ONLY overlay never written back to
        # `chat.manifest.harness`, so the token never lands on disk. A manifest
        # that already pins `agent_config` / `claude_env` (the e2e harness) wins.
        # opencode reads `agent_config`, Claude Code reads `claude_env`.
        harness_native: dict[str, Any] = dict(chat.manifest.harness)
        # The Alkera tool surface reaches BOTH backends through the per-project
        # ToolRegistry (Claude in-process SDK-MCP; opencode an `mcp` block for the
        # `alkera mcp` stdio entrypoint), the hot set from the same `hot_prefix()`.
        # What THIS chat sees is decided by its own org, read as its credential:
        # on a box serving several orgs the registry is shared, the view is not.
        web_tools = await self._resolve_web_search_flag(gateway_token)
        # The connection scope is the chat's the server knows: a subagent's
        # child chat is local to this box, so it asks as its root chat.
        scope_chat_id = chat.manifest.parent_session_id or chat.session_id
        connection_ids = await self._resolve_connection_scope(scope_chat_id)
        owner = knowledge_owner or ""
        tool_registry = await self.tool_registry_for(
            web_tools, connection_ids=connection_ids, knowledge_owner=owner
        )
        is_claude = chat.manifest.harness_type == CLAUDE_HARNESS
        session_mode = parse_mode(chat.manifest.permission_mode) or "default"
        # ONE parent-hosted loopback MCP server backs BOTH backends (Option A):
        # because dispatch runs in THIS process, the session broker + live mode +
        # loaded permissions gate writes identically on Claude and OpenCode. The
        # per-session binding is shared with the ChatSession so mode changes are
        # seen live; the server is torn down on close. A test that pinned its own
        # `alkera_mcp` (or the in-process `tool_registry` fallback) opts out.
        tool_server: AlkeraToolServer | None = None
        tool_binding: SessionToolBinding | None = None
        if "alkera_mcp" not in harness_native and "tool_registry" not in harness_native:
            from alkera_cli.plugins.plugin_base.permissions import load_permissions

            # A malformed permissions.yml is handled SILENTLY: the loader salvages
            # tighten-only / falls back to safe defaults and never raises. We
            # deliberately do NOT warn or log (user preference) — the safe-default
            # behavior is the guarantee, not a noisy notification.
            permissions = load_permissions(self._project.path)
            from alkera_core.project.chats.tasks import TaskStore

            from alkera_cli.plugins.plugin_base.permissions import DecisionSink

            tool_binding = SessionToolBinding(
                registry=tool_registry,
                web_tools=web_tools,
                connection_ids=connection_ids,
                scope_chat_id=scope_chat_id,
                knowledge_owner=owner,
                broker=permission_broker,
                permissions=permissions,
                session_id=chat.session_id,
                owner_session_id=owner_session(chat.session_id, chat.manifest.parent_session_id),
                permission_mode=session_mode,
                tool_scope=tool_scope,
                # One per-chat sink shared with ChatSession._audit_decision: the SQL
                # gate and the harness loop append to one log under one lock.
                decision_sink=DecisionSink(chat.path),
                # The SAME judge + persistence dir the harness loop uses, so an
                # in-tool SQL write is grounded in auto mode exactly like a bash
                # write. task_goal is set live per turn in send_prompt. The judge
                # and every tool's cloud call act as the chat's own credential.
                judge=bound_to(self._safety_judge, gateway_token),
                credential=credential,
                alkera_dir=self._project.path,
                # The per-chat TODO store; a subagent's task tool is gated out by
                # app="tasks", so only the root agent reaches it.
                task_store=TaskStore(chat.tasks_path),
                # The one directory a write is admitted into in every mode. A
                # local session runs in the customer's project, so its scratch
                # is the chat's own <chat>/sandbox/, made on demand by the tool
                # that writes to it. A cloud chat runs in its working directory
                # and names that same directory here: a child of its fenced chat
                # folder, never the whole folder with the chat's records in it.
                sandbox_dir=(
                    sandbox_dir if sandbox_dir is not None else chat.path / SANDBOX_SUBDIR
                ),
                # The bound the asks are decided by is the bound the in-tool shell
                # gate judges every command by: one object, two chokepoints.
                fence=path_fence.session if path_fence is not None else None,
            )
            tool_server = AlkeraToolServer(tool_binding)
            await tool_server.start()
            # The `web` mount only exists when the org's web toggle registered the
            # web tools — an empty extra MCP server would cost a connect for nothing.
            has_web_tools = tool_registry.tool_for("web.search") is not None
            harness_native["alkera_mcp"] = _alkera_mcp_block(
                tool_server.url,
                tool_server.auth_headers(),
                claude=is_claude,
                web_url=tool_server.web_url if has_web_tools else None,
            )
        if is_claude:
            # Claude never gets the `web` MCP mount — its web reach is the
            # vendor's own WebFetch, which the deployment's web.fetch switch
            # cannot touch from the registry alone. Hand the adapter the same
            # decision the mount is derived from, so the switch holds on both
            # backends instead of one. A manifest that pinned the key wins, the
            # way every other harness_native key does.
            if "web_fetch_enabled" not in harness_native:
                harness_native["web_fetch_enabled"] = (
                    tool_registry.tool_for("web.fetch") is not None
                )
            if "claude_env" not in harness_native and self._claude_env_builder is not None:
                # A per-session credential is passed only when there is one, so
                # a builder written for the local path (one positional argument)
                # keeps working there.
                claude_env = (
                    self._claude_env_builder(chat.manifest.model)
                    if gateway_token is None
                    else self._claude_env_builder(chat.manifest.model, token=gateway_token)
                )
                if claude_env is not None:
                    harness_native["claude_env"] = claude_env
        else:
            if "agent_config" not in harness_native and self._gateway_config_builder is not None:
                gateway_config = (
                    self._gateway_config_builder(chat.manifest.model)
                    if gateway_token is None
                    else self._gateway_config_builder(chat.manifest.model, token=gateway_token)
                )
                if gateway_config is not None:
                    harness_native["agent_config"] = gateway_config
        # The user's GLOBAL instructions (`~/.alkera/instructions.md`) apply to EVERY
        # project. Read + size-cap once here (a separate harness_native key so it never
        # disturbs the gateway/claude_env branches above); each adapter injects it its
        # own way — OpenCode via `config.instructions[]` (a materialized sandbox file),
        # Claude via the per-turn system prompt. Empty → no key, no injection. A caller
        # that pinned it (tests) wins. See `alkera_cli.preferences.instructions`.
        #
        # Each knowledge source's block (the workspace's source cards) and the environment
        # (`alkera_cli.environment`) ride the same key, after the user's own.
        if "global_instructions" not in harness_native:
            blocks: list[str] = []
            global_instructions = load_global_instructions()
            if global_instructions.strip():
                blocks.append(cap_global_instructions(global_instructions))
            # Rendered for the mode the chat opens in: a write to connected data
            # is refused, asked, or run by that mode, and the brief says which.
            # On a scoped runtime the cards are the chat's own connections, not
            # the stored document: that document describes every chat's union
            # on a shared box, and rendered as one chat's brief it named another
            # org's warehouses and tables. No scope answered, no source named.
            request = InstructionRequest(
                project=self._project, connection_ids=connection_ids, mode=session_mode
            )
            for provider in HARNESS_CONTEXT_PROVIDERS.items():
                if provider.instructions is None:
                    continue
                block = await provider.instructions(request)
                if block.strip():
                    blocks.append(block)
            # A session that runs somewhere other than the project root runs there
            # BECAUSE that directory is the only one it may touch; the model has to be
            # told, or it spends its turns on what the fence refuses. Named as the agent sees it.
            if working_dir is not None and working_dir != project_root:
                alias = path_fence.agent_home if path_fence is not None else None
                blocks.append(render_root_folder_brief(alias or str(working_dir)))
            tools = tool_registry.tool_for(CAPTURE_TOOL_NAME) is not None
            blocks.extend(await environment_blocks(working_dir or project_root, tools=tools))
            if blocks:
                harness_native["global_instructions"] = "\n\n".join(blocks)
        config = SessionConfig(
            session_id=chat.session_id,
            project_dir=project_root,
            chat_dir=chat.path,
            working_dir=working_dir,
            parent_session_id=chat.manifest.parent_session_id,
            model=(
                chat.manifest.model.get("provider_id")
                and {
                    "provider_id": str(chat.manifest.model.get("provider_id", "")),
                    "model_id": str(chat.manifest.model.get("model_id", "")),
                }
            )
            or None,
            agent=chat.manifest.agent,
            harness_native=harness_native,
            # Restore the persisted permission mode (plan / auto / bypass);
            # old manifests + unknown values fall back to "default".
            permission_mode=parse_mode(chat.manifest.permission_mode) or "default",
            # A PathFence means a cloud box: the box's env is scrubbed from the
            # agent, and the agent's store travels with the folder as a cache.
            fenced=path_fence is not None,
            store_is_cache=path_fence is not None,
            # The harness has its own spawner; withholding ours would only move
            # the delegation surface into the vendor's tool set.
            subagents_enabled=self.subagents_enabled,
        )
        adapter = self._adapter_factory(config, bus=bus, harness_type=chat.manifest.harness_type)
        session = ChatSession(
            chat=chat,
            adapter=adapter,
            bus=bus,
            runtime=self,
            permission_broker=permission_broker,
            question_broker=question_broker,
            permission_mode=config.permission_mode,
            tool_server=tool_server,
            tool_binding=tool_binding,
            path_fence=path_fence,
            credential=credential,
        )
        if tool_binding is not None:
            # The per-turn abort signal is wired for EVERY session (root + subagent)
            # so a turn cancel reaps THAT session's still-running foreground bash. It
            # is re-pointed at a fresh Event each turn by _send_prompt_locked; this
            # seeds the window before the first prompt fires.
            tool_binding.abort = session._turn_abort
            # The spawn + background-management tools are ROOT only, so a subagent
            # can't spawn (no recursion: the spawn tool sees ctx.spawn=None)
            # and never sees the job-management tools (they read this chat's registry).
            if not session.is_subagent:
                tool_binding.spawn = session.spawn_subagent
                tool_binding.background = session._background
        await session.start()
        await self.environment.start_session(chat, path_fence, self._project.path, tool_registry)
        # Persist the adapter's handles (merged) and flush now: a crash must NOT lose
        # the pinning, or the next open silently re-targets via pick-latest.
        native = adapter.native_state()
        if native:
            chat.manifest.harness = {**chat.manifest.harness, **native}
            chat.flush_manifest()
        return session


# ---------------------------------------------------------------------------
# ChatSession
# ---------------------------------------------------------------------------


class ChatSession:
    """Per-chat handle. Holds the lock for its lifetime.

    Async-with usage::

        async with await runtime.open_chat(sid) as session:
            async for event in session.subscribe():
                ...

    Or explicit close::

        session = await runtime.open_chat(sid)
        try:
            ...
        finally:
            await runtime.close_chat(session.session_id)
    """

    def __init__(
        self,
        *,
        chat: Chat,
        adapter: HarnessAdapter,
        bus: EventBus,
        runtime: HarnessRuntime,
        permission_broker: PermissionBroker | None,
        question_broker: QuestionBroker | None = None,
        permission_mode: PermissionMode = "default",
        tool_server: AlkeraToolServer | None = None,
        tool_binding: SessionToolBinding | None = None,
        path_fence: PathFence | None = None,
        credential: ChatCredential | None = None,
    ) -> None:
        self._chat = chat
        self._adapter = adapter
        self._bus = bus
        self._runtime = runtime
        #: A cloud chat's own token or a local chat's bound profile, read at each
        #: gateway call; never the sign-in that is current now.
        self._credential = credential
        self._broker = permission_broker
        self._question_broker = question_broker
        self._permission_mode: PermissionMode = permission_mode
        # The parent-hosted MCP server backing both backends + its per-session
        # dispatch binding (Option A). The binding's permission_mode is mutated in
        # place on set_permission_mode so the write gate sees the live mode.
        self._tool_server = tool_server
        self._tool_binding = tool_binding
        if tool_binding is not None:
            tool_binding.scope_runtime = runtime
        # What this session's tools may name on the filesystem, when something
        # outside the session bounds it (a cloud box: the project, nothing else).
        # None on every local session — nothing is bounded and nothing changes.
        self._path_fence = path_fence
        # Who decided each ask this session answered, by request id. The harness
        # echoes a reply as ``permission.resolved`` knowing only the option, so
        # the session stamps the provenance on that echo before the transcript
        # or a cloud mirror sees it — a fence's refusal never reads as a person's.
        self._decided_by: dict[str, str] = {}
        #: The ask the permission loop is deciding right now, and the sentence its
        #: record carries when a mode change had it decided again.
        self._deciding: str | None = None
        self._reconsidered_note: str | None = None
        self._bus.rewrite = self._rewrite_published
        # A turn that ends with nothing on screen names why, as the model's own
        # message would, ahead of that message's completion.
        self._bus.precede = EmptyAnswerWatch().precede
        # The subagent's identity prompt (its ``AgentDefinition.prompt``), injected
        # as the per-turn system addendum on a child's turns. Empty for a root.
        self._agent_prompt = ""
        self._persist_task: asyncio.Task[None] | None = None
        self._permission_task: asyncio.Task[None] | None = None
        self._question_task: asyncio.Task[None] | None = None
        self._last_user_text = ""
        #: The mode the model was last told about. ``None`` until the first turn seeds it
        #: (silently — resume/first-turn never fires a spurious "switched" notice); a
        #: later change emits a one-shot ``mode_switch_reminder``.
        self._last_steered_mode: PermissionMode | None = None
        self._decision_sink: DecisionSink | None = None
        self._closed = False
        # Gates the plugins/lineage/dbt orientation briefs onto the FIRST root turn
        # only. No KB facts ride any prompt; knowledge reaches the model as it acts.
        self._brief_emitted = False
        self._turn_briefs = turn_briefs(self)
        # Background-job machinery (all Alkera-owned; no vendored background path).
        # The registry supervises detached tool work (background subagents/SQL/bash);
        # a finished job re-enters this chat as a "Backgrounded Tool Finished" turn.
        self._background = BackgroundJobRegistry(
            on_submit=self._on_background_submit,
            on_terminal=self._on_background_terminal,
        )
        self._turn_state_task: asyncio.Task[None] | None = None
        self._turn_lifecycle = TurnLifecycle(
            TurnLifecycleConfig(
                adapter=adapter,
                bus=bus,
                session_id=chat.session_id,
                is_closed=lambda: self._closed,
                is_subagent=lambda: self.is_subagent,
                compact=self.compact,
                send_prompt_locked=self._send_prompt_locked,
                inject_background_results=self._inject_background_results,
                settlement_history=_SETTLEMENT_HISTORY,
                max_overflow_retries=MAX_OVERFLOW_RETRIES,
                overflow_compaction_timeout_seconds=OVERFLOW_COMPACTION_TIMEOUT_SECONDS,
                on_turn_event=self._on_turn_event,
                on_turn_end=self._settle_turn,
                max_model_retries=MAX_MODEL_RETRIES,
                model_retry_window_seconds=MODEL_RETRY_WINDOW_SECONDS,
                clock=lambda: MODEL_RETRY_CLOCK(),
            )
        )
        # Analyst mode: ``_turn`` is in flight, ``_verification`` is owed.
        persisted = chat.manifest.analysis_pipeline
        self._analysis_pipeline: AnalysisPipeline = (
            cast(AnalysisPipeline, persisted) if persisted in get_args(AnalysisPipeline) else "off"
        )
        self._turn_seq = 0
        self._turn: _Turn | None = None
        self._verification: _Verification | None = None
        #: The interrupted turn this open found (``None`` when there is none):
        #: :meth:`restart_interrupted_turn` runs it again.
        self._turn_restart: TurnRestart | None = runtime._turn_restarts.pop(chat.session_id, None)
        self._found_interrupted = chat.session_id in runtime._interrupted_opens
        runtime._interrupted_opens.discard(chat.session_id)

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._chat.session_id

    @property
    def interrupted_turn(self) -> TurnRestart | None:
        """The interrupted turn this open found (to restart, or ``give_up``: closed
        as failed) until :meth:`restart_interrupted_turn` consumes it."""
        return self._turn_restart

    @property
    def found_interrupted(self) -> bool:
        """Whether this open found a turn the local log left open (a process died
        in it on this disk): the mark a caller needs before restarting a turn
        whose message already has an answer after it on the cloud record."""
        return self._found_interrupted

    def can_restart(self, text: str) -> bool:
        """Whether the message ``text`` may be restarted once more: this open
        did not already close it as failed, and its restarts are not spent."""
        plan = self._turn_restart
        if plan is not None and plan.give_up:
            return False
        done, done_text = RestartLedger(self._chat.path).read()
        return not (done_text == text and done >= TURN_RESTART_LIMIT)

    async def report_interrupted_jobs(self) -> int:
        """Report each job a past process cut off (``interrupted_jobs``), once."""
        live = self._background.get
        jobs = [j for j in interrupted_background_jobs(self.events()) if live(j.job_id) is None]
        for job in jobs:
            await self._on_background_terminal(as_finished_job(job))
        return len(jobs)

    async def restart_interrupted_turn(
        self, text: str | None = None, *, prompt: str | None = None, **turn: Any
    ) -> bool:
        """Run the interrupted turn again from the same message, with one line
        the model reads saying so. ``False`` when there is nothing to restart
        or the message's restarts are spent.

        ``text`` is the message as the caller knows it — a cloud box reads it
        off the chat's record, which a box that never ran the turn has and its
        own log does not. Without it the message is the one this open found.
        ``prompt`` is what is sent when it differs from the words the count is
        kept by (the message with the files it names spelled out).
        The count is per message: :data:`TURN_RESTART_LIMIT` restarts of the
        same words, whichever process ran them on this disk.

        The attempt is counted before the turn is sent, so a process that dies
        again mid-restart counts it: the fourth interruption stops instead.
        ``turn`` carries the caller's per-turn options (effort, context); the
        restart line goes after any context the caller passes."""
        plan, self._turn_restart = self._turn_restart, None
        if plan is not None and plan.give_up:
            return False
        if text is None:
            if plan is None:
                return False
            text = plan.text
        ledger = RestartLedger(self._chat.path)
        if plan is None or plan.text != text:
            done, done_text = ledger.read()
            done = done if done_text == text else 0
            if done >= TURN_RESTART_LIMIT:
                return False
            plan = TurnRestart(text=text, attempt=done + 1)
        with contextlib.suppress(OSError):
            ledger.record(plan)
        context = turn.pop("context", None)
        blocks = [block for block in (context, TURN_RESTART_NOTE) if block]
        logger.info(
            "turn restart: restarting an interrupted turn for chat %s (attempt %d of %d)",
            self.session_id,
            plan.attempt,
            TURN_RESTART_LIMIT,
        )
        await self.send_prompt(prompt or text, context="\n\n".join(blocks), **turn)
        return True

    @property
    def tool_server(self) -> AlkeraToolServer | None:
        """The loopback MCP server for this agent's Alkera tools (None: pinned)."""
        return self._tool_server

    @property
    def tool_binding(self) -> SessionToolBinding | None:
        """The session's live dispatch binding (broker + mode + policy), threaded by
        the daemon's direct ``tool.call`` so it gates like the MCP transport.
        ``None`` when the session pinned its own transport (an e2e harness)."""
        return self._tool_binding

    @property
    def credential(self) -> ChatCredential | None:  # its bound profile, or its own token
        return self._credential

    @property
    def manifest(self) -> ChatManifest:
        return self._chat.manifest

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def _active_attempt(self) -> str | None:
        """Compatibility view of the lifecycle-owned active attempt."""
        return self._turn_lifecycle.active_attempt

    @property
    def _pending_background(self) -> list[tuple[BackgroundJob, str]]:
        """Compatibility view of lifecycle-owned queued background completions."""
        return self._turn_lifecycle.pending_background

    @property
    def _turn_abort(self) -> asyncio.Event:
        """Compatibility view of the lifecycle-owned foreground abort signal."""
        return self._turn_lifecycle.turn_abort

    @property
    def _turn_lock(self) -> asyncio.Lock:
        """Compatibility view of the lifecycle-owned turn critical section."""
        return self._turn_lifecycle.lock

    @property
    def _settlements(self) -> list[SessionStatusChanged | None]:
        """Compatibility view of the lifecycle-owned settlement history."""
        return self._turn_lifecycle.settlements

    @property
    def _settlements_base(self) -> int:
        """Compatibility view of the lifecycle-owned settlement base index."""
        return self._turn_lifecycle.settlements_base

    @property
    def background_jobs_running(self) -> int:
        """How many background jobs (bash / sql / subagent) are currently running in
        this chat. Drives the chat-list "active" indicator and the resident-session
        lifecycle (a chat with running jobs is kept alive across a UI detach)."""
        return self._background.running_count()

    @property
    def turn_active(self) -> bool:
        """Whether a foreground turn is running right now (the turn-state
        latch). Drives the chat list's working spinner for a chat that is
        open here but not being watched by the asking client."""
        return self._turn_lifecycle.turn_active

    @property
    def turn_settlements(self) -> int:
        """How many turns have settled. A caller that started a turn waits for
        the NEXT settlement edge instead of sampling ``turn_active``: a flush
        clears the latch and injects a queued background wake under one lock
        hold, so the boolean can be back up before any sample sees it down."""
        return self._turn_lifecycle.turn_settlements

    def settlement(self, index: int) -> SessionStatusChanged | None:
        """The terminal settlement ``index`` ended on, or ``None`` when nothing
        carried one (a cancel or clear the runtime settled itself). Indexed so a
        caller reads ITS turn's outcome even when a background wake turn has
        already settled behind it. History is bounded; a pruned index raises,
        since every consumer reads its entry within the turn's lifetime."""
        return self._turn_lifecycle.settlement(index)

    @property
    def pending_ask(self) -> Literal["permission", "question", "plan"] | None:
        """What this chat is waiting on the human for, or ``None`` while it
        owes nothing. Read off the brokers — the only path a genuine human
        prompt takes (auto-decisions never park there). A permission outranks
        a question, mirroring the webview fold's ordering."""
        if self._broker is not None and self._broker.has_pending_for(self.session_id):
            return "permission"
        if self._question_broker is not None:
            return self._question_broker.pending_kind_for(self.session_id)
        return None

    @property
    def has_running_background(self) -> bool:
        return self._background.has_running()

    def is_quiescent(self) -> bool:
        """Whether this chat owes nothing right now, and so may be put to sleep.

        Three things make a chat busy and each would be lost or orphaned by a
        sleep: a foreground turn in flight, a background job (bash / sql /
        subagent) still running, and an ask waiting on a human — a reader who
        answers a permission prompt whose session has been torn down gets no
        turn back. "Nothing published lately" is not enough on its own, which is
        exactly how a long background job used to be swept out from under its
        own chat.
        """
        return not self.turn_active and not self.has_running_background and self.pending_ask is None

    @property
    def chat_path(self) -> Path:
        return self._chat.path

    def events(self) -> Iterator[Event]:
        """The chat's full persisted event log (crash-safe JSONL reader). The
        daemon's ``open_chat`` replay reads through this public accessor rather
        than the private chat handle, keeping the runtime the single owner of
        how persisted events are read."""
        return self._chat.events()

    @property
    def project(self) -> ProjectDirectory:
        """The owning ``ProjectDirectory`` (``.alkera/``) — used by CLI surfaces
        like ``/cost`` to read the spend ledger + policy."""
        return self._runtime.project

    @property
    def decision_sink(self) -> DecisionSink:
        """This chat's OWN append-only permission-decision log, at
        ``<chat>/decisions.jsonl`` — next to the chat's ``chat.jsonl`` history, so
        each conversation owns its audit trail. Reuses the binding's sink instance
        (one per chat → one in-process lock) when present; else builds its own."""
        if self._decision_sink is None:
            from alkera_cli.plugins.plugin_base.permissions import DecisionSink

            if self._tool_binding is not None and self._tool_binding.decision_sink is not None:
                self._decision_sink = self._tool_binding.decision_sink
            else:
                self._decision_sink = DecisionSink(self._chat.path)
        sink = self._decision_sink
        assert sink is not None
        return sink

    @property
    def permission_mode(self) -> PermissionMode:
        """The active permission policy. Consulted by the caller's
        permission resolver to decide prompt/allow/reject."""
        return self._permission_mode

    def mark_seen(self) -> None:
        """Record that the client has rendered everything persisted so
        far: copies the manifest's ``last_event_id`` into
        ``last_seen_event_id`` and flushes.

        Self-throttling — a no-op (no write) when nothing new landed
        since the last call, so callers can invoke it per rendered
        event. Copy semantics (not "the event I just rendered") keep
        the field comparable against ``last_event_id`` even though
        live-only chunk events never persist."""
        if self._closed:
            return
        manifest = self._chat.manifest
        if manifest.last_seen_event_id == manifest.last_event_id:
            return
        manifest.last_seen_event_id = manifest.last_event_id
        self._chat.flush_manifest()

    @property
    def analysis_pipeline(self) -> AnalysisPipeline:
        return self._analysis_pipeline

    def set_analysis_pipeline(self, mode: AnalysisPipeline) -> None:
        """Switch the selector for the next turn; persisted so a resume keeps it."""
        if mode not in get_args(AnalysisPipeline):
            raise ValueError(f"unknown analysis pipeline {mode!r}; choose off or analyst")
        self._analysis_pipeline = mode
        self._chat.manifest.analysis_pipeline = mode
        self._chat.flush_manifest()

    @property
    def turn_origin(self) -> TurnOrigin | None:
        """Who issued the turn in flight; None between turns."""
        return None if self._turn is None else self._turn.origin

    @property
    def analysis_verification_pending(self) -> bool:
        """True from an answering turn's idle edge until its verification turn ends,
        so a driver waiting for quiet includes the verification turn."""
        return self._verification is not None and self._verification.pending

    @property
    def analysis_verification_fired(self) -> bool:
        """True once a verification turn was issued for the current caller prompt,
        whatever its outcome; reset when the next caller prompt starts."""
        return self._verification is not None and self._verification.seq is not None

    @property
    def verification_outcome(self) -> VerificationOutcome | None:
        """How the current caller prompt's verification ended; None when none has
        run (or it is still running)."""
        return None if self._verification is None else self._verification.outcome

    def set_permission_mode(self, mode: PermissionMode) -> None:
        """Switch the permission policy. Takes effect on the next
        permission request + next prompt (plan mode routes through the
        harness's planning agent).

        Persisted to the manifest so resuming the chat restores this mode
        instead of silently reverting to ``default``. ``flush_manifest`` is an
        atomic temp+rename write — cheap + safe to call synchronously here."""
        previous = self._permission_mode
        self._permission_mode = mode
        self._chat.manifest.permission_mode = mode
        self._chat.flush_manifest()
        # Keep the MCP server's write gate in lockstep with the live mode.
        if self._tool_binding is not None:
            self._tool_binding.permission_mode = mode
        # An ask a person is being shown was decided under the old stance. It is
        # decided again under this one, by the same ladder, as if it had arrived
        # now: the new stance may answer it outright, or refuse it, or still put
        # it to the person, whose prompt then stays exactly as it is.
        if (
            mode != previous
            and self._broker is not None
            and self._deciding is not None
            and self._broker.reconsider(self._deciding)
        ):
            self._reconsidered_note = mode_change_note(previous, mode)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn the adapter + wire pumps. Called by `HarnessRuntime`."""
        await self._adapter.start()
        # Persistence pump — UNBOUNDED queue so we NEVER drop events
        # that should land in chat.jsonl. The store-side filter (via
        # NON_PERSISTED_EVENT_TYPES) discards chunks/heartbeats inside
        # `Chat.append_event`, so the steady-state event rate is low.
        # Subscribe EAGERLY (synchronously) so any publish() after
        # start() returns is guaranteed to reach the pumps. Lazy
        # subscription inside the task body would race with a fast
        # publisher.
        persist_sub = self._bus.subscribe(maxsize=PERSIST_QUEUE_MAXSIZE)
        self._persist_task = asyncio.create_task(
            self._persist_loop(persist_sub),
            name=f"chat-persist-{self.session_id}",
        )
        # Permission router — fires when the harness asks the user to
        # approve a tool. The broker is supplied by the caller (CLI
        # prompts on stdin; daemon issues a JSON-RPC request).
        if self._broker is not None:
            perm_sub = self._bus.subscribe()
            self._permission_task = asyncio.create_task(
                self._permission_loop(perm_sub),
                name=f"chat-permission-{self.session_id}",
            )
        # Question router — parallel to permission, handles clarifier
        # questions from the harness (opencode's `question` tool, …).
        if self._question_broker is not None:
            q_sub = self._bus.subscribe()
            self._question_task = asyncio.create_task(
                self._question_loop(q_sub),
                name=f"chat-question-{self.session_id}",
            )
        # Turn-state pump — watches this chat's own status events so a background
        # job completing between turns can wake the chat immediately, while one
        # completing mid-turn is queued for the next idle edge. Subscribe eagerly
        # (like the pumps above) so no early status event is missed.
        turn_sub = self._bus.subscribe()
        self._turn_state_task = asyncio.create_task(
            self._turn_lifecycle.run(turn_sub),
            name=f"chat-turnstate-{self.session_id}",
        )

    async def close(self) -> None:
        """Tear down. Idempotent."""
        if self._closed:
            return
        self._closed = True

        # Drain background jobs and start-hook work (an environment restore) FIRST,
        # so the chat never tears down with detached work behind it; before
        # adapter.stop() so any job still talking to the adapter settles.
        with contextlib.suppress(Exception):
            await self._background.drain()
        await self._runtime.environment.close(self.session_id)

        # Reject any in-flight question / permission BEFORE stopping the adapter.
        # Both loops deliver a shielded reject-on-cancel over the adapter's HTTP
        # client, so the harness must still be ALIVE for it to land. The generic
        # teardown below cancels the pumps only AFTER `adapter.stop()`, by which
        # point the POST hits a dead client — so opencode keeps the question
        # pending and RE-ASKS it on resume (the double-card bug). Cancelling the
        # interrupt loops here resolves the pending tool while the harness can
        # still hear it. A no-op when nothing is pending (the loop is parked on the
        # bus, outside the reject-on-cancel guard).
        for task in (self._permission_task, self._question_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

        # Stop the adapter FIRST — SIGTERM subprocess, flush any final
        # synthesized events, and close its event bus (which enqueues the
        # stream-end sentinel to every subscriber). Closing the bus before
        # touching the pumps is what lets the persist loop DRAIN its queue
        # and write the last events — e.g. a `ConversationCleared` (or a
        # terminal `CompactionApplied`) published right before close — to
        # `chat.jsonl` instead of losing them to an eager task.cancel().
        with contextlib.suppress(Exception):
            await self._adapter.stop()
        # Idempotent (adapter.stop() also closes the bus); guarantees the
        # sentinel is enqueued even if the adapter raised mid-stop.
        with contextlib.suppress(Exception):
            await self._bus.close()
        # The launch this session's commands reused names the container and
        # slice its agent server ran in, gone now: the session's next open (or
        # the box's next week of chats) must not inherit it.
        forget_session_launches(self.session_id)

        # Drain the persist pump to completion. The bus is closed, so the
        # loop terminates once its queue empties (append_event is
        # synchronous — no await to interrupt mid-drain). Bounded so a
        # wedged pump can't hang teardown — but the queue is deliberately
        # unbounded so nothing is ever dropped, so what this cuts short on a
        # slow disk is the tail of chat.jsonl.
        if self._persist_task is not None and not self._persist_task.done():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._persist_task, timeout=PERSIST_DRAIN_SECONDS)

        # Belt-and-braces cancel of every pump: the permission/question loops were
        # already rejected + cancelled above (so this is a no-op for them unless one
        # somehow survived), and the persist task is cancelled here in case the
        # bounded drain timed out.
        for task in (
            self._persist_task,
            self._permission_task,
            self._question_task,
            self._turn_state_task,
        ):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

        # Stop the parent-hosted MCP server (Option A) — its loopback port + the
        # session binding die with the chat.
        if self._tool_server is not None:
            with contextlib.suppress(Exception):
                await self._tool_server.stop()

        # Release the chat's lock + persist manifest.
        with contextlib.suppress(Exception):
            self._chat.close()

    async def __aenter__(self) -> ChatSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()
        # Best-effort: also unregister from runtime's session map.
        self._runtime._sessions.pop(self.session_id, None)

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    async def send_prompt(
        self,
        text: str,
        **kwargs: Any,
    ) -> str:
        """Start a turn and return its transport attempt id (the stamp on every
        status the adapter emits for it). Serialized per chat via ``_turn_lock``
        so a user prompt and a background-result injection never fire the adapter
        concurrently; a prompt over a live turn supersedes it, and the withdrawn
        attempt can no longer settle the new one.

        ``_overflow_retry`` is set ONLY by the context-overflow recovery path (it
        re-sends the SAME turn after an auto-compaction); it preserves the per-turn
        retry tally so a compaction loop stays one-shot."""
        prompt, overflow_retry = _replay_prompt_from_kwargs(text, kwargs)
        async with self._turn_lock:
            # The settle hook fires an earned verification while it still holds this
            # lock, so a queued caller prompt can never steal the idle edge from it;
            # a caller prompt over a live turn supersedes it (the lifecycle withdraws
            # the old attempt).
            if not overflow_retry:
                # A caller prompt opens a fresh ledger; harness continuations never touch it.
                self._verification = None
            return await self._send_prompt_locked(prompt, overflow_retry=overflow_retry)

    async def _send_prompt_locked(
        self,
        prompt: ReplayPrompt,
        *,
        overflow_retry: bool = False,
        origin: TurnOrigin = "caller",
    ) -> str:
        """Compose + fire a turn. The caller MUST hold ``_turn_lock`` (the public
        ``send_prompt`` and the background-injection path both do). An overflow
        resend continues the same logical turn, so it keeps the standing ``_Turn``."""
        self._prepare_prompt_start(prompt)
        if not overflow_retry:
            self._turn_seq += 1
            self._turn = _Turn(self._turn_seq, origin)
        try:
            await self._refresh_web_tools()
            agent, system, latch_briefs = await self._compose_prompt_start(prompt)
            self._record_prompt_goal(prompt, overflow_retry=overflow_retry)
            model, variant = turn_model(prompt.model, prompt.variant, self.manifest.model)
            return await self._turn_lifecycle.fire_prompt(
                PromptInput(
                    text=prompt.text,
                    model=model,
                    variant=variant,
                    parts=prompt.parts or [],
                    agent=agent,
                    system=system,
                ),
                latch_briefs=self._mark_briefs_emitted if latch_briefs else None,
            )
        except BaseException:
            self._turn = None
            raise

    def serves_pinned_model(self, model: Mapping[str, Any] | None = None) -> bool:
        """Whether the running agent answers on the pin (or ``model``) unrespawned."""
        sent = pinned_model_key(self.manifest.model if model is None else model)
        return sent is None or self._adapter.serves_model(sent)

    async def _refresh_web_tools(self) -> None:
        """Re-read this chat's org web-tool flags and its connection scope at
        the start of a turn — the flags as the chat's own credential, the scope
        as the chat the server knows — and rebind the session's tool view.

        Both are policy that changes while chats are open, and on a box one
        process serves chats of several orgs, so neither the process nor the
        open is the right place to settle them: the turn is. The view the
        loopback MCP server lists and dispatches through, and the steering the
        prompt composes from it, both follow the rebinding. The `web` mount the
        subprocess was configured with at open stays as it was — a toggle turned
        ON mid-chat is advertised at the next open — while a toggle turned OFF
        binds at once: the mount lists nothing and a call by name is refused.
        A connection the chat's owner lost, or one the box stopped holding the
        chat for, is gone from ``sql.connections`` on this turn the same way.

        The view is re-derived from the runtime's CURRENT build every turn: a
        build dropped since (a connection discovered, a team row that landed)
        is otherwise one no open session consults until it is reopened.
        And the agent is told when its own list fell behind: it
        lists the loopback server once, as it connects, and a tool the view
        gained after that is one it answers "unknown tool" for until it lists
        again — so when what a mount last listed is no longer what it would
        list, the adapter is asked to re-list before the prompt goes out.
        """
        binding = self._tool_binding
        if binding is None:
            return
        flags = await self._runtime._resolve_web_search_flag(chat_token(self._credential))
        connection_ids = await self._runtime._resolve_connection_scope(binding.scope_chat_id)
        binding.web_tools = flags
        await binding.rebind(connection_ids)
        await self._relist_stale_tools()

    async def _relist_stale_tools(self) -> None:
        """Ask the harness to list the tool server again when the list it
        holds is behind the session's view. Best-effort: the turn runs on the
        old list if the harness cannot, which is what it ran on before."""
        server = self._tool_server
        if server is None:
            return
        stale = server.stale_mounts()
        if not stale:
            return
        logger.info(
            "chat %s: the agent's %s tool list is behind the session's; asking it to list again",
            self.session_id,
            ", ".join(stale),
        )
        try:
            await self._adapter.refresh_tools()
        except Exception:
            logger.warning(
                "chat %s: the agent could not re-list its tools", self.session_id, exc_info=True
            )

    def _prepare_prompt_start(self, prompt: ReplayPrompt) -> None:
        """Install per-turn abort state and reject invalid prompt options."""
        if self._closed:
            raise HarnessNotReadyError(_CHAT_SESSION_CLOSED_MESSAGE)
        turn_abort = self._turn_lifecycle.reset_abort()
        if self._tool_binding is not None:
            self._tool_binding.abort = turn_abort
        if prompt.variant is not None:
            allowed = self.manifest.model.get("efforts") or []
            if allowed and prompt.variant not in allowed:
                raise ValueError(
                    f"unknown reasoning effort {prompt.variant!r} for this chat; "
                    f"supported: {', '.join(map(str, allowed))}"
                )

    async def _compose_prompt_start(
        self, prompt: ReplayPrompt
    ) -> tuple[str | None, str | None, bool]:
        """Return the adapter agent, hidden system block, and brief-latch decision."""
        agent = mode_to_agent(self._permission_mode)
        addendum = await self._prompt_addendum(prompt.system_addendum)
        switch_notice: str | None = None
        if self._last_steered_mode is None:
            self._last_steered_mode = self._permission_mode
        elif self._last_steered_mode != self._permission_mode:
            switch_notice = mode_switch_reminder(self._last_steered_mode, self._permission_mode)
            self._last_steered_mode = self._permission_mode
        sandbox_hint = self._sandbox_hint()
        # The caller's per-turn context goes LAST: it introduces the words after it.
        system = _compose_system(
            switch_notice,
            mode_system_prompt(
                self._permission_mode,
                sandbox_dir=sandbox_hint,
                subagents=self._serves_spawn_tool(),
            ),
            addendum,
            *await brief_texts(self._turn_briefs, self.session_id),
            prompt.context,
        )
        latch_briefs = prompt.system_addendum is None and not self.is_subagent
        return agent, system, latch_briefs

    async def _prompt_addendum(self, system_addendum: str | None) -> str | None:
        """Return caller addendum, or root first-turn guidance when applicable."""
        if system_addendum is not None or self.is_subagent:
            return system_addendum
        return await self._compose_root_addendum()

    async def _task_reminder(self) -> str | None:
        """Render the live root task reminder for this turn, if one exists."""
        if self.is_subagent or self._tool_binding is None or self._tool_binding.task_store is None:
            return None
        task_list = await self._tool_binding.task_store.load()
        return render_task_reminder(task_list)

    @property
    def sandbox_dir(self) -> Path | None:
        """The directory a message's relative file paths are resolved against —
        the same one the tool binding fences writes to, so a pasted file lands
        where the agent reads and the agent's chart is found where it wrote it."""
        if self._tool_binding is None or self._tool_binding.sandbox_dir is None:
            return None
        return Path(self._tool_binding.sandbox_dir)

    def _sandbox_hint(self) -> str | None:
        """The session's sandbox as an absolute path, for the prompt to name.

        The same directory the tool binding fences writes to, so the sentence
        the model reads and the rule the gate applies cannot disagree — spelled
        the way the agent sees it: the sandbox's own ``/home/alkera`` where the
        directory is mounted there, the host path everywhere else.
        """
        if self._tool_binding is None or self._tool_binding.sandbox_dir is None:
            return None
        if self._path_fence is not None and self._path_fence.agent_home:
            return self._path_fence.agent_home
        return str(self._tool_binding.sandbox_dir)

    def _python_env_hint(self) -> str | None:
        """The chat's default Python environment, for the prompt to name, spelled as
        the agent sees it; only when the sandbox made it, and never for a local session."""
        if self._path_fence is None or self._tool_binding is None:
            return None
        env = session_default_env(self._chat.session_id, self._chat.path / RUNTIME_STATE_SUBDIR)
        # The environments are the agent's to write; looked into with no link followed.
        present = ChatTree(env.parent).is_file(f"{env.name}/bin/python")
        spell = getattr(self._path_fence.session, "spell", None)
        return (str(spell(env)) if callable(spell) else str(env)) if present else None

    def _record_prompt_goal(self, prompt: ReplayPrompt, *, overflow_retry: bool) -> None:
        """Record the goal and replay inputs for the turn being fired."""
        self._last_user_text = prompt.text
        self._turn_lifecycle.remember_prompt(prompt, overflow_retry=overflow_retry)
        if self._tool_binding is not None:
            self._tool_binding.task_goal = prompt.text

    def _mark_briefs_emitted(self) -> None:
        """Commit the first-turn brief latch after the adapter accepts a prompt."""
        self._brief_emitted = True

    # ------------------------------------------------------------------
    # Background jobs
    # ------------------------------------------------------------------

    def _on_turn_event(self, ev: Event) -> None:
        """Lifecycle seam: observe every event of a live attempt for the analyst
        ledger (what the turn read, whether its answer settled)."""
        self._note_data_read(ev)
        self._note_turn_failure(ev)

    def _note_data_read(self, ev: Event) -> None:
        """Mark this turn as answered from data once a data read COMPLETES without
        error, directly or through ``call_tool``; a refused or failed read earns
        nothing. Called only while the turn loop sees a live turn, so a read that
        lands between turns is never credited to the next one."""
        turn = self._turn
        if (
            turn is None
            or not isinstance(ev, (ToolCall, ToolCallUpdate))
            or _is_background_card(ev.tool_call_id)
        ):
            return
        # The name rides the tool when hot, else the ``call_tool`` dispatch input,
        # on the call or on an update; the completing update may carry no input.
        dispatched = ev.input.get("name") if isinstance(ev.input, dict) else None
        if dispatched in _DATA_READ_TOOLS or (
            isinstance(ev, ToolCall) and ev.tool_name in _DATA_READ_TOOLS
        ):
            turn.pending_calls.add(ev.tool_call_id)
        if (
            isinstance(ev, ToolCallUpdate)
            and ev.tool_call_id in turn.pending_calls
            and ev.status == "completed"
            and not ev.error_text
        ):
            turn.read_data = True

    def _note_turn_failure(self, ev: Event) -> None:
        """A refused, cut, or unsettled answer earns no verification."""
        if self._turn is None:
            return
        if isinstance(ev, TurnFinished) and ev.stop_reason not in SETTLED_STOP_REASONS:
            self._turn.failed = True
        if isinstance(ev, MessageCompleted) and ev.finish_reason in TRUNCATED_FINISH_REASONS:
            self._turn.failed = True

    def _earned_verification(self, ended: _Turn, status: str) -> bool:
        """A clean caller turn that read data earns one verification, never while one is owed."""
        return (
            ended.origin == "caller"
            and self._verification is None
            and self._analysis_pipeline == "analyst"
            and not self.is_subagent
            and status == "idle"
            and ended.read_data
            and not ended.failed
        )

    async def _settle_turn(self, terminal: SessionStatusChanged | None) -> None:
        """Lifecycle seam, called inside the settling critical section so no other
        sender can take the idle edge first: record how an ending verification went
        or fire the one the ending caller turn earned. It runs under the session's
        own permission mode: a read-only clamp measured 0/3 gold readings against
        3/5 without it. A failed fire leaves the first answer standing."""
        ended, self._turn = self._turn, None
        status = terminal.status if terminal is not None else "aborted"
        owed = self._verification
        if ended is None:
            return
        if owed is not None and owed.seq == ended.seq:
            owed.outcome = "verified" if status == "idle" else f"failed: {status}"
            return
        if not self._earned_verification(ended, status):
            return
        self._verification = owed = _Verification()
        prompt = ReplayPrompt(
            text=VERIFICATION_TURN_PROMPT,
            model=None,
            variant=None,
            parts=None,
            system_addendum=None,
        )
        try:
            await self._send_prompt_locked(prompt, origin="verification")
        except asyncio.CancelledError:
            owed.outcome = "failed: cancelled"
            raise
        except Exception:
            logger.warning("analyst mode: verification turn could not fire", exc_info=True)
            owed.outcome = "failed: could not fire"
            return
        owed.seq = self._turn_seq

    async def _on_background_terminal(self, job: BackgroundJob) -> None:
        """Registry callback fired when a background job ends. Publish the job's
        NATIVE-result frame (so the UI renders a normal async-finished tool card),
        then wake the model now if the chat is idle, else queue for the idle edge.
        The card's delivered bytes travel WITH the job to the wake, so a store
        failure between the two renders cannot make them diverge."""
        if self._closed:
            return
        delivered = await self._emit_background_frame(job)
        await self._turn_lifecycle.queue_or_inject_background(job, delivered)

    async def _emit_background_frame(self, job: BackgroundJob) -> str:
        """Publish a finished job's terminal transcript frame so the UI renders it as
        a normal tool card, returning the delivered card bytes ("" when the frame
        carries no rendered result). An agent folds its report onto the spawn card
        (``SubagentCompleted``); a SQL/bash job gets a self-contained tool card
        (``ToolCall`` + terminal ``ToolCallUpdate``, keyed by ``bgjob:<id>``) that
        both UIs route by tool name — see ``_emit_background_tool_card``."""
        if job.kind == "agent":
            result = job.result
            # On a cancelled/errored agent job, job.result is None, so the child id
            # off the result is "" — fall back to the id stashed on job.input at submit,
            # so SubagentCompleted always carries the real child id (the TUI folds the
            # outcome onto the card by it; resume_reconcile clears the dangling-subagent
            # record by it — an empty id would leave both wrong).
            child_sid = str(
                getattr(result, "child_session_id", "")
                or (job.input or {}).get("child_session_id", "")
                or ""
            )
            summary = getattr(result, "summary", None)
            error = job.error or getattr(result, "error", None)
            with contextlib.suppress(Exception):
                await self._bus.publish(
                    SubagentCompleted(
                        event_id=secrets.token_hex(10),
                        time=datetime.now(UTC),
                        session_id=self.session_id,
                        child_session_id=child_sid,
                        summary=summary,
                        error=error,
                    )
                )
        elif job.kind in _TOOL_CARD_BACKGROUND_KINDS:
            return await self._emit_background_tool_card(job)
        return ""

    async def _on_background_submit(self, job: BackgroundJob) -> None:
        """Registry SUBMIT callback — publish the RUNNING transcript card for a
        SQL/bash job the instant it's submitted, so a spinning "Background task" card
        shows immediately (the chat's in-transcript "a job is running" indicator). The
        agent kind already emits ``SubagentStarted`` at spawn, so only sql/bash need
        this. The terminal ``ToolCallUpdate`` (``_emit_background_tool_card``) fills
        this same ``bgjob:<id>`` card with the result — one card, running → finished,
        exactly like a synchronous tool call."""
        if job.kind not in _TOOL_CARD_BACKGROUND_KINDS:
            return
        with contextlib.suppress(Exception):
            await self._bus.publish(
                ToolCall(
                    event_id=secrets.token_hex(10),
                    time=datetime.now(UTC),
                    session_id=self.session_id,
                    tool_call_id=f"{_BACKGROUND_START_CARD}{job.job_id}",
                    message_id="",
                    tool_name=_background_tool_name(job.kind),
                    input=job.input or {},
                    status="running",
                )
            )

    async def _emit_background_tool_card(self, job: BackgroundJob) -> str:
        """At a SQL/bash job's terminal transition, emit TWO transcript frames:

        1. RESOLVE the in-place START card (``bgjob:<id>``, emitted at submit): it stops
           spinning where the agent LAUNCHED the job, always ``completed`` (the job did
           start), with no result body; the outcome rides the finish card.
        2. EMIT a separate FINISH card (``bgdone:<id>``) carrying the native result,
           serialized EXACTLY as a foreground tool call (``serialize_tool_result``), at
           the conversation TAIL where the reader is; a failed or cancelled job's card
           is errored with the job's error.
        A process that ends mid-job emits no finish card; the next open delivers one
        (``interrupted_jobs``). Returns the delivered bytes ("" for none) for the wake."""
        completed = job.state == "completed" and job.result is not None
        status: ToolCallStatus = "completed" if completed else "error"
        error_text = None if completed else (job.error or job.state)
        tool_name = _background_tool_name(job.kind)
        output = None
        if completed and job.result is not None:
            wire = await asyncio.to_thread(self._delivered_result_wire, tool_name, job.result)
            output = model_facing_text(wire)
            if is_tool_error_result(wire):
                # The job succeeded but its result could not be delivered (store
                # dead, irreducible envelope). The card must carry the error
                # signal, not a completed status wrapping an error body.
                status = "error"
                error_text = str(wire.get("error") or "result delivery failed")
        now = datetime.now(UTC)
        done_id = f"{_BACKGROUND_FINISH_CARD}{job.job_id}"
        with contextlib.suppress(Exception):
            # 1. Resolve the START card (stop the spinner) — always "completed" (it
            #    successfully launched; the outcome is on the finish card), no output.
            await self._bus.publish(
                ToolCallUpdate(
                    event_id=secrets.token_hex(10),
                    time=now,
                    session_id=self.session_id,
                    tool_call_id=f"{_BACKGROUND_START_CARD}{job.job_id}",
                    status="completed",
                )
            )
            # 2. The FINISH card — a NEW, self-contained ToolCall + result update at the
            #    tail, so it surfaces even if the at-submit START frame was lost.
            await self._bus.publish(
                ToolCall(
                    event_id=secrets.token_hex(10),
                    time=now,
                    session_id=self.session_id,
                    tool_call_id=done_id,
                    message_id="",
                    tool_name=tool_name,
                    input=job.input or {},
                    status=status,
                )
            )
            await self._bus.publish(
                ToolCallUpdate(
                    event_id=secrets.token_hex(10),
                    time=now,
                    session_id=self.session_id,
                    tool_call_id=done_id,
                    status=status,
                    output=output,
                    error_text=error_text,
                )
            )
        return output or ""

    async def _inject_background_results(self, jobs: list[tuple[BackgroundJob, str]]) -> bool:
        """Wake the model with the finished jobs' NATIVE results, framed as
        ``<backgrounded_tool_finished>``. The caller holds the turn lock."""
        if self._closed or not jobs:
            return True
        text = _render_background_wake(jobs, self._render_job_result)
        try:
            await self._send_prompt_locked(
                ReplayPrompt(
                    text=text,
                    model=None,
                    variant=None,
                    parts=None,
                    system_addendum=None,
                ),
                origin="wake",
            )
        except Exception:
            return False
        return True

    def _delivered_result_wire(self, tool_name: str, result: BaseModel) -> dict[str, Any]:
        """The one background render: the job's native result through the
        delivery door, exactly the foreground path. For a fitted result the door
        is a no-op; an irreducible envelope becomes a handle rather than an
        oversized payload in the chat log or the injected prompt. Both the
        finish card and the wake render here, so their bytes cannot drift from
        dispatch or from each other. Returns the wire DICT so the caller can
        read the error flag before encoding."""
        return deliver_generic(self._chat.blobs, tool_name, serialize_tool_result(result))

    def _render_job_result(self, job: BackgroundJob, delivered: str) -> str:
        """Render a finished job's NATIVE result for the model — the same content
        its synchronous tool result would carry (the agent's report; the SQL rows;
        the bash output). ``delivered`` is the finish card's rendered bytes, ""
        when no card rendered them."""
        if job.error is not None:
            return job.error
        result = job.result
        if job.kind == "agent":
            # Defensive: a SubagentRunResult that carries its own error (rather than
            # raising) must surface it, not a "(no report)" fallback that hides the
            # failure (the background _job re-raises, so this is belt-and-braces).
            err = getattr(result, "error", None)
            if err:
                return str(err)
            return str(getattr(result, "summary", "") or "(no report)")
        # The finish card rendered these bytes already; reuse them so the two
        # surfaces cannot diverge. The fallback render (a wake with no prior
        # card, e.g. a re-queued injection after a restart) blocks the loop
        # briefly on the rare over-cap blob write, which beats injecting the
        # oversized payload it would replace.
        if isinstance(result, BaseModel):
            if delivered:
                return delivered
            wire = self._delivered_result_wire(_background_tool_name(job.kind), result)
            return model_facing_text(wire)
        return str(result) if result is not None else "(no result)"

    async def _spawn_subagent_background(
        self,
        prompt: str,
        *,
        agent: str,
        description: str | None,
        child_sid: str,
        child_mode: PermissionMode,
        child_scope: ToolScope,
        agent_def: Any,
        model_slug: str | None,
    ) -> SubagentRunResult:
        """Detach a subagent run as a supervised background job; return a "started"
        stub immediately. The child is opened + its ``SubagentStarted`` published
        synchronously (so the UI binds the card now); the run + child teardown
        happen in the job coroutine, and the registry's terminal callback emits
        ``SubagentCompleted`` + wakes this chat with the report.

        A background subagent is clamped to READ-ONLY (tighten-only; a stricter
        ``plan`` ceiling is kept) AND its detached child is given a non-interactive,
        auto-rejecting permission broker (``_detached_child_auto_reject``) with no
        question broker — so a write/prompt that slips past the mode clamp (e.g. a
        project ``ask`` rule, which overrides the ``read_only`` mode base) is denied
        immediately instead of stalling on a human who isn't watching."""
        stub_stats = AgentUsageStats(model=model_slug)
        # Read-only clamp (tighten-only): anything that could write becomes read_only;
        # plan (stricter) stays plan.
        if child_mode != "plan":
            child_mode = "read_only"

        if not self._background.can_accept():
            return SubagentRunResult(
                summary="",
                stats=stub_stats,
                child_session_id="",
                error=(
                    "too many background jobs already running; cancel one with "
                    "background_cancel or run this agent in the foreground"
                ),
            )

        try:
            child = await self._runtime.open_chat(
                child_sid,
                harness_type=self._chat.manifest.harness_type,
                # Detached child: a non-interactive broker that auto-rejects any prompt
                # (no human is watching a background job), and no question broker — so
                # the run can never block forever waiting on a human. See
                # _detached_child_auto_reject.
                permission_broker=PermissionBroker(_detached_child_auto_reject),
                question_broker=None,
                tool_scope=child_scope,
                path_fence=self._path_fence,
                knowledge_owner=self._knowledge_owner(),
                credential=self._credential,
            )
            await self._bus.publish(
                SubagentStarted(
                    event_id=secrets.token_hex(10),
                    time=datetime.now(UTC),
                    session_id=self.session_id,
                    child_session_id=child_sid,
                    agent_name=agent,
                    description=description,
                    prompt=prompt,
                )
            )
            child.set_permission_mode(child_mode)
            child._agent_prompt = agent_def.prompt if agent_def is not None else ""
        except Exception as exc:
            with contextlib.suppress(Exception):
                await self._runtime.close_chat(child_sid)
            logger.exception("background subagent %s failed to start", child_sid)
            return SubagentRunResult(
                summary="",
                stats=stub_stats,
                child_session_id=child_sid,
                error=f"{type(exc).__name__}: {exc}",
            )

        async def _job() -> SubagentRunResult:
            try:
                summary, stats = await child._run_subagent_turn(prompt, model=model_slug)
                return SubagentRunResult(summary=summary, stats=stats, child_session_id=child_sid)
            except Exception:
                # RE-RAISE (not a non-raising error result like the foreground path):
                # a background subagent has no concurrent siblings to protect, so the
                # registry should record the job as ERROR (job.error/state) — that's
                # what makes the completion notification report the failure instead of
                # waking the model with a "(no report)" completed status.
                logger.exception("background subagent %s failed", child_sid)
                raise
            finally:
                # Reap the child whatever the outcome (success / failure / cancel) —
                # mirrors the foreground path's ``finally: close_chat``.
                with contextlib.suppress(Exception):
                    await self._runtime.close_chat(child_sid)

        try:
            # Stash child_sid on the job so the terminal SubagentCompleted can carry the
            # real child id even when the job is cancelled/errored (job.result is None
            # then, so reading the id off the result yields "" — see _emit_background_frame).
            self._background.submit(
                _job,
                kind="agent",
                title=description or f"@{agent}",
                input={"child_session_id": child_sid},
            )
        except Exception as exc:
            # The registry refused the job (e.g. the cap filled between can_accept()
            # and here) — close the child we already opened so it isn't orphaned.
            with contextlib.suppress(Exception):
                await self._runtime.close_chat(child_sid)
            logger.warning("background subagent %s could not start: %s", child_sid, exc)
            return SubagentRunResult(
                summary="", stats=stub_stats, child_session_id=child_sid, error=str(exc)
            )
        return SubagentRunResult(
            summary=(
                f"Background agent started (job for @{agent}). It runs while you "
                "continue; you'll be notified with its report when it finishes — do "
                "not poll. Continue with non-overlapping work, or stop if there's "
                "nothing else useful to do."
            ),
            stats=stub_stats,
            child_session_id=child_sid,
        )

    def _serves_spawn_tool(self) -> bool:
        """Whether this session actually carries the agent-spawning tool. Read off
        the registry, not the setting: whatever the reason there is no spawn tool,
        nothing this session is told may promise one."""
        return (
            self._tool_binding is not None
            and self._tool_binding.registry.tool_for("spawn_agent") is not None
        )

    async def _compose_root_addendum(self) -> str:
        """The always-on main-agent guidance, with the opening briefs prepended on the
        session's first root turn: plugins, the KB sample of verified notes, lineage,
        and dbt. Web-tool steering rides only when this session serves web tools and
        the backend is opencode; Claude uses its native WebSearch/WebFetch."""
        has_web_tools = (
            self._tool_binding is not None
            and self._tool_binding.registry.tool_for("web.search") is not None
            and self._chat.manifest.harness_type != CLAUDE_HARNESS
        )
        addendum = compose_main_agent_guidance(
            web_tools=has_web_tools,
            subagents=self._serves_spawn_tool(),
            analyst=self._analysis_pipeline == "analyst",
            sandbox_dir=self._sandbox_hint(),
            python_env=self._python_env_hint(),
            overrides=self._runtime.system_block_overrides,
            notebooks=notebooks.serves(self._tool_binding),
        )
        if self._brief_emitted:
            return addendum
        briefs = [b for b in (await self._plugins_brief(), *await self._provider_briefs()) if b]
        return "\n\n".join([*briefs, addendum]) if briefs else addendum

    async def _provider_briefs(self) -> list[str]:
        """Each registered knowledge source's brief for the first root turn, in
        registration order. A source that fails is silent; it never raises into a
        turn."""
        binding = self._tool_binding
        request = BriefRequest(
            project=self.project,
            tools=binding.registry if binding is not None else None,
            scoped=binding is not None and binding.connection_ids is not None,
            # A background seed in flight: an empty or partial result reads as
            # "not indexed yet", not "nothing".
            seeding=bool(self._runtime._seed_tasks),
            plugins=self._runtime.plugin_registry,
        )
        briefs: list[str] = []
        for provider in HARNESS_CONTEXT_PROVIDERS.items():
            if provider.opening_brief is None:
                continue
            try:
                briefs.append(await provider.opening_brief(request))
            except Exception:
                logger.debug("%s brief unavailable", provider.name, exc_info=True)
        return briefs

    def _knowledge_owner(self) -> str | None:
        """The principal this session's knowledge is filed under, for a child it
        spawns: a subagent reads and writes as the chat it serves. ``None`` on a
        session with no binding (a test-pinned tool registry)."""
        binding = self._tool_binding
        return binding.knowledge_owner if binding is not None else None

    async def _plugins_brief(self) -> str:
        """The upfront integrations brief — active plugins, their live connections, and
        each one's URN format (so the agent can construct asset URNs by hand) — plus
        what's available-but-inactive, for the first root turn. Silent on any failure;
        never raises into a turn."""
        try:
            # The session's view when it has one: on a shared box the project's
            # own snapshot lists every chat's connections, and read as this
            # chat's brief it named another org's handles as active here.
            binding = self._tool_binding
            if binding is not None:
                snapshot = binding.registry.plugin_snapshot
            else:
                registry = await self._runtime.plugin_registry()
                snapshot = registry.plugin_snapshot()
        except Exception:
            logger.debug("plugins brief unavailable", exc_info=True)
            return ""
        # A user-DISABLED plugin is off the agent's surface even if its signal is
        # present, so it's not an "active integration"; it falls into the
        # available-to-enable list below (the user can re-enable it).
        active = [p for p in snapshot if p.active and p.enabled]
        if not active:
            return ""
        lines = ["ACTIVE DATA INTEGRATIONS (call list_plugins for the full picture):"]
        for plugin in active:
            live = ", ".join(c.handle for c in plugin.connections if c.added)
            conns = f" — connections: {live}" if live else ""
            urn = f" — URN: {plugin.urn_format}" if plugin.urn_format else ""
            lines.append(f"- {plugin.name}{conns}{urn}")
        inactive = sorted(p.name for p in snapshot if not (p.active and p.enabled))
        if inactive:
            lines.append(
                "Available to enable (ask the user to turn them on): " + ", ".join(inactive) + "."
            )
        return "\n".join(lines)

    async def cancel(self) -> None:
        await self._turn_lifecycle.cancel()

    async def compact(self) -> None:
        """Force a context compaction (summarize). The summary surfaces as
        a `CompactionApplied` event over the firehose; later turns use the
        compacted context."""
        if self._closed:
            return
        await self._adapter.compact()

    async def clear(self) -> None:
        """Reset the conversation context to empty — the next turn starts
        fresh. Surfaces a `ConversationCleared` event over the firehose.

        The adapter may swap its durable native handle (opencode mints a
        fresh session), so re-flush `native_state()` into the manifest
        immediately — `_build_and_start_session` only flushes it at start,
        and resume MUST re-attach to the cleared session, not the old one."""
        if self._closed:
            return
        await self._turn_lifecycle.clear()
        native = self._adapter.native_state()
        if native:
            self._chat.manifest.harness = {
                **self._chat.manifest.harness,
                **native,
            }
            self._chat.flush_manifest()

    async def set_title(self, title: str) -> None:
        """Set the chat's title.

        Updates the manifest immediately (so the header + a follow-up
        `/title` reflect it at once) AND publishes a `SessionUpdated` over
        the firehose. The persist pump writes that event to chat.jsonl and
        mirrors the title into the manifest; `manifest.json` (re)flushed on
        close is what resume reads back, so the title survives a reopen.

        Title is a pure alkera-manifest concern: opencode's own session
        title is never surfaced (the translator drops it from
        `session.updated`), so nothing clobbers a user-set title."""
        if self._closed:
            return
        self._chat.manifest.title = title
        self._chat.flush_manifest()
        await self._bus.publish(
            SessionUpdated(
                event_id=secrets.token_hex(10),
                time=datetime.now(UTC),
                session_id=self.session_id,
                title=title,
            )
        )

    async def publish_event(self, event: Event) -> None:
        """Put a synthetic event on this chat's stream as if the harness had
        emitted it: the persist pump writes it to chat.jsonl and every live
        subscriber sees it. For the events a host synthesizes around the
        harness (a cloud re-run's tool result, a budget stop) — never for
        events the harness itself owns."""
        if self._closed:
            return
        await self._bus.publish(event)

    async def set_model_effort(self, effort: str) -> None:
        """Persist the user's reasoning-effort choice for this chat.

        Stored in ``manifest.model["effort"]``, the slot every turn reads its
        effort from (``turn_model``), so it also survives a resume. Rejects an
        effort the pinned model is KNOWN not to offer, but accepts any when the
        manifest carries no ``efforts`` (the gateway is the backstop), the same
        rule as ``send_prompt``'s variant validation.
        """
        if self._closed:
            # Raise rather than no-op: a silent return would let the daemon
            # report success with the UNCHANGED effort, telling the user their
            # choice took when it didn't.
            raise HarnessNotReadyError(_CHAT_SESSION_CLOSED_MESSAGE)
        efforts = self._chat.manifest.model.get("efforts")
        allowed = [e for e in efforts if isinstance(e, str)] if isinstance(efforts, list) else []
        if allowed and effort not in allowed:
            raise ValueError(
                f"effort {effort!r} is not offered by this chat's model "
                f"(offers: {', '.join(allowed)})"
            )
        self._chat.manifest.model = {**self._chat.manifest.model, "effort": effort}
        self._chat.flush_manifest()

    async def set_model(self, selection: dict[str, Any]) -> None:
        """Repin the chat's model, keeping the formats of the models it moved off:
        manifest now + `SessionUpdated` (what a reopen folds the manifest from)."""
        if self._closed:
            return
        self._chat.manifest.model = with_reasoning_history(self._chat.manifest.model, selection)
        self._chat.flush_manifest()
        await self._bus.publish(
            SessionUpdated(
                event_id=secrets.token_hex(10),
                time=datetime.now(UTC),
                session_id=self.session_id,
                model=dict(self._chat.manifest.model),
            )
        )

    async def resolve_permission(
        self, request_id: str, option_id: str, *, reason: str | None = None
    ) -> None:
        """The DIRECT reply door — an editor answering out of band, past the
        broker the canonical flow goes through.

        It reaches no engine, so Alkera records no rule from it; a standing
        grant sent on from here would be recorded by the VENDOR instead, as a
        coarse command-prefix rule that then stops the ask being raised at all.
        The person's verdict stands for the call they answered and promises
        nothing past it."""
        if self._closed:
            return
        bound = bind_to_this_ask(cast(PermissionOptionId, option_id))
        await self._adapter.resolve_permission(request_id, bound, reason=reason)

    async def answer_question(self, request_id: str, answers: list[list[str]]) -> None:
        if self._closed:
            return
        await self._adapter.answer_question(request_id, answers)

    async def reject_question(self, request_id: str, reason: str | None = None) -> None:
        if self._closed:
            return
        await self._adapter.reject_question(request_id, reason)

    @property
    def is_subagent(self) -> bool:
        """True if this chat was spawned by another (has a parent)."""
        return bool(self._chat.manifest.parent_session_id)

    @property
    def _is_claude(self) -> bool:
        """Whether this session runs the Claude harness (vs opencode). Drives the one
        harness-specific denial step: a human reject must INTERRUPT Claude (its SDK
        continues on a deny) but not opencode (it ends the turn on a reason-less
        reject itself)."""
        return self._chat.manifest.harness_type == CLAUDE_HARNESS

    def _resolve_agent_def(self, name: str) -> Any:
        """Resolve a subagent ``AgentDefinition`` by name across all three sources
        (built-ins < ``.alkera/agents/*.md`` < plugin ``AgentProvider`` defs).
        Returns ``None`` if unknown. Late import keeps the harness decoupled from
        the plugin registry's load order."""
        from alkera_cli.plugins.plugin_base.agents import AGENTS_SUBDIR, resolve_agents

        # Plugin-contributed (programmatic) agents — wired in so an AgentProvider's
        # subagents are actually spawnable, not just collected. The registry
        # is already discovered for the parent session; tolerate it being absent.
        programmatic = (
            self._runtime._plugin_registry.agent_definitions()
            if self._runtime._plugin_registry is not None
            else None
        )
        agents = resolve_agents(
            agents_dir=self._runtime._project.path / AGENTS_SUBDIR, programmatic=programmatic
        )
        return agents.get(name)

    async def spawn_subagent(
        self,
        prompt: str,
        *,
        agent: str = "explore",
        description: str | None = None,
        background: bool = False,
    ) -> SubagentRunResult:
        """Run a linked child chat and return its report, stats, and child id.

        Recursion is forbidden, fan-out is uncapped, and child permission mode only
        tightens the parent mode. Child failures return `SubagentRunResult.error`
        instead of raising so sibling tool calls and the parent turn survive.
        """
        if self.is_subagent:
            raise SubagentError("a subagent cannot spawn another subagent")

        agent_def = self._resolve_agent_def(agent)

        child_model = self._chat.manifest.model or None
        resolver = bound_to(self._runtime._subagent_model_resolver, chat_token(self._credential))
        if resolver is not None and agent_def is not None:
            routed = await resolver(agent_def, child_model)
            if routed is not None:
                child_model = routed
        model_slug = _model_slug(child_model)

        # Tighten-only: a read-only/explore agent definition clamps the child mode.
        child_mode = _child_permission_mode(self._permission_mode, agent_def)

        child_manifest = await self._runtime.new_chat(
            harness_type=self._chat.manifest.harness_type,
            parent_session_id=self.session_id,
            model=child_model,
        )
        child_sid = child_manifest.session_id

        # Child prompts share the parent brokers; `tool_scope` limits visible tools.
        child_scope: ToolScope = agent_def.tool_scope if agent_def is not None else None

        if background:
            return await self._spawn_subagent_background(
                prompt,
                agent=agent,
                description=description,
                child_sid=child_sid,
                child_mode=child_mode,
                child_scope=child_scope,
                agent_def=agent_def,
                model_slug=model_slug,
            )

        summary = ""
        stats = AgentUsageStats(model=model_slug)
        error: str | None = None
        try:
            child = await self._runtime.open_chat(
                child_sid,
                harness_type=self._chat.manifest.harness_type,
                permission_broker=self._broker,
                question_broker=self._question_broker,
                tool_scope=child_scope,
                # A child of a bounded session is bounded the same way — a fence
                # a subagent walks around is not a fence.
                path_fence=self._path_fence,
                knowledge_owner=self._knowledge_owner(),
                credential=self._credential,
            )
            # Publish only after open_chat registers the child as observable.
            await self._bus.publish(
                SubagentStarted(
                    event_id=secrets.token_hex(10),
                    time=datetime.now(UTC),
                    session_id=self.session_id,
                    child_session_id=child_sid,
                    agent_name=agent,
                    description=description,
                    prompt=prompt,
                )
            )
            try:
                child.set_permission_mode(child_mode)
                child._agent_prompt = agent_def.prompt if agent_def is not None else ""
                summary, stats = await child._run_subagent_turn(prompt, model=model_slug)
            finally:
                await self._runtime.close_chat(child_sid)
        except Exception as exc:
            # A child failure must NEVER crash the parent: capture it as
            # the result's error so the spawn tool surfaces a clean {error, tool}.
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("subagent %s failed", child_sid)

        return SubagentRunResult(
            summary=summary, stats=stats, child_session_id=child_sid, error=error
        )

    async def _run_subagent_turn(
        self, prompt: str, *, model: str | None = None
    ) -> tuple[str, AgentUsageStats]:
        """Drive this (child) session to completion and return ``(summary, stats)``.

        The EXPLORATION phase runs unbounded unless an operator set
        ``SUBAGENT_TURN_BUDGET_SECONDS``; on a budget hit we do NOT kill — we
        interrupt the child and force a final summary turn, itself bounded only by
        ``SUBAGENT_REPORT_CEILING_SECONDS`` when that too is set. Either wait ends
        on the attempt's own terminal, which the adapter synthesizes when the child
        dies. ``truncated`` records whether the budget fired. Stats are tallied
        from the child's tool events (counts from ``ToolCall``; file paths merged
        from ``ToolCall`` + the populated ``ToolCallUpdate``, since a streamed
        ``ToolCall.input`` is empty until the update).
        """
        sub = self._bus.subscribe()
        loop = asyncio.get_running_loop()
        started = loop.time()

        collector = _SubagentTurnCollector()

        # ---- Bounded exploration ----
        truncated = False
        # ``None`` is a turn the operator asked to leave unbounded: the wait then
        # ends on the attempt's own terminal, exactly as the report turn's does.
        budget = SUBAGENT_TURN_BUDGET_SECONDS
        deadline = None if budget is None else started + budget
        attempt = await self.send_prompt(prompt, system_addendum=self._agent_prompt or None)
        while True:
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                truncated = True
                break
            try:
                async with asyncio.timeout(remaining):
                    ev = await anext(sub)
            except TimeoutError:
                truncated = True
                break
            except StopAsyncIteration:
                break  # bus closed — return what we have, no force-summary
            if ends_attempt(ev, attempt):
                break
            collector.consume(ev)

        if not truncated:
            if not collector.needs_forced_report:
                return collector.texts[-1], self._subagent_stats(
                    collector.by_tool, collector.files, started, model, truncated=False
                )

        summary = await self._force_subagent_summary(
            sub, collector.consume, collector.texts, interrupt=truncated
        )
        if truncated and budget is not None:
            note = _BUDGET_CUT_NOTE.format(minutes=budget / 60.0)
            summary = f"{summary}\n\n{note}"
        return summary, self._subagent_stats(
            collector.by_tool, collector.files, started, model, truncated=True
        )

    async def _force_subagent_summary(
        self,
        sub: AsyncIterator[Event],
        consume: Callable[[Event], None],
        texts: list[str],
        *,
        interrupt: bool,
    ) -> str:
        """Drive the report turn and return its text."""
        if interrupt:
            await self._drain_cancelled_subagent_turn(sub, consume)
        text_floor = len(texts)
        attempt = await self.send_prompt(
            _FORCE_SUMMARY_PROMPT if interrupt else _ASK_FOR_REPORT_PROMPT,
            system_addendum=self._agent_prompt or None,
        )
        await self._collect_subagent_until(sub, consume, attempt, SUBAGENT_REPORT_CEILING_SECONDS)
        return self._summary_after_floor(texts, text_floor)

    async def _drain_cancelled_subagent_turn(
        self, sub: AsyncIterator[Event], consume: Callable[[Event], None]
    ) -> None:
        """Cancel exploration and drain up to that attempt's terminal."""
        attempt = self._active_attempt
        await self.cancel()
        if attempt is None:
            return
        await self._collect_subagent_until(sub, consume, attempt, _DRAIN_CEILING_SECONDS)

    async def _collect_subagent_until(
        self,
        sub: AsyncIterator[Event],
        consume: Callable[[Event], None],
        attempt: str,
        ceiling_seconds: float | None,
    ) -> None:
        """Collect child events until ``attempt`` ends or the ceiling expires.

        A ``None`` ceiling — the default — waits for the attempt's terminal, which
        the adapter synthesizes when the child dies, so the parent is bounded by the
        child being gone rather than by a clock."""
        loop = asyncio.get_running_loop()
        ceiling = None if ceiling_seconds is None else loop.time() + ceiling_seconds
        while True:
            remaining = None if ceiling is None else ceiling - loop.time()
            if remaining is not None and remaining <= 0:
                return
            try:
                async with asyncio.timeout(remaining):
                    ev = await anext(sub)
            except (TimeoutError, StopAsyncIteration):
                return
            if ends_attempt(ev, attempt):
                return
            consume(ev)

    @staticmethod
    def _summary_after_floor(texts: list[str], text_floor: int) -> str:
        """Return the report text, falling back only when the report turn was silent."""
        report_texts = texts[text_floor:]
        if report_texts:
            return report_texts[-1]
        if texts:
            return texts[-1]  # best-effort: the pre-cancel partial
        # A wedged child that produced nothing at all → a clean error, never a hang
        # (resilience invariant). Surfaces as a {error, tool} to the parent.
        raise SubagentError("subagent produced no summary")

    @staticmethod
    def _subagent_stats(
        by_tool: dict[str, int],
        files: set[str],
        started: float,
        model: str | None,
        *,
        truncated: bool,
    ) -> AgentUsageStats:
        return AgentUsageStats(
            tool_calls=sum(by_tool.values()),
            by_tool=dict(by_tool),
            files_read=len(files),
            duration_seconds=asyncio.get_running_loop().time() - started,
            model=model,
            truncated=truncated,
        )

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    def subscribe(self) -> AsyncIterator[Event]:
        """Subscribe to the live event firehose for this chat.

        Each call returns an independent iterator. Multiple consumers
        (e.g. CLI REPL + a separate watcher) can subscribe in parallel.
        """
        return self._bus.subscribe()

    # ------------------------------------------------------------------
    # Internal pumps
    # ------------------------------------------------------------------

    async def _persist_loop(self, sub: AsyncIterator[Event]) -> None:
        """Drain the bus and append every semantically-meaningful event
        to `chat.jsonl`. Subscription is created eagerly by `start()`
        and handed in. The filter for non-persisted event types
        (chunks, heartbeats) lives in `Chat.append_event` itself.
        """
        async for event in sub:
            try:
                self._chat.append_event(event)
            except Exception:
                logger.exception("chat.jsonl persist failed for event %r", event)
            self._runtime._otel_chat_event(self.session_id, event)

    def _rewrite_published(self, event: Event) -> Event:
        """What this session corrects on every event it publishes, before the
        transcript, a cloud mirror or a live reader sees it: who decided an ask,
        and which decisions an ask may offer."""
        if isinstance(event, PermissionRequest):
            return self._offered_decisions(event)
        return self._stamp_provenance(event)

    def _offered_decisions(self, event: PermissionRequest) -> PermissionRequest:
        """Withhold the standing grants from a fenced ask no rule is recorded on
        (:func:`standing_answer_recordable`); one named anyway binds its call."""
        fence = self._path_fence
        if fence is None or not event.options:
            return event
        owner = self._knowledge_owner() or ""
        kept = without_standing_grant(event.options)
        if kept == list(event.options) or standing_answer_recordable(
            event, owner, lambda e: fence.escape(e) is None and not self._reach_unprovable(e)
        ):
            return event
        return event.model_copy(update={"options": kept})

    def _reach_unprovable(self, event: PermissionRequest) -> bool:
        """Whether this session's fence could not prove where an ask goes.

        The ONE spelling of that question, because it is asked twice — once when
        the ask is published, to decide what it may offer, and once when it is
        decided. It RAISES whatever the fence's own judge raised: a judge that
        could not run is not the same fact as a judge that ran and could not
        read the command, and the two call sites answer it differently.
        """
        fence = self._path_fence
        if fence is None or fence.must_ask is None:
            return False
        return fence.must_ask(event)

    def _stamp_provenance(self, event: Event) -> Event:
        """Put who decided on the harness's echo of a permission reply.

        Both backends emit ``permission.resolved`` from their reply path with
        the schema's default (``user``); only this session knows whether a
        person answered, the prompt ran out, or it decided itself. An echo for
        an ask this session never decided (a replay after resume) is left as
        it came.
        """
        if not isinstance(event, PermissionResolved):
            return event
        decided_by = self._decided_by.pop(event.request_id, None)
        if decided_by is None:
            return event
        return event.model_copy(update={"decided_by": _event_provenance(decided_by)})

    async def _permission_loop(self, sub: AsyncIterator[Event]) -> None:
        """Watch for `PermissionRequest` events, run the effect-aware policy, and
        only prompt the human for a genuine PROMPT.

        The policy (``request_auto_decision``) is the single chokepoint: it reads
        the request's typed ``subject`` (bash via the classifier, SQL via the
        gate, fs/network via the adapter map) and the project policy, so an
        auto-allow/-reject resolves here without ever calling the broker. That
        gives daemon parity for free — the editor resolver only fires for prompts.
        """
        assert self._broker is not None
        async for event in sub:
            if not isinstance(event, PermissionRequest):
                continue
            if getattr(event, "subject_pending", False) is True:
                # The ask reached the adapter before the part naming what it
                # gates; the adapter re-raises it, under the same id, once the
                # part lands. Deciding now would judge a blank subject.
                continue
            self._deciding = event.request_id
            try:
                option_id, reason, decided_by = await self._decide_reconsidering(event)
            except AuditUnavailableError:
                # An unrecordable decision is refused, not granted in silence. The
                # engine already does this for the descriptor path; this catches the
                # coarse kind-only and sandbox paths, which audit outside it.
                option_id, reason, decided_by = "reject_once", AUDIT_FAILED_REASON, "audit"
            except asyncio.CancelledError:
                # Same teardown race as the question loop: a cancel mid-decide
                # would leave the harness parked on a permission reply forever.
                # Deliver the rejection (shielded from this cancellation), then
                # unwind.
                self._decided_by[event.request_id] = "cancelled"
                with contextlib.suppress(Exception):
                    await asyncio.shield(
                        self._adapter.resolve_permission(event.request_id, "reject_once")
                    )
                raise
            finally:
                self._deciding = None
                self._reconsidered_note = None
                # A prompt the final decision did not come from is taken down,
                # and is down before the agent is answered: no reader may still
                # hold an ask the agent has already been told the outcome of.
                await self._broker.withdraw(event.request_id)
            # Recorded BEFORE the reply goes out: the harness echoes it back as
            # ``permission.resolved`` and the stamp must already be waiting.
            self._decided_by[event.request_id] = decided_by
            if not str(option_id).startswith("allow"):
                # The one place a refusal's words are composed for the model and
                # the card: the policy's reason alone, or a person's refusal.
                reason = refusal_feedback(decided_by, reason)
            try:
                await self._adapter.resolve_permission(event.request_id, option_id, reason=reason)
            except Exception:
                logger.exception(
                    "adapter rejected permission reply for %s",
                    event.request_id,
                )
                continue
            if not str(option_id).startswith("allow"):
                await self._after_reject(event, decided_by)

    async def _decide_reconsidering(
        self, event: PermissionRequest
    ) -> tuple[PermissionOptionId, str | None, str]:
        """``_decide_permission``, run again from the top each time the mode
        changes while a person is being asked, until a decision stands."""
        while True:
            try:
                return await self._decide_permission(event)
            except AskReconsideredError:
                logger.info(
                    "permission mode changed to %s; deciding %s again",
                    self._permission_mode,
                    event.request_id,
                )

    def _audit_sink(self) -> Any:
        """Where a decision is recorded: the session's log, with the mode change
        named on a decision a mode change had made again."""
        note = self._reconsidered_note
        if note is None:
            return self.decision_sink
        return NotedDecisionSink(self.decision_sink, note)

    async def _after_reject(self, event: PermissionRequest, decided_by: str) -> None:
        """Decide what a delivered REJECT does to the turn. The lever is the denial
        MESSAGE (set in ``_decide_permission``): an AUTOMATIC policy denial
        (mode/rule/floor/judge) rides a reason back, which the harness surfaces as a
        recoverable tool-error so the turn keeps going and the model adapts in-turn
        (opencode: a reason-bearing reject is a ``CorrectedError``, NOT turn-ending;
        Claude: the SDK feeds the deny back and continues). A MANUAL ``human`` reject
        sent NO reason, so opencode ends the turn itself (a ``RejectedError``); the
        Claude SDK still continues on any deny, so we interrupt it to hand control back.
        An automatic denial repeated is not a reason to end the turn: the repetition
        costs the model its own round trips, while ending the turn costs the person
        everything else that turn was going to do. Every ask is still recorded."""
        if decided_by in _HANDED_BACK:
            # opencode already ended the turn (reason-less RejectedError). Claude's SDK
            # doesn't, so interrupt it. Scoped to Claude because a redundant interrupt
            # on opencode would synthesize a spurious 'aborted' after the clean stop.
            if self._is_claude:
                with contextlib.suppress(Exception):
                    await self.cancel()

    def _is_sandbox_write(self, descriptor: ActionDescriptor | None) -> bool:
        """True when ``descriptor`` is a mutation confined to THIS chat's sandbox dir.

        Alkera-managed scratch, auto-allowed in every mode (incl. plan / read_only) so
        the model can write its ``plan.md`` and scratch files there while project edits
        are refused, unless the sandbox is a workspace's shared folder the mode does not
        write (:func:`write_free_root`).

        Two families qualify, and a shell command qualifies only when EVERY destination
        it writes is inside the sandbox. A shell write used to be excluded wholesale, so
        ``echo plan >> sandbox/plan.md`` was refused while the edit tool could write the
        same file. Reading the destinations closes both halves: a redirect into the
        sandbox is the scratch write it looks like, and one anywhere else is not folded
        in with it. A command whose destinations cannot be read at all is NOT a sandbox
        write; the unreadable case falls to the gate, which is the side that asks.
        """
        if descriptor is None or self._tool_binding is None:
            return False
        binding, mode = self._tool_binding, self._permission_mode
        sandbox = write_free_root(mode, binding.sandbox_dir, self._chat.path)
        if sandbox is None or str(descriptor.effect) not in ("write", "destroy"):
            return False
        try:
            sandbox_resolved = Path(sandbox).resolve()
        except (OSError, ValueError):
            return False
        if descriptor.capability == "shell":
            return self._shell_writes_only_into(descriptor, sandbox_resolved)
        # Scope tightly: an actual filesystem mutation (not a shell command that merely
        # mentions the path, not a read), and only write/destroy effects.
        if descriptor.capability != "fs":
            return False
        raw = descriptor.raw or (descriptor.targets[0].name if descriptor.targets else None)
        if not isinstance(raw, str) or not raw:
            return False
        return self._within_sandbox(raw, sandbox_resolved)

    def _shell_writes_only_into(self, descriptor: ActionDescriptor, sandbox: Path) -> bool:
        """Whether every destination this shell command writes is under ``sandbox``.

        A command the model cannot read whole, one that writes nothing it can
        see, and one whose destination follows a ``cd`` are all the gate's to
        judge, not the sandbox's.
        """
        import alkera_cli.plugins.plugin_base.permissions.shell as shell

        command = descriptor.raw
        if not isinstance(command, str) or not command:
            return False
        effects = shell.analyze_shell(command, backslash_escapes=shell.backslash_escapes_here())
        if not effects.readable or not effects.writes:
            return False
        return all(not w.moved and self._within_sandbox(w.text, sandbox) for w in effects.writes)

    def _within_sandbox(self, raw: str, sandbox: Path) -> bool:
        """Whether ``raw`` names a location inside ``sandbox``.

        A wildcard is never inside it: what a glob expands to is decided when the
        command runs, and no check made now can vouch for every match.
        """
        if any(char in raw for char in "*?["):
            return False
        if self._path_fence is not None:
            # A path the agent spelled at the sandbox's own alias of the
            # directory is the host directory it is bound to.
            raw = self._path_fence.host_path(raw)
        try:
            target = Path(raw).expanduser()
            if not target.is_absolute():
                # opencode reports the path worktree-relative; the worktree is the
                # project root (the parent of the .alkera dir).
                target = self._runtime.project.path.parent / target
            target = target.resolve()
        except (OSError, ValueError, RuntimeError):
            return False
        return target == sandbox or sandbox in target.parents

    async def _decide_permission(
        self, event: PermissionRequest
    ) -> tuple[PermissionOptionId, str | None, str]:
        """Resolve one vendor tool ask via the SINGLE engine (``DecisionEngine``)
        — the SAME pipeline the in-tool SQL gate uses, so the policy + floor + the
        auto-mode judge + the human prompt + the audit + always-persist apply
        uniformly. Returns ``(option, reason, decided_by)``: the reason rides a REJECT
        back to the model as a provenance-aware denial message (``denial_message`` —
        names the mode/rule/floor/judge so it course-corrects, not just "rejected");
        ``decided_by`` tells the loop whether this was a MANUAL ``human`` reject (end
        the turn) or an automatic policy denial (continue). A subject-LESS ask (no
        typed descriptor) takes the coarse kind-only fallback. A session carrying a
        ``path_fence`` is bounded first: an ask naming a location outside the fence
        is refused with the fence's reason, ahead of every allow this method can
        reach (the read fast path included). The matching
        ``PermissionResolved`` is emitted by the reply path on BOTH backends, so a
        request never dangles."""
        assert self._broker is not None
        from alkera_cli.plugins.plugin_base.permissions import (
            DecisionEngine,
            load_permissions_cached,
        )
        from alkera_cli.plugins.plugin_base.permissions.audit import ledger_for_sink
        from alkera_cli.plugins.plugin_base.permissions.wiring import fs_write_evidence

        try:
            permissions = load_permissions_cached(self._runtime.project.path)
        except Exception:  # a broken policy file must never wedge the gate
            permissions = None
        rd = request_auto_decision(
            self._permission_mode,
            event,
            permissions,
            # A session with a fence is a cloud box's, which serves more than one person.
            shared_host=self._path_fence is not None,
        )
        # What a bounded session may touch is decided BEFORE any allow. A read is
        # the reason this cannot wait for the broker: an in-root read auto-allows
        # here, and a read of the box's own token would auto-allow the same way, so
        # the bound has to be part of this decision rather than an answer to a
        # prompt that is never raised. The reject carries the fence's reason, which
        # is what makes it a recoverable tool error instead of the end of the turn.
        if self._path_fence is not None:
            bounded = self._permission_mode != "bypass"
            try:
                escaped = self._path_fence.escape(event)
                unprovable = escaped is None and self._reach_unprovable(event)
            except Exception:
                # The judge did not run, so it read nothing: it caught no escape
                # and it cleared none either. "The boundary could not be
                # checked" is not "there is no boundary", so the stance that
                # asks nobody still does not run this — bypass hands over the
                # ASKING, never the bound, exactly as it does for a location the
                # fence DID read as an escape. Where a person is asked, they are
                # asked, below. Either way the exception stops here: letting it
                # leave would end the permission loop, the one task that answers
                # asks, and the chat would hold a card nobody could settle.
                logger.exception("path fence could not judge %s", event.request_id)
                if not bounded:
                    await self._audit_decision(event, rd, "reject_once", "fence_error", None)
                    return "reject_once", FENCE_UNCHECKED_REASON, "fence_error"
                escaped, unprovable = None, True
            if escaped is not None:
                await self._audit_decision(event, rd, "reject_once", "fence", None)
                return "reject_once", self._path_fence.explain(event, escaped), "fence"
            if unprovable and bounded:
                # The fence could not prove where this goes, so no rule, judge or
                # read fast path may allow it on its own. The mode's own refusal
                # still comes first (an analyst's session runs no shell at all);
                # after it, a person is asked. Bypass is the stance that runs
                # everything without asking: a command the fence cannot read is
                # not a boundary it caught, so it takes the ordinary ladder below,
                # exactly as a classified write does. The boundary itself is not
                # waived — an escape the fence DID read was refused just above.
                if rd.decision == "reject":
                    await self._audit_decision(event, rd, "reject_once", "mode", None)
                    return (
                        "reject_once",
                        denial_message(mode=self._permission_mode, decided_by="mode"),
                        "mode",
                    )
                # The one standing answer that reaches a command the fence
                # cannot read: an exact-text "allow" in the box owner's policy.
                # Answers a chat recorded are never consulted on a shared host.
                exact = getattr(
                    permissions.for_host(shared_host=True) if permissions is not None else None,
                    "exact_text_decision",
                    None,
                )
                if rd.descriptor is not None and exact is not None:
                    if exact(rd.descriptor, mode=self._permission_mode) is AutoDecision.ALLOW:
                        await self._audit_decision(event, rd, "allow_once", "rule", None)
                        return "allow_once", None, "rule"
                decided = await self._broker.decide(event)
                # A fenced session records no standing answer (the ask was
                # published without one). An answer that names one anyway is
                # bound to this call rather than sent on to the vendor, which
                # would learn a coarse prefix rule and stop raising the ask for
                # everything under it, retiring the fence.
                option = bind_to_this_ask(decided.option)
                await self._audit_decision(event, rd, option, decided.decided_by, None)
                return option, decided.reason, decided.decided_by
        # The chat sandbox is Alkera-managed scratch — a filesystem write confined to
        # it is auto-allowed in EVERY mode (incl. plan / read_only), so the model can
        # write its plan.md + scratch files there even while project edits are refused.
        # This is what makes file-based plan mode work on opencode (the model writes
        # <sandbox>/plan.md, then present_plan(path)); the Claude adapter has the
        # equivalent carve-out in can_use_tool. Decided BEFORE the mode reject below.
        if self._is_sandbox_write(rd.descriptor):
            await self._audit_decision(event, rd, "allow_once", "sandbox", None)
            return "allow_once", None, "sandbox"
        if rd.descriptor is None:
            return await self._coarse_decision(event, rd)

        engine = DecisionEngine(
            sink=self._audit_sink(),
            permissions=permissions,
            broker=self._broker,
            judge=bound_to(self._runtime._safety_judge, chat_token(self._credential)),
            intents=ledger_for_sink(self.decision_sink),
            alkera_dir=self._runtime.project.path,
            workspace_root=str(self._runtime.project.path.parent),
            source="harness",
            session_id=self.session_id,
            shared_host=self._path_fence is not None,
            owner=self._knowledge_owner() or "",
        )
        res = await engine.resolve(
            rd.descriptor,
            mode=self._permission_mode,
            task_goal=self._last_user_text,
            tool_call_id=event.tool_call_id,
            request=event,
            evidence=await fs_write_evidence(
                rd.descriptor,
                registry=self._tool_binding.registry if self._tool_binding else None,
                workspace_root=self._runtime.project.path.parent,
                alkera_dir=self._runtime.project.path,
            ),
        )
        if res.judge_unavailable:
            await self._stop_turn("auto mode judge unavailable; stopping the turn")
        out_reason = res.reason
        if not res.allowed and res.decided_by in _HANDED_BACK:
            # Manual reject → no reason, so opencode ends the turn (RejectedError). See
            # the coarse-path note above; Claude is interrupted in _after_reject.
            out_reason = None
        elif not res.allowed:
            # An AUTOMATIC denial → a provenance-aware message (mode/rule/floor/judge +
            # the classifier's detail; a self-explaining reason rides verbatim) sent back
            # as a recoverable tool-error so the model course-corrects in-turn.
            out_reason = denial_message(
                mode=self._permission_mode,
                decided_by=res.decided_by,
                classifier_reason=res.reason,
                effect=str(rd.descriptor.effect),
            )
        return res.option, out_reason, res.decided_by

    async def _coarse_decision(
        self, event: PermissionRequest, rd: RequestDecision
    ) -> tuple[PermissionOptionId, str | None, str]:
        """The subject-less fallback: no descriptor to judge or persist, so the
        coarse kind decision stands and only the human middle asks."""
        assert self._broker is not None
        option: PermissionOptionId
        reason: str | None
        if rd.decision == "allow":
            option, decided_by, reason = "allow_once", "kind", None
        elif rd.decision == "reject":
            # A coarse reject comes from the active mode policy → tell the model.
            option, decided_by = "reject_once", "kind"
            reason = denial_message(mode=self._permission_mode, decided_by="mode")
        else:
            # A human reject sends NO reason on purpose: opencode turns a
            # reason-less reject into a turn-ending ``RejectedError`` (control goes
            # back to the user), where a reason-bearing one would be a recoverable
            # ``CorrectedError`` that keeps the turn going. (Claude is interrupted
            # in ``_after_reject`` instead — its SDK continues on any deny.) A
            # prompt that ran out is the same hand-back, attributed honestly; a
            # resolver refusing on its own grounds carries its reason and stays.
            decided = await self._broker.decide(event)
            option, reason, decided_by = decided.option, decided.reason, decided.decided_by
            if self._path_fence is not None:
                option = bind_to_this_ask(option)
        await self._audit_decision(event, rd, option, decided_by, None)
        return option, reason, decided_by

    async def _audit_decision(
        self,
        event: PermissionRequest,
        rd: RequestDecision,
        option: PermissionOptionId,
        decided_by: str,
        extra_reasons: list[str] | None,
    ) -> None:
        """Append the resolved decision to ``.alkera/decisions.jsonl`` (off the
        event loop). Raises ``AuditUnavailableError`` when the append fails, which the
        permission loop turns into a refusal."""
        from alkera_cli.plugins.plugin_base.permissions import DecisionRecord

        decision = "allow" if str(option).startswith("allow") else "reject"
        if rd.descriptor is not None:
            rec = DecisionRecord.from_descriptor(
                rd.descriptor,
                decision=decision,
                decided_by=decided_by,
                mode=self._permission_mode,
                source="harness",
                session_id=self.session_id,
                request_id=event.request_id,
                tool_call_id=event.tool_call_id,
                extra_reasons=extra_reasons,
            )
        else:
            rec = DecisionRecord(
                at=datetime.now(UTC).timestamp(),
                session_id=self.session_id,
                request_id=event.request_id,
                tool_call_id=event.tool_call_id,
                source="harness",
                capability=event.permission_kind,
                operation=event.canonical_kind,
                mode=self._permission_mode,
                decision=decision,
                decided_by=decided_by,
                reasons=extra_reasons or [],
            )
        await asyncio.to_thread(self._audit_sink().record, rec)

    async def _stop_turn(self, detail: str) -> None:
        """Cancel the in-flight turn and surface a clear error — mirrors how a
        gateway credit exhaustion ends a turn."""
        try:
            await self._bus.publish(
                SessionStatusChanged(
                    event_id=secrets.token_hex(10),
                    time=datetime.now(UTC),
                    session_id=self.session_id,
                    status="error",
                    phase="error",
                    detail=detail,
                    turn_id=self._active_attempt,
                )
            )
        except Exception:
            logger.debug("stop-turn status publish failed", exc_info=True)
        with contextlib.suppress(Exception):
            await self._adapter.cancel()

    async def _question_loop(self, sub: AsyncIterator[Event]) -> None:
        """Watch for `QuestionRequest` events and route to the broker.

        The broker resolves to either an answer or a rejection; we
        forward via `adapter.answer_question` / `reject_question`.
        Errors auto-reject so the harness doesn't deadlock waiting.
        """
        assert self._question_broker is not None
        async for event in sub:
            if not isinstance(event, QuestionRequest):
                continue
            try:
                resolution = await self._question_broker.resolve(event)
            except asyncio.CancelledError:
                # Teardown (client disconnect / close_chat) cancelled us while
                # the question was pending — the harness is still parked on a
                # reply. Deliver the rejection on the way out, shielded so the
                # same cancellation can't kill the delivery. The server fails
                # outbound futures before the shutdown hooks cancel this task,
                # but whether the resulting ConnectionError wakeup or the
                # cancel lands first is a scheduler race (the Windows
                # thread-bridged pipes reliably lose it); reject-on-cancel
                # makes the auto-reject order-independent.
                with contextlib.suppress(Exception):
                    await asyncio.shield(
                        self._adapter.reject_question(event.request_id, "client-disconnected")
                    )
                raise
            except Exception:
                logger.exception(
                    "question broker failed; auto-rejecting %s",
                    event.request_id,
                )
                resolution = ("reject", "broker-error")
            kind, payload = resolution
            try:
                if kind == "answer":
                    await self._adapter.answer_question(
                        event.request_id,
                        payload,  # type: ignore[arg-type]
                    )
                else:
                    await self._adapter.reject_question(
                        event.request_id,
                        payload,  # type: ignore[arg-type]
                    )
            except Exception:
                logger.exception(
                    "adapter rejected question reply for %s",
                    event.request_id,
                )


__all__ = [
    "PERSIST_QUEUE_MAXSIZE",
    "AdapterFactory",
    "ChatSession",
    "HarnessRuntime",
    "ends_attempt",
]
