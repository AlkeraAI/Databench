"""Reading an id out of a stored string that may be missing or malformed."""

from __future__ import annotations

from uuid import UUID


def uuid_or_none(value: str | None) -> UUID | None:
    """``value`` as a UUID, or ``None`` when it is empty or not a UUID. For ids a
    stored spec carries as text, where a bad one means "not bound" rather than
    an error to raise."""
    if not value:
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


__all__ = ["uuid_or_none"]
