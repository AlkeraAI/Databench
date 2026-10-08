"""The permission-mode vocabulary every surface offers, in the order it offers it.

One list, read by the web composer's mode menu (through the generated
``@alkera/chat-model`` copy), the Slack card's "Change mode" menu and the
``/alkera mode`` command. A stance added here reaches all three; a label changed
here reads the same everywhere. ``CloudPermissionMode`` is the set the server
accepts, and ``packages/api-core/tests/permission_presentation`` pins the two
to each other.

Who may pick a mode is not a property of the mode: on the web it is whoever may
speak in the chat (the ``SEND`` gate), and in Slack only the thread's owner, a
subset of those. No stance is offered to one reader and withheld from another,
so the menu is the whole list on every surface.
"""

from __future__ import annotations

from dataclasses import dataclass

from alkera_core.schemas.objects.specs import CloudPermissionMode


@dataclass(frozen=True, slots=True)
class PermissionModeSpec:
    """One stance as a reader meets it."""

    value: CloudPermissionMode
    label: str
    description: str
    #: Whether the agent can run code or change files outside this chat. A chat
    #: in a writing stance is placed on the shared pool only on a sandboxed
    #: (gVisor) machine.
    writes: bool
    #: How permissive the stance is, lowest first. Only the order matters: it is
    #: what lets a template narrow the chat it starts without widening it.
    rank: int
    #: The compact label a narrow trigger shows, when the full one does not fit.
    short: str | None = None
    #: Other words a person may type for it (``/alkera mode ask``).
    aliases: tuple[str, ...] = ()


PERMISSION_MODES: tuple[PermissionModeSpec, ...] = (
    PermissionModeSpec(
        value="default",
        label="Default",
        description="Runs reads freely; asks before changes outside the chat's files.",
        writes=True,
        rank=2,
        aliases=("ask",),
    ),
    PermissionModeSpec(
        value="plan",
        label="Plan",
        description="Explores, writes only in the chat's files, and proposes a plan.",
        writes=False,
        rank=1,
    ),
    PermissionModeSpec(
        value="auto",
        label="Auto",
        description="Works on its own and pauses only for risky or destructive steps.",
        writes=True,
        rank=3,
    ),
    PermissionModeSpec(
        value="read_only",
        label="Read-only",
        description="Reads freely, writes only in this chat, and runs no shell.",
        writes=False,
        rank=0,
        aliases=("read-only", "readonly"),
    ),
    PermissionModeSpec(
        value="bypass",
        label="Bypass permissions",
        description="Runs everything without asking.",
        writes=True,
        rank=4,
        short="Bypass",
    ),
)

_BY_VALUE: dict[str, PermissionModeSpec] = {mode.value: mode for mode in PERMISSION_MODES}

#: The stances in which the agent can run code or change files outside the chat.
WRITING_MODES: frozenset[CloudPermissionMode] = frozenset(
    mode.value for mode in PERMISSION_MODES if mode.writes
)

#: Each stance's permissiveness, lowest first.
MODE_RANK: dict[CloudPermissionMode, int] = {mode.value: mode.rank for mode in PERMISSION_MODES}


#: What a permission card says in place of Allow, by the stances in which the
#: box discards a relayed approval: ``read_only`` refuses every write before
#: anyone is asked and ``plan`` only explores. The other stances act on an
#: approval. The box's own derivation (``MODES_THAT_DISCARD_AN_APPROVAL``) is
#: pinned to these keys, and the server hands the verdict to every reader on
#: the chat read, so no surface keeps a list of its own.
APPROVAL_REFUSALS: dict[CloudPermissionMode, str] = {
    "read_only": "This workspace is read-only, so it won't run this.",
    "plan": "Plan mode explores and proposes a plan, so it won't run this.",
}


def approval_refusal(mode: str | None) -> str | None:
    """Why an approval in a chat in ``mode`` would be discarded, or ``None``
    when the box acts on it. A word that is not a stance is the read-only
    floor: an approval nobody can account for is never offered."""
    spec = _BY_VALUE.get(mode or "")
    if spec is None:
        return APPROVAL_REFUSALS["read_only"]
    return APPROVAL_REFUSALS.get(spec.value)


def mode_spec(value: str) -> PermissionModeSpec | None:
    return _BY_VALUE.get(value)


def mode_words() -> dict[str, CloudPermissionMode]:
    """Every word that names a stance -- its value and its aliases -- lowered."""
    words: dict[str, CloudPermissionMode] = {}
    for mode in PERMISSION_MODES:
        words[mode.value] = mode.value
        for alias in mode.aliases:
            words[alias] = mode.value
    return words


__all__ = [
    "APPROVAL_REFUSALS",
    "MODE_RANK",
    "PERMISSION_MODES",
    "WRITING_MODES",
    "PermissionModeSpec",
    "approval_refusal",
    "mode_spec",
    "mode_words",
]
