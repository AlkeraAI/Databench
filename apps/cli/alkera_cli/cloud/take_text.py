"""Why a chat's folder could not be taken, said twice: once for the reader
(the kind of failure, never the deployment's topology) and once for the box's
journal (the host it asked, never a URL, which may carry a signed token)."""

from __future__ import annotations

import re

import httpx
from alkera_sdk.client import AlkeraHTTPError

from alkera_cli.files.wire import conflict_code

_DRIVE_CODE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")


def take_refusal_text(exc: BaseException) -> str:
    """Why a take failed, in the terms a reader of the chat may see.

    The kind of failure and nothing about where: this text is published as the
    chat's refusal and reaches every surface that shows the chat, and a host,
    an IP, a port or a URL is the deployment's topology, not the reader's
    business (a content URL also carries a signed token). A drive that answered
    is told apart from one that could not be reached, and its answer names only
    the status and the drive's own code. The journal line keeps the host: see
    ``take_failure_text``.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        code = conflict_code(exc)
        answered = f"the file store answered {exc.response.status_code}"
        # The code is the drive's own vocabulary (``files.live_pending``); a
        # body that put anything else there is not repeated to the reader.
        return f"{answered} ({code})" if code and _DRIVE_CODE.fullmatch(code) else answered
    if isinstance(exc, AlkeraHTTPError):
        answered = f"the file store answered {exc.status}"
        return (
            f"{answered} ({exc.code})" if exc.code and _DRIVE_CODE.fullmatch(exc.code) else answered
        )
    return "the file store could not be reached from the machine serving this chat"


def take_failure_text(exc: BaseException, *, box: str) -> str:
    """Why a take failed, for the box's journal: the host it asked, never the
    URL (a content URL carries a signed token). Never shown to a reader — the
    chat's refusal is ``take_refusal_text``.

    A host that answered is not a host that could not be reached: a ``409``
    from the drive names its code, and only a failure with no answer at all
    reads as unreachable.
    """
    host = unreachable_host(exc) or "the drive"
    if isinstance(exc, httpx.HTTPStatusError):
        code = conflict_code(exc)
        answered = f"{host} answered {exc.response.status_code}"
        return f"{answered} ({code})" if code else answered
    if isinstance(exc, AlkeraHTTPError):
        answered = f"{host} answered {exc.status}"
        return f"{answered} ({exc.code})" if exc.code else answered
    return f"{host} is not reachable from {box}"


def take_failure_detail(exc: BaseException) -> str:
    """The exception behind a failed take, for the journal: its type, and its
    text only when that text cannot carry a URL. An ``HTTPStatusError`` spells
    the request's URL, and a content read's is the signed one; its status and
    code are already in the line beside this."""
    if isinstance(exc, httpx.HTTPStatusError):
        return type(exc).__name__
    return f"{type(exc).__name__}: {exc}"


def unreachable_host(exc: BaseException) -> str:
    """The host a failed request could not reach, off the request itself.

    The host and never the URL: a Files content URL carries a signed token, and
    a log line is the one place a reader of the box's journal would find it.
    Empty when the failure carries no request (a bare ``OSError`` from a socket
    the chain opened itself).
    """
    url = getattr(getattr(exc, "request", None), "url", None)
    host = getattr(url, "host", "")
    return str(host) if host else ""


__all__ = ["take_failure_detail", "take_failure_text", "take_refusal_text", "unreachable_host"]
