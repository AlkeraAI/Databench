"""The one typed error set every object-store driver normalizes onto.

Callers above the store never see a driver's native exception: a driver that
cannot map a failure onto one of these raises :class:`Unavailable`.
"""

from __future__ import annotations


class StoreError(Exception):
    """Base class for every object-store failure."""


class PreconditionFailed(StoreError):  # noqa: N818 - the shared error-set names drivers normalize onto
    """A conditional write lost, or a multipart part list did not match."""


class NotFound(StoreError):  # noqa: N818 - the shared error-set names drivers normalize onto
    """The key does not exist."""


class NoSuchBucket(NotFound):
    """The bucket itself does not exist — a misconfiguration, not a missing key.

    A subclass of :class:`NotFound` so every caller that already treats "no such
    object" as an absence keeps its behaviour, and a distinct type so the code
    that must tell "the store answered, there is nothing there" apart from "this
    deployment is pointed at a bucket that was never created" can.
    """


class Throttled(StoreError):  # noqa: N818 - the shared error-set names drivers normalize onto
    """The store asked us to slow down."""

    def __init__(self, message: str = "throttled", *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after: float | None = retry_after


class Unavailable(StoreError):  # noqa: N818 - the shared error-set names drivers normalize onto
    """The store is unreachable or answered with an unmapped server fault."""


class ChecksumMismatch(StoreError):  # noqa: N818 - the shared error-set names drivers normalize onto
    """The bytes do not hash to the checksum the caller declared."""


class InvalidRequest(StoreError):  # noqa: N818 - the shared error-set names drivers normalize onto
    """The call itself is malformed: the caller asked for something incoherent.

    A sibling of :class:`InvalidKey` — both say the request was wrong before
    the store was ever consulted — for the arguments that are not keys: a
    stream that does not match its declared size, a completed multipart upload
    with no whole-object checksum.
    """


class InvalidKey(InvalidRequest):
    """The key is not a portable, domain-relative store key."""


class ExpiredCredentials(StoreError):  # noqa: N818 - the shared error-set names drivers normalize onto
    """The vended session credential expired mid-request.

    A driver raises it for the ``ExpiredToken`` / ``ExpiredTokenException`` /
    ``InvalidToken`` class of store errors; a scoped factory answers by
    re-vending once and retrying a *non-streaming* call exactly once.
    """


class AccessDenied(StoreError):  # noqa: N818 - the shared error-set names drivers normalize onto
    """The role or key could not be assumed, or addressed a refused prefix."""
