"""The ``If-Match`` precondition on a cap write.

Every cap slot is read with a ``version``; a client that edits it sends that
version back in ``If-Match``, and a write built on a reading that is no longer
true is refused with a 409 rather than landing on top of someone else's. The
refusal names the slot so the screen can say what happened; the client's
mutation policy refreshes the reading the write was built on. A write with no
``If-Match`` is unconditional — an API caller may mean "set it to this
whatever it was" — and ``*`` says the same explicitly.
"""

from __future__ import annotations

from typing import Annotated

from alkera_core.cap_versions import version_matches
from fastapi import Header, HTTPException, status

#: The header as a route declares it, so it is on the API surface a client reads.
IfMatch = Annotated[
    str | None,
    Header(
        alias="If-Match",
        description=(
            "The slot's version as the caller last read it. A write whose version "
            "no longer matches is refused with a 409; omit it, or send *, to write "
            "unconditionally."
        ),
    ),
]

STALE_WRITE_CODE = "stale_write"


def require_current(expected: str | None, current: str, *, what: str) -> None:
    """Refuse the write when ``expected`` (the caller's ``If-Match``) no longer
    names the slot's ``current`` version."""
    if version_matches(expected, current):
        return
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": STALE_WRITE_CODE,
            "message": (
                f"This {what} changed since you loaded it. "
                "The current value is shown; edit it again to replace it."
            ),
            "version": current,
        },
    )


__all__ = ["STALE_WRITE_CODE", "IfMatch", "require_current"]
