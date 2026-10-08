"""Alkera harness library: drives external coding agents (opencode, Claude Code)
behind one async surface.

The CLI (`alkera run`) and the daemon (`alkera_cli.daemon.methods.harness`)
both import it in-process and share the same `HarnessRuntime`, `ChatSession`
and `HarnessAdapter` instances. The daemon adds no business logic; it
translates JSON-RPC requests into runtime calls and streams events back as
notifications.

The adapter contract and its invariants are in this package's `README.md`.
"""

from __future__ import annotations

from alkera_cli.harness.adapter import (
    HarnessAdapter,
    HarnessCrashError,
    HarnessError,
    HarnessGoneError,
    HarnessInfo,
    HarnessModelError,
    HarnessNotReadyError,
    HarnessStartError,
    HarnessStartRefusedError,
    HarnessUnavailableError,
    PathFence,
    PromptInput,
    SessionConfig,
)
from alkera_cli.harness.event_bus import DEFAULT_QUEUE_MAXSIZE, EventBus
from alkera_cli.harness.opencode_binary import (
    OpencodeBinaryNotFoundError,
    ResolvedOpencodeBinary,
    ResolvedSource,
    resolve_opencode_binary,
)
from alkera_cli.harness.permission_broker import PermissionBroker, Resolver
from alkera_cli.harness.permission_mode import (
    ALL_MODES,
    MODE_LABELS,
    MODE_MENU_ORDER,
    MODE_RULES,
    PLAN_ACCEPT_OPTIONS,
    ModeRule,
    PermissionMode,
    mode_auto_decision,
    mode_to_agent,
    next_mode,
    parse_mode,
    plan_label_to_mode,
)
from alkera_cli.harness.question_broker import (
    QuestionBroker,
    QuestionResolution,
    QuestionResolver,
)
from alkera_cli.harness.registry import (
    CLAUDE_HARNESS,
    HARNESS_CHOICES,
    OPENCODE_HARNESS,
    resolve_harness_type,
)
from alkera_cli.harness.runtime import (
    PERSIST_QUEUE_MAXSIZE,
    AdapterFactory,
    ChatSession,
    HarnessRuntime,
    ends_attempt,
)

__all__ = [
    "ALL_MODES",
    "CLAUDE_HARNESS",
    "DEFAULT_QUEUE_MAXSIZE",
    "HARNESS_CHOICES",
    "MODE_LABELS",
    "MODE_MENU_ORDER",
    "MODE_RULES",
    "OPENCODE_HARNESS",
    "PERSIST_QUEUE_MAXSIZE",
    "PLAN_ACCEPT_OPTIONS",
    "AdapterFactory",
    "ChatSession",
    "EventBus",
    "HarnessAdapter",
    "HarnessCrashError",
    "HarnessError",
    "HarnessGoneError",
    "HarnessInfo",
    "HarnessModelError",
    "HarnessNotReadyError",
    "HarnessRuntime",
    "HarnessStartError",
    "HarnessStartRefusedError",
    "HarnessUnavailableError",
    "ModeRule",
    "OpencodeBinaryNotFoundError",
    "PathFence",
    "PermissionBroker",
    "PermissionMode",
    "PromptInput",
    "QuestionBroker",
    "QuestionResolution",
    "QuestionResolver",
    "ResolvedOpencodeBinary",
    "ResolvedSource",
    "Resolver",
    "SessionConfig",
    "ends_attempt",
    "mode_auto_decision",
    "mode_to_agent",
    "next_mode",
    "parse_mode",
    "plan_label_to_mode",
    "resolve_harness_type",
    "resolve_opencode_binary",
]
