"""Exactly-once for non-GET Files requests.

Every non-GET carries an ``Idempotency-Key`` scoped to ``(org, key, principal,
route)`` — the primary key of ``file_idempotency_keys``. The record is claimed
*before* the effect and in the same transaction as the effect, which is what
makes a replay free of a second effect: the claim and the work commit together
or neither does.

The claim itself, and how a race between two first requests is settled, is
the shared :mod:`alkera_core.idempotency`; this module is the Files spelling of
it: the key owned by the org and the acting principal, the answer a whole HTTP
response.

The stored answer is returned byte-for-byte, so a client that retries a
`create` gets the very bytes it would have got, including the id it must
already have written down. A different body under the same key is a 422 rather
than a silent replay of an answer to a question nobody asked; a missing key on
a non-GET is a 428 the caller raises before ever getting here.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final

from alkera_core.authz.principal import ActingContext
from alkera_core.files.checkpoints import Checkpoints, NoopCheckpoints
from alkera_core.files.errors import FilesError, InvalidRequest
from alkera_core.files.repo import FilesRepo
from alkera_core.idempotency import (
    Claim,
    IdempotencyMismatch,
    KeyOwner,
    Replay,
    principal_uuid,
    register_scope,
    replay_or_claim,
    settle,
)

#: How long a record answers replays; the row's ``expires_at`` is this far
#: past its claim, and the janitor prunes on it.
IDEMPOTENCY_RETENTION: Final = timedelta(hours=24)

#: The scope Files claims its keys under.
FILES_SCOPE: Final = register_scope("files", retention=IDEMPOTENCY_RETENTION)

#: The one point a test needs between the claim and the effect: a concurrent
#: replay must already be blocked on the unique index when the first request
#: is still working.
AFTER_INSERT: Final = "idempotency.after_insert"

#: The production default, as a singleton so it is not built per call.
_NO_CHECKPOINTS: Final = NoopCheckpoints()

#: What a Files client is told when a key is reused for a different request.
#: Files answers 422 under its own code, which its clients key off; the shared
#: refusal is translated here rather than changing a promised answer.
MISMATCH_CODE: Final = "files.idempotency_mismatch"


@dataclass(frozen=True, slots=True)
class StoredResponse:
    """What a route answered, kept whole so a replay is byte-identical."""

    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


class MissingIdempotencyKey(FilesError):  # noqa: N818 — reads as `errors`-style catalogue
    """A non-GET arrived with no ``Idempotency-Key``.

    Raised by the caller (the dependency), never from inside :func:`idempotent`
    — by the time we are here a key exists. ``428`` and not ``400`` because the
    request is otherwise valid and becomes acceptable once the header is added.
    """

    status = 428

    def __init__(
        self, code: str = "files.idempotency_key_required", message: str | None = None
    ) -> None:
        super().__init__(code, message)


def principal_column(ctx: ActingContext) -> uuid.UUID:
    """The acting principal as the UUID the key is scoped to."""
    return principal_uuid(ctx.acting_principal.id)


def _owner(repo: FilesRepo, ctx: ActingContext) -> KeyOwner:
    return KeyOwner(org_id=repo.scope.org_team_id, principal_id=principal_column(ctx))


def _encode(response: StoredResponse) -> dict[str, Any]:
    """The response as JSONB. The body is base64 so arbitrary bytes survive."""
    return {
        "status": response.status,
        "body": base64.b64encode(response.body).decode("ascii"),
        "headers": dict(response.headers),
    }


def _decode(stored: dict[str, Any]) -> StoredResponse:
    return StoredResponse(
        status=int(stored["status"]),
        body=base64.b64decode(stored["body"]),
        headers=dict(stored.get("headers") or {}),
    )


async def claim_key(
    repo: FilesRepo,
    ctx: ActingContext,
    *,
    route: str,
    key: str,
    request_hash: bytes,
    checkpoints: Checkpoints = _NO_CHECKPOINTS,
) -> StoredResponse | None:
    """Claim ``(org, key, principal, route)`` inside the caller's transaction.

    ``None`` means this request owns the key and must do the work, then
    :func:`settle_key` it in the same transaction. A stored answer means the work
    was already done: the caller answers with it and does nothing. The same key
    with a different ``request_hash`` raises :class:`InvalidRequest`.

    Public for a caller that is already inside its own unit of work and must
    claim at a particular point in it -- after a fence, say -- rather than
    around it, which is what :func:`idempotent` does.
    """

    async def reached() -> None:
        await checkpoints.reach(AFTER_INSERT)

    try:
        outcome = await replay_or_claim(
            repo.session,
            FILES_SCOPE,
            key,
            request_hash,
            owner=_owner(repo, ctx),
            route=route,
            after_insert=reached,
        )
    except IdempotencyMismatch:
        raise InvalidRequest(
            MISMATCH_CODE, "this idempotency key was used for a different request body"
        ) from None
    if isinstance(outcome, Replay):
        return _decode(outcome.answer)
    return None


async def settle_key(
    repo: FilesRepo,
    ctx: ActingContext,
    *,
    route: str,
    key: str,
    response: StoredResponse,
) -> None:
    """Record the answer to a key this request :func:`claim_key`-ed, in the same
    transaction as the work it answers for."""
    await settle(
        repo.session, Claim(owner=_owner(repo, ctx), route=route, key=key), _encode(response)
    )


async def idempotent(
    repo: FilesRepo,
    ctx: ActingContext,
    *,
    route: str,
    key: str,
    request_hash: bytes,
    run: Callable[[], Awaitable[StoredResponse]],
    checkpoints: Checkpoints = _NO_CHECKPOINTS,
) -> StoredResponse:
    """Run ``run`` at most once for ``(org, key, principal, route)``.

    The claim, the effect and the stored answer are one transaction. A replay
    performs no work and returns the first answer's exact bytes; the same key
    with a different body raises :class:`InvalidRequest`.
    """
    async with repo.transaction():
        stored = await claim_key(
            repo,
            ctx,
            route=route,
            key=key,
            request_hash=request_hash,
            checkpoints=checkpoints,
        )
        if stored is not None:
            return stored
        response = await run()
        await settle_key(repo, ctx, route=route, key=key, response=response)
    return response


__all__ = [
    "AFTER_INSERT",
    "FILES_SCOPE",
    "IDEMPOTENCY_RETENTION",
    "MissingIdempotencyKey",
    "StoredResponse",
    "claim_key",
    "idempotent",
    "principal_column",
    "settle_key",
]
