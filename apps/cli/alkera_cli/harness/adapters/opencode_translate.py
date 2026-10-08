"""opencode → IR event translator.

Pure function over the native opencode `{type, properties}` envelope:
given a native event, produce zero / one / many IR events. No I/O, no
spawn, no SSE, no HTTP — testable in isolation.

`OpencodeHttpAdapter` delegates `_translate(...)` here. The mutable
piece of context the translator needs (the in-flight `open_parts`
table + the alkera session id) lives in `_TranslatorContext`, which
the adapter passes by reference so a mutation on the translator side
is visible from the adapter's synthesize-close path.

Wire shapes pinned against `vendor/opencode/packages/opencode/src/...`
— especially `session/message-v2.ts`, `session/session.ts`,
`session/status.ts`, `permission/index.ts`, `question/index.ts`. When
upstream drifts, `apps/cli/tests/harness/test_harness_opencode_translate.py`
is the first thing to break — that's the signal to revisit the mapping.
"""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from alkera_core.credit_refusal import CreditRefusal, credit_refusal_from_error_payload
from alkera_core.overflow import error_payload_is_overflow
from alkera_core.schemas.chat import (
    AgentMessageChunk,
    AgentThoughtChunk,
    CanonicalPermissionKind,
    CommandExecuted,
    CompactionApplied,
    Event,
    FileEdited,
    Heartbeat,
    MessageCompleted,
    MessageCreated,
    PartCreated,
    PartStarted,
    PartType,
    PermissionOption,
    PermissionRequest,
    PermissionResolved,
    PlanUpdated,
    QuestionAnswered,
    QuestionOption,
    QuestionPrompt,
    QuestionRejected,
    QuestionRequest,
    RawEvent,
    ReasoningPart,
    Retrying,
    SessionStatusChanged,
    TextPart,
    ToolCall,
    ToolCallUpdate,
)

from alkera_cli.contracts.tool_types import ActionDescriptor, Effect, ResourceRef
from alkera_cli.harness.adapters._file_diff import diff_preview
from alkera_cli.harness.adapters.error_text import readable_error
from alkera_cli.harness.sensitive_paths import escalate_sensitive_path
from alkera_cli.harness.turn_model import ran_model_stamp
from alkera_cli.host.limits import env_count, env_seconds
from alkera_cli.plugins.plugin_base.permissions import (
    classify_command,
    command_touches_sensitive_path,
)
from alkera_cli.plugins.plugin_base.permissions.consent_scope import (
    EXACT_ALWAYS_LABEL,
    standing_allow_scope,
)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


# Sentinel header that our patched `plan_present` tool
# (vendor/opencode/.../tool/plan.ts) stamps on its question. The IR
# exposes a typed `QuestionRequest.kind="plan_approval"` derived from
# this — consumers MUST switch on `kind`, never on the header string.
_PLAN_APPROVAL_HEADER = "alkera:plan-approval"


# opencode-native permission key → canonical kind. The harness-agnostic
# policy code (mode_auto_decision) only reasons in canonical terms; the
# mapping HAS to live in the adapter. Native keys not listed fall
# through to ``"other"``.
_OPENCODE_TO_CANONICAL_KIND: dict[str, CanonicalPermissionKind] = {
    # File mutation (opencode's write, edit, patch all funnel here).
    "edit": "edit",
    # Shell. opencode keeps the legacy "bash" key (shell/id.ts comment)
    # even though it now dispatches to pwsh/cmd internally.
    "bash": "shell",
    # Network access.
    "webfetch": "network",
    "websearch": "network",
    # Subagent spawn.
    "task": "task",
    # Cross-directory access (shell.ts maps to this when the cwd differs).
    "external_directory": "external",
}


def _opencode_kind_to_canonical(native: str) -> CanonicalPermissionKind:
    """Translate opencode's native permission key to the harness-agnostic
    `CanonicalPermissionKind` the orchestrator's policy code consumes.
    Unknown keys map to ``"other"`` — the default, so unmapped tools
    still flow through the prompt path safely."""
    return _OPENCODE_TO_CANONICAL_KIND.get(native, "other")


# opencode native key → (capability, effect) for the static (non-bash)
# descriptors the broker policy reasons over. Everything that gates gets a typed
# descriptor so it flows through the SAME policy + auto-mode judge as a bash tool
# — a subject-LESS ask would otherwise PROMPT in auto (we can't judge it). Cross-
# directory / repo access is modelled as a recoverable fs WRITE (conservative: in
# auto it's judged, in default it prompts, never silently allowed as a read).
# ``task`` (subagent spawn) stays absent — it's denied upstream, not gated here.
_OPENCODE_KIND_TO_ACTION: dict[str, tuple[str, Effect]] = {
    "edit": ("fs", Effect.WRITE),
    "read": ("fs", Effect.READ),
    "glob": ("fs", Effect.READ),
    "grep": ("fs", Effect.READ),
    "list": ("fs", Effect.READ),
    "lsp": ("fs", Effect.READ),
    # An outbound fetch is a WRITE channel, not a read: whatever is already in the
    # model's context can be encoded into the URL's path/query and shipped to an
    # attacker-chosen host. EGRESS makes it hit the floor (prompts in `default`,
    # refused in `read_only`/`plan`, grounded by the safety judge in `auto`) instead
    # of auto-allowing silently. `websearch` stays READ — its query goes to a fixed
    # search provider, not to a host the prompt injector controls.
    "webfetch": ("network", Effect.EGRESS),
    "websearch": ("network", Effect.READ),
    # Access to a path OUTSIDE the project cwd, and a repo CLONE (fetch + write a
    # cache) — a write outside the safe workspace; judged in auto, prompts in
    # default. repo_overview only READS a cached repo dir.
    "external_directory": ("fs", Effect.WRITE),
    "repo_clone": ("fs", Effect.WRITE),
    "repo_overview": ("fs", Effect.READ),
    # The doom-loop guard fires when the model repeats the same tool+input 3x —
    # a meta-signal, NOT a tool. Model it as a recoverable write so auto JUDGES it
    # (it can spot a stuck/looping agent) instead of PROMPTING the human.
    "doom_loop": ("shell", Effect.WRITE),
    "workflow_tool_approval": ("shell", Effect.WRITE),
}


# Metadata keys under which opencode names a real filesystem LOCATION. Each
# path-taking tool picks a different one: `read`/`edit`/`write` and the
# `external_directory` pre-check ship `filepath`; `grep`/`glob`/`repo_overview`
# put the search DIRECTORY in `path` (their `patterns[0]` is the search PATTERN,
# not a location); `lsp` ships `filePath`; `grep`'s `include` is a filename glob.
# Reading only `filepath` — a shape three of those tools never emit — is how a
# grep of ~/.aws stayed an auto-allowed plain READ.
_OPENCODE_PATH_METADATA_KEYS: tuple[str, ...] = (
    "filepath",
    "filePath",
    "path",
    "parentDir",
    "include",
)


#: The pattern opencode's generic tool ask carries (`session/tools.ts`): the
#: permission RULE glob that matched, not anything the tool was called with.
OPENCODE_RULE_GLOB = "*"


def strip_rule_glob(patterns: Sequence[str]) -> list[str]:
    """``patterns`` with opencode's match-everything rule glob removed.

    The glob names the permission RULE that matched, never the thing the tool was
    called with, so it can stand for neither the subject a reader approves nor the
    text a classifier reads. A list left empty is an ask that named nothing.
    """
    return [p.strip() for p in patterns if p.strip() and p.strip() != OPENCODE_RULE_GLOB]


#: Ports `Wildcard.match` (`vendor/opencode/packages/core/src/util/wildcard.ts`):
#: the metacharacters it escapes before turning `*` into `.*` and `?` into `.`.
_WILDCARD_ESCAPE = re.compile(r"[.+^${}()|\[\]\\]")


def _compile_glob(glob: str) -> re.Pattern[str]:
    """``glob`` as the regex opencode compiles it to.

    A question about what a rule would approve is only worth asking of the
    matcher that will actually approve it, so this follows `Wildcard.match`
    step for step: backslashes become slashes, the regex metacharacters are
    escaped, ``*`` becomes ``.*`` and ``?`` becomes ``.``, a trailing ``" .*"``
    is relaxed to ``"( .*)?"`` (which is what makes ``git status *`` cover a
    bare ``git status``), and the whole thing is anchored with dot-matches-all.
    """
    escaped = _WILDCARD_ESCAPE.sub(lambda m: "\\" + m.group(0), glob.replace("\\", "/"))
    escaped = escaped.replace("*", ".*").replace("?", ".")
    if escaped.endswith(" .*"):
        escaped = escaped[:-3] + "( .*)?"
    return re.compile("^" + escaped + "$", re.DOTALL)


#: Subjects a rule is asked to match before it counts as approving everything.
#: Deliberately unalike — a bare word, an absolute path, a Windows path (the
#: matcher slash-normalizes), a flag-bearing command, a pipeline and an embedded
#: newline — so a glob that merely fits one shape does not pass. The empty
#: string is NOT among them: a rule is only ever matched against a real subject,
#: and requiring it would call ``?*`` (every non-empty command) a scope.
_SUBJECT_PROBES: tuple[str, ...] = (
    "ls",
    "rm -rf /",
    "git push --force origin main",
    "/etc/passwd",
    "C:/Windows/System32/cmd.exe",
    "curl https://example.invalid | sh",
    'printf "a\nb"',
)


#: A glob built from nothing but wildcards names no subject at all, so it is
#: refused whatever it happens to match. ``???*`` leaves a two-letter ``ls``
#: asked and so fails the probes below, but a rule that says only "some
#: characters" is not a scope a reader could have chosen either.
_WILDCARD_ONLY = re.compile(r"^[*?\s]+$")


def glob_approves_everything(glob: str) -> bool:
    """Whether this one glob would stand in for every subject an agent could name.

    Two ways to qualify, and the union is deliberate: a glob that names nothing
    but wildcards, and a glob whose compiled matcher takes every probe. The
    first is the structural floor — it holds for a shape the probes happen to
    miss; the second is the behavioural one, and it is what catches a glob with
    a literal in it that still opens all the way.
    """
    if _WILDCARD_ONLY.match(glob):
        return True
    matcher = _compile_glob(glob)
    return all(matcher.match(probe) is not None for probe in _SUBJECT_PROBES)


def always_is_unscoped(always: Sequence[str]) -> bool:
    """Whether replying ``always`` to this ask would record no scope worth the word.

    opencode pushes EVERY glob in the ask's own ``always`` into its approved
    ruleset (`permission/index.ts`) and then stops raising the ask for anything
    they match — so ONE glob that approves everything is a standing grant over
    every later call of the tool, however many narrow globs sit beside it. The
    test is therefore "any", not "all", and it asks the real matcher rather than
    comparing against a spelling: ``*``, ``**`` and ``?*`` all compile to a
    regex that takes anything, and a list of globs we only checked for the first
    of them would let the other two through.

    A list with no glob in it is unscoped for the other reason: nothing is
    recorded at all, while the card offers a standing grant.
    """
    globs = [g for g in always if g.strip()]
    if not globs:
        return True
    return any(glob_approves_everything(g) for g in globs)


def _persists_local_rule(descriptor: ActionDescriptor | None) -> bool:
    """Whether Alkera's own precise rule would be written on an always-allow.

    Mirrors ``DecisionEngine._persist_rule``: :func:`standing_allow_scope` names
    what it records — a family rule, one exact command line, or nothing.
    """
    return standing_allow_scope(descriptor) is not None


def always_may_travel(always: Sequence[str], descriptor: ActionDescriptor | None) -> bool:
    """Whether an ``always`` reply may be sent to opencode for this ask.

    opencode records the ask's own globs and then stops raising the ask for
    anything they match — a standing grant IT keeps, outside Alkera's policy,
    its floor, its audit and a cloud session's workspace fence. So it travels
    only for a real scope, and NEVER for a shell command, whose glob is a
    command PREFIX: ``uv *``, learned from ``uv run pytest``, also approves
    ``uv run python -c '…'`` and everything else under that first word. A
    person's "always" on a command is carried by Alkera's own precise
    ``(capability, operation)`` rule instead, which re-gates each later command
    on its real classification.
    """
    if always_is_unscoped(always):
        return False
    return descriptor is None or descriptor.capability != "shell"


def ask_options(
    *, always: Sequence[str], descriptor: ActionDescriptor | None
) -> list[PermissionOption]:
    """The decisions an ask offers.

    "Always allow" is offered only where something narrower than everything would
    actually be recorded by it — either an opencode glob the reply may travel
    for, or Alkera's own precise rule. Where neither can be, the choice reads as
    a standing grant and is one of two lies: opencode's ``*`` would approve every
    later call of the tool unasked, and an empty glob list would record nothing
    while promising the reader it had. The subject-less ask opencode raises for
    every MCP tool is exactly that case, so the shell reached through one offers
    approval for the one command and nothing beyond it.
    """
    options = [PermissionOption(option_id="allow_once", name="Allow once")]
    if always_may_travel(always, descriptor) or _persists_local_rule(descriptor):
        exact = descriptor is not None and standing_allow_scope(descriptor) == "exact"
        options.append(
            PermissionOption(
                option_id="allow_always", name=EXACT_ALWAYS_LABEL if exact else "Always allow"
            )
        )
    options.append(PermissionOption(option_id="reject_once", name="Reject once"))
    options.append(PermissionOption(option_id="reject_always", name="Always reject"))
    return options


# Tools whose ask names a DIRECTORY to walk (or no path at all) rather than the
# single file in `patterns`. When they omit it the vendored tool falls back to
# the worktree root (`params.path ?? ins.directory` in grep.ts / glob.ts; lsp's
# `workspaceSymbol` carries no path whatsoever), so an absent path resolves to
# the workspace root — and when even THAT is unknown we cannot say what is being
# read, so the ask is escalated instead of falling through to READ.
_OPENCODE_DIRECTORY_SCOPED_KINDS: frozenset[str] = frozenset(
    {"glob", "grep", "list", "lsp", "repo_overview"}
)

# Shell word boundaries — whitespace plus the metacharacters that can abut a path.
_SHELL_WORD_SPLIT = re.compile(r"""[\s;|&()<>"'`]+""")


def _shell_sensitive_candidates(command: str) -> list[str]:
    """The individual WORDS of a shell command that trip the secret-path marker
    scan, so the sandbox carve-out can be applied per path instead of to the whole
    command line (the chat's scratch dir sits under ``.alkera/``, which the markers
    cover, and plan mode reads its own files back).

    Falls back to the whole command when the match can't be attributed to a single
    word — the ``gcloud auth`` marker spans a space — so a match is never lost."""
    if not command_touches_sensitive_path(command):
        return []
    matched = [
        word
        for word in _SHELL_WORD_SPLIT.split(command)
        if word and command_touches_sensitive_path(word)
    ]
    return matched or [command]


def _metadata_paths(metadata: Mapping[str, Any] | None) -> list[str]:
    """Every filesystem location named in an ask's ``metadata``, in the order
    the keys are declared. Non-string / empty values are dropped."""
    if not metadata:
        return []
    return [
        value
        for value in (metadata.get(key) for key in _OPENCODE_PATH_METADATA_KEYS)
        if isinstance(value, str) and value
    ]


def _descriptor_for_opencode(
    native_kind: str,
    patterns: list[str],
    real_target: str | None = None,
    *,
    metadata: Mapping[str, Any] | None = None,
    sandbox_dir: Path | None = None,
    workspace_root: Path | None = None,
) -> ActionDescriptor | None:
    """Build the typed ``ActionDescriptor`` the broker policy reasons over.

    Bash is classified by the shell classifier over the tree-sitter-extracted
    ``patterns`` (so ``rm -rf`` hits the floor and ``ls`` auto-allows); fs/network
    map statically. ``None`` (subject-less) for kinds we don't model — they fall
    back to the canonical-kind prompt.

    ``real_target`` is the concrete path/URL opencode carries in the ask's
    ``metadata`` (``filepath`` for an ``external_directory`` ask) — the actual file
    being touched, NOT the ``<dir>/*`` allow-rule glob in ``patterns``. When
    present it becomes the descriptor's ``raw``/target so the UI shows the real
    file, never the glob. ``metadata`` is the WHOLE ask payload bag: the
    sensitive-path floor scans every path-bearing key in it, because each tool
    names its target under a different one."""
    if native_kind == "bash":
        # opencode's generic tool ask — the one the parent-hosted shell reaches
        # it as, being an MCP tool — names no command: its one pattern is the
        # permission RULE glob ``*``. Classified as a command, the glob read as
        # a heuristic write named ``*`` that a cloud chat's write fence then
        # quoted back as a write outside the folder, with no card, for ``ls``.
        # The glob is dropped; an ask left with no command fails closed below.
        commands = strip_rule_glob(patterns)
        if not commands:
            # A bash ask with no parsed command → fail closed to a prompting write.
            return ActionDescriptor(
                capability="shell",
                effect=Effect.WRITE,
                operation="unknown",
                confidence="unknown",
                classifier="opencode-empty",
                reasons=["bash permission with no command patterns"],
            )
        # The patterns are per-command source; classify them together so the
        # descriptor's effect is the MAX over the whole compound command.
        command = "\n".join(commands)
        # Same floor the in-process shell gate applies (`gate_shell_action`): a
        # command NAMING a secret path can't stay an auto-allowed read just
        # because it reaches the file through the harness's own shell tool
        # instead of ours.
        return escalate_sensitive_path(
            classify_command(command),
            _shell_sensitive_candidates(command),
            sandbox_dir=sandbox_dir,
            workspace_root=workspace_root,
        )
    action = _OPENCODE_KIND_TO_ACTION.get(native_kind)
    if action is None:
        return None
    capability, effect = action
    # The real path (from metadata) wins over the glob pattern — the glob is an
    # allow-rule artifact, not what the agent is touching.
    target = real_target or (patterns[0] if patterns else None)
    targets = (
        [ResourceRef(kind="file" if capability == "fs" else "url", name=target)] if target else []
    )
    descriptor = ActionDescriptor(
        capability=capability,
        effect=effect,
        operation=native_kind,
        targets=targets,
        raw=target,
        classifier="opencode-tool",
    )
    if capability != "fs":
        return descriptor
    metadata_paths = _metadata_paths(metadata)
    # `target` first (the most specific location we have), then every OTHER
    # path-bearing key, then the patterns — a caller that passes `real_target`
    # without the surrounding metadata bag is still covered.
    candidates = [c for c in (target, *metadata_paths, *patterns) if c]
    # A LOCATION the ask actually named (as opposed to a search pattern that
    # happens to sit in `patterns`).
    located = [p for p in (real_target, *metadata_paths) if p]
    unresolved = False
    if native_kind in _OPENCODE_DIRECTORY_SCOPED_KINDS and not located:
        # No directory in the payload → the vendored tool walks the worktree root.
        # Scan THAT; with no workspace root to resolve it we can't tell what is
        # being read, so fail closed.
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


#: Permission keys whose tool is served BY THIS PROCESS over the loopback MCP
#: and gates its own action inside its own body (the broker, the fence, the
#: classifier over the real arguments). A new parent-hosted mount is admitted by
#: adding its permission prefix here — the vendor names a local-MCP tool
#: ``<server>_<tool>``, so a mount's whole tool set is one entry.
PARENT_GATED_PERMISSION_PREFIXES: tuple[str, ...] = ("alkera_", "web_")


def permission_is_parent_gated(native_kind: str, *, parent_hosted_shell: bool) -> bool:
    """Whether this process is going to gate this call itself.

    The vendor raises one permission ask per tool call, before the tool's body
    runs. For a tool we host, that ask can name nothing useful — an MCP tool's
    ask carries the permission RULE glob and no arguments — while the body it
    gates then classifies the real command and raises Alkera's own ask for the
    same call. Two cards reach the reader for one command, the first of which
    names no command and decides nothing: answering it only lets the body run
    on to the ask that matters.

    So an ask for a parent-hosted tool is not forwarded at all; it is answered
    by the adapter, exactly as the ``alkera_*`` / ``web_*`` / ``external_directory``
    entries in the vendor permission ruleset already answer theirs. The gate is
    not lost — it moves to the one place that can see what is being run.

    ``bash`` qualifies only where the parent-hosted shell is actually
    registered: wherever that patch is off, ``bash`` is the vendor's own shell
    and nothing else would gate it.
    """
    if native_kind == "bash":
        return parent_hosted_shell
    return native_kind.startswith(PARENT_GATED_PERMISSION_PREFIXES)


#: How long an ask that named a call no part has been seen for waits for that
#: part before it is raised with the fields it has. The part follows the ask
#: within milliseconds when opencode publishes both; the wait exists for a part
#: that never comes (a dropped frame on an SSE reconnect has no replay), so a
#: policy that leaves a waiting ask alone is still handed it, and the turn ends.
ENV_ASK_SUBJECT_WAIT = "ALKERA_OPENCODE_ASK_SUBJECT_WAIT_SECONDS"
ASK_SUBJECT_WAIT_SECONDS: float = (
    env_seconds(os.environ.get(ENV_ASK_SUBJECT_WAIT), default=5.0) or 0.0
)
#: How many asks may wait for their part at once; the oldest is raised as it is
#: when one more arrives.
ENV_ASK_SUBJECT_WAIT_MAX = "ALKERA_OPENCODE_ASK_SUBJECT_WAIT_MAX"
ASK_SUBJECT_WAIT_MAX: int = env_count(os.environ.get(ENV_ASK_SUBJECT_WAIT_MAX), default=8) or 1


@dataclass(slots=True)
class _WaitingAsk:
    """A ``permission.asked`` payload held until the part naming its subject
    lands, and the moment it is raised as it is if that part never does."""

    props: dict[str, Any]
    due: datetime


# ---------------------------------------------------------------------------
# Open-part state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _OpenPart:
    """Tracks an in-flight part so we can synthesize a closing
    `PartCreated` on cancel/crash."""

    message_id: str
    part_id: str
    part_type: PartType
    buffer: list[str] = field(default_factory=list)
    """For text/reasoning: concatenated deltas the harness sent. On
    forced close we emit this as `final_text`."""


@dataclass(slots=True)
class _TranslatorContext:
    """Mutable state the translator needs across event calls.

    The adapter holds the canonical instance and passes it to the
    translator; the `open_parts` dict is shared by reference so the
    adapter's synthesize-close path sees the same in-flight parts the
    translator has been writing to.
    """

    session_id: str
    """The alkera chat id we stamp on every IR event. Distinct from
    opencode's internal session id (which we keep in the adapter)."""
    workspace_root: Path | None = None
    """The user's code root — resolves the worktree-relative paths opencode
    reports so the sensitive-path floor sees a real filesystem location."""
    sandbox_dir: Path | None = None
    """This chat's Alkera-managed scratch dir (``<chat>/sandbox/``), exempt from
    the sensitive-path escalation even though it sits under ``.alkera/``."""
    open_parts: dict[str, _OpenPart] = field(default_factory=dict)
    part_by_call_id: dict[str, str] = field(default_factory=dict)
    """Tool part id by the provider's call id (``part.callID``). A permission
    ask names the call the provider's way (``tool.callID``) while every
    ``ToolCall`` / ``ToolCallUpdate`` is keyed by the part id, so the ask is
    translated onto the part id here — the transcript's key — and keeps the
    provider id alongside. An entry is dropped when its part's real closing
    frame lands (a synthetically closed part keeps its entry for the life of
    the session — ids are never reused, so nothing stale can match)."""
    synthetically_closed_tool_parts: set[str] = field(default_factory=set)
    """Tool part ids whose closing ``ToolCallUpdate`` the adapter synthesized
    because the real frame never arrived before the idle-hold grace expired
    (an SSE reconnect can drop frames — there is no replay). A late real
    frame for one of these must be swallowed: the part is no longer in
    ``open_parts``, so it would otherwise re-open as a spurious ``ToolCall``
    after the turn already went idle."""
    command_by_call_id: dict[str, str] = field(default_factory=dict)
    """The shell command a call was invoked with, by the provider's call id.
    opencode's generic MCP-tool ask carries no arguments at all, so the command a
    reader is being asked to approve exists only on the call the ask gates; it is
    recorded here as the call's input lands, and dropped with the call's
    ``part_by_call_id`` entry when the call closes."""
    asks_awaiting_part: dict[str, _WaitingAsk] = field(default_factory=dict)
    """``permission.asked`` payloads that named a call whose part had not been
    seen, by the provider's call id. Each is translated again — same ask id —
    from the part frame that first carries the call, so the ask goes out with
    the command it gates whichever frame opencode published first. One whose
    part never comes is raised as it is once its wait is up (see
    ``ask_subject_wait_seconds``), and the map never holds more than
    ``ask_subject_wait_max``: a policy that skips a waiting ask must be
    handed it eventually, or the turn never ends."""
    ask_subject_wait_seconds: float = field(default_factory=lambda: ASK_SUBJECT_WAIT_SECONDS)
    ask_subject_wait_max: int = field(default_factory=lambda: ASK_SUBJECT_WAIT_MAX)
    parent_hosted_shell: bool = False
    """Whether the tool opencode calls ``bash`` is THIS process's own shell
    (the loopback-MCP tool the ``ALKERA_PARENT_SHELL`` patch advertises under
    that name) rather than the vendor's. It decides whether a ``bash``
    permission ask is one this process is also going to raise itself — see
    :func:`permission_is_parent_gated`."""
    parent_gated_requests: list[str] = field(default_factory=list)
    """Ask ids the vendor raised for a tool THIS process gates itself, in
    arrival order. They are never published as asks: the tool body raises the
    real one, naming the command, and the adapter answers these off this list
    so the tool gets to run and reach that gate. Drained by the adapter after
    every translated frame."""
    scoped_always_requests: set[str] = field(default_factory=set)
    """Ask ids an ``always`` reply may travel for (:func:`always_may_travel`) —
    a real scope, and not a command prefix. It is the proof that is recorded,
    never the suspicion: an ask this translator never saw, or one whose record a
    stream reset dropped, falls to ``once``, which costs one more prompt where
    the old shape would have granted every later call of the tool. Dropped when
    the ask is replied to."""
    unpublished_tool_closures: set[str] = field(default_factory=set)
    """Tool part ids the translator has already popped from ``open_parts`` but
    whose closing ``ToolCallUpdate`` has not reached the bus yet. The pop and
    the publish are separated by awaits, so between them a part is closed but
    its closure is still in flight — the window the idle-hold watchdog must
    defer to. Empty means nothing is in flight, so a still-held idle has
    nothing left to wait for."""

    # ---- compaction tracking ----
    # opencode streams a compaction as: a `type:"compaction"` part on a
    # user message (the boundary, carries tail_start_id), then an assistant
    # message with `info.summary == true` whose text parts hold the summary
    # markdown, then a `session.compacted` event. We capture the summary +
    # boundary and SUPPRESS the summary message from the normal stream so
    # the raw template never renders as a chat turn — surfacing it instead
    # as a single `CompactionApplied` on `session.compacted`.
    message_order: list[str] = field(default_factory=list)
    """Assistant/user message ids in creation order — used to derive the
    elided set (everything before `tail_start_id`)."""
    summary_message_id: str | None = None
    """The id of the in-flight `summary:true` assistant message, if any."""
    completed_messages: dict[str, tuple[str, float | None, dict[str, int]]] = field(
        default_factory=dict
    )
    """What each already-completed assistant message last reported —
    `(finish_reason, cost, tokens)`. opencode writes a completed message more
    than once (the step's own `updateMessage`, then the turn's finalizer), and
    an identical restatement is not a second completion: emitting it put two
    identical rows on the transcript and made every token aggregate — the
    manifest's `tokens_total`, the cloud turn meter — charge the step twice.
    A restatement that actually REVISES the message is still emitted."""
    summary_buffer: dict[str, str] = field(default_factory=dict)
    """Finalized text parts of the summary message, keyed by part id in arrival
    order and joined on compaction. Keyed, not appended: opencode rewrites a
    finalized summary part when it normalizes the summary, and a rewrite
    replaces that part's text; a part rewritten to empty drops out."""
    compaction_tail_start_id: str | None = None
    """First RETAINED message id from the compaction boundary part."""

    # ---- write/edit diff capture ----
    # The write/edit tools compute a unified diff and ship it in the
    # `permission.asked` metadata (`{filepath, diff}`) BEFORE publishing
    # `file.edited`. We stash it keyed by filepath, then drain it onto the
    # next `file.edited` for that path so the UI can render the diff.
    pending_file_diffs: dict[str, str] = field(default_factory=dict)
    """filepath → unified diff text, captured at `permission.asked`."""

    # ---- attempt attribution ----
    # opencode's status frames carry no attempt identity, but the session runner
    # serializes runs: every run opens with `busy`, no run's frame precedes the
    # previous run's tail, and one attempt yields one to five terminal-shaped
    # frames. Attribution is decided by run boundaries, through the transitions
    # below and nowhere else.
    attempt_id: str | None = None
    """The attempt every status frame is stamped with right now."""
    attempt_closed: bool = True
    """The attempt's first terminal was emitted (or no attempt has run yet);
    further idle/error frames of it are dropped."""
    closed_by_adapter: bool = False
    """The adapter synthesized the terminal (cancel, clear, crash), so every
    native terminal until the next `busy` is that attempt's redundant tail."""
    pending_attempt: str | None = None
    """A prompt sent while the attempt was closed; the next `busy` opens it."""

    @property
    def attempt_live(self) -> bool:
        """An attempt has opened and its terminal has not been emitted."""
        return self.attempt_id is not None and not self.attempt_closed

    def open_attempt(self, turn_id: str) -> None:
        """A prompt is being sent. A live run absorbs it, so a live attempt
        transfers to the new id; otherwise the id stays pending until the run's
        `busy`."""
        if self.attempt_live:
            self.attempt_id = turn_id
        else:
            self.pending_attempt = turn_id

    def abandon_attempt(self, turn_id: str, previous: str | None) -> None:
        """The prompt's POST raised: undo `open_attempt` from the state observed
        NOW, not the snapshot before the await. A pending prompt is dropped; a
        transfer hands the live run back to ``previous``; a run promoted by
        ``begin_run`` during the failing POST is real and keeps its id."""
        if self.pending_attempt == turn_id:
            self.pending_attempt = None
        elif previous is not None and self.attempt_id == turn_id and not self.attempt_closed:
            # A transfer already closed by a terminal sticks: the run observed it.
            self.attempt_id = previous

    def begin_run(self) -> None:
        """A `busy` frame: the run starts the pending prompt, or continues
        (reopens) the current attempt when nothing is pending."""
        if self.pending_attempt is not None and not self.attempt_live:
            self.attempt_id, self.pending_attempt = self.pending_attempt, None
        self.attempt_closed = False
        self.closed_by_adapter = False

    def close_attempt(self) -> bool:
        """A native terminal frame arrived. True when it is the attempt's first
        (emit it); False when it is a redundant further terminal of a closed
        attempt or part of an adapter-closed tail. Before any attempt there is
        nothing to attribute, so such frames pass through unstamped."""
        if self.attempt_closed and (self.attempt_id is not None or self.closed_by_adapter):
            return False
        self.attempt_closed = True
        return True

    def end_attempt(self) -> str | None:
        """The adapter synthesizes the terminal (cancel, clear, crash): the stamp
        is the pending prompt if one exists, else the live attempt, else None (a
        cancel at idle); the attempt is closed and its native tail is dropped
        until the next `busy`."""
        if self.pending_attempt is not None:
            self.attempt_id, self.pending_attempt = self.pending_attempt, None
            self.attempt_closed = False
        stamp = self.attempt_id if self.attempt_live else None
        self.attempt_closed = True
        self.closed_by_adapter = True
        return stamp

    def reset_attempt(self) -> None:
        self.attempt_id = None
        self.attempt_closed = True
        self.closed_by_adapter = False
        self.pending_attempt = None


# ---------------------------------------------------------------------------
# Translator
# ---------------------------------------------------------------------------


class OpencodeEventTranslator:
    """Stateless-ish translator over native opencode events.

    Construction:
        ctx = _TranslatorContext(session_id="our-sid")
        translator = OpencodeEventTranslator(ctx)
        ev = translator.translate(native_event)

    All mutable state lives on `ctx`; the translator itself is just
    the dispatch logic.
    """

    def __init__(self, ctx: _TranslatorContext) -> None:
        self._ctx = ctx

    # ------------------------------------------------------------------
    # Public entrypoint
    # ------------------------------------------------------------------

    def translate(self, native: dict[str, Any]) -> Event | list[Event] | None:
        """Map native opencode event → IR event(s). Returns `None` to
        drop. A few opencode events expand to multiple IR events
        (e.g. session.status retry → both SessionStatusChanged and
        Retrying) so the return type accommodates a list.

        Unknown event types fall through to `RawEvent` so we never lose
        information.

        An ask still waiting for the part naming its subject is raised as it
        is — ahead of this event — once its wait has run out, or once the
        session goes idle, since no part can follow an idle. opencode's stream
        carries a heartbeat every few seconds, so a wait that ran out is
        noticed within one of them.
        """
        ev_type = native.get("type")
        now = self._now()
        sid = self._ctx.session_id
        overdue = self._raise_overdue_asks(
            now, sid, everything=ev_type in ("session.idle", "session.status")
        )
        out = self._translate_event(ev_type, native, now, sid)
        if not overdue:
            return out
        if out is None:
            return [*overdue]
        return [*overdue, *(out if isinstance(out, list) else [out])]

    def _raise_overdue_asks(
        self, now: datetime, sid: str, *, everything: bool
    ) -> list[PermissionRequest]:
        if not self._ctx.asks_awaiting_part:
            return []
        due = [
            cid
            for cid, waiting in self._ctx.asks_awaiting_part.items()
            if everything or waiting.due <= now
        ]
        raised: list[PermissionRequest] = []
        for cid in due:
            waiting = self._ctx.asks_awaiting_part.pop(cid)
            raised.append(self._translate_permission_asked(waiting.props, now, sid, wait=False))
        return raised

    def _translate_event(
        self, ev_type: Any, native: dict[str, Any], now: datetime, sid: str
    ) -> Event | list[Event] | None:
        props = native.get("properties", {}) or {}

        # ---- server lifecycle ----
        if ev_type in ("server.connected", "global.disposed"):
            # Transport-level signals; nothing semantic to surface.
            return None
        if ev_type == "server.heartbeat":
            # Schema: `{}`. The keep-alive comes from the SSE comment
            # frame, not the payload — we synthesize a heartbeat with
            # `0` last-activity since opencode doesn't track it.
            return Heartbeat(
                event_id=self._new_event_id(),
                time=now,
                session_id=sid,
                last_activity_ms=0,
            )

        # ---- session status ----
        if ev_type == "session.status":
            # Schema: `{sessionID, status: {type: "idle"|"retry"|"busy", ...}}`
            return self._translate_session_status(props, now, sid)
        if ev_type == "session.idle":
            # Deprecated upstream but still emitted alongside
            # `session.status{type:"idle"}`; the attempt rule coalesces the pair.
            if not self._ctx.close_attempt():
                return None
            return self._status("idle", "idle", now, sid)
        if ev_type == "session.error":
            return self._translate_session_error(props.get("error"), now, sid)

        # ---- session.updated ----
        if ev_type == "session.updated":
            # Schema: a partial of UpdatedInfo (title/model/agent/tokens/...).
            # No status info here — that's `session.status`. We let
            # the manifest sync handle the title/model bookkeeping and
            # surface the raw event for any consumer that cares.
            return RawEvent(
                event_id=self._new_event_id(),
                time=now,
                session_id=sid,
                event_type=ev_type,
            )

        # ---- message lifecycle ----
        if ev_type == "message.updated":
            return self._translate_message_updated(props, now, sid)

        if ev_type == "message.removed":
            return RawEvent(
                event_id=self._new_event_id(),
                time=now,
                session_id=sid,
                event_type=ev_type,
            )

        # ---- part lifecycle ----
        if ev_type == "message.part.updated":
            return self._translate_part_updated(props, now, sid)

        if ev_type == "message.part.delta":
            return self._translate_part_delta(props, now, sid)

        if ev_type == "message.part.removed":
            return RawEvent(
                event_id=self._new_event_id(),
                time=now,
                session_id=sid,
                event_type=ev_type,
            )

        # ---- permission ----
        if ev_type == "permission.asked":
            if permission_is_parent_gated(
                str(props.get("permission", "run")),
                parent_hosted_shell=self._ctx.parent_hosted_shell,
            ):
                # This process gates the call itself and raises the ask that
                # names the command. Hand this one to the adapter to answer
                # instead of publishing a second card for the same call.
                request_id = str(props.get("id", ""))
                if request_id:
                    self._ctx.parent_gated_requests.append(request_id)
                return None
            return self._translate_permission_asked(props, now, sid)
        if ev_type == "permission.replied":
            # Schema: `{sessionID, requestID, reply: "once"|"always"|"reject"}`.
            # NOT `id` — the upstream renamed this field.
            replied_id = str(props.get("requestID", ""))
            self._ctx.scoped_always_requests.discard(replied_id)
            self._ctx.asks_awaiting_part = {
                cid: ask
                for cid, ask in self._ctx.asks_awaiting_part.items()
                if str(ask.props.get("id", "")) != replied_id
            }
            return PermissionResolved(
                event_id=self._new_event_id(),
                time=now,
                session_id=sid,
                request_id=replied_id,
                option_id=self._opencode_reply_to_option(props.get("reply")),
            )

        # ---- question (clarifier from the assistant) ----
        if ev_type == "question.asked":
            # Schema (question/index.ts:58-66):
            #   `{id, sessionID, questions: Info[], tool?: {messageID, callID}}`
            return self._translate_question_asked(props, now, sid)
        if ev_type == "question.replied":
            # Schema (question/index.ts:78-82):
            #   `{sessionID, requestID, answers: Answer[]}`
            answers_raw = props.get("answers") or []
            answers: list[list[str]] = []
            if isinstance(answers_raw, list):
                for a in answers_raw:
                    if isinstance(a, list):
                        answers.append([str(x) for x in a if isinstance(x, str)])
            return QuestionAnswered(
                event_id=self._new_event_id(),
                time=now,
                session_id=sid,
                request_id=str(props.get("requestID", "")),
                answers=answers,
                decided_by="user",
            )
        if ev_type == "question.rejected":
            # Schema (question/index.ts:84-87): `{sessionID, requestID}`.
            return QuestionRejected(
                event_id=self._new_event_id(),
                time=now,
                session_id=sid,
                request_id=str(props.get("requestID", "")),
            )

        # ---- session compaction ----
        if ev_type == "session.compacted":
            # Payload is just `{sessionID}` (compaction.ts:27). The summary
            # text + boundary were captured from the (suppressed) summary
            # message + compaction part that streamed just before this.
            summary_text = "\n\n".join(self._ctx.summary_buffer.values()).strip()
            tail = self._ctx.compaction_tail_start_id
            if tail and tail in self._ctx.message_order:
                elided = list(self._ctx.message_order[: self._ctx.message_order.index(tail)])
            else:
                elided = []
            event = CompactionApplied(
                event_id=self._new_event_id(),
                time=now,
                session_id=sid,
                summarised_message_ids=elided,
                summary_text=summary_text,
            )
            # Reset per-compaction state (a session may compact repeatedly).
            self._reset_compaction_state()
            return event

        # ---- todo list / planner ----
        if ev_type == "todo.updated":
            # Schema (session/todo.ts:Updated):
            #   `{sessionID, todos: Todo[]}`
            todos_raw = props.get("todos") or []
            entries: list[dict[str, Any]] = []
            if isinstance(todos_raw, list):
                for t in todos_raw:
                    if isinstance(t, dict):
                        entries.append(
                            {
                                "content": str(t.get("content", "")),
                                "status": str(t.get("status", "pending")),
                                "priority": str(t.get("priority", "medium")),
                            }
                        )
            return PlanUpdated(
                event_id=self._new_event_id(),
                time=now,
                session_id=sid,
                entries=entries,
            )

        # ---- file edited (a tool edited a file) ----
        if ev_type == "file.edited":
            # Schema (file/index.ts:Edited): `{file: string}`.
            file_path = props.get("file")
            path = str(file_path) if isinstance(file_path, str) else ""
            return self._file_edited(path, now, sid)

        # ---- slash command executed ----
        if ev_type == "command.executed":
            # Schema (command/index.ts:Executed):
            #   `{name, sessionID, arguments, messageID}`
            name = props.get("name")
            arguments = props.get("arguments")
            message_id = props.get("messageID")
            return CommandExecuted(
                event_id=self._new_event_id(),
                time=now,
                session_id=sid,
                name=str(name) if isinstance(name, str) else "",
                arguments=str(arguments) if isinstance(arguments, str) else "",
                message_id=(str(message_id) if isinstance(message_id, str) else None),
            )

        # ---- fallthrough ----
        return RawEvent(
            event_id=self._new_event_id(),
            time=now,
            session_id=sid,
            event_type=str(ev_type or "unknown"),
        )

    # ------------------------------------------------------------------
    # Per-event translators
    # ------------------------------------------------------------------

    def _translate_question_asked(
        self,
        props: dict[str, Any],
        now: datetime,
        sid: str,
    ) -> QuestionRequest:
        """Translate `question.asked` SSE → `QuestionRequest`.

        Wire shape (question/index.ts:58-66):
          `{id, sessionID, questions: Info[], tool?: {messageID, callID}}`
        where each `Info` is
          `{question, header, options: Option[], multiple?, custom?}`
        and each `Option` is `{label, description}`.

        Defensive coercion: any field of an unexpected type falls back
        to a safe default so we never raise on a hostile / drifted
        payload — the user just sees fewer / less-rich options.
        """
        tool_info = props.get("tool")
        tool_call_id: str | None = None
        if isinstance(tool_info, dict):
            cid = tool_info.get("callID")
            if isinstance(cid, str):
                tool_call_id = cid

        questions_raw = props.get("questions") or []
        prompts: list[QuestionPrompt] = []
        if isinstance(questions_raw, list):
            for q in questions_raw:
                if not isinstance(q, dict):
                    continue
                opts_raw = q.get("options") or []
                opts: list[QuestionOption] = []
                if isinstance(opts_raw, list):
                    for o in opts_raw:
                        if not isinstance(o, dict):
                            continue
                        label = o.get("label")
                        if not isinstance(label, str):
                            continue
                        desc = o.get("description")
                        opts.append(
                            QuestionOption(
                                label=label,
                                description=desc if isinstance(desc, str) else None,
                            )
                        )
                question_text = q.get("question")
                header = q.get("header")
                multiple = q.get("multiple")
                custom = q.get("custom")
                prompts.append(
                    QuestionPrompt(
                        question=str(question_text) if isinstance(question_text, str) else "",
                        header=header if isinstance(header, str) else None,
                        options=opts,
                        multiple=bool(multiple) if isinstance(multiple, bool) else False,
                        # opencode's `custom` defaults to true upstream
                        # (question/index.ts:43-45). Mirror that.
                        custom=bool(custom) if isinstance(custom, bool) else True,
                    )
                )

        # Detect the plan-approval sentinel header set by our patched
        # `plan_present` tool. The IR carries a typed `kind` field;
        # consumers branch on that, not on the header string.
        kind: Literal["question", "plan_approval"] = "question"
        plan_markdown = ""
        plan_prompt = next((p for p in prompts if p.header == _PLAN_APPROVAL_HEADER), None)
        if plan_prompt is not None:
            kind = "plan_approval"
            # `plan_present` reads the model's plan FILE and puts its content in the
            # question body; surface it as `plan_markdown` so the UI renders the plan
            # from one field across both adapters.
            plan_markdown = plan_prompt.question

        return QuestionRequest(
            event_id=self._new_event_id(),
            time=now,
            session_id=sid,
            request_id=str(props.get("id", "")),
            tool_call_id=tool_call_id,
            questions=prompts,
            kind=kind,
            plan_markdown=plan_markdown,
        )

    def _translate_message_updated(
        self,
        props: dict[str, Any],
        now: datetime,
        sid: str,
    ) -> Event | None:
        """Translate `message.updated` against opencode's User|Assistant
        info schema.

        Wire shape (per message-v2.ts):
          props.sessionID: SessionID
          props.info: User | Assistant
            User:      {id, role: "user", time: {created}, ...}
            Assistant: {id, role: "assistant",
                        time: {created, completed?},      ← "done" flag lives here
                        tokens?, cost?,
                        finish?: {reason, ...},           ← NOT `finishReason`
                        error?, ...}

        Completion is the presence of `time.completed` (assistant only).
        """
        info_raw = props.get("info")
        info: dict[str, Any] = info_raw if isinstance(info_raw, dict) else {}
        message_id = str(info.get("id", ""))
        role = info.get("role")
        if not message_id or role not in ("user", "assistant", "system", "tool"):
            return RawEvent(
                event_id=self._new_event_id(),
                time=now,
                session_id=sid,
                event_type="message.updated",
            )

        # Track creation order so we can derive the elided set on compaction.
        if message_id not in self._ctx.message_order:
            self._ctx.message_order.append(message_id)

        # opencode's compaction summary is an assistant message flagged
        # `summary:true`. Suppress it from the normal stream (both its
        # create and complete `message.updated`s) — its text parts are
        # captured into the summary buffer and surfaced as a single
        # `CompactionApplied` on `session.compacted`, so the raw summary
        # template never renders as an assistant turn.
        if role == "assistant" and info.get("summary") is True:
            first_sight = self._ctx.summary_message_id is None
            self._ctx.summary_message_id = message_id
            # The summary message's CREATE is the earliest "compaction has begun"
            # signal opencode gives — emit the `compacting` phase once so the UI
            # opens its "Compacting…" card now, instead of only flashing the
            # finished card on `session.compacted`. Its later updates are silent.
            if first_sight:
                return self._status("running", "compacting", now, sid)
            return None

        time_raw = info.get("time")
        time_struct: dict[str, Any] = time_raw if isinstance(time_raw, dict) else {}
        completed_at = time_struct.get("completed")
        if completed_at is not None and role == "assistant":
            # message-v2.ts:486 — `finish: Schema.optional(Schema.String)`.
            # A bare optional string, NOT a nested `{reason: string}` dict.
            finish_raw = info.get("finish")
            finish_reason = finish_raw if isinstance(finish_raw, str) else ""
            # message-v2.ts:474-483 — `tokens: {total?, input, output,
            # reasoning, cache: {read, write}}`. Flatten `cache.{read,write}`
            # into the IR `dict[str, int]` as `cache_read`/`cache_write`
            # so the manifest's `tokens_total` fold picks them up
            # (store.py:_reconstruct_manifest_from_jsonl already reads
            # `cache_read`/`cache_write` from the persisted shape).
            tokens = _flatten_assistant_tokens(info.get("tokens"))
            cost = info.get("cost")
            cost_value = float(cost) if isinstance(cost, (int, float)) else None
            reported = (finish_reason, cost_value, tokens)
            if self._ctx.completed_messages.get(message_id) == reported:
                # The same completion restated — see `completed_messages`.
                return None
            self._ctx.completed_messages[message_id] = reported
            return MessageCompleted(
                event_id=self._new_event_id(),
                time=now,
                session_id=sid,
                message_id=message_id,
                finish_reason=finish_reason,
                tokens=tokens,
                cost=cost_value,
            )
        return MessageCreated(
            event_id=self._new_event_id(),
            time=now,
            session_id=sid,
            message_id=message_id,
            role=role,
            model=ran_model_stamp(info) if role == "assistant" else {},
        )

    def _translate_permission_asked(
        self, props: dict[str, Any], now: datetime, sid: str, *, wait: bool = True
    ) -> PermissionRequest:
        """Translate ``permission.asked``.

        Schema: `{id, sessionID, permission, patterns, metadata, always,
        tool?: {messageID, callID}}`. opencode raises the ask from the tool's
        own ``execute`` while the part naming the call's arguments is published
        by a separate consumer of the same model stream, so the ask can land
        BEFORE the part. An ask naming a call no part has been seen for goes out
        marked ``subject_pending`` — a reader sees it waiting, the policy leaves
        it alone — and is raised again, under the same id and with the command
        the part carried, when that part lands.
        """
        tool_info = props.get("tool")
        tool_call_id: str | None = None
        provider_call_id: str | None = None
        if isinstance(tool_info, dict):
            cid = tool_info.get("callID")
            if isinstance(cid, str) and cid:
                # The ask names the provider's call; the transcript keys the
                # call by its part. Carry the part id as the call id whenever
                # the part has been seen, and the provider id alongside, so
                # a reader acting on the ask later finds the call by either.
                provider_call_id = cid
                tool_call_id = self._ctx.part_by_call_id.get(cid, cid)
        native_kind = str(props.get("permission", "run"))
        raw_patterns = [p for p in (props.get("patterns") or []) if isinstance(p, str)]
        always = [p for p in (props.get("always") or []) if isinstance(p, str)]
        # The rule glob never travels as the subject: a reader asked to approve
        # `*` is being asked to approve a command they cannot see, and a
        # classifier handed it reads it as a write named `*`. Where the glob was
        # all there was — opencode's generic ask for an MCP tool names no
        # arguments — the command is taken from the call the ask gates, which is
        # the only place it exists at ask time.
        patterns = strip_rule_glob(raw_patterns)
        # opencode carries the REAL file path in `metadata.filepath` (an
        # `external_directory` ask, where `patterns` is only the `<dir>/*`
        # allow-rule glob). Recover it so the descriptor — and the UI — name the
        # actual file, not the glob.
        meta = props.get("metadata")
        meta = meta if isinstance(meta, dict) else None
        real_target = meta.get("filepath") if meta else None
        subject_pending = False
        if not patterns and provider_call_id is not None:
            gated_command = self._ctx.command_by_call_id.get(provider_call_id)
            if gated_command:
                patterns = [gated_command]
            elif (
                wait
                and not isinstance(real_target, str)
                and provider_call_id not in self._ctx.part_by_call_id
            ):
                # Nothing on the ask names its subject and no part for this
                # call has been seen: the command is still on its way. Raised
                # again from the part branch when it lands, or as it is once
                # the wait runs out.
                subject_pending = True
                self._ctx.asks_awaiting_part[provider_call_id] = _WaitingAsk(
                    dict(props), now + timedelta(seconds=self._ctx.ask_subject_wait_seconds)
                )
                while len(self._ctx.asks_awaiting_part) > self._ctx.ask_subject_wait_max:
                    self._ctx.asks_awaiting_part.pop(next(iter(self._ctx.asks_awaiting_part)))
        descriptor = _descriptor_for_opencode(
            native_kind,
            patterns,
            real_target if isinstance(real_target, str) else None,
            metadata=meta,
            sandbox_dir=self._ctx.sandbox_dir,
            workspace_root=self._ctx.workspace_root,
        )
        request_id = str(props.get("id", ""))
        if request_id and always_may_travel(always, descriptor):
            self._ctx.scoped_always_requests.add(request_id)
        # Capture the edit's diff and carry it ON the request so the UI can
        # show it in the permission card before the user decides — the
        # `file.edited` that also carries it only lands AFTER the write.
        captured = self._capture_file_diff(props.get("metadata"))
        insertions, deletions, preview = diff_preview(*captured) if captured else (None, None, None)
        extra: dict[str, Any] = {"subject_pending": True} if subject_pending else {}
        return PermissionRequest(
            event_id=self._new_event_id(),
            time=now,
            session_id=sid,
            request_id=request_id,
            tool_call_id=tool_call_id,
            provider_call_id=provider_call_id,
            permission_kind=native_kind,
            canonical_kind=_opencode_kind_to_canonical(native_kind),
            patterns=patterns,
            **extra,
            subject=descriptor.model_dump(mode="json") if descriptor else None,
            insertions=insertions,
            deletions=deletions,
            preview=preview,
            options=ask_options(always=always, descriptor=descriptor),
        )

    def _translate_part_updated(
        self,
        props: dict[str, Any],
        now: datetime,
        sid: str,
    ) -> Event | list[Event] | None:
        """Translate `message.part.updated`.

        Wire shape (per message-v2.ts):
          props.sessionID: SessionID
          props.part: <Part discriminated union by `type`>
          props.time: NonNegativeInt

        ToolPart-specific fields (where opencode differs from a guess):
          part.tool        — the tool NAME (string).      ← NOT `part.name`
          part.callID      — opencode's tool-call id.
          part.state       — DISCRIMINATED UNION by `status`:
                              {status: "pending", input?, time?, ...}
                              {status: "running", input?, output?, ...}
                              {status: "completed", input, output, time, ...}
                              {status: "error", input?, error, ...}
          (There is NO `part.kind` field upstream.)

        Opencode emits `message.part.updated` both at part-start AND
        finalize. We track `ctx.open_parts` keyed by `part_id` so we
        emit `PartStarted` / `ToolCall` on first sight and
        `PartCreated` / `ToolCallUpdate` on subsequent updates.
        """
        part_raw = props.get("part")
        part: dict[str, Any] = part_raw if isinstance(part_raw, dict) else {}
        part_id = str(part.get("id", ""))
        message_id = str(part.get("messageID", ""))
        if not part_id or not message_id:
            return RawEvent(
                event_id=self._new_event_id(),
                time=now,
                session_id=sid,
                event_type="message.part.updated",
            )

        # Compaction boundary marker (on the user message). opencode sets
        # `tail_start_id` AFTER summarizing, so it may arrive empty first
        # then updated — capture whenever present. Suppress from the stream.
        if part.get("type") == "compaction":
            tail = part.get("tail_start_id")
            if isinstance(tail, str) and tail:
                self._ctx.compaction_tail_start_id = tail
            return None

        # Text parts of the suppressed `summary:true` message: capture the
        # finalized text into the summary buffer, emit nothing.
        if message_id and message_id == self._ctx.summary_message_id:
            time_struct = part.get("time")
            time_end = time_struct.get("end") if isinstance(time_struct, dict) else None
            if time_end is not None:
                text_field = part.get("text")
                if isinstance(text_field, str) and text_field:
                    self._ctx.summary_buffer[part_id] = text_field
                elif text_field == "":
                    self._ctx.summary_buffer.pop(part_id, None)
            return None

        part_type = self._opencode_part_type(part.get("type"))
        was_open = part_id in self._ctx.open_parts

        # State is a discriminated union; for non-tool parts it may be
        # absent. Always normalize via `.get` to avoid AttributeError.
        state_raw = part.get("state")
        state: dict[str, Any] = state_raw if isinstance(state_raw, dict) else {}
        state_status = state.get("status") if isinstance(state, dict) else None

        # Tool-call lifecycle ----------------------------------------
        if part_type == "tool_call":
            if part_id in self._ctx.synthetically_closed_tool_parts:
                # The adapter already closed this tool synthetically (its real
                # closing frame lost the race past the idle-hold grace); a late
                # arrival must not re-open the part as a fresh ToolCall.
                return None
            tool_name = part.get("tool")  # NOT `part.name`
            provider_call_id_raw = part.get("callID")
            provider_call_id = (
                provider_call_id_raw
                if isinstance(provider_call_id_raw, str) and provider_call_id_raw
                else None
            )
            completed_ask: PermissionRequest | None = None
            if provider_call_id is not None:
                self._ctx.part_by_call_id[provider_call_id] = part_id
                self._remember_call_command(provider_call_id, state.get("input"))
                waiting = self._ctx.asks_awaiting_part.pop(provider_call_id, None)
                if waiting is not None:
                    # The ask that raced ahead of this part: raised again with
                    # the command this frame carries, under its own id.
                    completed_ask = self._translate_permission_asked(
                        waiting.props, now, sid, wait=False
                    )
            call: ToolCall | None = None
            if not was_open:
                call = ToolCall(
                    event_id=self._new_event_id(),
                    time=now,
                    session_id=sid,
                    tool_call_id=part_id,
                    provider_call_id=provider_call_id,
                    message_id=message_id,
                    tool_name=str(tool_name) if isinstance(tool_name, str) else "",
                    tool_kind=None,  # opencode doesn't ship a separate kind
                    input=_safe_dict(state.get("input")),
                    status=self._opencode_tool_status(state_status),
                )
                if state_status not in ("completed", "error"):
                    self._ctx.open_parts[part_id] = _OpenPart(
                        message_id=message_id,
                        part_id=part_id,
                        part_type=part_type,
                    )
                    return call if completed_ask is None else [call, completed_ask]
                # First sight already terminal: an SSE reconnect has no
                # replay, so the pending/running frames can be lost and the
                # part's first visible frame is its final state. Fall through
                # to emit the closing update alongside the ToolCall instead
                # of stranding the part open (a stranded part holds every
                # later idle for the watchdog grace, then gets a fabricated
                # synthetic closure over this real one).
            output_field = state.get("output")
            # opencode's ToolState union (message-v2.ts:248-308): only
            # `pending` may omit `input`; `running`/`completed` always
            # carry it. Forward whenever present so UIs can patch
            # their cards (the initial ToolCall fired with `input={}`).
            update_input_raw = state.get("input")
            update_input = update_input_raw if isinstance(update_input_raw, dict) else None
            metadata_raw = state.get("metadata")
            update = ToolCallUpdate(
                event_id=self._new_event_id(),
                time=now,
                session_id=sid,
                tool_call_id=part_id,
                status=self._opencode_tool_status(state_status),
                input=update_input,
                output=output_field if isinstance(output_field, (dict, str)) else None,
                error_text=(
                    str(state.get("error")) if isinstance(state.get("error"), str) else None
                ),
                # The tool's structured result (the question tool's
                # `{answers: [...]}`) — the UI reads the per-row answer from here.
                metadata=metadata_raw if isinstance(metadata_raw, dict) else {},
            )
            if state_status in ("completed", "error"):
                self._ctx.open_parts.pop(part_id, None)
                self._ctx.unpublished_tool_closures.add(part_id)
                if provider_call_id is not None:
                    self._ctx.part_by_call_id.pop(provider_call_id, None)
                    self._ctx.command_by_call_id.pop(provider_call_id, None)
            out: list[Event] = [update] if call is None else [call, update]
            if completed_ask is not None:
                out.insert(len(out) - 1, completed_ask)
            return out[0] if len(out) == 1 else out

        # Text / reasoning parts ------------------------------------
        # CRITICAL: text/reasoning parts are NOT a status-discriminated
        # union like tool parts (message-v2.ts:97-123). They have NO
        # `state` field. They finalize when `time.end` is set — that's
        # the signal the part is complete. User-message text parts
        # typically arrive with `time.end` ALREADY set on first sight
        # (the user's text is complete on submit), so we must handle
        # "open + finalize in one event".
        time_struct = part.get("time")
        time_end = time_struct.get("end") if isinstance(time_struct, dict) else None
        finalized = time_end is not None
        text_field = part.get("text")
        text = text_field if isinstance(text_field, str) else ""
        # opencode marks injected/internal text parts `synthetic` — both
        # ours (the plan-mode reminder we send) and its own (e.g. the
        # "plan approved, execute it" nudge). Carry the flag through so
        # the UI can skip rendering them.
        synthetic = part.get("synthetic") is True

        if not was_open:
            started = PartStarted(
                event_id=self._new_event_id(),
                time=now,
                session_id=sid,
                message_id=message_id,
                part_id=part_id,
                part_type=part_type,
                initial={k: v for k, v in part.items() if k != "state"},
            )
            if finalized:
                # Already-complete part (e.g. a user-message text part).
                # Emit PartStarted + PartCreated in one shot; don't
                # register as open since it's done.
                pair: list[Event] = [
                    started,
                    self._finalized_text_part(
                        now, sid, part_id, message_id, part_type, text, synthetic
                    ),
                ]
                return pair
            self._ctx.open_parts[part_id] = _OpenPart(
                message_id=message_id,
                part_id=part_id,
                part_type=part_type,
            )
            return started

        # Subsequent update on an open part.
        if finalized:
            existing = self._ctx.open_parts.pop(part_id, None)
            # opencode populates `part.text` on finalize for TEXT parts but
            # leaves it EMPTY for REASONING (whose text only ever arrives via
            # deltas). The finalized part is the ONLY thing persisted to
            # chat.jsonl, so fall back to the deltas we buffered — otherwise a
            # replayed reasoning part is textless, the fold drops it, and the UI
            # shows no reasoning even when the effective effort enables it.
            if not text and existing is not None and existing.buffer:
                text = "".join(existing.buffer)
            return self._finalized_text_part(
                now, sid, part_id, message_id, part_type, text, synthetic
            )
        # Intermediate growth — the live UI is driven by message.part.delta
        # chunks; only finalized parts are persisted. Drop the interim
        # update rather than polluting chat.jsonl with a RawEvent.
        return None

    def _finalized_text_part(
        self,
        now: datetime,
        sid: str,
        part_id: str,
        message_id: str,
        part_type: PartType,
        text: str,
        synthetic: bool = False,
    ) -> PartCreated:
        """Build the finalized `PartCreated` for a text or reasoning
        part. Reasoning parts get a `ReasoningPart`; everything else a
        `TextPart`."""
        part: TextPart | ReasoningPart
        if part_type == "reasoning":
            part = ReasoningPart(part_id=part_id, message_id=message_id, text=text)
        else:
            part = TextPart(
                part_id=part_id,
                message_id=message_id,
                text=text,
                synthetic=synthetic,
            )
        return PartCreated(
            event_id=self._new_event_id(),
            time=now,
            session_id=sid,
            part=part,
        )

    def _translate_part_delta(
        self,
        props: dict[str, Any],
        now: datetime,
        sid: str,
    ) -> Event | None:
        """Translate `message.part.delta` (the token-stream event).

        Wire shape (per message-v2.ts):
          props.sessionID: SessionID
          props.messageID: MessageID
          props.partID:    PartID
          props.field:     string  — which field is being delta'd
                                      (always "text" in current upstream)
          props.delta:     string  — the delta value, NOT a dict

        Emits `AgentMessageChunk` for text parts and `AgentThoughtChunk`
        for reasoning parts. We use `ctx.open_parts` to disambiguate
        (the part-type is captured by the prior `message.part.updated`).
        Both TextPart and ReasoningPart have a `text` field, so opencode
        sends `field="text"` for both — only the part_type tells us
        which IR event to emit.
        """
        part_id = str(props.get("partID", ""))
        message_id = str(props.get("messageID", ""))
        field_name = props.get("field")
        text_delta_raw = props.get("delta")
        # delta is `Schema.String` upstream — defensive isinstance in
        # case a future schema bump widens it.
        text_delta = text_delta_raw if isinstance(text_delta_raw, str) else ""
        if not text_delta or not part_id or not message_id:
            return None
        # Suppress live token deltas of the compaction summary message —
        # the finalized text is captured in `_translate_part_updated`, and
        # the whole summary surfaces once via `CompactionApplied`.
        if message_id == self._ctx.summary_message_id:
            return None
        if field_name != "text":
            # Future-proof: opencode currently only deltas the `text`
            # field. Other fields (e.g. tool `input`) would need a
            # different IR shape — surface as RawEvent for visibility.
            return RawEvent(
                event_id=self._new_event_id(),
                time=now,
                session_id=sid,
                event_type="message.part.delta",
            )

        existing = self._ctx.open_parts.get(part_id)
        if existing is None:
            # Delta arrived before the part.updated event — defensive
            # registration with a best-guess part_type of "text".
            existing = _OpenPart(
                message_id=message_id,
                part_id=part_id,
                part_type="text",
            )
            self._ctx.open_parts[part_id] = existing
        existing.buffer.append(text_delta)
        sequence = len(existing.buffer) - 1

        if existing.part_type == "reasoning":
            return AgentThoughtChunk(
                event_id=self._new_event_id(),
                time=now,
                session_id=sid,
                message_id=message_id,
                part_id=part_id,
                sequence=sequence,
                text=text_delta,
            )
        return AgentMessageChunk(
            event_id=self._new_event_id(),
            time=now,
            session_id=sid,
            message_id=message_id,
            part_id=part_id,
            sequence=sequence,
            text=text_delta,
        )

    def _translate_session_status(
        self,
        props: dict[str, Any],
        now: datetime,
        sid: str,
    ) -> Event | list[Event] | None:
        """Translate `session.status` (the canonical replacement for
        `session.idle`).

        Wire shape (per status.ts):
          props.sessionID: SessionID
          props.status:    {type: "idle" | "retry" | "busy", ...}
                            retry variant carries: attempt, message,
                            action?, next.
        """
        status_struct = props.get("status")
        if not isinstance(status_struct, dict):
            return None
        status_type = status_struct.get("type")
        if status_type == "idle":
            if not self._ctx.close_attempt():
                return None
            return self._status("idle", "idle", now, sid)
        if status_type == "busy":
            self._ctx.begin_run()
            return self._status("running", "awaiting_llm", now, sid)
        if status_type == "retry":
            message = str(status_struct.get("message", "retrying"))
            attempt_raw = status_struct.get("attempt")
            attempt = int(attempt_raw) if isinstance(attempt_raw, (int, float)) else 0
            next_raw = status_struct.get("next")
            next_in_ms = int(next_raw) if isinstance(next_raw, (int, float)) else None
            # Emit both: SessionStatusChanged keeps the REPL's
            # "is the harness busy?" gate accurate; Retrying gives UIs
            # a typed signal they can render distinctly.
            return [
                self._status("running", "awaiting_llm", now, sid, detail=message),
                Retrying(
                    event_id=self._new_event_id(),
                    time=now,
                    session_id=sid,
                    attempt=attempt,
                    reason=message,
                    next_attempt_in_ms=next_in_ms,
                ),
            ]
        return self._status(
            "running", None, now, sid, detail=f"unknown status type: {status_type!r}"
        )

    def _translate_session_error(
        self, error_field: Any, now: datetime, sid: str
    ) -> SessionStatusChanged | None:
        """Translate `session.error` (`{sessionID?, error?: <assistant error
        union>}`, stringified since the union is opencode-internal).

        A failed compaction surfaces here rather than via `session.compacted`,
        so half-captured compaction state is dropped. `overflow` marks the
        gateway's `context_length_exceeded` code (also when buried in a
        stringified-JSON message) so the runtime auto-compacts and retries."""
        self._reset_compaction_state()
        ctx = self._ctx
        if ctx.attempt_closed and not ctx.closed_by_adapter:
            # An error while nothing runs settles the pending prompt if one
            # exists (`prompt_async` publishes a failure with no `busy`), else
            # the current attempt again.
            ctx.begin_run()
        if not ctx.close_attempt():
            return None
        # A money refusal rides as the gateway's structured code, lifted off
        # whatever wrapper the agent put around the 402 body; the detail is
        # then the refusal's own sentence rather than the wrapper's summary.
        refusal = credit_refusal_from_error_payload(error_field)
        return self._status(
            "error",
            "error",
            now,
            sid,
            detail=refusal.message if refusal is not None else _summarize_error(error_field),
            overflow=error_payload_is_overflow(error_field),
            refusal=refusal,
        )

    def _status(
        self,
        status: Any,
        phase: Any,
        now: datetime,
        sid: str,
        *,
        detail: str | None = None,
        overflow: bool = False,
        refusal: CreditRefusal | None = None,
    ) -> SessionStatusChanged:
        """Every status this translator constructs, stamped with the attempt."""
        return SessionStatusChanged(
            event_id=self._new_event_id(),
            time=now,
            session_id=sid,
            status=status,
            phase=phase,
            detail=detail,
            overflow=overflow,
            turn_id=self._ctx.attempt_id,
            refusal=refusal,
        )

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _new_event_id(self) -> str:
        return secrets.token_hex(10)

    def _capture_file_diff(self, metadata: Any) -> tuple[str, str] | None:
        """Stash a write/edit tool's diff from `permission.asked` metadata and
        return the captured `(filepath, diff)`, or `None` for a non-edit ask.

        Both the write and edit tools ship `{filepath, diff}` in the
        permission-ask metadata BEFORE publishing `file.edited`, so this fires
        first: the caller shows the diff on the permission card immediately,
        and the stash is drained onto the matching `file.edited` for the
        post-write / replay paths.
        """
        if not isinstance(metadata, dict):
            return None
        filepath = metadata.get("filepath")
        diff = metadata.get("diff")
        if isinstance(filepath, str) and filepath and isinstance(diff, str) and diff:
            self._ctx.pending_file_diffs[filepath] = diff
            return filepath, diff
        return None

    def _file_edited(self, path: str, now: datetime, sid: str) -> FileEdited:
        """Build a `FileEdited`, attaching a captured diff preview when one
        was stashed for this path (drained so it can't leak to a later edit)."""
        diff = self._ctx.pending_file_diffs.pop(path, None)
        if diff is None:
            return FileEdited(event_id=self._new_event_id(), time=now, session_id=sid, path=path)
        insertions, deletions, preview = diff_preview(path, diff)
        return FileEdited(
            event_id=self._new_event_id(),
            time=now,
            session_id=sid,
            path=path,
            insertions=insertions,
            deletions=deletions,
            preview=preview,
        )

    def _reset_compaction_state(self) -> None:
        """Clear the in-flight compaction capture (summary message id +
        buffer + tail). Called when a compaction completes (`session
        .compacted`) or fails (`session.error`)."""
        self._ctx.summary_message_id = None
        self._ctx.summary_buffer.clear()
        self._ctx.compaction_tail_start_id = None

    def reset(self) -> None:
        """Drop ALL cross-event state so nothing bleeds across a hard
        conversation boundary (a `/clear`). Beyond the compaction capture,
        this forgets any open (still-streaming) parts and the message-order
        ledger — the new opencode session re-issues fresh ids, and stale
        ones must not leak into post-clear events."""
        self._reset_compaction_state()
        self._ctx.open_parts.clear()
        self._ctx.message_order.clear()
        self._ctx.completed_messages.clear()
        self._ctx.pending_file_diffs.clear()
        self._ctx.command_by_call_id.clear()
        self._ctx.asks_awaiting_part.clear()
        self._ctx.parent_gated_requests.clear()
        self._ctx.scoped_always_requests.clear()
        self._ctx.reset_attempt()

    def _remember_call_command(self, provider_call_id: str, tool_input: Any) -> None:
        """Keep the command a call was invoked with, so an ask that carries no
        arguments of its own can still name it.

        Only a non-empty string is kept: opencode restates a running call with no
        input on some frames, and a later restatement must not erase what the frame
        that carried the arguments already reported."""
        if not isinstance(tool_input, dict):
            return
        command = tool_input.get("command")
        if isinstance(command, str) and command.strip():
            self._ctx.command_by_call_id[provider_call_id] = command

    def _opencode_part_type(self, raw: Any) -> PartType:
        if raw in ("reasoning", "thinking"):
            return "reasoning"
        if raw in ("tool_call", "tool", "tool-call"):
            return "tool_call"
        if raw == "file":
            return "file"
        return "text"

    def _opencode_tool_status(self, raw: Any) -> Any:
        mapping = {
            "pending": "pending",
            "running": "running",
            "in_progress": "running",
            "completed": "completed",
            "success": "completed",
            "failed": "error",
            "error": "error",
        }
        return mapping.get(raw, "pending")

    def _opencode_reply_to_option(self, raw: Any) -> Any:
        mapping = {
            "once": "allow_once",
            "always": "allow_always",
            "reject": "reject_once",
        }
        return mapping.get(raw, "reject_once")


# ---------------------------------------------------------------------------
# Module-level helpers (re-exported for the adapter)
# ---------------------------------------------------------------------------


def _safe_dict(value: Any) -> dict[str, Any]:
    """Coerce an unknown JSON value into a `dict[str, Any]` for IR
    fields that require one. Non-dicts become an empty dict."""
    return value if isinstance(value, dict) else {}


def _pick_latest_session(sessions: list[Any]) -> str | None:
    """Pick the session id with the most recent `time.updated`
    (falling back to `time.created`) from opencode's session list.

    Shape (session/session.ts:208-227): each entry carries
    `{id, time: {created, updated, ...}, ...}`. We're defensive
    against missing fields — entries without a time struct sort
    behind anything that has one.
    """

    def sort_key(s: Any) -> int:
        if not isinstance(s, dict):
            return -1
        t = s.get("time")
        if not isinstance(t, dict):
            return -1
        updated = t.get("updated")
        created = t.get("created")
        for v in (updated, created):
            if isinstance(v, (int, float)):
                return int(v)
        return -1

    best = max(sessions, key=sort_key, default=None)
    if not isinstance(best, dict):
        return None
    sid = best.get("id")
    return str(sid) if isinstance(sid, str) else None


def _flatten_assistant_tokens(raw: Any) -> dict[str, int]:
    """Translate opencode's nested Assistant.tokens struct to the IR's
    flat `dict[str, int]`.

    Upstream shape (message-v2.ts:474-483):
      `{total?: Finite, input: Finite, output: Finite,
        reasoning: Finite, cache: {read: Finite, write: Finite}}`

    The IR's `MessageCompleted.tokens` is `dict[str, int]`. We promote
    `cache.read` / `cache.write` to top-level `cache_read` / `cache_write`
    so the manifest's `tokens_total` aggregator (which already expects
    those names — see
    `ChatStore._reconstruct_manifest_from_jsonl`) picks them up
    losslessly on persistence.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key in ("total", "input", "output", "reasoning"):
        v = raw.get(key)
        if isinstance(v, (int, float)):
            out[key] = int(v)
    cache = raw.get("cache")
    if isinstance(cache, dict):
        for key, alias in (("read", "cache_read"), ("write", "cache_write")):
            v = cache.get(key)
            if isinstance(v, (int, float)):
                out[alias] = int(v)
    return out


def _summarize_error(error_field: Any) -> str:
    """Opencode's `AssistantError` union as one line a reader may see (:func:`readable_error`)."""
    if error_field is None:
        return "harness error"
    if isinstance(error_field, str):
        return readable_error(error_field)
    if isinstance(error_field, dict):
        # Common shapes:
        #   {data: {message}}  — generic
        #   {name, data: {message}}
        #   {type, message}
        for key in ("message", "detail", "reason"):
            v = error_field.get(key)
            if isinstance(v, str) and v:
                return readable_error(v)
        data = error_field.get("data")
        if isinstance(data, dict):
            msg = data.get("message")
            if isinstance(msg, str) and msg:
                return readable_error(msg)
        name = error_field.get("name") or error_field.get("type")
        if isinstance(name, str) and name:
            return name
    return "harness error"


__all__ = [
    "OpencodeEventTranslator",
]
