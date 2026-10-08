"""The shape every string identifier in a URL is held to.

An id that is not a UUID — a drive or item id, a machine id, a catalog model id,
a KB item id, an emailed token — used to reach the route as whatever the caller
typed: a NUL, a control character, ten kilobytes. Each one travelled on into a
lookup, a lock key or an audit row, and the database refused it there as a 500.

Held here instead, before the endpoint runs: one to 255 printable characters
(255 is the widest id column the API looks up and the outbox's ``entity_id``),
anything else is the ordinary ``422`` a malformed parameter earns. A route takes
a path identifier as ``PathId`` rather than a bare ``str``; a test walks the
product's app and fails on a bare one.

An id with a fixed shape of its own (a content digest, a minted frame or cell
id) is held to that shape instead, through :func:`strict_path_id`, which admits
only a pattern that bounds the id's length and names its characters.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Final

from fastapi import Path

#: The longest identifier a URL may carry.
MAX_ID_LENGTH: Final = 255

#: No C0 control character and no DEL — NUL among them — anywhere in the id.
ID_PATTERN: Final = r"^[^\x00-\x1f\x7f]+$"

PathId = Annotated[str, Path(min_length=1, max_length=MAX_ID_LENGTH, pattern=ID_PATTERN)]

#: What a fixed-shape pattern may not contain: an unbounded repeat, a wildcard
#: or a negated class, any of which would let a NUL or ten kilobytes through.
_LOOSE: Final = re.compile(r"(?<!\\)[*+]|\{\d*,\}|(?<!\\)\.|\[\^")

_strict: set[str] = set()


def strict_path_id(pattern: str) -> Any:
    """A path parameter held to ``pattern``, an id's own fixed shape.

    The pattern must be anchored at both ends and name every character it
    admits with a bounded repeat, so whatever it matches is short and printable
    by construction. Each one admitted is remembered, so the route walk can
    tell a declared shape from a bare string.
    """
    if not (pattern.startswith("^") and pattern.endswith("$")) or _LOOSE.search(pattern):
        raise ValueError(f"not a fixed id shape: {pattern!r}")
    _strict.add(pattern)
    return Path(pattern=pattern)


def strict_id_patterns() -> frozenset[str]:
    """Every pattern :func:`strict_path_id` has admitted."""
    return frozenset(_strict)


__all__ = ["ID_PATTERN", "MAX_ID_LENGTH", "PathId", "strict_id_patterns", "strict_path_id"]
