"""Permission modes — a Claude Code-style approval policy layered on top
of the `PermissionBroker`.

The harness (opencode today) is configured to *ask* before running any
mutating tool. The active `PermissionMode` then decides what happens
when a `PermissionRequest` arrives: prompt the user, silently approve,
or silently reject. This keeps the underlying harness config static —
mode is a fast in-memory toggle, no respawn.

Modes:

- ``read_only``    — analyst mode: reads run, every mutation is auto-rejected.
                     Recording knowledge (``Effect.MEMORY``, the agent's memory)
                     is not a mutation of anything and runs in every mode.
- ``default``      — normal: reads run, every mutating tool asks the user.
- ``auto``         — autonomous: a grounded safety judge clears the recoverable
                     write middle; the destroy/egress floor still asks.
- ``plan``         — read-only planning. Edits are auto-rejected here as
                     a belt-and-braces guard; the harness should also be
                     routed through its own read-only planning agent (see
                     ``mode_to_agent``) so edits are refused server-side.
- ``bypass``       — auto-approve everything, no exceptions: runs every action
                     (including the destroy/egress floor) without asking ("yolo").

This module is intentionally harness-agnostic: it speaks only the IR's
``CanonicalPermissionKind`` set, so the daemon (and any future client)
can reuse the exact same policy regardless of which harness emitted the
request.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from alkera_core.schemas.chat import CanonicalPermissionKind

from alkera_cli.plugins.plugin_base.permissions.policy import (
    DATA_WRITE_RULES,
    KNOWLEDGE_IS_MEMORY,
)
from alkera_cli.plugins.plugin_base.permissions.refusal_words import (
    NOT_GRANTED,
    POLICY_REFUSED,
    person_decided,
    refusal_feedback,
)

PermissionMode = Literal["read_only", "default", "auto", "plan", "bypass"]

ALL_MODES: tuple[PermissionMode, ...] = (
    "read_only",
    "default",
    "auto",
    "plan",
    "bypass",
)

#: Display order in the ``/mode`` picker — least to most autonomy, with
#: ``read_only`` slotted after ``plan`` (both are read-only-ish: plan plans,
#: read-only analyzes). ``ALL_MODES`` is the canonical SET; this is only the
#: menu's visual ordering, so the picker reads default → plan → read-only →
#: auto → bypass regardless of how ``ALL_MODES`` is declared.
MODE_MENU_ORDER: tuple[PermissionMode, ...] = (
    "default",
    "plan",
    "read_only",
    "auto",
    "bypass",
)

#: The Shift+Tab cycle order (the TUI wires the keybinding; ``next_mode``
#: cycles this). It IS ``MODE_MENU_ORDER`` so Shift+Tab steps through exactly
#: the sequence the ``/mode`` picker shows — one canonical UI ordering, never
#: two that disagree.
CYCLE_MODES: tuple[PermissionMode, ...] = MODE_MENU_ORDER

# Canonical kinds that count as "file edits". Adapters MUST map any
# native edit-family permission (opencode's ``edit`` for write/edit/patch,
# Claude Code's ``editor.write``, …) onto the canonical ``"edit"`` so this
# set stays harness-agnostic.
EDIT_PERMISSION_KINDS: frozenset[CanonicalPermissionKind] = frozenset({"edit"})

# Decision the broker acts on for a single permission request.
AutoDecision = Literal["allow", "prompt", "reject"]

# Human-facing labels (also what the CLI accepts as `/mode <label>`).
MODE_LABELS: dict[PermissionMode, str] = {
    "read_only": "read-only",
    "default": "default",
    "auto": "auto",
    "plan": "plan",
    "bypass": "bypass",
}

# Aliases the CLI accepts for switching, mapped to the canonical mode.
# ``accept-edits`` is a LEGACY alias → ``default`` (the mode was retired); it
# keeps a resumed manifest or an old muscle-memory ``/accept-edits`` from erroring.
MODE_ALIASES: dict[str, PermissionMode] = {
    "read_only": "read_only",
    "read-only": "read_only",
    "readonly": "read_only",
    "ro": "read_only",
    "default": "default",
    "normal": "default",
    "accept-edits": "default",
    "accept_edits": "default",
    "auto": "auto",
    "automatic": "auto",
    "plan": "plan",
    "bypass": "bypass",
    "yolo": "bypass",
}


@dataclass(frozen=True, slots=True)
class ModeRule:
    """One mode's contract: the sentence a picker shows, beside what the
    permission chokepoint actually decides for each shape of action.

    They live in one row on purpose. A stance is a promise about when the reader
    will be asked, and the only way that promise stays true is for the sentence
    and the outcomes to be edited together — a picker that says "asks before
    every edit" while a write runs unasked costs the reader the one thing the
    control was for. ``apps/cli/tests/harness/test_stance_contract.py`` drives
    the real policy for each shape and fails on any row that has drifted, and pins the
    browser picker's copy against these sentences.
    """

    label: str
    description: str
    """The one line every mode picker shows, held inside the width the popup has."""
    read: AutoDecision
    """A read-effect action: a file read, a listing, a select."""
    chat_folder_change: AutoDecision
    """A write confined to this chat's own working folder. Alkera-managed scratch —
    where plan.md is drafted and where a cloud agent's whole working tree lives —
    so every mode admits it, the no-mutation modes included."""
    shared_folder_change: AutoDecision
    """A write into a workspace's shared folder: the working folder every chat of
    one workspace runs in, so not this chat's own. A mode that writes only in this
    chat refuses it; plan admits it because its plan file has nowhere else to go."""
    workspace_change: AutoDecision
    """A file edit, or a command that changes state, anywhere else."""
    destroy: AutoDecision
    """An irreversible change: ``rm -rf``, a drop, a truncate."""


#: What each mode does, and what each mode says it does.
MODE_RULES: dict[PermissionMode, ModeRule] = {
    "default": ModeRule(
        label="Default",
        description="Runs reads freely; asks before changes outside the chat's files.",
        read="allow",
        chat_folder_change="allow",
        shared_folder_change="allow",
        workspace_change="prompt",
        destroy="prompt",
    ),
    "plan": ModeRule(
        label="Plan",
        description="Explores, writes only in the chat's files, and proposes a plan.",
        read="allow",
        chat_folder_change="allow",
        shared_folder_change="allow",
        workspace_change="reject",
        destroy="reject",
    ),
    "read_only": ModeRule(
        label="Read-only",
        description="Reads freely, writes only in this chat, and runs no shell.",
        read="allow",
        chat_folder_change="allow",
        shared_folder_change="reject",
        workspace_change="reject",
        destroy="reject",
    ),
    "auto": ModeRule(
        label="Auto",
        description="Works on its own and pauses only for risky or destructive steps.",
        read="allow",
        chat_folder_change="allow",
        shared_folder_change="allow",
        workspace_change="allow",
        destroy="prompt",
    ),
    "bypass": ModeRule(
        label="Bypass permissions",
        description="Runs everything without asking.",
        read="allow",
        chat_folder_change="allow",
        shared_folder_change="allow",
        workspace_change="allow",
        destroy="allow",
    ),
}


def write_free_root(mode: PermissionMode, sandbox: object, chat_folder: Path) -> Path | None:
    """The directory a write lands in without an ask under ``mode``, or ``None``.

    That is the session's sandbox while it is this chat's own, a directory inside
    the chat's folder. A workspace member's sandbox is the workspace's shared
    folder, outside every member's chat folder, and only a mode whose
    ``shared_folder_change`` allows it writes there unasked; every other write
    goes on to the mode's own decision."""
    if not isinstance(sandbox, (str, Path)):
        return None
    root = Path(sandbox)
    if MODE_RULES[mode].shared_folder_change == "allow":
        return root
    try:
        resolved, folder = root.resolve(), chat_folder.resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    return root if resolved == folder or folder in resolved.parents else None


def approval_reaches_the_agent(mode: PermissionMode) -> bool:
    """Whether a reader's Allow on an ask raised in ``mode`` is acted on.

    Read off the stance's own row rather than listed beside it. A stance that
    refuses a mutating class OUTRIGHT refuses it whatever anyone answers, so an
    approval offered there is a control that lies; a stance that asks about it —
    or runs it, and raised this ask for some reason of its own — acts on the
    answer. ``auto`` is exactly the second case: it clears the recoverable
    middle itself and PAUSES for the destroy floor, and that pause is the
    reader's to answer.

    The read and chat-folder classes are not consulted: every stance admits
    both, so neither can be what an approval is discarded over.

    A stance's row is where its outcomes are decided (``MODE_RULES``), so
    deriving from it is what keeps a new stance from having to be remembered in
    a second place — which is how ``auto``, added after the lists were written,
    came to be missing from them.
    """
    rule = MODE_RULES[mode]
    return "reject" not in (rule.workspace_change, rule.destroy)


#: The stances in which the machine ACTS on a reader's approval. Every surface
#: that decides whether to offer one reads this rather than keeping a list:
#: the cloud mirror's ``NO_WRITE_MODES`` is its complement, and the browser
#: card's own table is pinned to it in
#: ``apps/cli/tests/harness/test_stance_contract.py``.
MODES_THAT_HONOUR_AN_APPROVAL: frozenset[PermissionMode] = frozenset(
    mode for mode in ALL_MODES if approval_reaches_the_agent(mode)
)

#: The complement: the stances that refuse the write whatever the reader
#: answers, so an approval must not be offered in them at all.
MODES_THAT_DISCARD_AN_APPROVAL: frozenset[PermissionMode] = (
    frozenset(ALL_MODES) - MODES_THAT_HONOUR_AN_APPROVAL
)


# ---------------------------------------------------------------------------
# Plan approval
# ---------------------------------------------------------------------------
#
# In plan mode the harness presents a plan and then asks the user to approve
# it (and pick how to proceed) or reject it with a reason. We reuse the
# harness's generic "question" surface for this, tagging the question with a
# typed `QuestionRequest.kind == "plan_approval"` field so the CLI knows the
# answer should ALSO flip the permission mode (not just flow back to the
# harness). Adapter-internal sentinels (e.g. opencode's header string) stay
# inside the adapter — consumers MUST switch on `kind`, not on header strings.
#
# The accept-option LABELS below MUST stay byte-identical to the labels the
# opencode `plan_present` tool offers (vendor/opencode/.../tool/plan.ts), since
# the mapping from chosen label → mode happens on both sides off the same
# string. Anything that isn't one of these labels is treated as a free-form
# rejection reason (the user stays in plan mode and the harness revises).

# Ordered accept options: (label shown to the user, resulting mode).
PLAN_ACCEPT_OPTIONS: tuple[tuple[str, PermissionMode], ...] = (
    ("Accept — run normally (ask before each change)", "default"),
    ("Accept — auto mode (run automatically, pause for risky steps)", "auto"),
    ("Accept — bypass all permission prompts", "bypass"),
)

_PLAN_LABEL_TO_MODE: dict[str, PermissionMode] = {
    label: mode for label, mode in PLAN_ACCEPT_OPTIONS
}


def plan_label_to_mode(label: str) -> PermissionMode | None:
    """Map a chosen plan-approval label to the mode to switch into, or
    ``None`` if the answer isn't an accept option (→ treat as a rejection
    reason; the user stays in plan mode)."""
    return _PLAN_LABEL_TO_MODE.get(label.strip())


# A reader may write a note with either plan answer: guidance for the run on an
# approval, what to change on a rejection. It rides the plan prompt's answer as
# the entry after the chosen label, so the label still decides the mode and a
# resolver that knows nothing of notes reads the answer it always did.


def plan_answers_with_note(answers: list[list[str]], note: str | None) -> list[list[str]]:
    """``answers`` with ``note`` carried after the plan prompt's chosen label;
    unchanged when there is no note or nothing was chosen."""
    note = (note or "").strip()
    if not note or not answers or not answers[0]:
        return answers
    return [[answers[0][0], note], *answers[1:]]


def plan_note_of(answers: object) -> str | None:
    """The note a plan answer carries, or ``None``."""
    if not isinstance(answers, list) or not answers:
        return None
    first = answers[0]
    if not isinstance(first, list) or len(first) < 2:
        return None
    note = str(first[1]).strip()
    return note or None


def with_plan_note(text: str, note: str | None) -> str:
    """The model-facing outcome of a plan answer, with the reader's note after it."""
    return f"{text} Note from the user: {note}" if note else text


# Per-turn system-prompt addendum injected on EVERY plan-mode turn (passed
# via PromptInput.system, which the runtime emits as a synthetic
# `<system-reminder>` text part — matching opencode's own once-per-turn
# cadence convention, see vendor/opencode/.../session/prompt.ts).
# Scoping it per-turn (and keeping it concise) avoids the accumulation
# problem of stuffing the steering directive into the base system prompt:
# a tight single paragraph is enough to drive `plan_present` reliably.
def plan_mode_steering(
    plan_tool: str, question_tool: str | None = None, *, subagents: bool = True
) -> str:
    """The UNIFIED plan-mode steering, parameterized by each backend's own plan /
    question tool name (the single source of the plan-mode contract). Re-injected
    every plan-mode turn — that per-turn reminder IS the soft nudge to converge on
    a plan or questions. Also leans HEAVILY on Explore here AND on the
    knowledge base: planning is exactly when broad, parallel read-only codebase
    understanding and grounding in recorded decisions/conventions/gotchas pay off.

    ``subagents`` False is a deployment that serves no agent-spawning tools: the
    lean-on-Explore paragraph becomes read-widely-yourself. This steering is the
    strongest delegation instruction the model gets — it says it ALWAYS has
    ``spawn_agent`` and must never work inline — so it is the one that must not
    survive the switch."""
    ask = (
        f"ask the user clarifying questions (call `{question_tool}`"
        + (", batching multiple questions when useful)" if question_tool else ")")
        if question_tool
        else "ask the user clarifying questions"
    )
    text = (
        "Plan mode is READ-ONLY this turn: explore freely with read-only tools to "
        "understand the work, but do NOT edit files or run mutating commands — "
        f"they are disabled. {DATA_WRITE_RULES['plan']} {KNOWLEDGE_IS_MEMORY} This is "
        "exactly when broad, parallel codebase "
        "understanding pays off: LEAN ON EXPLORE HEAVILY — spawn parallel Explore "
        'agents (the `spawn_agent` tool, agent="explore"; you ALWAYS have it — never '
        "do this inline assuming you can't delegate, and call `list_agent_types` if "
        "unsure which agents exist) to map the relevant "
        "subsystems, find all call sites, and surface constraints before you commit "
        "to a plan; batch several in one turn to fan out. And ground the plan in "
        "RECORDED KNOWLEDGE, not just the code: `context_search` the "
        "project knowledge base FIRST and again as the plan takes shape (local, free, "
        "instant) for past decisions, conventions, gotchas, and anything you're unsure "
        "about — an unclear column, data source, or assumption. End EVERY plan-mode turn by "
        "EITHER presenting your plan for the user's approval — write the full plan as "
        "Markdown to `plan.md` in your sandbox directory (its path is given below) using "
        "the write tool (you can edit it as you refine the plan): in a reply, name it as "
        "`[Plan](plan.md)`, then call "
        f"`{plan_tool}` with that file's PATH (NOT the plan text) — OR by {ask} — "
        "including after a rejection. Do not end a plan-mode turn still mid-investigation "
        "with neither a plan nor questions."
    )
    if subagents:
        return text
    # Guarded by the same table as the always-on blocks: reword the paragraph
    # above without updating it and this raises rather than leaving a session
    # that cannot delegate being told it always can.
    from alkera_cli.harness.system_prompt import without_delegation

    return without_delegation("plan_mode", text)


#: opencode's plan tool is ``plan_present`` + the native ``question`` tool.
PLAN_MODE_SYSTEM_PROMPT = plan_mode_steering("plan_present", "question")
#: The same steering for a deployment with no agent-spawning tools.
PLAN_MODE_SYSTEM_PROMPT_NO_SUBAGENTS = plan_mode_steering(
    "plan_present", "question", subagents=False
)

# Per-turn steering injected on EVERY auto-mode turn (same mechanism as plan
# mode). Tells the model it has real autonomy in a trusted, sandboxed workspace —
# so it should just DO the work (edit, write, run builds/tests/commands) without
# asking for permission on ordinary steps. The destroy/egress floor still pauses
# for the human, and a grounded safety judge clears the recoverable write middle;
# the model never sees those mechanics — it just acts.
AUTO_MODE_SYSTEM_PROMPT = (
    "Auto mode: you are running autonomously in a trusted, sandboxed workspace "
    "where it is acceptable to make and fix mistakes. Use mutating tools freely "
    "and within reason — edit and write files, run builds, tests, and ordinary "
    "shell/data commands — to complete the task end to end. Do NOT stop to ask "
    "permission for routine edits or commands; just do the work. Irreversible or "
    "destructive actions (and anything off-task) may still pause for the human, so "
    f"stay on task and prefer reversible steps. {DATA_WRITE_RULES['auto']}"
)


#: Provenances whose refusal already tells the model what to do next. The impact
#: gate names the assets it would break and how to proceed; wrapping that in the
#: generic "don't retry the same action" text would contradict it.
#: ``fence`` is here because a fence's reason IS the sentence the model must
#: read ("I can only save files inside this chat's own folder"), whichever
#: seam raised it — the session's own path fence or a resolver's.
_SELF_EXPLAINING = frozenset({"impact", "unsure", "audit", "fence"})


def denial_message(
    *,
    mode: PermissionMode,
    decided_by: str,
    classifier_reason: str | None = None,
    effect: str | None = None,
) -> str:
    """The MODEL-visible message for a denied tool call — names WHY it was refused so
    the agent course-corrects instead of seeing a bare "rejected".

    ``decided_by`` is the resolver's provenance (``mode|rule|floor|judge|human|
    fail_closed`` + the coarse-path kinds). ``classifier_reason`` is the action
    classifier's own description (e.g. "writes a file via redirect"); ``effect`` is the
    action's effect name when known. A ``human`` reject ends the turn anyway (the
    runtime cancels on it), so its text is a short hand-back; a self-explaining
    provenance's reason passes through verbatim; every other (automatic) denial keeps
    the turn going, so its text tells the model what's blocked and how to proceed
    without simply retrying the same action."""
    if decided_by in _SELF_EXPLAINING and classifier_reason:
        return classifier_reason
    detail = f" Reason: {classifier_reason.strip().rstrip('.')}." if classifier_reason else ""
    label = MODE_LABELS.get(mode, mode)
    acts = f"{effect} actions" if effect else "mutating actions"
    if decided_by == "mode":
        return (
            f"Refused: this chat is in {label} mode, which the user set — it auto-rejects "
            f"{acts}.{detail} Don't retry the same action; do read-only work instead, or "
            "tell the user you need them to switch modes or approve it."
        )
    if decided_by == "rule":
        return (
            f"Refused by a user-defined permission rule.{detail} Don't retry; take a "
            "different approach or ask the user to adjust the rule."
        )
    if decided_by == "floor":
        return (
            "Refused: this is a destructive or data-exfiltrating action that needs explicit "
            f"approval, which wasn't granted.{detail} Don't retry; ask the user."
        )
    if decided_by == "judge":
        return (
            f"Refused by the auto-mode safety check.{detail} Don't retry the same action; "
            "take a safer approach or ask the user."
        )
    if decided_by == "human":
        return (
            "The user declined this specific action. Ask what they'd like instead before retrying."
        )
    if decided_by == "fail_closed":
        return f"Refused: no approver is available to grant this action.{detail} Ask the user."
    # confidence / coarse kind / unknown provenance: still actionable, never silent.
    return (
        f"Refused.{detail} Don't retry the same action; take a different approach or ask the user."
    )


#: How a refusal the model is handed begins: every automatic reason
#: :func:`denial_message` writes, both sentences :func:`refusal_feedback`
#: composes when the decider gives no words of its own, and opencode's own
#: words for a reject it ended the turn on.
_DENIAL_OPENINGS: tuple[str, ...] = (
    "Refused",
    NOT_GRANTED,
    POLICY_REFUSED,
    "The user declined this specific action",
    "The user rejected permission",
)


def is_denial_message(text: str | None) -> bool:
    """Whether a failed call's text is a permission refusal rather than the tool's
    own failure — the call never ran. A self-explaining provenance (a fence, an
    impact gate) hands over its reason verbatim and is not recognised here; a
    surface that saw the decision marks those itself."""
    if not text:
        return False
    stripped = text.lstrip()
    return any(
        stripped.startswith(opening)
        and (len(stripped) == len(opening) or not stripped[len(opening)].isalnum())
        for opening in _DENIAL_OPENINGS
    )


#: One concise sentence per mode describing what it DOES — the model-facing rules.
#: Reused by ``mode_system_prompt`` (the per-turn steer for the simple modes) AND by
#: ``mode_switch_reminder`` (the one-shot "you were just switched" notice), so the two
#: never drift. ``plan``/``auto`` carry their own richer per-turn prompts above; this is
#: the short form used in the switch notice for every mode. Every entry quotes the
#: mode's ``DATA_WRITE_RULES`` sentence, so a write to connected data is described to
#: the model by the same words the gate acts on — and a switch from read-only to default
#: mid-chat tells it that data writes are now asked, not refused.
_MODE_DESCRIPTION: dict[PermissionMode, str] = {
    "read_only": (
        "Reads run freely. The one place you may write is this chat's own folder (your "
        "working directory): a file written or edited there with the file tools needs no "
        "approval. A workspace's shared folder, which other chats also write, is not this "
        "chat's own: if a write in your working directory is refused, it is one, and this "
        "mode writes nothing there. Every edit outside this chat's folder, every shell "
        "command (the shell does not run in "
        "this mode, so no `>`/`tee` either; use the file tools), and every write to a "
        "connected data system is refused this session; don't retry a refused one. "
        f"{DATA_WRITE_RULES['read_only']} {KNOWLEDGE_IS_MEMORY}"
    ),
    "default": (
        "Read-only actions run freely, and so does a file written or edited in your working "
        "directory. Every other edit, write, or command pauses for the user's approval before "
        f"it runs. {DATA_WRITE_RULES['default']} {KNOWLEDGE_IS_MEMORY}"
    ),
    "auto": (
        "You can edit, write, and run ordinary commands autonomously; only destructive "
        f"or data-exfiltrating actions pause for the user. {DATA_WRITE_RULES['auto']}"
    ),
    "plan": (
        "Read-only this session — explore and propose a plan. Edits to the project and "
        "mutating commands are refused; the one FILE you may write is plan.md in your "
        f"per-chat sandbox dir, drafted before you present it for approval. "
        f"{DATA_WRITE_RULES['plan']} {KNOWLEDGE_IS_MEMORY}"
    ),
    "bypass": (
        "All permission prompts are off — every action, including destructive ones, "
        f"runs immediately without asking. {DATA_WRITE_RULES['bypass']}"
    ),
}


def mode_switch_reminder(old: PermissionMode, new: PermissionMode) -> str:
    """The one-shot ``<system-reminder>`` text announcing that the USER just changed the
    permission mode mid-conversation, with an explanation of the new mode so the model
    adapts its plan immediately (rather than only learning at the next denial). Injected
    once on the first turn after a switch via the same hidden synthetic channel as the
    per-turn steering — so it never renders in any chat UI."""
    return (
        f"The user just switched the permission mode from {MODE_LABELS.get(old, old)} to "
        f"{MODE_LABELS.get(new, new)}. {_MODE_DESCRIPTION[new]} Adjust your approach to "
        "the new mode."
    )


def mode_change_note(old: PermissionMode, new: PermissionMode) -> str:
    """The reason on the record of an ask a mode change had decided again. It
    says only what happened -- the ask was raised under ``old`` and the mode
    moved to ``new`` while it waited -- because who then decided it (the stance,
    or the person it was still put to) is the record's own ``decided_by``."""
    return (
        f"asked in {MODE_LABELS.get(old, old)} mode; the permission mode changed to "
        f"{MODE_LABELS.get(new, new)} while it waited"
    )


def mode_system_prompt(
    mode: PermissionMode, *, sandbox_dir: str | None = None, subagents: bool = True
) -> str | None:
    """The per-turn ``<system-reminder>`` steering for a mode. ``plan``/``auto`` carry
    their own richer prompts; ``read_only``/``default``/``bypass`` restate their rules
    so the model always knows its live mode (robust across context compaction).

    ``sandbox_dir`` (the per-chat scratch dir) is appended to the plan-mode steering so
    the model knows the absolute path to write ``plan.md`` to (the steering tells it to
    write the plan there, then present it by path).

    ``subagents`` False drops the steering's lean-on-Explore paragraph. Plan mode
    is where that instruction is most insistent — it tells the model it ALWAYS
    has ``spawn_agent`` and must never work inline — so a session with no spawn
    tool would spend the turn trying to follow it."""
    if mode == "plan":
        plan = PLAN_MODE_SYSTEM_PROMPT if subagents else PLAN_MODE_SYSTEM_PROMPT_NO_SUBAGENTS
        if sandbox_dir:
            return (
                f"{plan}\n\nYour sandbox directory is `{sandbox_dir}` — "
                f"write your plan there as `{sandbox_dir}/plan.md`, then pass that path to "
                "the plan tool."
            )
        return plan
    if mode == "auto":
        return AUTO_MODE_SYSTEM_PROMPT
    return f"You are in {MODE_LABELS[mode]} mode, set by the user. {_MODE_DESCRIPTION[mode]}"


def mode_auto_decision(
    mode: PermissionMode, canonical_kind: CanonicalPermissionKind
) -> AutoDecision:
    """Given the active mode + the canonical kind of permission being
    requested, decide whether to auto-allow, auto-reject, or prompt the
    user. Callers MUST pass ``PermissionRequest.canonical_kind``, never
    the adapter-native ``permission_kind`` string — that's what makes
    this policy portable across harnesses."""
    if mode == "bypass":
        return "allow"
    if mode == "read_only":
        # Analyst mode: a permission ask only fires for a mutation (reads run
        # freely), so anything that reaches here is rejected.
        return "reject"
    if mode == "auto":
        # Fallback only: when the request carries no ``subject`` ActionDescriptor
        # (legacy/Fake events) auto can't see effect, so allow the recoverable
        # middle here. The real grounded path (effect-aware, judge-gated) runs in
        # the runtime permission loop via ``request_auto_decision``; the floor is
        # enforced deterministically at the classifier + connector chokepoint.
        return "allow"
    if mode == "plan":
        # The planning agent should already deny edits server-side; this
        # is a defensive guard in case an edit ask still reaches us.
        return "reject" if canonical_kind in EDIT_PERMISSION_KINDS else "prompt"
    # default
    return "prompt"


def mode_to_agent(mode: PermissionMode) -> str | None:
    """Map a mode to the harness agent that should run the turn, or
    ``None`` to use the harness default. Only ``plan`` needs a special
    agent (a read-only planner)."""
    if mode == "plan":
        return "plan"
    return None


def parse_mode(raw: str) -> PermissionMode | None:
    """Resolve a user-typed mode name/alias to a canonical mode, or
    ``None`` if unrecognized."""
    return MODE_ALIASES.get(raw.strip().lower())


def next_mode(mode: PermissionMode) -> PermissionMode:
    """Cycle to the next mode along ``CYCLE_MODES`` (Shift+Tab order). A mode not
    on the cycle enters the loop at its head (defensive — every real mode is on
    the cycle today)."""
    try:
        idx = CYCLE_MODES.index(mode)
    except ValueError:
        return CYCLE_MODES[0]
    return CYCLE_MODES[(idx + 1) % len(CYCLE_MODES)]


__all__ = [
    "ALL_MODES",
    "AUTO_MODE_SYSTEM_PROMPT",
    "CYCLE_MODES",
    "DATA_WRITE_RULES",
    "EDIT_PERMISSION_KINDS",
    "KNOWLEDGE_IS_MEMORY",
    "MODES_THAT_DISCARD_AN_APPROVAL",
    "MODES_THAT_HONOUR_AN_APPROVAL",
    "MODE_ALIASES",
    "MODE_LABELS",
    "MODE_MENU_ORDER",
    "NOT_GRANTED",
    "PLAN_ACCEPT_OPTIONS",
    "PLAN_MODE_SYSTEM_PROMPT",
    "POLICY_REFUSED",
    "AutoDecision",
    "PermissionMode",
    "approval_reaches_the_agent",
    "denial_message",
    "is_denial_message",
    "mode_auto_decision",
    "mode_change_note",
    "mode_switch_reminder",
    "mode_system_prompt",
    "mode_to_agent",
    "next_mode",
    "parse_mode",
    "person_decided",
    "plan_label_to_mode",
    "plan_mode_steering",
    "refusal_feedback",
]
