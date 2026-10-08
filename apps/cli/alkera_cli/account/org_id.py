"""The one spelling of an org id.

An org is a team row on the server, keyed by a UUID. Tokens carry the bare hex
form, the API the hyphenated one, and a hand-edited file may carry braces or
upper case, so every key and every comparison goes through
:func:`canonical_org_id`. Anything that is not a UUID is refused rather than
passed through: a value that merely looks like an org id must never key a
partition, a header or a stored sign-in. A caller that would rather read a bad
value as "no org" catches :class:`MalformedOrgIdError` itself, where the reader
can see it.

Standard library only: the box supervisor imports this before anything heavy.
"""

from __future__ import annotations

from uuid import UUID


class MalformedOrgIdError(ValueError):
    """A value offered as an org id that is not a UUID."""

    def __init__(self, value: object) -> None:
        super().__init__(f"not an org id: {value!r}")
        self.value = value


def canonical_org_id(value: object) -> str:
    """``value`` as the hyphenated lowercase UUID string, surrounding space
    ignored. Raises :class:`MalformedOrgIdError` for anything else, the empty
    string included."""
    if isinstance(value, str):
        try:
            return str(UUID(value.strip()))
        except ValueError:
            pass
    raise MalformedOrgIdError(value)


__all__ = ["MalformedOrgIdError", "canonical_org_id"]
