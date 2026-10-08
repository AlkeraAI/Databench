"""Reading a stamp a JSON spec or a document column keeps as text.

Chat specs, workspace specs and the realtime document's turn columns hold
their moments as ISO-8601 strings. Every reader wants the same thing from
one: an aware instant, or nothing when the stamp is missing or does not
parse. The rule lives here, once.
"""

from __future__ import annotations

from datetime import UTC, datetime


def instant(value: object) -> datetime | None:
    """``value`` as an aware instant, or ``None`` for a missing stamp or one
    that does not parse. A stamp with no zone is read as UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


__all__ = ["instant"]
