"""The one safe-slug rule for names that become filesystem path components.

A connection handle (and a plugin id) ends up as a path component of a
credential directory on every member machine, so it must be a safe slug:
starts alphanumeric, then only ``[A-Za-z0-9._-]``. The backend save boundary,
the CLI's local add/rename paths, and the member sync lane all enforce THIS
constant — a name that saves must be a name that reaches members, never one
their machines quarantine.
"""

from __future__ import annotations

import re

#: Anchored on ``\Z``, not ``$``: ``$`` also matches just before a trailing
#: newline, so the rule would admit a character its own definition excludes and
#: the name that saves would not be the name every other surface validates.
SAFE_HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def is_safe_handle(value: str) -> bool:
    return bool(SAFE_HANDLE_RE.match(value))


def handle_error(value: str) -> str:
    """The user-facing rejection for an unsafe handle — one message everywhere."""
    return (
        f"invalid connection name {value!r}: use letters, digits, '.', '_', '-' "
        "(no spaces or path separators)"
    )


__all__ = ["SAFE_HANDLE_RE", "handle_error", "is_safe_handle"]
