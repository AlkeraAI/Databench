"""How the box reads the drive's refusal of one write: retry it, leave that
file behind, or end the push."""

from __future__ import annotations

import re

from alkera_cli.files.name_rules import files_own_refusal_code
from alkera_cli.files.wire import conflict_code

__all__ = [
    "dead_session",
    "files_own_refusal",
    "no_room",
    "refusal_text",
    "stale_version",
    "status_of",
]


def status_of(exc: BaseException) -> int | None:
    """The HTTP status behind a refusal, however the client spelled it."""
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return code
    direct = getattr(exc, "status_code", None)
    if isinstance(direct, int):
        return direct
    # The generated SDK raises a plain ``RuntimeError`` whose text carries the
    # status ("… returned 404 — {…}"), so the message is the only place a 404
    # is visible; matching it narrowly beats treating every error as absence.
    found = re.search(r"returned (\d{3})", str(exc))
    return int(found.group(1)) if found else None


def stale_version(exc: BaseException) -> bool:
    """Whether a refused write compared against a version that has moved.

    A 412 is the ``If-Match`` the push just read no longer being the head; a
    409 ``files.exists`` is the name landing between the lookup that found
    nothing and the create. Both are answered by looking again, once — not by
    reading them as the lease being gone.
    """
    status = status_of(exc)
    return status == 412 or (status == 409 and conflict_code(exc) == "files.exists")


def dead_session(exc: BaseException) -> bool:
    """Whether a refused commit names a session that can no longer complete —
    aborted or otherwise past uploading — rather than the bytes or the name."""
    return status_of(exc) == 409 and conflict_code(exc) == "files.session_state"


def files_own_refusal(exc: BaseException) -> bool:
    """Whether the drive refused this one file for a reason of the file's own."""
    status = status_of(exc)
    if status is None or status < 400 or status >= 500:
        return False
    return files_own_refusal_code(conflict_code(exc))


def no_room(exc: BaseException) -> bool:
    """Whether the drive refused this file's bytes for want of room."""
    return status_of(exc) == 507 and conflict_code(exc) == "files.quota_bytes"


def refusal_text(exc: BaseException) -> str:
    code = conflict_code(exc)
    return f"{code}: {exc}" if code else str(exc)
