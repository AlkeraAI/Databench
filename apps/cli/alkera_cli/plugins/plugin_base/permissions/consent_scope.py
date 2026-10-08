"""How far a person's "Always allow" on a shell command reaches.

A standing allow is recorded as Alkera's own rule (``add_local_rule``). For most
commands the rule names the command's FAMILY — the classifier's operation, such
as ``ls`` or ``git_push`` — so "Always allow" on ``ls -la`` also covers
``ls src``. For a command whose effect is destructive or irreversible that is
far more than was consented to: one "yes" to ``rm -rf build/`` would read as a
yes to every ``rm`` there will ever be. Those commands are remembered as the
exact command line the person saw, and a different line raises a fresh card.

Which commands are held to their exact text is the data table
:data:`EXACT_CONSENT` below, plus every shell command the classifier itself
rates ``destroy`` (the floor), so a destructive verb the table does not name is
still never widened to its family. A row names a classifier operation (an
``fnmatch`` glob over :attr:`ActionDescriptor.operation`: ``rm``,
``git_push_force``, ``mkfs*``) and, optionally, the flags that make it
destructive (``chmod`` only with ``-R``). Adding a row is the whole change:
every place that offers, records or reads a standing allow asks
:func:`standing_allow_scope`, which reads the table.

An exact grant is only recorded for a command whose text fixes what it does. A
command that expands at run time — a variable, a substitution, a glob — does
something different each time it runs, so its text is not the consent, and no
standing allow is offered or written for it.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Literal

from alkera_cli.contracts.tool_types import ActionDescriptor, Effect

StandingScope = Literal["family", "exact"]

#: The card label for a standing allow that remembers one command line.
EXACT_ALWAYS_LABEL = "Always allow this exact command"


@dataclass(frozen=True, slots=True)
class ExactConsent:
    """One row of :data:`EXACT_CONSENT`.

    ``operation`` is an ``fnmatch`` glob over the classifier's operation name.
    ``any_flag`` empty means the operation is held to its exact text with any
    arguments; otherwise only when one of these flags is present (a short flag
    also matches inside a bundle, so ``-R`` catches ``-Rf``; a long flag also
    matches its ``--flag=value`` form)."""

    operation: str
    any_flag: tuple[str, ...] = ()


#: Commands whose "Always allow" remembers the exact command line. Pure data:
#: add a row to hold another command to its exact text.
EXACT_CONSENT: tuple[ExactConsent, ...] = (
    ExactConsent("rm"),
    ExactConsent("rmdir"),
    ExactConsent("unlink"),
    ExactConsent("shred"),
    ExactConsent("srm"),
    ExactConsent("wipe"),
    ExactConsent("truncate"),
    ExactConsent("dd"),
    ExactConsent("mkfs*"),
    ExactConsent("mkswap"),
    ExactConsent("wipefs"),
    ExactConsent("blkdiscard"),
    ExactConsent("fdisk"),
    ExactConsent("parted"),
    ExactConsent("git_push_force"),
    ExactConsent("git_reset_hard"),
    ExactConsent("git_clean"),
    ExactConsent("git_restore"),
    ExactConsent("git_reflog_expire"),
    ExactConsent("git_filter_branch"),
    ExactConsent("git_gc_prune"),
    ExactConsent("git_stash_drop"),
    ExactConsent("git_update_ref_delete"),
    ExactConsent("git_branch_delete", ("-D", "--force", "-f")),
    ExactConsent("find_delete"),
    ExactConsent("find_exec"),
    ExactConsent("chmod", ("-R", "--recursive")),
    ExactConsent("chown", ("-R", "--recursive")),
    ExactConsent("chgrp", ("-R", "--recursive")),
    ExactConsent("kill", ("-9", "-KILL", "-SIGKILL", "-s", "--signal")),
    ExactConsent("pkill", ("-9", "-KILL", "-SIGKILL", "--signal")),
    ExactConsent("killall", ("-9", "-KILL", "-SIGKILL", "-s", "--signal")),
    ExactConsent("mv", ("-f", "--force")),
    ExactConsent("cp", ("-f", "--force")),
    ExactConsent("ln", ("-f", "--force")),
    # Any other command told to force its way through.
    ExactConsent("*", ("--force",)),
)

#: Characters that make a command's text say less than what it will do: a
#: variable or substitution (``$``, a backtick) and a glob (``*``, ``?``, ``[``).
_EXPANDS: frozenset[str] = frozenset("$`*?[")


def normalize_command(raw: str) -> str:
    """``raw`` with each run of spaces and tabs OUTSIDE quotes collapsed to one
    space and the ends trimmed. Quoted text and escaped characters are kept as
    written, and a newline is kept, since it separates commands: two spellings
    this maps to one string are the same command to the shell."""
    out: list[str] = []
    quote: str | None = None
    escaped = False
    pending_space = False
    for ch in raw.strip():
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if quote is None and ch in " \t":
            pending_space = True
            continue
        if pending_space:
            out.append(" ")
            pending_space = False
        out.append(ch)
        if ch == "\\" and quote != "'":
            escaped = True
        elif quote is None and ch in "'\"":
            quote = ch
        elif ch == quote:
            quote = None
    return "".join(out)


def _flag_present(tokens: Sequence[str], flag: str) -> bool:
    for token in tokens:
        if token == flag:
            return True
        if flag.startswith("--"):
            if token.startswith(flag + "="):
                return True
        elif (
            len(flag) == 2
            and flag[1].isalpha()
            and token.startswith("-")
            and not token.startswith("--")
            and flag[1] in token[1:]
        ):
            return True
    return False


def _row_matches(row: ExactConsent, operation: str, tokens: Sequence[str] | None) -> bool:
    if not fnmatchcase(operation, row.operation):
        return False
    if not row.any_flag:
        return True
    if tokens is None:
        # Arguments that cannot be read cannot rule the flag out: held exact.
        return True
    return any(_flag_present(tokens, flag) for flag in row.any_flag)


def needs_exact_consent(
    descriptor: ActionDescriptor, table: Sequence[ExactConsent] | None = None
) -> bool:
    """Whether an "Always allow" on this shell command may only remember its
    exact text: the classifier rates it ``destroy``, or a row of ``table``
    names it."""
    if descriptor.capability != "shell":
        return False
    if descriptor.effect == Effect.DESTROY:
        return True
    raw = descriptor.raw or ""
    try:
        tokens: list[str] | None = shlex.split(raw)
    except ValueError:
        tokens = None
    operation = descriptor.operation or ""
    rows = EXACT_CONSENT if table is None else table
    return any(_row_matches(row, operation, tokens) for row in rows)


def text_fixes_effect(raw: str | None) -> bool:
    """Whether ``raw`` does the same thing every time it runs: it names no
    variable, substitution or glob."""
    return bool(raw and raw.strip()) and not any(ch in _EXPANDS for ch in raw or "")


def standing_allow_scope(
    descriptor: ActionDescriptor | None, table: Sequence[ExactConsent] | None = None
) -> StandingScope | None:
    """What an "Always allow" on this action records, or ``None`` when it
    records nothing (the answer then holds for this one call).

    ``exact``: the command line itself — a destructive or table-listed shell
    command whose text fixes its effect, or a compound command (it has no
    family). ``family``: the ``(capability, operation)`` rule — any other
    classified action off the floor. ``None``: an action nothing could classify,
    any other floor action, or a destructive command that expands at run time."""
    if descriptor is None or not descriptor.capability:
        return None
    if descriptor.confidence == "unknown":
        return None
    if descriptor.capability == "shell" and needs_exact_consent(descriptor, table):
        # The floor stays whole for exfiltration and a program on the data host;
        # only a destructive or listed command is waived by its exact text.
        if descriptor.effect in (Effect.EGRESS, Effect.EXEC):
            return None
        return "exact" if text_fixes_effect(descriptor.raw) else None
    from alkera_cli.plugins.plugin_base.permissions.policy import is_floor

    if is_floor(descriptor):
        return None
    if descriptor.capability == "shell" and descriptor.scope == "command":
        return "exact"
    return "family"


__all__ = [
    "EXACT_ALWAYS_LABEL",
    "EXACT_CONSENT",
    "ExactConsent",
    "StandingScope",
    "needs_exact_consent",
    "normalize_command",
    "standing_allow_scope",
    "text_fixes_effect",
]
