"""`HarnessAdapter` abstract base and supporting types.

A harness adapter wraps one external coding agent (opencode, Claude Code) in a
uniform async surface. The orchestrator (`HarnessRuntime`) only knows this ABC;
concrete adapters translate native protocols (HTTP+SSE, the Claude Agent SDK)
into the `alkera_core.schemas.chat.Event` IR.

The contract::

  start() ─► subscribe() yields events ─► send_prompt(…) ─► … ─► stop()

Errors raised by the adapter:

- `HarnessStartError`: the harness could not come up (binary missing, port
  bind failure, listen-line timeout, auth failure).
- `HarnessCrashError`: the harness died while in use (subprocess exited,
  repeated HTTP probe failures).
- `HarnessNotReadyError`: an operation was called before `start()` succeeded.

Streaming-interruption invariant: every `PartStarted` is followed by exactly
one `PartCreated` (the finalized form). On any failure mid-turn the adapter
synthesizes the missing ones, plus one terminal `SessionStatusChanged` stamped
with the attempt in flight (`PromptInput.turn_id`). `TurnStarted` and
`TurnFinished` are a Claude-only enrichment; opencode emits neither.
"""

from __future__ import annotations

import secrets
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alkera_core.schemas.chat import (
    CanonicalPermissionKind,
    Event,
    PermissionOptionId,
    PermissionRequest,
)

from alkera_cli.harness.permission_mode import PermissionMode

#: Effectively-infinite MCP request timeout in milliseconds. An alkera tool call
#: legitimately blocks for as long as a HUMAN sits on a permission prompt (gated
#: writes dispatch in-parent and await the broker) or a subagent runs — the MCP
#: transport must never be the watchdog, or the prompt dies under the user with
#: ``-32001 Request timed out``. 2^31-1 ms (~24.8 days) is the Node ``setTimeout``
#: ceiling; anything larger overflows and fires IMMEDIATELY. Lives here (a leaf
#: module) so the opencode config block (runtime) and the Claude env var
#: (claude_agent) reference ONE source, never two spellings that can drift.
MCP_NEVER_TIMEOUT_MS = 2_147_483_647

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class HarnessError(Exception):
    """Base class for adapter errors."""


class HarnessStartError(HarnessError):
    """The harness couldn't start. Raised from `start()` after exhausting
    retries. Caller surfaces a clear message + recovery hint to the
    user (CLI: print + exit; webview: error banner with "Retry").
    """


class HarnessStartRefusedError(HarnessStartError):
    """The agent came up — it bound its port and answered a request — and then
    REFUSED one of the calls `start()` makes to attach to a session.

    Kept apart from a plain `HarnessStartError` because the two ask for opposite
    responses. A spawn or a bind that fails rode machine weather (a cold launch,
    a scanner holding the fresh binary, a port hiccup), and a second attempt
    usually wins. A refusal comes from a process that is already serving: it
    describes state — an unreadable config root, a database it cannot open — a
    fresh process would meet again, so `start()` surfaces it instead of paying
    for two more spawns and reporting the third one's error. Still a
    `HarnessStartError`, so every caller's handling is unchanged.
    """


class HarnessSandboxRefusedError(HarnessStartRefusedError):
    """The box offers less sandbox than the chat requires. The message is the
    reader's whole sentence; the chat is refused, never run less bounded."""


class HarnessStoreUnreadableError(HarnessStartRefusedError):
    """The agent came up but cannot read its own session store — the database
    that travelled with the chat's folder from another machine answers a
    server error to the session listing. The transcript is the chat's record
    and the store a cache of it, so ``start()`` sets the store aside and opens
    a fresh session once, rather than refusing the chat on every attempt."""


class HarnessCrashError(HarnessError):
    """The harness died mid-session. Raised when we detect:
    - subprocess.wait() returns non-zero,
    - three consecutive HTTP probes fail,
    - the SSE stream errors AND reconnect-backoff exhausts.

    Before raising, the adapter MUST emit synthetic close events for
    any open parts + a terminal `SessionStatusChanged(status="error")`
    stamped with the attempt in flight.
    """


class HarnessGoneError(HarnessCrashError):
    """The agent process this adapter drove is gone — it exited, was killed
    (a memory limit, a signal) or stopped answering — and the adapter will
    never serve another turn. Raised by every operation after the crash was
    declared, so a caller that owns the session can tell "start a fresh agent
    and ask again" apart from a turn that failed on a live one."""


class HarnessNotReadyError(HarnessError):
    """Operation invoked before `start()` returned. Almost certainly a
    programming bug rather than runtime — surface loudly."""


class HarnessUnavailableError(HarnessError):
    """The harness required by a chat isn't available on this install
    (binary missing, wrong build, future `harness_type` value unknown
    to this client). User-facing — surface a clear message + recovery
    hint. Distinct from `HarnessStartError` (binary is present but
    fails to come up) and from `HarnessCrashError` (was running, died).
    """


class HarnessModelError(HarnessError):
    """The turn was refused because it has no model the harness may run:
    the session names none at all, or it names a provider the injected
    agent config does not route through the Alkera gateway. There is no
    fallback — a harness that guessed a model would run a turn nobody
    metered, billed or audited. User-facing: the adapter publishes the
    same message as a ``status="error"`` on the bus so every chat surface
    renders it, then raises this.
    """


@dataclass(slots=True, frozen=True)
class HarnessInfo:
    """Generic, harness-agnostic identity surface.

    What the CLI / webview banner needs to know about the adapter
    currently bound to a chat — without referencing any concrete
    harness type. Adapters synthesize this from their own internals
    (e.g. opencode's resolved-binary source + sha).
    """

    name: str
    """Same value as `HarnessAdapter.name` — the `harness_type` slug."""
    source: str | None = None
    """Where the binary came from: `"bundled"`, `"staged"`, `"dev"`,
    or `None` if the adapter doesn't surface that distinction."""
    version: str | None = None
    """A short identifier (commit SHA, semver, ...) — opaque to the UI;
    used purely for the banner."""


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class PathFence:
    """A bound on which filesystem locations a session's tools may name.

    A session that runs somewhere the operator owns — a cloud box holding its
    own credentials next to the customer's project — may read the project and
    nothing else. That bound is not a permission mode: a read inside the
    project auto-allows, so by the time any prompt exists the read has already
    happened. So the bound is checked at the session's decision chokepoint,
    ahead of every allow, and it is injected rather than assumed: a session
    without one (every local CLI / editor session) is decided exactly as
    before.

    ``escape`` answers one permission ask with the first location that is out
    of bounds — spelled as the ask spelled it, so a caller can quote it — or
    ``None`` when the ask stays inside. ``reason`` is what the model is told
    when one is: it rides the reject back to the harness, which makes the
    refusal a recoverable tool error rather than the end of the turn.

    ``reason`` may be a callable taking the ask and the location that escaped,
    for a fence whose sentence depends on them — the operation it refused and
    the boundary that ask may work inside are not the same for a read and a
    write, and a fence that can only say one sentence tells the model the wrong
    one half the time.
    """

    escape: Callable[[PermissionRequest], str | None]
    reason: str | Callable[[PermissionRequest, str], str]
    session: Any = None
    """The bound itself (a cloud chat's ``SessionFence``), handed on to the
    session's tool binding so the in-tool shell gate judges every command
    through the same object that decides the asks here. ``None`` keeps a fence
    that only answers asks."""
    must_ask: Callable[[PermissionRequest], bool] | None = None
    """Whether an ask the fence could not vouch for has to be put to a person:
    its locations could not be proved, so no mode, rule or judge may allow it on
    its own. ``None`` means the fence never says so."""
    agent_home: str | None = None
    """The path the agent sees its working directory at when that is not the
    host path — the sandbox's ``/home/alkera``, where the directory is mounted.
    What the brief names, so the model reads the path it can actually type.
    ``None`` when the agent sees the host path."""
    canonical: Callable[[str], str] | None = None
    """Spells a path the agent named as the host path it reaches — the alias
    above mapped back — so a check made against a host directory judges what
    the agent actually touches. ``None`` leaves every path as spelled."""

    def host_path(self, text: str) -> str:
        return text if self.canonical is None else self.canonical(text)

    def explain(self, request: PermissionRequest, escape: str) -> str:
        """What the model is told about THIS refusal."""
        if callable(self.reason):
            return self.reason(request, escape)
        return self.reason


@dataclass(slots=True, frozen=True)
class SessionConfig:
    """Per-session configuration handed to `start()`.

    Adapters that don't support a field SHOULD ignore it (don't raise).
    Capabilities are reported separately via `HarnessAdapter.capabilities`.
    """

    session_id: str
    project_dir: Path
    """The user's code root — what the session may READ."""
    chat_dir: Path
    """The chat folder (`<.alkera>/chats/<sid>/`). The adapter owns
    `<chat_dir>/.harness/<adapter_name>/` exclusively."""
    parent_session_id: str | None = None
    """For sub-agents — links back to the parent chat."""
    model: dict[str, str] | None = None
    """E.g. `{"provider_id": "anthropic", "model_id": "claude-opus-4-7"}`."""
    agent: str | None = None
    """Adapter-specific agent flavor (`"general"`, `"explorer"`, ...)."""
    permission_mode: PermissionMode = "default"
    """Initial permission policy for the session. `"default"` makes
    every mutating tool ask. Switchable at runtime via
    `ChatSession.set_permission_mode`."""
    fenced: bool = False
    """True when the session runs under a `PathFence` — a cloud box holding its
    own credentials next to the customer's project.

    What it buys is the env scrub: the box's own environment (its
    ``ALKERA_HOME``, bearer, leased database and cloud-provider credentials)
    is taken out of the spawned harness process before the harness's own names
    are set, so the agent carries what it needs to serve and nothing the box
    needs to be.

    It is NOT what gates the shell. On POSIX ``bash`` IS Alkera's own
    parent-hosted tool, and the vendor's ask for it names the permission rule
    glob and no command — nothing the fence or the mode policy can judge, so
    that ask is the runtime's to answer and never a person's. The gate that can
    actually see the command is ``gate_shell_action`` in the tool body, which
    applies the fence, the read-only refusal and the effect classification on
    every session, fenced or not. ``False`` for every local CLI / editor
    session."""
    store_is_cache: bool = False
    """True when the agent's session store travels with the chat's folder and
    the transcript is the chat's record (a cloud box). A pinned session the
    store does not hold then opens a fresh session instead of refusing: the
    store may simply not have landed before the last box let the folder go.
    ``False`` locally, where a missing pin is a wiped store to repair."""
    working_dir: Path | None = None
    """Where the agent RUNS: the directory a relative path in a tool call
    resolves against, and the harness's own cwd (opencode's
    `x-opencode-directory`, the Claude SDK's `cwd`). `None` (every local CLI or
    editor session) means `project_dir`.

    A cloud chat sets it to the one directory it may write (its own chat
    folder), because a model told to "create a file" names it relatively: with
    the cwd on the customer's project root that write lands outside the write
    fence and is refused every time, however the reader answers."""
    subagents_enabled: bool = True
    """Whether this session may delegate to a subagent. False is a deployment
    that serves no agent-spawning tool at all: the runtime keeps `spawn_agent` /
    `list_agent_types` out of the tool registry, and an adapter must also stop
    its harness advertising ITS OWN spawner (opencode's `task`), or the model is
    still offered a door out of the chat."""
    harness_native: dict[str, Any] = field(default_factory=dict)
    """Pass-through bag for adapter-specific config the IR doesn't
    model. Survives via `extra="allow"`."""

    @property
    def cwd(self) -> Path:
        """The directory the harness runs in, and the base a relative path in a
        tool call resolves against."""
        return self.working_dir or self.project_dir


@dataclass(slots=True, frozen=True)
class PromptInput:
    """A user prompt to deliver to the harness."""

    text: str
    parts: list[dict[str, Any]] = field(default_factory=list)
    """Additional structured parts (file attachments, images) keyed by
    the adapter's part schema. The IR doesn't dictate the shape here
    — adapter translates."""
    model: dict[str, str] | None = None
    """Per-prompt model override (rare; usually session-scoped)."""
    variant: str | None = None
    """Per-prompt reasoning-effort variant (e.g. `"low"`, `"high"`). The
    model stays pinned to the session; only the effort changes. The
    adapter composes the gateway model id (`<model_id>::<variant>`) from
    the session's pinned model + this variant. None = use the session's
    configured default model/effort unchanged."""
    agent: str | None = None
    """Per-prompt harness agent override. Used by plan mode to route
    the turn through the harness's read-only planning agent."""
    system: str | None = None
    """Per-prompt system-prompt addendum (appended to the base system
    prompt, not a replacement). Used to inject the plan-mode directive
    only on plan-mode turns."""
    turn_id: str = field(default_factory=lambda: secrets.token_hex(10))
    """Identity of this transport attempt, minted at construction, before the
    fallible send. The adapter stamps it onto every `SessionStatusChanged`
    (and turn event) the attempt produces."""


# ---------------------------------------------------------------------------
# Adapter ABC
# ---------------------------------------------------------------------------


class HarnessAdapter(ABC):
    """One concrete coding-agent harness. ONE instance per active chat.

    The adapter's `start()` brings the harness up (spawn subprocess +
    connect HTTP + open the SSE stream). `stop()` tears it down
    cleanly. Between, operations stream IR events via `subscribe()`.

    Lifecycle invariants:
    - `start()` is called exactly once before any other method.
    - After `stop()`, no other methods may be called (the adapter is
      single-use).
    - Operations during the time `start()` is in-flight raise
      `HarnessNotReadyError`.

    Threading:
    - All methods are async. The adapter owns its own asyncio tasks
      (SSE consumer, heartbeat). Cancellation safety: `stop()` must
      cancel + await every task before returning.

    Concurrency:
    - At most one `send_prompt` per chat — but the orchestrator
      enforces this via the per-chat lock. The adapter is permitted
      to assume single-pump access.

    Permission kind mapping:
    - Every `PermissionRequest` emitted by an adapter MUST carry both
      the native `permission_kind` (verbatim, for UIs) AND a
      `canonical_kind` drawn from `CanonicalPermissionKind`. The
      orchestrator's policy code (`mode_auto_decision`) only consults
      the canonical kind, so portability across harnesses is mechanical.
      Each adapter owns its own native→canonical mapping table (see
      `_opencode_kind_to_canonical` in the opencode adapter for the
      pattern).
    """

    name: str = "unknown"
    """Identifier in events' `harness` field — e.g. `"opencode"`."""

    capabilities: frozenset[str] = frozenset()
    """Feature flags the orchestrator inspects to gate features.
    Common: `{"fork", "resume", "share", "summarize", "clear",
    "mcp_dynamic", "subagents", "revert"}`."""

    @classmethod
    def is_available(cls) -> bool:
        """Whether this harness can run on THIS machine right now — e.g. its
        binary is installed/discoverable. A class-level fact (no instance/config
        needed) so the runtime/UI can gate harness selection BEFORE creating a
        chat: don't offer a harness that can't start here.

        Default: always available (the common case — a bundled binary). A harness
        that depends on something the user must supply (a separately-installed
        proprietary binary) overrides this. Should be cheap (no network, no
        subprocess turn) so a UI can call it freely."""
        return True

    def info(self) -> HarnessInfo:
        """Identity surface used by UIs (CLI banner, webview header).

        Default just echoes `name`. Adapters that have a binary or
        version to report override this — keeps the CLI/webview out
        of the harness's concrete details."""
        return HarnessInfo(name=self.name)

    def native_state(self) -> dict[str, Any]:
        """Adapter-specific durable handles to persist into
        `ChatManifest.harness` after `start()` returns. The runtime
        merges the returned dict into the manifest so the NEXT
        ``open_chat`` can re-attach deterministically (without scanning
        + picking-latest).

        Examples:
        - opencode: ``{"agent_session_id": <sid>}`` so we re-attach to the
          exact same SQLite session instead of guessing. (The key is
          shared by every adapter, so it names no harness.)
        - future Claude Code: ``{"claude_thread_id": <tid>}``.

        The default returns ``{}`` — adapters with no durable handles
        opt out by inheriting it. Returned keys MUST be JSON-safe (the
        manifest is persisted as JSON)."""
        return {}

    def serves_model(self, model: Mapping[str, str]) -> bool:
        """Whether this RUNNING harness can answer a turn on ``model``
        (``{provider_id, model_id}``, the model id carrying any ``::effort``)
        without being spawned again. A host that moved the chat onto a model
        the agent was not configured for reopens the session before the next
        turn instead of sending one the agent would refuse.

        The default is yes: a harness that takes any model of its wire per turn
        (the claude agent's ``set_model``) needs no respawn. A harness that is
        handed a fixed list of models when it is spawned overrides this."""
        return True

    # ---- lifecycle --------------------------------------------------

    @abstractmethod
    async def start(self) -> None:
        """Bring the harness up. Idempotent (calling twice raises)."""

    @abstractmethod
    async def stop(self) -> None:
        """Tear down. Idempotent (calling twice is a no-op)."""

    # ---- main operations --------------------------------------------

    @abstractmethod
    async def send_prompt(self, prompt: PromptInput) -> None:
        """Deliver a user prompt. Returns once the harness ACKs receipt
        (not when the turn completes). Events stream via `subscribe()`.
        Raises `HarnessNotReadyError` if `start()` hasn't finished.
        Raises `HarnessCrashError` if the harness has died.
        """

    @abstractmethod
    async def cancel(self) -> None:
        """Cancel the in-flight turn (if any). The adapter MUST close the
        attempt BEFORE the transport call (so the native tail it triggers is
        dropped), then emit synthetic close events for every open part + one
        terminal `SessionStatusChanged(status="aborted")` stamped with that
        attempt. With NO attempt in flight it publishes nothing: an unstamped
        terminal means "a turn collapsed and I lost which", and the runtime
        settles on it, so publishing one at idle would end the prompt typed
        next. Safe to call when no turn is active."""

    @abstractmethod
    async def resolve_permission(
        self, request_id: str, option_id: PermissionOptionId, *, reason: str | None = None
    ) -> None:
        """Reply to a pending permission ask. The adapter forwards to
        the underlying harness's approval API. ``reason`` (set on a REJECT by the
        auto-mode judge / the policy) is surfaced to the MODEL as the denial
        message — instead of the vendor's generic "the user rejected" — so it can
        course-correct. Adapters ignore it on an allow."""

    @abstractmethod
    async def answer_question(self, request_id: str, answers: list[list[str]]) -> None:
        """Reply to a pending question. `answers` is parallel to
        `QuestionRequest.questions` (entry N is the user's chosen
        labels for question N). For single-select that's a 1-element
        list; multi-select uses N. Adapters that don't support
        questions raise `NotImplementedError`."""

    @abstractmethod
    async def reject_question(self, request_id: str, reason: str | None = None) -> None:
        """User declined to answer. The harness sees this as a hard
        rejection — its question-tool call errors out. Adapters that
        don't support questions raise `NotImplementedError`."""

    async def refresh_tools(self) -> None:
        """Make the harness list the Alkera tool server's tools again.

        A harness lists the loopback server once, as it connects, and keeps
        that list for the life of the process; the session calls this at the
        start of a turn when the listing the server last gave no longer
        matches what it would give now (a team connection the box loaded
        after the chat spawned, an org toggle), so the turn about to run sees
        the tools the session has. Best-effort: a harness that cannot re-list
        keeps the set it had, which is what it would have had without the
        call. The default does nothing — a harness whose tool list is read
        per turn needs no refresh."""
        return None

    async def compact(self) -> None:
        """Force a context compaction (summarize) of the session.

        Optional capability — adapters that advertise `"summarize"` in
        `capabilities` override this; others raise `NotImplementedError`.
        The summary + any context fold are surfaced through the normal
        event stream as a `CompactionApplied` event (and applied to the
        harness's own session store, so later turns/resumes use the
        compacted context)."""
        raise NotImplementedError(f"{self.name} does not support compaction")

    async def clear(self) -> None:
        """Reset the conversation context to empty — the next turn starts
        fresh, seeing none of the prior turns.

        Optional capability — adapters that advertise `"clear"` in
        `capabilities` override this; others raise `NotImplementedError`.
        The reset is surfaced through the normal event stream as a
        `ConversationCleared` event (and applied to the harness's own
        session store, so later turns/resumes start from the cleared
        state). Must NOT touch the workspace files."""
        raise NotImplementedError(f"{self.name} does not support clear")

    # ---- streaming --------------------------------------------------

    @abstractmethod
    def subscribe(self) -> AsyncIterator[Event]:
        """Async iterator over IR events. One iterator per consumer.

        Backpressure policy: the adapter publishes via an `EventBus`
        which DROPS events for slow subscribers (logs + counts). The
        chat.jsonl writer is a special subscriber that must not be
        dropped — adapters wire it up to a separate, eager path.
        """


__all__ = [
    "CanonicalPermissionKind",
    "HarnessAdapter",
    "HarnessCrashError",
    "HarnessError",
    "HarnessInfo",
    "HarnessModelError",
    "HarnessNotReadyError",
    "HarnessSandboxRefusedError",
    "HarnessStartError",
    "HarnessStartRefusedError",
    "HarnessUnavailableError",
    "PathFence",
    "PromptInput",
    "SessionConfig",
]
