"""Exactly-once for a retried request: claim a key, do the work, settle it.

A client that may retry a request names it with an idempotency key. The first
request with that key claims it *before* its effect and in the same transaction
as its effect, and records its answer there too: the claim, the effect and the
answer commit together or not at all. A repeat finds the answer and does no
work; a repeat whose request differs from the first is refused, because
replaying an answer to a question nobody asked is the one thing a key must
never do (the IETF "Idempotency-Key HTTP Header Field" draft and Stripe agree
on this).

The race is settled by Postgres, not by a check-then-write. The first request
INSERTs the claim; a concurrent repeat's INSERT blocks on the primary key until
the first transaction ends, then either fails with a unique violation (the
first committed: read its stored answer) or succeeds (the first rolled back:
its effect never happened, so this request owns the key). A repeat after
completion takes the same path. There is no window in which two callers both
believe they are first.

A key is owned by an org and a principal and is scoped to a ``route``: the
same key from another principal, or on another route, is another request.

Every key is claimed under a registered :class:`IdempotencyScope`, which names
the domain that owns it and how long its answer is kept. A scope is registered
once, by the module that claims under it (:func:`register_scope`). The row
carries its own ``expires_at``, so the janitor that prunes the table needs no
registry of its own: a worker that never imports the domain still drops each
row on the day its domain chose.

The rows live in ``file_idempotency_keys``, the table Files has always kept
its replay records in. It is a Files tenant table under row-level security, so
a caller in a Files transaction (running as the restricted Files role) reads
and writes only its own org's rows; a platform caller's runtime login bypasses
the policy, and every statement here carries the org predicate explicitly
anyway.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final, cast

from sqlalchemy import Table, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.models.files.ops import FileIdempotencyKey

_KEYS: Final = cast(Table, FileIdempotencyKey.__table__)

#: Names a principal deterministically when its id is not itself a UUID (an
#: agent's chat session id), so the principal column stays a UUID for every kind.
_PRINCIPAL_NAMESPACE: Final = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


#: The longest a scope may keep its answers. A key is a retry guard, not a
#: ledger: the durable record of what happened belongs to the domain's own rows.
MAX_RETENTION: Final = timedelta(days=30)

#: The widest scope name the ``scope`` column holds.
MAX_SCOPE_NAME: Final = 64


@dataclass(frozen=True, slots=True)
class IdempotencyScope:
    """A domain that claims keys, and how long it keeps their answers."""

    name: str
    retention: timedelta


_SCOPES: dict[str, IdempotencyScope] = {}


def register_scope(name: str, *, retention: timedelta) -> IdempotencyScope:
    """Register the scope ``name`` with its retention and answer it.

    Registering the same name again with the same retention answers the same
    scope (a module re-imported by a test); a second retention for one name is
    two owners for one fact and raises.
    """
    if not name or len(name) > MAX_SCOPE_NAME:
        raise ValueError(f"an idempotency scope name is 1 to {MAX_SCOPE_NAME} characters")
    if not timedelta(0) < retention <= MAX_RETENTION:
        raise ValueError(f"an idempotency scope keeps its answers for up to {MAX_RETENTION}")
    scope = IdempotencyScope(name=name, retention=retention)
    existing = _SCOPES.setdefault(name, scope)
    if existing != scope:
        raise ValueError(f"the idempotency scope {name!r} is already registered")
    return existing


def registered_scopes() -> Mapping[str, IdempotencyScope]:
    """Every scope registered in this process, by name."""
    return dict(_SCOPES)


def body_digest(document: Mapping[str, Any]) -> bytes:
    """The digest of a JSON request body, independent of key order and spacing.

    For a caller that holds the parsed request rather than its bytes: two
    bodies that mean the same thing digest the same, and any changed value
    digests differently.
    """
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).digest()


def principal_uuid(raw: str) -> uuid.UUID:
    """A principal id as the UUID a key is owned by.

    A user, token or PAT principal id already is a UUID string. An agent's is a
    chat session id, which may not be; it is folded to a UUID5 so two different
    session ids can never collide onto one key.
    """
    try:
        return uuid.UUID(raw)
    except ValueError:
        return uuid.uuid5(_PRINCIPAL_NAMESPACE, raw)


@dataclass(frozen=True, slots=True)
class KeyOwner:
    """Who a key belongs to: the org it was sent in and the principal that sent it."""

    org_id: uuid.UUID
    principal_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class Claim:
    """This request owns the key and must do the work, then :func:`settle` it in
    the same transaction."""

    owner: KeyOwner
    route: str
    key: str


@dataclass(frozen=True, slots=True)
class Replay:
    """The work was already done; ``answer`` is what the first request recorded."""

    answer: dict[str, Any]


class IdempotencyMismatch(Exception):  # noqa: N818 - reads as the refusal it is
    """The key was already used for a different request.

    A caller with an HTTP surface answers it as 409 ``idempotency.mismatch``
    unless its API already promised another answer (Files answers 422
    ``files.idempotency_mismatch``, which its clients key off).
    """

    code: Final = "idempotency.mismatch"
    status: Final = 409
    message: Final = "This idempotency key was already used for a different request."

    def __init__(self, key: str) -> None:
        super().__init__(self.message)
        self.key = key


def _where(owner: KeyOwner, *, route: str, key: str) -> Any:
    return (
        (_KEYS.c.org_team_id == owner.org_id)
        & (_KEYS.c.key == key)
        & (_KEYS.c.principal_id == owner.principal_id)
        & (_KEYS.c.route == route)
    )


async def replay_or_claim(
    session: AsyncSession,
    scope: IdempotencyScope,
    key: str,
    request_digest: bytes,
    *,
    owner: KeyOwner,
    route: str | None = None,
    after_insert: Callable[[], Awaitable[None]] | None = None,
) -> Claim | Replay:
    """Claim ``(owner, route, key)`` inside the caller's transaction, or find
    the answer an earlier request with the same key recorded.

    ``route`` narrows the key within the scope (Files keys each route on its
    own); it defaults to the scope's name.

    Raises :class:`IdempotencyMismatch` when the key was claimed for a request
    whose digest differs from ``request_digest``.

    ``after_insert`` runs between a successful claim and the caller's work; it
    is the one point a test needs to hold a concurrent repeat on the key.
    """
    if _SCOPES.get(scope.name) != scope:
        raise ValueError(f"the idempotency scope {scope.name!r} is not registered")
    route = route if route is not None else scope.name
    digest = request_digest.hex()
    where = _where(owner, route=route, key=key)
    expires_at = func.now() + scope.retention
    # A SAVEPOINT so a unique violation costs this statement, not the
    # transaction: without it Postgres would abort the whole unit of work and
    # the repeat could not go on to read the stored answer.
    savepoint = await session.begin_nested()
    try:
        await session.execute(
            insert(_KEYS).values(
                org_team_id=owner.org_id,
                key=key,
                principal_id=owner.principal_id,
                route=route,
                request_hash=digest,
                status="in_progress",
                scope=scope.name,
                expires_at=expires_at,
            )
        )
    except IntegrityError:
        await savepoint.rollback()
    else:
        await savepoint.commit()
        if after_insert is not None:
            await after_insert()
        return Claim(owner=owner, route=route, key=key)
    # The first request's row lock is held until it commits, so this blocks
    # exactly as long as the work it is repeating takes.
    existing = (
        (await session.execute(select(_KEYS).where(where).with_for_update())).mappings().one()
    )
    if existing["request_hash"] != digest:
        raise IdempotencyMismatch(key)
    if existing["status"] == "succeeded" and existing["body"] is not None:
        return Replay(answer=dict(existing["body"]))
    # The claim is visible but carries no answer: its writer ended without
    # recording one, so its effect never landed and this request owns the key.
    await session.execute(
        update(_KEYS)
        .where(where)
        .values(status="in_progress", body=None, scope=scope.name, expires_at=expires_at)
    )
    return Claim(owner=owner, route=route, key=key)


async def recorded_answer(
    session: AsyncSession,
    scope: IdempotencyScope,
    key: str,
    request_digest: bytes,
    *,
    owner: KeyOwner,
    route: str | None = None,
) -> dict[str, Any] | None:
    """The answer an earlier request with this key and this body recorded, or
    ``None``. Reads only: nothing is claimed or locked. A route reads it to
    decide a repeat as the repeat it is (a purchase that already holds its
    machine asks no quota) before :func:`replay_or_claim` answers it."""
    route = route if route is not None else scope.name
    row = (
        (await session.execute(select(_KEYS).where(_where(owner, route=route, key=key))))
        .mappings()
        .one_or_none()
    )
    if row is None or row["request_hash"] != request_digest.hex():
        return None
    if row["status"] != "succeeded" or row["body"] is None:
        return None
    return dict(row["body"])


async def settle(session: AsyncSession, claim: Claim, answer: Mapping[str, Any]) -> None:
    """Record the answer to a key this request claimed, in the same transaction
    as the work it answers for."""
    await session.execute(
        update(_KEYS)
        .where(_where(claim.owner, route=claim.route, key=claim.key))
        .values(status="succeeded", body=dict(answer))
    )


__all__ = [
    "MAX_RETENTION",
    "Claim",
    "IdempotencyMismatch",
    "IdempotencyScope",
    "KeyOwner",
    "Replay",
    "body_digest",
    "principal_uuid",
    "recorded_answer",
    "register_scope",
    "registered_scopes",
    "replay_or_claim",
    "settle",
]
