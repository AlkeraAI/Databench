"""Which names a push can file, and which refusals belong to one file alone.

The box and the drive apply one naming rule, from one place: the box imports
:func:`alkera_core.files.names.validate`, the function the server refuses a
name with, and adds the one rule the wire imposes (a name that is not UTF-8
cannot ride in a JSON body). A name that breaks either stays on the machine
and is reported; it never stops the rest of the tree.

The drive's answer is honoured per file too, because a box and a server of
different versions can disagree about a rule: a refusal coded as the file's
own is said for that file and the push goes on.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from alkera_core.files import names

from alkera_cli.files.walk import Entry

logger = logging.getLogger("alkera_cli.files.live_sync")

#: Refusals that are about ONE file and answer the same on every retry: the
#: commit's parts disagreeing with the store, a part the session would not
#: take, a session the drive has already closed, a file larger than the
#: deployment accepts (its message names both sizes). Never the fence (the lease
#: is gone for the whole folder), never the quota (the drive is full for every
#: file), never a transport failure — those still end the push.
FILE_REFUSAL_CODES: frozenset[str] = frozenset(
    {
        "files.parts_mismatch",
        "files.size_mismatch",
        "files.empty_part",
        "files.session_state",
        "files.too_large",
        # A path no lease of this holder's covers, while the lease the push
        # names is still live (a nested lease, a workspace's narrowing): that
        # file stays behind, the folder is not lost.
        "files.lease_mismatch",
    }
)

#: Code prefixes that name the file's own name or size as the reason.
FILE_REFUSAL_PREFIXES: tuple[str, ...] = ("files.name_", "files.invalid_name.")


def files_own_refusal_code(code: str | None) -> bool:
    """Whether a 4xx with ``code`` refused one file for a reason of its own."""
    found = code or ""
    return found in FILE_REFUSAL_CODES or found.startswith(FILE_REFUSAL_PREFIXES)


def name_refusal(name: bytes) -> str | None:
    """Why the drive would not file ``name``, or ``None`` when it would."""
    try:
        name.decode("utf-8")
    except UnicodeDecodeError:
        return "not UTF-8"
    try:
        names.validate(name)
    except names.InvalidName as invalid:
        return invalid.code
    return None


def split_unfileable(entries: Sequence[Entry]) -> tuple[list[Entry], list[tuple[bytes, str]]]:
    """``entries`` split into what can be filed and what cannot, with why.

    An entry is refused for its own last segment; everything under a refused
    folder goes with it, since it has nowhere to land.
    """
    refused: list[tuple[bytes, str]] = []
    kept: list[Entry] = []
    for entry in entries:
        if any(entry.relative.startswith(prefix + b"/") for prefix, _ in refused):
            continue
        reason = name_refusal(entry.relative.rsplit(b"/", 1)[-1])
        if reason is None:
            kept.append(entry)
        else:
            refused.append((entry.relative, reason))
    return kept, refused


def path_refusal(relative: str) -> str | None:
    """Why the drive would not file some segment of ``relative`` (a path as
    the watch spells it), or ``None`` when it would file every one."""
    for segment in relative.split("/"):
        reason = name_refusal(segment.encode("utf-8", "surrogateescape"))
        if reason is not None:
            return reason
    return None


class RefusedNames:
    """The live plane's side of the rule: a path with a segment the drive
    would not file is never queued, and is said once.

    The watch spells a name that is not UTF-8 as a surrogate-escaped ``str``,
    which neither the journal nor the JSON wire can carry: queued, it ended
    the whole live sync on the journal's write. Refused here, it stays on the
    machine like any other unfileable name, and everything beside it syncs.
    """

    def __init__(self) -> None:
        self._said: set[str] = set()

    def refused(self, relative: str, *, who: str) -> bool:
        reason = path_refusal(relative)
        if reason is not None:
            if relative not in self._said:
                self._said.add(relative)
                logger.warning(
                    "live sync of %s: %r stays on this machine: the drive cannot file it (%s)",
                    who,
                    relative,
                    reason,
                )
            return True
        return False


__all__ = [
    "FILE_REFUSAL_CODES",
    "FILE_REFUSAL_PREFIXES",
    "RefusedNames",
    "files_own_refusal_code",
    "name_refusal",
    "path_refusal",
    "split_unfileable",
]
