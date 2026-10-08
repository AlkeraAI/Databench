"""Conflict names: `report (1).pdf` and `report (conflicted copy from …).pdf`.

One algorithm for every façade, so a name a user sees in the browser, on a mount and
in a materialized workspace is the same name. Pure: no I/O, no clock — the caller
supplies both the sibling predicate and the timestamp.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final

from alkera_core.files.names import NAME_MAX_BYTES, display

__all__ = [
    "MAX_CONFLICT_INDEX",
    "NAME_MAX_BYTES",
    "NoAvailableNameError",
    "conflict_rename",
    "conflicted_copy_name",
    "free_conflicted_copy_name",
]

MAX_CONFLICT_INDEX: Final = 10_000
"""Bound on the enumeration so a `taken` predicate that never yields cannot hang."""

# ` (n)` with n a positive decimal without a leading zero: `(0)` and `(007)` are
# ordinary text a user typed, not markers this module produced.
_MARKER = re.compile(rb"^(?P<base>.*) \((?P<index>[1-9][0-9]*)\)$", re.DOTALL)


class NoAvailableNameError(ValueError):
    """Every candidate up to `MAX_CONFLICT_INDEX` was taken."""


def _split_suffix(name: bytes) -> tuple[bytes, bytes]:
    """Split into (stem, suffix) at the LAST dot, when the stem would be non-empty.

    `report.pdf` → (`report`, `.pdf`); `a.tar.gz` → (`a.tar`, `.gz`), so a conflicted
    `a.tar.gz` becomes `a.tar (1).gz`; `.bashrc` and `report` have no suffix at all.
    """
    stem, dot, extension = name.rpartition(b".")
    if not dot or not stem:
        return name, b""
    return stem, dot + extension


def _decorate(base: bytes, marker: bytes, suffix: bytes) -> bytes:
    """`base + marker + suffix`, truncating the stem — never the marker — to fit."""
    room = NAME_MAX_BYTES - len(marker) - len(suffix)
    if room >= 1:
        return base[:room] + marker + suffix
    # The suffix alone leaves no room for the marker, so it stops being a suffix and
    # is truncated with the rest of the stem. Distinct markers still give distinct
    # names, so the enumeration below still terminates.
    return (base + suffix)[: NAME_MAX_BYTES - len(marker)] + marker


def conflict_rename(name: bytes, taken: Callable[[bytes], bool]) -> bytes:
    """Return the first name in `name`, `base (1)…`, `base (2)…` that is not taken.

    Stable: the answer depends only on `name` and on which names `taken` accepts, not
    on the order siblings were visited. Never longer than 255 bytes, and never a name
    for which `taken` is true.
    """
    if not taken(name):
        return name

    stem, suffix = _split_suffix(name)
    match = _MARKER.match(stem)
    if match is None:
        base, start = stem, 1
    else:
        base, start = match.group("base"), int(match.group("index")) + 1

    for index in range(start, start + MAX_CONFLICT_INDEX):
        candidate = _decorate(base, b" (%d)" % index, suffix)
        if not taken(candidate):
            return candidate

    msg = f"no free conflict name for {name!r} within {MAX_CONFLICT_INDEX} attempts"
    raise NoAvailableNameError(msg)


def _escape_actor(actor: str) -> bytes:
    """Render an actor visibly and injectively for use inside a file name.

    `names.display` is the one escaping table — it renders NUL, every control and
    format code point, bidi overrides and undecodable bytes as visible escapes, and
    escapes the escape character so the mapping stays injective. The separator is the
    one addition: a `/` is legal in a display string but would split the name into
    two path segments, so it is escaped the same way (after `display`, where the only
    remaining `/` characters are literal ones).
    """
    return display(actor.encode("utf-8", "surrogateescape")).replace("/", "\\x2f").encode("utf-8")


def conflicted_copy_name(name: bytes, actor: str, at: datetime) -> bytes:
    """`report (conflicted copy from alice, 2026-09-08 23.11 UTC).pdf`.

    The timestamp is UTC to the minute and says so: whoever reads the name may be in
    any zone, and the machine that names the copy cannot know which. A fixed-width ISO
    date keeps copies sorting by time, and the time has no colons, so the name is legal
    on every façade. A naive `at` is read as UTC.
    """
    moment = at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)
    stamp = moment.strftime("%Y-%m-%d %H.%M UTC").encode("ascii")
    stem, suffix = _split_suffix(name)
    prefix, infix, closer = b" (conflicted copy from ", b", ", b")"
    # Truncate the actor — never the marker's shape — so at least one byte of the
    # stem and the whole suffix survive the 255-byte bound.
    fixed = len(prefix) + len(infix) + len(stamp) + len(closer)
    room = max(NAME_MAX_BYTES - 1 - len(suffix) - fixed, 0)
    marker = prefix + _escape_actor(actor)[:room] + infix + stamp + closer
    return _decorate(stem, marker, suffix)


def free_conflicted_copy_name(
    name: bytes, actor: str, at: datetime, taken: Callable[[bytes], bool]
) -> bytes:
    """The conflicted-copy name for ``name``, made free among ``taken``.

    The first copy in a minute is ``report (conflicted copy from alice …).pdf``;
    a second one in the same minute is ``… (2).pdf``, then ``(3)`` — never
    ``(1)``, because the unnumbered name already is the first. Built on
    :func:`conflict_rename` by counting ``(1)`` as taken, so the enumeration and
    its bounds are that function's.
    """
    copy = conflicted_copy_name(name, actor, at)
    if not taken(copy):
        return copy
    stem, suffix = _split_suffix(copy)
    first = _decorate(stem, b" (1)", suffix)
    return conflict_rename(copy, lambda candidate: candidate == first or taken(candidate))
