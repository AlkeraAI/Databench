"""Concrete `HarnessAdapter` driving Claude Code through the Claude Agent SDK.

The SDK (`claude-agent-sdk`) spawns and manages the `claude` CLI subprocess. We
hold a `ClaudeSDKClient`, pump its `receive_messages()` stream into our
`EventBus` (translated to IR), and drive turns with `query()` / `interrupt()`.
It parallels :class:`alkera_cli.harness.adapters.opencode_http.OpencodeHttpAdapter`
with the SDK as the transport.

- **Permission parity with opencode.** Claude Code stays in
  `permission_mode="default"` and every mutating tool is forced to "ask" via
  `settings.permissions`, so our broker and `mode_auto_decision` decide every
  gate through the `can_use_tool` callback. No native Claude Code mode is pushed.
- **Plan mode is ours.** A steering prompt plus an in-process `present_plan`
  SDK-MCP tool drive the `QuestionRequest(kind="plan_approval")` surface; the
  broker blocks edits.
- **Per-chat state and deterministic resume.** `CLAUDE_CONFIG_DIR` points the
  CLI's transcript at `<chat>/.runtime/`; we pin a session UUID
  (`native_state()["agent_session_id"]`) and resume by id.
- **Gateway-routed.** The lockdown env keeps non-model traffic quiet and all
  model traffic goes through `ANTHROPIC_BASE_URL` (the gateway). WebFetch and
  WebSearch are enabled behind the broker, so this is not a strict airgap.
  `harness_native["web_fetch_enabled"] = False` removes WebFetch from the tool
  set, because this backend never gets the `web` MCP mount that switch acts
  through on opencode.
- **Streaming closure.** Open text and reasoning parts are synthesized closed on
  cancel or crash, as on opencode.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
import secrets
import subprocess
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from alkera_core.process import bind_to_parent_lifetime, spawn_kwargs
from alkera_core.schemas.chat import (
    CanonicalPermissionKind,
    ConversationCleared,
    Event,
    FileEdited,
    MessageCreated,
    PartCreated,
    PermissionOption,
    PermissionRequest,
    PermissionResolved,
    QuestionOption,
    QuestionPrompt,
    QuestionRequest,
    ReasoningPart,
    SessionStatusChanged,
    TextPart,
    ToolCallUpdate,
    TurnFinished,
    TurnStarted,
)
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLIConnectionError,
    CLINotFoundError,
    PermissionResultAllow,
    PermissionResultDeny,
    ProcessError,
    create_sdk_mcp_server,
    tool,
)

from alkera_cli.contracts.tool_types import ActionDescriptor, Effect, ResourceRef
from alkera_cli.harness.adapter import (
    MCP_NEVER_TIMEOUT_MS,
    HarnessAdapter,
    HarnessCrashError,
    HarnessInfo,
    HarnessNotReadyError,
    HarnessStartError,
    HarnessUnavailableError,
    PromptInput,
    SessionConfig,
)
from alkera_cli.harness.adapters._file_diff import diff_preview, unified_diff_for
from alkera_cli.harness.adapters.claude_model import ClaudeModelState, ClaudeModelSwitching
from alkera_cli.harness.adapters.claude_translate import (
    ClaudeEventTranslator,
    _ClaudeTranslatorContext,
    _OpenQuery,
)
from alkera_cli.harness.claude_binary import ResolvedClaudeBinary, claude_is_available
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.orphan_sweep import register_agent
from alkera_cli.harness.permission_mode import (
    NOT_GRANTED,
    PLAN_ACCEPT_OPTIONS,
    plan_label_to_mode,
    plan_mode_steering,
    plan_note_of,
    with_plan_note,
)
from alkera_cli.harness.sensitive_paths import escalate_sensitive_path
from alkera_cli.plugins.plugin_base.permissions import (
    classify_command,
)
from alkera_cli.plugins.plugin_base.permissions.resolve import scoped_options

logger = logging.getLogger(__name__)

# The MCP server + tool the model calls to hand a plan to the user. The model
# sees the namespaced name ``mcp__<server>__<tool>``.
_PLAN_SERVER = "plan"
_PLAN_TOOL = "present_plan"
_PLAN_TOOL_FQN = f"mcp__{_PLAN_SERVER}__{_PLAN_TOOL}"

#: OUR general-question tool (the harness-owned replacement for Claude Code's
#: disallowed native AskUserQuestion). Drives `QuestionRequest(kind="question")`.
_ASK_SERVER = "ask"
_ASK_TOOL = "present_question"
_ASK_TOOL_FQN = f"mcp__{_ASK_SERVER}__{_ASK_TOOL}"

#: The Alkera plugin tool surface — the fixed hot set
#: (search_tools + call_tool) as in-process SDK-MCP tools. Identical contract to
#: the OpenCode local-MCP transport (both derive from `ToolRegistry.hot_prefix`).
_ALKERA_SERVER = "alkera"

# Per-turn plan-mode steering — the SAME unified contract as opencode (single
# source: ``plan_mode_steering``), named with OUR plan/question tools.
_CLAUDE_PLAN_STEERING = plan_mode_steering(_PLAN_TOOL_FQN, _ASK_TOOL_FQN)
#: The same steering for a deployment that serves no agent-spawning tools.
_CLAUDE_PLAN_STEERING_NO_SUBAGENTS = plan_mode_steering(
    _PLAN_TOOL_FQN, _ASK_TOOL_FQN, subagents=False
)

# Appended to Claude Code's DEFAULT system prompt (preset, not replaced) on every
# connect — steers the model to OUR question tool, since its native AskUserQuestion
# is disallowed. One line so it doesn't bloat the prompt.
_CLAUDE_SYSTEM_APPEND = (
    f"To ask the user a question mid-task, call the `{_ASK_TOOL_FQN}` tool and wait "
    "for the answer — never stop with the question written as plain prose. (Native "
    "tools like AskUserQuestion are unavailable in this environment.)"
)

# Returned when a subagent reaches an interactive tool (plan approval / ask-user).
# A subagent runs headless — no human watches it — so blocking on a human verdict
# would deadlock it. We never surface these tools to a subagent (see `_build_options`);
# this is the belt-and-braces handler guard for any path that slips through (e.g. a
# resumed transcript that already contains the call).
_SUBAGENT_INTERACTIVE_DENIED = (
    "This tool is unavailable to a subagent — there is no user watching a subagent to "
    "answer. Do not ask for approval or input: finish your work and return your findings "
    "to the agent that spawned you."
)


# Hand-written JSON Schema for present_question. The SDK passes an input_schema
# dict that has a top-level "type" straight through (otherwise it treats the dict
# as {field: type}). We build it by hand rather than from a TypedDict because this
# module uses `from __future__ import annotations`: PEP 563 stringizes the field
# annotations, which makes TypedDict mis-compute `NotRequired` (every field would
# be marked required). A raw schema gives exact control over the optional fields.
_ASK_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "description": (
                "The question(s) to ask. Each needs a `question`; optionally a "
                "`header` (short title), `options` to choose from, and `multiSelect`."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question text to show the user.",
                    },
                    "header": {
                        "type": "string",
                        "description": "Short category/title for the question (e.g. 'Framework').",
                    },
                    "options": {
                        "type": "array",
                        "description": "Suggested answers to choose from; omit for free-form.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {
                                    "type": "string",
                                    "description": "A short, selectable answer shown to the user.",
                                },
                                "description": {
                                    "type": "string",
                                    "description": "Optional one-line elaboration of this option.",
                                },
                            },
                            "required": ["label"],
                        },
                    },
                    "multiSelect": {
                        "type": "boolean",
                        "description": "Allow selecting multiple options (default false).",
                    },
                },
                "required": ["question"],
            },
        }
    },
    "required": ["questions"],
}

# Claude Code tool name → canonical permission kind. Unknown / MCP tools → "other"
# (still flow through the prompt path safely). The orchestrator's policy reasons
# ONLY in canonical terms (see permission_mode.mode_auto_decision).
_CC_TOOL_TO_CANONICAL: dict[str, CanonicalPermissionKind] = {
    "Bash": "shell",
    "Edit": "edit",
    "Write": "edit",
    "MultiEdit": "edit",
    "NotebookEdit": "edit",
    "Task": "task",
    "WebFetch": "network",
    "WebSearch": "network",
}

# The file-writing tools whose target path we check against the chat sandbox to
# auto-allow scratch writes (incl. plan.md in plan mode).
_SANDBOX_WRITE_TOOLS: frozenset[str] = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})

# EVERY permission-relevant tool is forced to "ask" (→ our can_use_tool) — NO
# vendor heuristic decides anything (full passthrough). Even read-only
# tools (Read/Glob/Grep/LS) ask, so OUR classifier+policy auto-allows them rather
# than Claude Code's built-in read-only heuristic. The ONLY auto-allowed entries
# are our own in-process MCP tools (plan/ask), which gate in-parent. Sandbox is
# pinned off so a future SDK default flip can't auto-allow bash without consulting
# the broker. The direct analogue of opencode's `_OPENCODE_PERMISSION_ASK`.
_PERMISSION_SETTINGS: dict[str, Any] = {
    "permissions": {
        "allow": [_PLAN_TOOL_FQN, _ASK_TOOL_FQN],
        # Current Claude Code tool names (MultiEdit was merged into Edit). The
        # canonical map still covers MultiEdit harmlessly for older/other builds.
        # WebFetch/WebSearch are gated here too — Claude has web access back,
        # routed through our broker (GETs classify as read, so they auto-allow).
        # WebFetch drops out of BOTH this list and the tool set when the
        # deployment's web.fetch switch is off (`_web_fetch_enabled`).
        "ask": ["Bash", "Edit", "Write", "Read", "Glob", "Grep", "LS", "WebFetch", "WebSearch"],
    },
    # Belt-and-braces: sandbox stays off so `autoAllowBashIfSandboxed` can never
    # bypass our broker for bash (we own permissions, not the vendor sandbox).
    "sandbox": {"enabled": False},
}

# Tools disabled entirely. We keep ONLY the portable, harness-agnostic primitives
# (Bash/Edit/Write + the read-only set above + the broker-gated WebFetch/WebSearch)
# and disallow everything Claude Code-specific, because Alkera owns those features
# itself and must NOT depend on the vendor's implementation. Disallowed = removed
# from the model's tool set (it can't call them; no permission prompt fires).
# AskUserQuestion is here: in our headless wrapping it isn't wired to a real UI (it
# returns an empty answer) and our harness drives questions via its own
# QuestionRequest surface (e.g. plan approval). We also do NOT use Claude's native
# plan mode (EnterPlanMode/ExitPlanMode) — our plan flow is the `present_plan` MCP
# tool + steering. (WebFetch/WebSearch are intentionally NOT here — Claude has web
# access, gated through the broker; this trades the strict airgap for web reach.
# WebFetch is APPENDED per session when the deployment's web.fetch switch is off.)
_DISALLOWED_TOOLS: list[str] = [
    # questions / subagents / notebooks — Alkera provides these itself
    "AskUserQuestion",
    "Task",
    "NotebookEdit",
    # extensibility surfaces — vendor-specific
    "Skill",
    "Workflow",
    # native plan mode — we drive plan mode ourselves (present_plan + steering)
    "EnterPlanMode",
    "ExitPlanMode",
    # worktrees / scheduling / cron / monitoring / notifications — Alkera's domain
    "EnterWorktree",
    "ExitWorktree",
    "ScheduleWakeup",
    "CronCreate",
    "CronDelete",
    "CronList",
    "Monitor",
    "PushNotification",
    "RemoteTrigger",
    # task / todo / progress tracking — Alkera owns this surface itself (the
    # unified `manage_tasks` DAG tool, plugin_base/task_tools.py)
    "TodoWrite",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskUpdate",
    "TaskOutput",
    "TaskStop",
]

# Native bash is disabled in favor of OUR parent-hosted `bash` loopback-MCP tool —
# but ONLY on POSIX, where that replacement is registered (it relies on process
# groups / signals). On Windows the (Windows-capable) native Bash stays until the
# parent-hosted port is cross-platform, so a Windows agent always has a shell.
if os.name == "posix":
    _DISALLOWED_TOOLS.append("Bash")

# The ONLY tools that should legitimately reach `can_use_tool`: the full
# permission-relevant set the broker now gates — the "ask" list (incl. the
# read-only tools, per the passthrough flip) + MultiEdit for older builds.
# Vendor-specific tools are disallowed. The deny-list above is necessarily a
# moving target — newer Claude Code builds keep adding vendor tools (Monitor /
# PushNotification / RemoteTrigger appeared after the first cut) — so anything
# else arriving at the callback is a tool this harness doesn't support and is
# auto-DENIED, never prompted. Alkera owns these features; we never fall back to
# the vendor's implementation.
_GATED_TOOLS: frozenset[str] = frozenset(
    {"Bash", "Edit", "Write", "MultiEdit", "Read", "Glob", "Grep", "LS", "WebFetch", "WebSearch"}
)

# Claude tool name → (capability, effect) for the static (non-bash) descriptors
# the broker reasons over. Bash is classified by tree-sitter (see
# ``_descriptor_for_tool``); these are the deterministic rest.
#
# WebFetch is EGRESS, not read: whatever the model already holds can be encoded
# into the URL's path or query and shipped to a host the fetched instruction
# chose, which is a channel out of the machine rather than a look at one.
# WebSearch stays read — its query goes to a fixed search provider, not to a host
# the prompt injector picks.
_CC_TOOL_TO_ACTION: dict[str, tuple[str, Effect]] = {
    "Edit": ("fs", Effect.WRITE),
    "Write": ("fs", Effect.WRITE),
    "MultiEdit": ("fs", Effect.WRITE),
    "NotebookEdit": ("fs", Effect.WRITE),
    "Read": ("fs", Effect.READ),
    "Glob": ("fs", Effect.READ),
    "Grep": ("fs", Effect.READ),
    "LS": ("fs", Effect.READ),
    "WebFetch": ("network", Effect.EGRESS),
    "WebSearch": ("network", Effect.READ),
}

# Every input key under which a Claude Code tool names a filesystem LOCATION.
# Each tool picks a different one — Read/Write/Edit ship ``file_path``, Glob and
# Grep put the directory they walk in ``path`` (their ``pattern`` is the search
# pattern, not a place), NotebookEdit ships ``notebook_path`` — so reading only
# ``file_path`` would leave a grep of ``~/.aws`` looking like a plain read.
_CC_PATH_INPUT_KEYS: tuple[str, ...] = (
    "file_path",
    "notebook_path",
    "path",
    "glob",
)

# Tools whose ask names a DIRECTORY to walk rather than a single file. When they
# omit it they walk the project root, so that is what gets scanned; with no root
# to fall back on we cannot say what is being read and the ask is escalated
# rather than trusted.
_CC_DIRECTORY_SCOPED_TOOLS: frozenset[str] = frozenset({"Glob", "Grep", "LS"})


# The file-mutating tools whose diff we compute from the tool input at ask-time.
# MultiEdit is included for older builds (it was merged into Edit upstream).
_EDIT_TOOLS: frozenset[str] = frozenset({"Write", "Edit", "MultiEdit"})


def _edit_diff_from_input(
    tool_input: dict[str, Any],
) -> tuple[int, int, dict[str, Any]] | None:
    """Build `(insertions, deletions, preview)` for a Write/Edit/MultiEdit by
    diffing the file's current bytes against the proposed result. The SDK gives
    no diff, so we read the old text here (a missing file is a create → old is
    ""). Returns ``None`` when there's no usable `file_path` or the edit's
    `old_string` isn't found in the file (the SDK would reject it too)."""
    path = tool_input.get("file_path")
    if not isinstance(path, str) or not path:
        return None
    old = _read_text_or_empty(path)
    new = _apply_edit_to_text(old, tool_input)
    if new is None or new == old:
        return None
    return diff_preview(path, unified_diff_for(old, new, path))


def _read_text_or_empty(path: str) -> str:
    """The file's text, or "" when it doesn't exist / can't be read — a missing
    file is the create case (an all-additions diff), never an error."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _apply_edit_to_text(old: str, tool_input: dict[str, Any]) -> str | None:
    """The proposed new file text. Write replaces the whole file with `content`;
    Edit replaces `old_string` with `new_string` (the SDK's `replace_all` flag
    swaps every occurrence, else just the first); MultiEdit applies its `edits`
    list in order. ``None`` when an edit's `old_string` isn't present — an edit
    the harness can't apply, so there's nothing to preview."""
    if "content" in tool_input:  # Write — full-file replacement
        content = tool_input.get("content")
        return content if isinstance(content, str) else None
    raw_edits = tool_input.get("edits")
    edits = raw_edits if isinstance(raw_edits, list) else [tool_input]
    text = old
    for edit in edits:
        if not isinstance(edit, dict):
            continue
        target = edit.get("old_string")
        replacement = edit.get("new_string")
        if not isinstance(target, str) or not isinstance(replacement, str):
            return None
        if target not in text:
            return None
        count = -1 if edit.get("replace_all") is True else 1
        text = text.replace(target, replacement, count)
    return text


def _input_paths(tool_input: dict[str, Any]) -> list[str]:
    """Every filesystem location a tool's input names, in key order."""
    return [
        value
        for value in (tool_input.get(key) for key in _CC_PATH_INPUT_KEYS)
        if isinstance(value, str) and value
    ]


def _descriptor_for_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    *,
    sandbox_dir: Path | None = None,
    workspace_root: Path | None = None,
) -> ActionDescriptor | None:
    """Build the typed ``ActionDescriptor`` the broker policy reasons over.

    Bash is classified by the shell classifier (so ``rm -rf`` hits the floor and
    ``ls`` auto-allows); fs/network tools map statically. Every fs descriptor then
    passes the sensitive-path floor, so a read of a credential file cannot be
    auto-allowed just because it arrived through a file tool instead of a shell.
    ``None`` for a tool with no descriptor (the resolver falls back to the
    canonical kind)."""
    if tool_name == "Bash":
        command = tool_input.get("command")
        if isinstance(command, str):
            return classify_command(command)
        return None
    action = _CC_TOOL_TO_ACTION.get(tool_name)
    if action is None:
        return None
    capability, effect = action
    target = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("url")
    targets = (
        [ResourceRef(kind="file" if capability == "fs" else "url", name=str(target))]
        if isinstance(target, str)
        else []
    )
    descriptor = ActionDescriptor(
        capability=capability,
        effect=effect,
        operation=tool_name.lower(),
        targets=targets,
        raw=str(target) if isinstance(target, str) else None,
        classifier="claude-tool",
    )
    if capability != "fs":
        return descriptor
    located = _input_paths(tool_input)
    candidates = list(located)
    unresolved = False
    if tool_name in _CC_DIRECTORY_SCOPED_TOOLS and not located:
        # No directory in the input → the tool walks the project root. Scan THAT;
        # with no root to resolve we cannot tell what is being read, so fail closed.
        if workspace_root is None:
            unresolved = True
        else:
            candidates.append(str(workspace_root))
    return escalate_sensitive_path(
        descriptor,
        candidates,
        unresolved=unresolved,
        sandbox_dir=sandbox_dir,
        workspace_root=workspace_root,
    )


# Lockdown env — silence every non-model outbound call. (Belt-and-suspenders: a
# real egress allowlist is still recommended at deploy time.)
_LOCKDOWN_ENV: dict[str, str] = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_FEEDBACK_COMMAND": "1",
    "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
    "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
}

_PERMISSION_OPTIONS: list[PermissionOption] = [
    PermissionOption(option_id="allow_once", name="Allow once"),
    PermissionOption(option_id="allow_always", name="Always allow"),
    PermissionOption(option_id="reject_once", name="Reject once"),
    PermissionOption(option_id="reject_always", name="Always reject"),
]


#: Windows Job Object handles for spawned `claude` children, held open for the
#: process lifetime so the OS kills the children when alkera dies. Empty/no-op on
#: POSIX (binding happens at spawn via setsid + PR_SET_PDEATHSIG).
_claude_jobs: list[Any] = []


def _detach_claude_from_controlling_terminal() -> None:
    """Spawn the underlying ``claude`` in its OWN session (``setsid``), so the
    controlling terminal's Ctrl+C — delivered by the kernel as ``SIGINT`` to the
    ENTIRE foreground process group — does NOT reach the child. The ``alkera``
    REPL installs its own SIGINT handler that routes Ctrl+C to a graceful turn
    interrupt (``adapter.cancel()`` → ``client.interrupt()``); without this
    detachment the terminal would SIGINT-kill ``claude`` directly instead. This
    mirrors the opencode harness, which spawns its subprocess with
    ``start_new_session=True`` for exactly the same reason.

    The Claude Agent SDK calls ``anyio.open_process(...)`` with no
    ``start_new_session`` and exposes no option for it, so we rebind the ``anyio``
    NAME in its subprocess-transport module to a thin proxy that injects
    ``start_new_session=True`` and delegates everything else to the real module.
    Scoped to that one module — the global ``anyio`` is untouched. Idempotent +
    best-effort (logs and continues if the SDK's internals move, in which case
    Ctrl+C reverts to interrupting the child directly)."""
    try:
        from claude_agent_sdk._internal.transport import subprocess_cli
    except Exception:  # pragma: no cover - SDK internal layout changed
        logger.warning(
            "claude harness: SDK subprocess transport not found; the child won't be "
            "detached from the terminal, so Ctrl+C may interrupt it directly"
        )
        return

    # The SDK module's `anyio` is a plain module attribute we deliberately rebind;
    # mypy can't model that dynamic patch, so reach it through an `Any` view.
    transport_mod: Any = subprocess_cli
    real = transport_mod.anyio
    if getattr(real, "_alkera_detached", False):
        return  # already proxied (idempotent)

    class _DetachedAnyio:
        """Proxy for the SDK transport's ``anyio`` that detaches spawned children
        from the terminal's Ctrl-C AND binds them to our lifetime (no orphans);
        everything else delegates to the real module."""

        _alkera_detached = True

        async def open_process(self, *args: Any, **kwargs: Any) -> Any:
            # Detach the child (setsid / process group) and give it no stdin
            # unless the SDK asks for a pipe, never clobbering an explicit value
            # and injecting ONLY kwargs open_process accepts. anyio has no
            # `preexec_fn` (Linux PR_SET_PDEATHSIG), so the claude child relies on
            # the agent registry and startup sweep for parent-death cleanup.
            injectable = {"stdin": subprocess.DEVNULL, **spawn_kwargs()}
            try:
                params = inspect.signature(real.open_process).parameters
                has_var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
                if not has_var_kw:
                    injectable = {k: v for k, v in injectable.items() if k in params}
            except (TypeError, ValueError):
                # Signature not introspectable — inject only the one kwarg
                # every anyio version has accepted.
                injectable = {k: v for k, v in injectable.items() if k == "start_new_session"}
            for key, value in injectable.items():
                kwargs.setdefault(key, value)
            proc = await real.open_process(*args, **kwargs)
            # Bind the child to our lifetime so a hard-killed alkera can't orphan
            # it, and register it for the startup sweep (macOS net). Best-effort.
            pid = getattr(proc, "pid", None)
            if isinstance(pid, int):
                with contextlib.suppress(Exception):
                    job = bind_to_parent_lifetime(pid)
                    if job is not None:
                        # Hold the Job handle open for the process lifetime — when
                        # alkera dies, the OS closes it and kills the child.
                        _claude_jobs.append(job)
                    register_agent(pid)
            return proc

        def __getattr__(self, name: str) -> Any:
            return getattr(real, name)

    transport_mod.anyio = _DetachedAnyio()


@dataclass(slots=True)
class _AdapterState:
    started: bool = False
    stopping: bool = False
    crashed: bool = False
    #: True for the whole of a clear's transport swap: the OLD subprocess dying
    #: inside the window is the swap itself, never a crash of the adapter.
    retiring: bool = False
    client: ClaudeSDKClient | None = None
    pump_task: asyncio.Task[None] | None = None


# Resolution of a question/permission bridge: ("answer", answers) | ("reject", reason).
_QuestionResolution = tuple[str, Any]


class ClaudeAgentAdapter(ClaudeModelSwitching, HarnessAdapter):
    """Drives one Claude Code session (via the SDK) for one chat."""

    # The manifest harness_type slug + the value in events' `harness` field.
    # Distinct from opencode's "agent". Stored in every chat manifest, so it never
    # changes.
    name = "claude-agent"
    capabilities = frozenset({"resume", "clear", "permission_runtime", "subagents"})

    @classmethod
    def is_available(cls) -> bool:
        # Only available when Claude Code is installed LOCALLY — we never ship the
        # proprietary `claude` binary, so we drive the user's own install
        # (discovered via ALKERA_CLAUDE_BIN / $PATH / known locations / the
        # claude-agent-sdk wheel's bundled CLI in dev). See claude_binary.py.
        return claude_is_available()

    def __init__(
        self,
        config: SessionConfig,
        *,
        binary: ResolvedClaudeBinary,
        event_bus: EventBus | None = None,
    ) -> None:
        self._config = config
        self._binary = binary
        self._bus = event_bus or EventBus()
        self._state = _AdapterState()
        self._translator_ctx = _ClaudeTranslatorContext(session_id=config.session_id)
        self._translator = ClaudeEventTranslator(self._translator_ctx)
        self._harness_dir = config.chat_dir / ".runtime"

        # Resume precedence: a pinned session id (from a prior run) → resume it;
        # otherwise mint a fresh UUID for a new chat. (CC requires a UUID.)
        pinned = config.harness_native.get("agent_session_id")
        if isinstance(pinned, str) and pinned:
            self._session_id = pinned
            self._resume = True
        else:
            self._session_id = str(uuid.uuid4())
            self._resume = False

        self._models = ClaudeModelState.from_config(config.model)
        self._model_id = self._models.applied

        # Callback bridges: the SDK awaits these; resolve_permission /
        # answer_question / reject_question complete the parked futures. A
        # permission future resolves (option_id, deny_reason) — the reason (judge/
        # policy) becomes the model-visible denial message.
        self._pending_permissions: dict[str, asyncio.Future[tuple[str, str | None]]] = {}
        self._pending_questions: dict[str, asyncio.Future[_QuestionResolution]] = {}
        # Edit diffs computed at ask-time, keyed by tool_use_id. The SDK gives no
        # post-write diff, so we build it from the tool input in `_can_use_tool`
        # (and ship it on the PermissionRequest for the pre-approval view), then
        # drain it onto a synthesized `FileEdited` when the tool's result lands —
        # so the write card renders the same diff opencode's `file.edited` carries.
        self._pending_edit_diffs: dict[str, tuple[str, int, int, dict[str, Any]]] = {}
        """tool_use_id → (path, insertions, deletions, preview)."""

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def info(self) -> HarnessInfo:
        return HarnessInfo(name=self.name, source=self._binary.source, version=self._binary.version)

    def native_state(self) -> dict[str, Any]:
        # The harness-agnostic manifest key, the same one opencode's adapter uses.
        return {"agent_session_id": self._session_id}

    @property
    def event_bus(self) -> EventBus:
        return self._bus

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def _is_subagent(self) -> bool:
        """Whether this session was spawned by another (mirrors the runtime's
        ``is_subagent``). Subagents are denied the interactive plan/ask tools."""
        return self._config.parent_session_id is not None

    @property
    def _web_fetch_enabled(self) -> bool:
        """Whether this session may reach an arbitrary URL.

        The deployment's ``AGENT_WEB_FETCH_ENABLED`` switch, resolved by the
        runtime from the same tool registry the opencode `web` mount is derived
        from — one decision, two backends. Absent means enabled: an adapter built
        directly (a test, an older embedder) keeps the web reach it always had,
        and the switch only ever subtracts."""
        return self._config.harness_native.get("web_fetch_enabled", True) is not False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._state.started:
            raise RuntimeError("ClaudeAgentAdapter.start() called twice")
        self._harness_dir.mkdir(parents=True, exist_ok=True)
        await self._connect(resume=self._resume)
        self._state.started = True

    async def stop(self) -> None:
        if self._state.stopping:
            return
        self._state.stopping = True
        await self._teardown()
        await self._bus.close()

    async def _connect(self, *, resume: bool) -> None:
        client = await self._spawn_client(resume=resume)
        self._state.client = client
        self._state.pump_task = asyncio.create_task(self._pump(), name="claude-pump")

    async def _spawn_client(self, *, resume: bool) -> ClaudeSDKClient:
        """Spawn and connect a client without installing it, so a fallible
        (re)connect can run before any state is torn down."""
        # Spawn `claude` in its own session so the terminal's Ctrl+C interrupts
        # the TURN (via our SIGINT handler → cancel() → interrupt()) instead of
        # killing the child. Idempotent; applied here, right before the spawn.
        _detach_claude_from_controlling_terminal()
        options = self._build_options(resume=resume)
        client = ClaudeSDKClient(options)
        try:
            await client.connect(prompt=None)
        except CLINotFoundError as exc:
            raise HarnessUnavailableError(
                f"the Claude Code CLI isn't available on this install — {exc}"
            ) from exc
        except (CLIConnectionError, ProcessError) as exc:
            raise HarnessStartError(f"claude harness failed to start: {exc}") from exc
        except Exception as exc:
            raise HarnessStartError(f"claude harness failed to start: {exc}") from exc
        return client

    async def _teardown(self) -> None:
        """Stop the pump + disconnect the client. Cancelling the pump raises
        CancelledError (NOT treated as a crash), so this is safe for both stop()
        and clear()."""
        task = self._state.pump_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._state.pump_task = None
        client = self._state.client
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()
        self._state.client = None

    # ------------------------------------------------------------------
    # Message pump
    # ------------------------------------------------------------------

    async def _pump(self) -> None:
        client = self._state.client
        assert client is not None
        try:
            async for message in client.receive_messages():
                for event in self._translator.translate(message):
                    await self._bus.publish(event)
                    await self._maybe_emit_file_edited(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._state.stopping or self._state.crashed:
                return
            if self._state.retiring or self._state.client is not client:
                # A clear is swapping the transport (or already has): this
                # pump's death is the OLD subprocess going away, not a crash
                # of the adapter now serving the chat.
                return
            self._state.crashed = True
            logger.warning("claude harness pump error: %s", exc, exc_info=True)
            await self._synthesize_close_events(
                self._translator_ctx.close_open_queries(oldest_only=False),
                stop_reason="error",
                error_detail=str(exc) or type(exc).__name__,
            )

    def _capture_edit_diff(
        self, tool_name: str, tool_input: dict[str, Any], tool_use_id: str | None
    ) -> tuple[int | None, int | None, dict[str, Any] | None]:
        """Compute an edit tool's diff at ask-time (the SDK ships none) and stash
        it by ``tool_use_id`` for `_maybe_emit_file_edited` to drain. Returns the
        preview for the `PermissionRequest`, or all-``None`` when there's nothing
        to show (non-edit tool, unappliable edit, or no tool_use_id)."""
        if tool_name not in _EDIT_TOOLS or not isinstance(tool_use_id, str):
            return None, None, None
        captured = _edit_diff_from_input(tool_input)
        if captured is None:
            return None, None, None
        insertions, deletions, preview = captured
        self._pending_edit_diffs[tool_use_id] = (
            str(tool_input.get("file_path", "")),
            insertions,
            deletions,
            preview,
        )
        return insertions, deletions, preview

    async def _maybe_emit_file_edited(self, event: Event) -> None:
        """Drain an ask-time edit diff onto a `FileEdited` once its write lands —
        the post-write signal opencode emits natively but the Claude SDK does not.
        Only a TERMINAL `ToolCallUpdate` drains (an interim ``running`` update
        carries the same id and must not consume it early); a failed write drops
        the stash so the card shows the error."""
        if not isinstance(event, ToolCallUpdate) or event.status not in ("completed", "error"):
            return
        stashed = self._pending_edit_diffs.pop(event.tool_call_id, None)
        if stashed is None or event.status == "error":
            return
        path, insertions, deletions, preview = stashed
        await self._bus.publish(
            FileEdited(
                event_id=self._new_id(),
                time=self._now(),
                session_id=self._our_sid(),
                path=path,
                insertions=insertions,
                deletions=deletions,
                preview=preview,
            )
        )

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    async def send_prompt(self, prompt: PromptInput) -> None:
        self._require_ready()
        # The model string carries the turn's model, effort and display.
        ran = await self._apply_turn_model(prompt)
        client = self._state.client
        assert client is not None

        turn_id = prompt.turn_id
        # Registered BEFORE the write (a fast result must find its record) and
        # retired if the write raises (the CLI never received it).
        query = _OpenQuery(turn_id)
        self._translator_ctx.open_queries.append(query)
        now = self._now()
        sid = self._our_sid()
        # Record the user's message (MessageCreated[user] + its text part) BEFORE
        # the turn. The SDK never streams the human prompt back (unlike opencode's
        # SSE), so we synthesize it — otherwise nothing user-role is persisted and
        # resume-replay, which anchors turns on user-role MessageCreated, replays
        # nothing and the chat looks brand new. Use prompt.text (the original), not
        # the steered text below. Mirrors opencode's user-message emission.
        user_message_id = f"umsg_{secrets.token_hex(8)}"
        await self._bus.publish(
            MessageCreated(
                event_id=self._new_id(),
                time=now,
                session_id=sid,
                message_id=user_message_id,
                role="user",
            )
        )
        if prompt.text:
            await self._bus.publish(
                PartCreated(
                    event_id=self._new_id(),
                    time=now,
                    session_id=sid,
                    part=TextPart(
                        part_id=self._new_id(),
                        message_id=user_message_id,
                        text=prompt.text,
                    ),
                )
            )
        await self._bus.publish(
            TurnStarted(
                event_id=self._new_id(),
                time=now,
                session_id=sid,
                turn_id=turn_id,
                user_message_id=user_message_id,
                model=ran,
            )
        )
        await self._bus.publish(
            SessionStatusChanged(
                event_id=self._new_id(),
                time=self._now(),
                session_id=self._our_sid(),
                status="running",
                phase="submitting",
                turn_id=turn_id,
            )
        )

        text = prompt.text
        steer = self._plan_steering(prompt)
        if steer:
            text = f"<system-reminder>\n{steer}\n</system-reminder>\n\n{text}"

        try:
            self._models.queried = True
            await client.query(text)
        except BaseException as exc:
            # The CLI never received the query, so no result will ever pop it:
            # retire it if it is still queued, however a concurrent cancel may
            # have marked it meanwhile (only a pump-popped record stays gone).
            if query in self._translator_ctx.open_queries:
                self._translator_ctx.open_queries.remove(query)
            if isinstance(exc, (ProcessError, CLIConnectionError)):
                raise HarnessCrashError(f"send_prompt failed: {type(exc).__name__}: {exc}") from exc
            raise

    def _plan_steering_text(self) -> str:
        """The plan-mode steering for THIS session. A registry with subagents off
        never served the spawn tool, so the variant that leans on Explore would
        send the turn after a tool the model does not have. No registry (tests)
        keeps the full steering, matching what those sessions advertise."""
        registry = self._config.harness_native.get("tool_registry")
        if registry is not None and not getattr(registry, "subagents_enabled", True):
            return _CLAUDE_PLAN_STEERING_NO_SUBAGENTS
        return _CLAUDE_PLAN_STEERING

    def _plan_steering(self, prompt: PromptInput) -> str | None:
        """Per-turn steering text injected as a leading ``<system-reminder>``. Plan
        turns (agent=="plan") get OUR plan prompt; ANY explicit per-turn ``system``
        addendum (mode steering + the main-agent guidance / a subagent's prompt) is
        APPENDED — never dropped — so a plan-mode turn keeps both. The user's GLOBAL
        instructions are appended last (the Claude analogue of opencode's
        `config.instructions[]` — Claude's sandboxed home means it can't find a
        global CLAUDE.md, so we feed the content through the same per-turn channel the
        rest of the steering uses)."""
        parts = [
            p
            for p in (
                # A subagent is never put in plan mode (the runtime downgrades a plan
                # ceiling to read_only), and even if it were, it has no present_plan /
                # present_question tool — so never steer it toward them.
                self._plan_steering_text()
                if (prompt.agent == "plan" and not self._is_subagent)
                else None,
                prompt.system,
                self._global_instructions_block(),
            )
            if p
        ]
        return "\n\n".join(parts) if parts else None

    def _global_instructions_block(self) -> str | None:
        """The user's GLOBAL instructions (`~/.alkera/instructions.md`), already
        size-capped by the runtime and threaded in via
        `harness_native["global_instructions"]`. Labeled so the model reads it as
        standing user guidance. ``None`` when unset."""
        content = self._config.harness_native.get("global_instructions")
        if isinstance(content, str) and content.strip():
            return f"Global instructions (apply to every project):\n{content}"
        return None

    async def cancel(self) -> None:
        if not self._state.started or self._state.crashed:
            return
        client = self._state.client
        if client is None:
            return
        # The query closes BEFORE the interrupt: its result follows the ack and
        # must find it closed however the tasks interleave.
        closing = self._translator_ctx.close_open_queries(oldest_only=True)
        with contextlib.suppress(Exception):
            await client.interrupt()
        # Close any open parts immediately so the UI never sees orphans, even if
        # the CLI's post-interrupt result lags or omits content_block_stop.
        await self._synthesize_close_events(closing, stop_reason="cancelled")

    async def clear(self) -> None:
        """Reset the conversation to empty — next turn sees none of the prior
        turns. Claude Code has no "clear context, keep files" op, so we mint a
        fresh session id and connect a new client to it (no resume).

        The new client connects FIRST: a failed spawn leaves the conversation,
        any running turn, and this adapter untouched, so the runtime's promise
        that a failed clear settles nothing holds on this transport too."""
        self._require_ready()
        old = self._session_id
        self._session_id = str(uuid.uuid4())
        # `retiring` covers the WHOLE swap, the spawn await included: the old
        # pump still names the installed client while the replacement spawns,
        # so a client-identity check alone cannot tell its death from a crash.
        self._state.retiring = True
        try:
            client = await self._spawn_client(resume=False)
        except BaseException:
            self._session_id = old
            self._state.retiring = False
            raise
        try:
            cleared = list(self._translator_ctx.seen_message_ids)
            closing = self._translator_ctx.close_open_queries(oldest_only=False)
            if closing or self._translator_ctx.open_parts:
                await self._synthesize_close_events(closing, stop_reason="cancelled")
            await self._teardown()
            self._translator.reset()
            self._pending_edit_diffs.clear()
            self._state.client = client
            self._state.pump_task = asyncio.create_task(self._pump(), name="claude-pump")
        finally:
            self._state.retiring = False
        await self._bus.publish(
            ConversationCleared(
                event_id=self._new_id(),
                time=self._now(),
                session_id=self._our_sid(),
                cleared_message_ids=cleared,
            )
        )
        logger.info(
            "cleared chat %s: claude session %s → %s",
            self._our_sid(),
            old,
            self._session_id,
        )

    async def resolve_permission(
        self, request_id: str, option_id: str, *, reason: str | None = None
    ) -> None:
        fut = self._pending_permissions.get(request_id)
        if fut is not None and not fut.done():
            fut.set_result((option_id, reason))
        # Emit the resolution so the chat record pairs every request with an
        # outcome (symmetric with opencode's permission.replied). The accurate
        # policy/user provenance is recorded separately in decisions.jsonl.
        await self._bus.publish(
            PermissionResolved(
                event_id=self._new_id(),
                time=self._now(),
                session_id=self._our_sid(),
                request_id=request_id,
                option_id=option_id,  # type: ignore[arg-type]
            )
        )

    async def answer_question(self, request_id: str, answers: list[list[str]]) -> None:
        fut = self._pending_questions.get(request_id)
        if fut is not None and not fut.done():
            fut.set_result(("answer", answers))

    async def reject_question(self, request_id: str, reason: str | None = None) -> None:
        fut = self._pending_questions.get(request_id)
        if fut is not None and not fut.done():
            fut.set_result(("reject", reason))

    def subscribe(self) -> AsyncIterator[Event]:
        return self._bus.subscribe()

    # ------------------------------------------------------------------
    # Permission bridge (can_use_tool → broker → resolve_permission)
    # ------------------------------------------------------------------

    def _is_sandbox_path(self, tool_input: dict[str, Any]) -> bool:
        """True when an edit/write targets a file inside THIS chat's sandbox dir —
        Alkera-managed scratch we auto-allow in every mode (incl. plan, where the
        model writes plan.md). Resolves a relative path against the project cwd."""
        raw = tool_input.get("file_path") or tool_input.get("notebook_path")
        if not isinstance(raw, str) or not raw:
            return False
        sandbox = (self._config.chat_dir / "sandbox").resolve()
        try:
            target = Path(raw)
            if not target.is_absolute():
                target = self._config.cwd / target
            target = target.resolve()
        except (OSError, ValueError):
            return False
        return target == sandbox or sandbox in target.parents

    async def _can_use_tool(
        self, tool_name: str, tool_input: dict[str, Any], context: Any
    ) -> PermissionResultAllow | PermissionResultDeny:
        # Alkera's own MCP tools (the parent-hosted loopback server, Option A) gate
        # write effects INSIDE dispatch via the session broker, so auto-allow
        # them at this layer. IMPORTANT: external HTTP-MCP tools DO reach
        # can_use_tool (unlike in-process SDK-MCP tools like plan/ask), so without
        # this branch the model's mcp__alkera__* calls are auto-denied below.
        if tool_name.startswith(f"mcp__{_ALKERA_SERVER}__"):
            return PermissionResultAllow(updated_input=tool_input)
        # Defense-in-depth (see _GATED_TOOLS): only the broker-gated mutating tools
        # ever legitimately reach here. Anything else is a vendor tool that slipped
        # past the disallow-list (e.g. a newly-shipped Claude Code tool) — auto-deny
        # it silently rather than surfacing a permission prompt for a tool we don't
        # support. This is what makes AskUserQuestion-style leaks never prompt.
        if tool_name not in _GATED_TOOLS:
            logger.info("claude harness: auto-denying unsupported tool %r", tool_name)
            return PermissionResultDeny(message=f"{tool_name} is not available in this harness")
        # Auto-allow writes confined to THIS chat's sandbox (Alkera-managed scratch) —
        # including the plan.md write in plan mode, where edits are otherwise denied.
        # The sandbox is the agent's private scratch, never the user's project files.
        if tool_name in _SANDBOX_WRITE_TOOLS and self._is_sandbox_path(tool_input):
            return PermissionResultAllow(updated_input=tool_input)
        loop = asyncio.get_running_loop()
        tool_use_id = getattr(context, "tool_use_id", None)
        request_id = tool_use_id or self._new_id()
        fut: asyncio.Future[tuple[str, str | None]] = loop.create_future()
        self._pending_permissions[request_id] = fut
        # Attach the typed ActionDescriptor (bash via the classifier, fs/network
        # static) so the broker policy reasons over effect — not the coarse kind.
        # The sandbox dir and project root let the fs floor tell a credential path
        # from the chat's own scratch, and resolve a relative path before scanning.
        descriptor = _descriptor_for_tool(
            tool_name,
            tool_input,
            sandbox_dir=self._config.chat_dir / "sandbox",
            # The agent's own cwd: what a relative path in the call resolves
            # against, which for a cloud chat is its folder, not the project.
            workspace_root=self._config.cwd,
        )
        insertions, deletions, preview = self._capture_edit_diff(tool_name, tool_input, tool_use_id)
        await self._bus.publish(
            PermissionRequest(
                event_id=self._new_id(),
                time=self._now(),
                session_id=self._our_sid(),
                request_id=request_id,
                # Claude Code keys the call and the ask by the one tool_use id, so
                # the transcript's key and the provider's are the same value.
                tool_call_id=tool_use_id if isinstance(tool_use_id, str) else None,
                provider_call_id=tool_use_id if isinstance(tool_use_id, str) else None,
                permission_kind=tool_name,
                canonical_kind=_cc_tool_to_canonical(tool_name),
                patterns=[descriptor.raw] if descriptor and descriptor.raw else [],
                subject=descriptor.model_dump(mode="json") if descriptor else None,
                options=scoped_options(_PERMISSION_OPTIONS, descriptor),
                insertions=insertions,
                deletions=deletions,
                preview=preview,
            )
        )
        try:
            option_id, deny_reason = await fut
        except asyncio.CancelledError:
            return PermissionResultDeny(message="cancelled")
        finally:
            self._pending_permissions.pop(request_id, None)
        if option_id in ("allow_once", "allow_always"):
            return PermissionResultAllow(updated_input=tool_input)
        # The judge's / policy's reason becomes the model-visible denial message so
        # the model understands WHY and course-corrects (not just "rejected").
        return PermissionResultDeny(message=deny_reason or NOT_GRANTED)

    # ------------------------------------------------------------------
    # Plan-approval bridge (present_plan MCP tool → question broker)
    # ------------------------------------------------------------------

    def _read_plan_file(self, path: str) -> str:
        """Read the model's plan file, constrained to the chat sandbox. Returns ``""``
        when missing / unreadable (the caller then steers the model to write it). A
        relative path resolves against the project cwd; anything outside the sandbox
        falls back to the same basename INSIDE the sandbox (where the model was told
        to write), so a stray path can't read the user's project files."""
        raw = path.strip()
        if not raw:
            return ""
        sandbox = (self._config.chat_dir / "sandbox").resolve()
        try:
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = self._config.cwd / candidate
            candidate = candidate.resolve()
        except (OSError, ValueError):
            return ""
        target = (
            candidate
            if candidate == sandbox or sandbox in candidate.parents
            else sandbox / Path(raw).name
        )
        try:
            return target.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return ""

    async def _handle_present_plan(self, path: str) -> dict[str, Any]:
        if self._is_subagent:
            return _tool_text(_SUBAGENT_INTERACTIVE_DENIED)
        # Path-based: the model wrote the plan to a file in its sandbox; read it and
        # surface its content to the UI via the event (plan_markdown) — the model's
        # tool result stays a short approve/reject outcome, never the full plan.
        plan_markdown = self._read_plan_file(path)
        if not plan_markdown.strip():
            return _tool_text(
                f"No plan found at {path!r}. Write your plan as Markdown to a `plan.md` "
                f"file in your sandbox (`{self._config.chat_dir / 'sandbox' / 'plan.md'}`) "
                "with the write tool first, then call this tool with that path."
            )
        loop = asyncio.get_running_loop()
        request_id = self._new_id()
        fut: asyncio.Future[_QuestionResolution] = loop.create_future()
        self._pending_questions[request_id] = fut
        await self._bus.publish(
            QuestionRequest(
                event_id=self._new_id(),
                time=self._now(),
                session_id=self._our_sid(),
                request_id=request_id,
                kind="plan_approval",
                plan_markdown=plan_markdown,
                questions=[
                    QuestionPrompt(
                        question="The assistant proposed a plan. Approve it (and "
                        "choose how to proceed) or reject with a reason.",
                        header="plan",
                        options=[QuestionOption(label=label) for label, _ in PLAN_ACCEPT_OPTIONS],
                        multiple=False,
                        custom=True,
                    )
                ],
            )
        )
        try:
            kind, payload = await fut
        except asyncio.CancelledError:
            return _tool_text("Plan approval was cancelled.")
        finally:
            self._pending_questions.pop(request_id, None)
        if kind == "answer":
            chosen = ""
            if isinstance(payload, list) and payload and payload[0]:
                chosen = str(payload[0][0])
            note = plan_note_of(payload)
            # Only one of the explicit Accept options is an approval; the
            # orchestrator flips the permission mode for those (plan_label_to_mode
            # → a mode). ANY other answer is free-form feedback — i.e. a rejection
            # the user wants the model to revise against (parity with opencode,
            # whose custom text also rejects). plan_label_to_mode returns None for
            # non-accept answers.
            if plan_label_to_mode(chosen) is not None:
                # Echo the chosen accept label (as opencode's plan_present does) so
                # the CLI can recover which mode the approval switched into when it
                # replays this from disk — the live resolver knows it directly.
                return _tool_text(
                    with_plan_note(
                        f'Plan approved ("{chosen}"). Proceed with the implementation.', note
                    )
                )
            reason = chosen or "no reason given"
            return _tool_text(
                with_plan_note(
                    f"Plan rejected: {reason}. Revise the plan and call {_PLAN_TOOL_FQN} again.",
                    note,
                )
            )
        reason = str(payload) if payload else "no reason given"
        return _tool_text(
            f"Plan rejected: {reason}. Revise the plan and call {_PLAN_TOOL_FQN} again."
        )

    def _make_plan_server(self) -> Any:
        adapter = self
        # This is a CUSTOM tool the model wasn't trained on, so the description +
        # the parameter doc are its only spec — spell out the call's effect and the
        # return contract explicitly (the model can't infer them).
        _desc = (
            "Present your finished implementation plan to the user for approval. FIRST "
            "write the full plan as Markdown to a `plan.md` file in your sandbox directory "
            "(using the write tool), then call this tool with that file's PATH. Calling "
            "this BLOCKS until the user decides, and the returned text is your instruction: "
            "on approval, carry out the plan; if the user sends back feedback instead, that "
            "is a REJECTION — revise the plan FILE and call this tool again. This is the "
            "ONLY way to get a plan approved; never ask for approval in plain prose."
        )
        path_param = Annotated[
            str,
            "The PATH to your plan file — the plan.md you wrote in your sandbox. The user "
            "reviews its contents before ANY edit or command runs, so write a specific, "
            "self-contained plan there first.",
        ]

        @tool(_PLAN_TOOL, _desc, {"path": path_param})
        async def present_plan(args: dict[str, Any]) -> dict[str, Any]:
            return await adapter._handle_present_plan(str(args.get("path", "")))

        return create_sdk_mcp_server(_PLAN_SERVER, tools=[present_plan])

    # ------------------------------------------------------------------
    # Question bridge (present_question MCP tool → question broker)
    # ------------------------------------------------------------------

    async def _handle_present_question(self, questions_raw: Any) -> dict[str, Any]:
        """Surface the model's question(s) through the harness question system
        (``QuestionRequest(kind="question")``), block until the user answers via
        ``answer_question`` / ``reject_question``, and return their selection as
        text the model can act on. The harness-owned analogue of Claude Code's
        (disallowed) native AskUserQuestion + opencode's `question` tool."""
        if self._is_subagent:
            return _tool_text(_SUBAGENT_INTERACTIVE_DENIED)
        prompts = _coerce_question_prompts(questions_raw)
        if not prompts:
            return _tool_text("No question was provided (expected a non-empty `questions` list).")
        loop = asyncio.get_running_loop()
        request_id = self._new_id()
        fut: asyncio.Future[_QuestionResolution] = loop.create_future()
        self._pending_questions[request_id] = fut
        await self._bus.publish(
            QuestionRequest(
                event_id=self._new_id(),
                time=self._now(),
                session_id=self._our_sid(),
                request_id=request_id,
                kind="question",
                questions=prompts,
            )
        )
        try:
            kind, payload = await fut
        except asyncio.CancelledError:
            return _tool_text("The user dismissed the question without answering.")
        finally:
            self._pending_questions.pop(request_id, None)
        if kind == "answer" and isinstance(payload, list):
            return _tool_text(_format_question_answers(prompts, payload))
        reason = f" ({payload})" if (kind == "reject" and payload) else ""
        return _tool_text(f"The user dismissed the question without answering{reason}.")

    def _make_ask_server(self) -> Any:
        adapter = self
        # Custom tool — the description + parameter docs are its whole spec.
        _desc = (
            "Ask the user one or more questions and BLOCK until they answer; returns "
            "their chosen answer(s) as text for you to act on. Use this whenever you "
            "need a decision, preference, or missing detail only the user can provide "
            "(e.g. which option to take, an ambiguous requirement). This is the ONLY "
            "way to ask the user something mid-task — never just write the question "
            "as prose and stop."
        )

        @tool(_ASK_TOOL, _desc, _ASK_INPUT_SCHEMA)
        async def present_question(args: dict[str, Any]) -> dict[str, Any]:
            return await adapter._handle_present_question(args.get("questions"))

        return create_sdk_mcp_server(_ASK_SERVER, tools=[present_question])

    def _make_alkera_server(self) -> Any:
        """Expose the Alkera tool surface — the fixed hot set (``search_tools`` +
        ``call_tool``) — as in-process SDK-MCP tools. The model reaches the long
        tail via ``call_tool``, IDENTICAL to the OpenCode local-MCP transport
        (both derive from ``ToolRegistry.hot_prefix``). Returns
        ``None`` when no registry is wired (e.g. tests without plugins)."""
        registry = self._config.harness_native.get("tool_registry")
        if registry is None:
            return None
        from alkera_cli.plugins.plugin_base.mcp_entry import alkera_tool_descriptors
        from alkera_cli.plugins.plugin_base.wire import is_tool_error_result, model_facing_text

        def _make_handler(tool_name: str) -> Any:
            async def _handler(args: dict[str, Any]) -> dict[str, Any]:
                # MCP tools auto-allow → write effects gate INSIDE dispatch.
                # The hot tools are READ, so no broker is threaded here.
                result = await registry.dispatch(tool_name, args, broker=None)
                out = _tool_text(model_facing_text(result))
                # A failed tool call → the SDK-MCP ``is_error`` flag, so Claude gets a
                # proper tool-error result (mirrors the loopback transport's isError).
                if is_tool_error_result(result):
                    out["is_error"] = True
                return out

            return _handler

        tools_list = [
            tool(desc.name, desc.description, desc.input_schema)(_make_handler(desc.name))
            for desc in alkera_tool_descriptors(registry)
        ]
        return create_sdk_mcp_server(_ALKERA_SERVER, tools=tools_list)

    # ------------------------------------------------------------------
    # Options + env
    # ------------------------------------------------------------------

    def _permission_settings(self) -> dict[str, Any]:
        """The Claude Code ``settings`` payload. A subagent gets neither the plan nor
        the ask in-process tool registered (see ``_build_options``), so its allow-list
        is empty — there's no interactive in-process tool left to auto-allow.

        With the deployment's web.fetch switch off, WebFetch leaves the ask list
        as well as the tool set: a tool the model cannot call has no business
        being described as one the person will be asked about."""
        permissions: dict[str, Any] = dict(_PERMISSION_SETTINGS["permissions"])
        if self._is_subagent:
            permissions["allow"] = []
        if not self._web_fetch_enabled:
            permissions["ask"] = [t for t in permissions["ask"] if t != "WebFetch"]
        if permissions == _PERMISSION_SETTINGS["permissions"]:
            return _PERMISSION_SETTINGS
        return {**_PERMISSION_SETTINGS, "permissions": permissions}

    def _disallowed_tools(self) -> list[str]:
        """Tools removed from the model's set for this session.

        The static list plus, when the deployment's web.fetch switch is off,
        Claude's native ``WebFetch``. It has to be disallowed rather than left in
        the ask list: a broker denial still advertises the tool, so the model
        spends a turn discovering a refusal it was always going to get — and an
        operator who believes the switch took fetch away would be wrong on this
        backend. ``WebSearch`` is untouched; its query goes to a fixed provider,
        and the switch is about fetching an arbitrary URL."""
        tools = list(_DISALLOWED_TOOLS)
        if not self._web_fetch_enabled and "WebFetch" not in tools:
            tools.append("WebFetch")
        return tools

    def _build_options(self, *, resume: bool) -> ClaudeAgentOptions:
        mcp_servers: dict[str, Any] = {}
        # The plan-approval (`present_plan`) and ask-user (`present_question`) tools
        # are INTERACTIVE — they block on a human verdict. A subagent runs headless
        # (no human watches it), so surfacing them lets it deadlock. Never advertise
        # them to a subagent: it returns its findings to the parent instead. The main
        # session keeps both.
        if not self._is_subagent:
            mcp_servers[_PLAN_SERVER] = self._make_plan_server()
            mcp_servers[_ASK_SERVER] = self._make_ask_server()
        # Option A: prefer the parent-hosted loopback MCP server (shared with the
        # OpenCode backend) so writes gate through the live session broker. The
        # in-process SDK-MCP server remains a fallback for direct-adapter tests
        # that wire only a `tool_registry`.
        alkera_http = self._config.harness_native.get("alkera_mcp")
        if isinstance(alkera_http, dict) and isinstance(alkera_http.get("alkera"), dict):
            mcp_servers[_ALKERA_SERVER] = alkera_http["alkera"]
        else:
            alkera_server = self._make_alkera_server()
            if alkera_server is not None:
                mcp_servers[_ALKERA_SERVER] = alkera_server
        kwargs: dict[str, Any] = {
            "cwd": str(self._config.cwd),
            "env": self._build_env(),
            "permission_mode": "default",
            "can_use_tool": self._can_use_tool,
            "mcp_servers": mcp_servers,
            "disallowed_tools": self._disallowed_tools(),
            "settings": json.dumps(self._permission_settings()),
            "setting_sources": [],
            "include_partial_messages": True,
            # APPEND to (not replace) Claude Code's default prompt — steers the
            # model to our present_question tool (native AskUserQuestion is gone).
            # A subagent has no present_question tool, so it gets no such steering.
            "system_prompt": {
                "type": "preset",
                "preset": "claude_code",
                "append": "" if self._is_subagent else _CLAUDE_SYSTEM_APPEND,
            },
        }
        # Always pin our resolved binary — never let the SDK fall back to $PATH.
        kwargs["cli_path"] = str(self._binary.path)
        if self._model_id:
            kwargs["model"] = self._model_id
        if resume:
            kwargs["resume"] = self._session_id
        else:
            kwargs["session_id"] = self._session_id
        return ClaudeAgentOptions(**kwargs)

    def _build_env(self) -> dict[str, str]:
        env: dict[str, str] = dict(_LOCKDOWN_ENV)
        # Per-chat transcript isolation (the CC analogue of opencode's XDG redirect).
        env["CLAUDE_CONFIG_DIR"] = str(self._harness_dir)
        # Neutralize the PARENT Claude Code identity if alkera itself is running
        # inside a Claude Code session (dogfooding) — otherwise the child can
        # inherit the parent's session id / model. Harmless in production (these
        # are absent). options.env can only override, not delete, so set empty.
        env["CLAUDE_CODE_SESSION_ID"] = ""
        env["CLAUDE_CODE_EXECPATH"] = ""
        # An MCP tool call legitimately blocks while a human sits on a
        # permission prompt (gated writes dispatch in-parent and await the
        # broker) or a subagent runs — Claude Code's MCP timeouts must never be
        # the watchdog. Three DISTINCT knobs in the bundled binary, all needed:
        #   MCP_TOOL_TIMEOUT      — the per-call MCP-protocol timeout;
        #   MCP_TIMEOUT           — the underlying HTTP TRANSPORT request timeout
        #                           (default 30s; aborts the held-open request
        #                           with a "The operation timed out." DOMException,
        #                           NOT extended by progress — a SEPARATE layer);
        #   MCP_CONNECT_TIMEOUT_MS — the transport connect timeout (default 5s).
        # Covers the loopback alkera server AND the in-process SDK-MCP tools
        # (present_plan/present_question). One source for the value (see
        # MCP_NEVER_TIMEOUT_MS) so the opencode + Claude knobs never drift.
        env["MCP_TOOL_TIMEOUT"] = str(MCP_NEVER_TIMEOUT_MS)
        env["MCP_TIMEOUT"] = str(MCP_NEVER_TIMEOUT_MS)
        env["MCP_CONNECT_TIMEOUT_MS"] = str(MCP_NEVER_TIMEOUT_MS)
        # Caller-supplied gateway/auth/model env (the seam, like opencode's
        # agent_config): ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY / ANTHROPIC_MODEL /
        # ANTHROPIC_DEFAULT_HAIKU_MODEL, etc. Wins over the lockdown defaults.
        override = self._config.harness_native.get("claude_env")
        if isinstance(override, dict):
            env.update({str(k): str(v) for k, v in override.items()})
        return env

    # ------------------------------------------------------------------
    # Synthesize close (cancel / crash)
    # ------------------------------------------------------------------

    async def _synthesize_close_events(
        self,
        closed: list[_OpenQuery],
        *,
        stop_reason: str = "cancelled",
        error_detail: str | None = None,
    ) -> None:
        """Close open parts, emit a ``TurnFinished`` for each query in
        ``closed`` (what ``close_open_queries`` returned when the caller closed
        them), and publish a terminal for those closes or for crash/error."""
        now = self._now()
        sid = self._our_sid()
        for part in list(self._translator_ctx.open_parts.values()):
            text = "".join(part.buffer)
            closing: ReasoningPart | TextPart
            if part.part_type == "reasoning":
                closing = ReasoningPart(
                    part_id=part.part_id,
                    message_id=part.message_id,
                    text=text,
                    signature=part.signature,
                )
            else:
                closing = TextPart(part_id=part.part_id, message_id=part.message_id, text=text)
            await self._bus.publish(
                PartCreated(event_id=self._new_id(), time=now, session_id=sid, part=closing)
            )
        self._translator_ctx.open_parts.clear()

        for query in closed:
            await self._bus.publish(
                TurnFinished(
                    event_id=self._new_id(),
                    time=now,
                    session_id=sid,
                    turn_id=query.turn_id,
                    stop_reason=stop_reason,  # type: ignore[arg-type]
                    error_detail=error_detail,
                )
            )

        # Idle cancel stays silent; crash/error still reaches subscribers.
        if not closed and stop_reason != "error":
            return
        await self._bus.publish(
            SessionStatusChanged(
                event_id=self._new_id(),
                time=now,
                session_id=sid,
                status="error" if stop_reason == "error" else "aborted",
                phase="error" if stop_reason == "error" else "idle",
                detail=error_detail,
                turn_id=closed[-1].turn_id if closed else None,
            )
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _require_ready(self) -> None:
        if not self._state.started:
            raise HarnessNotReadyError("ClaudeAgentAdapter.start() not finished")
        if self._state.crashed:
            raise HarnessCrashError("claude adapter is in a crashed state")
        if self._state.stopping:
            raise HarnessCrashError("claude adapter is stopping")

    def _our_sid(self) -> str:
        return self._config.session_id

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _new_id(self) -> str:
        return secrets.token_hex(10)


def _cc_tool_to_canonical(tool_name: str) -> CanonicalPermissionKind:
    if tool_name.startswith("mcp__"):
        return "other"
    return _CC_TOOL_TO_CANONICAL.get(tool_name, "other")


def _tool_text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _coerce_question_prompts(raw: Any) -> list[QuestionPrompt]:
    """Defensively coerce the model-supplied ``questions`` arg into typed
    ``QuestionPrompt``s (mirrors opencode's translator: any bad field falls back
    to a safe default rather than raising on hostile/drifted input). Always sets
    ``custom=True`` so the user may type a free-form answer."""
    if not isinstance(raw, list):
        return []
    prompts: list[QuestionPrompt] = []
    for q in raw:
        if not isinstance(q, dict):
            continue
        question = q.get("question")
        if not isinstance(question, str) or not question:
            continue
        options: list[QuestionOption] = []
        raw_opts = q.get("options")
        if isinstance(raw_opts, list):
            for o in raw_opts:
                if not isinstance(o, dict):
                    continue
                label = o.get("label")
                if not isinstance(label, str) or not label:
                    continue
                desc = o.get("description")
                options.append(
                    QuestionOption(label=label, description=desc if isinstance(desc, str) else None)
                )
        header = q.get("header")
        multi = q.get("multiSelect")
        prompts.append(
            QuestionPrompt(
                question=question,
                header=header if isinstance(header, str) else None,
                options=options,
                multiple=multi if isinstance(multi, bool) else False,
                custom=True,
            )
        )
    return prompts


def _format_question_answers(prompts: list[QuestionPrompt], answers: list[Any]) -> str:
    """Render the user's per-question selections as text the model can act on,
    pairing each question (by its header or text) with the chosen label(s)."""
    lines: list[str] = []
    for i, prompt in enumerate(prompts):
        selected = answers[i] if i < len(answers) and isinstance(answers[i], list) else []
        chosen = ", ".join(str(s) for s in selected if s) if selected else "(no answer)"
        lines.append(f"- {prompt.header or prompt.question}: {chosen}")
    return "The user answered:\n" + "\n".join(lines)


# Touch imports reserved for type-only / future use so linters stay quiet.
_RESERVED: tuple[Any, ...] = (Path, field)


__all__ = ["ClaudeAgentAdapter"]
