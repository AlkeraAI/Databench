"""The refusals of the Loro CRDT lane, raised wherever a decision is made (the
store, a document type, a session's write back) and answered in band by the
gateway."""

from __future__ import annotations

from alkera_core.db.errors import contention, sqlstate_of
from alkera_core.schemas.realtime import CrdtSaving


class CrdtError(Exception):
    """A refusal the gateway answers in band. ``code`` is the error code the
    peer is told; ``reason`` a finer detail; ``retry_after_ms`` set on a
    refusal about load rather than content; ``epoch`` the current epoch on a
    ``stale_epoch``, and ``saving`` whether the document is saving then, for
    the ``reload`` that follows."""

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        reason: str | None = None,
        retry_after_ms: int | None = None,
        epoch: int | None = None,
        saving: CrdtSaving | None = None,
    ) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.reason = reason
        self.retry_after_ms = retry_after_ms
        self.epoch = epoch
        self.saving = saving


def busy_for(exc: BaseException) -> CrdtError | None:
    """``crdt_busy`` for a busy database (a held row past ``lock_timeout``, a
    drained pool, a database restarting), with the wait the classifier names,
    or ``None`` when ``exc`` is not contention. Nothing was written, so the
    same frame sent again later is taken. The one mapping of database
    contention onto the lane, for every caller that answers it."""
    busy = contention(exc)
    if busy is None:
        return None
    return CrdtError(
        "crdt_busy",
        busy.message,
        reason=str(busy.code),
        retry_after_ms=busy.retry_after_seconds * 1000,
    )


class CorruptHistoryError(Exception):
    """The stored history does not rebuild into the stored document."""


class SourceGoneError(Exception):
    """A document's source holds nothing a session can edit any more (the
    file was deleted, grew too large, or became binary); ``reason`` says
    which."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SourceLandingError(Exception):
    """The source exists but its bytes have not reached the drive yet (a file
    the machine holding its folder has just written). Nothing is wrong with
    it: a session starts from it once they land."""


class SourceStaleError(Exception):
    """A write back named a source version that is no longer current: the
    source changed outside the session."""


class SourceRefusedError(Exception):
    """The source will not take a write back (a lease that takes no inbound
    write there, a ceiling, the author's access); ``reason`` says which."""

    def __init__(self, reason: str, code: str = "") -> None:
        super().__init__(f"{reason}: {code}" if code else reason)
        self.reason = reason
        self.code = code


__all__ = [
    "CorruptHistoryError",
    "CrdtError",
    "SourceGoneError",
    "SourceLandingError",
    "SourceRefusedError",
    "SourceStaleError",
    "busy_for",
    "safe_error",
]


def safe_error(exc: BaseException) -> str:
    """What a log line may say about ``exc``: its type, and a database error's
    SQLSTATE. Never its text: a database error's carries the failing
    statement's parameters, and on this lane those are people's drafts. A
    lane refusal says its code and reason, which are the lane's own words."""
    name = type(exc).__name__
    if isinstance(exc, CrdtError):
        return f"{name}[{exc.code}:{exc.reason}]" if exc.reason else f"{name}[{exc.code}]"
    state = sqlstate_of(exc)
    return f"{name}[{state}]" if state else name
