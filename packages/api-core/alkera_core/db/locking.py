"""Locks: which rows and names the app serializes on, in what order, how long.

Postgres row locks and advisory locks are the app's mutexes. Each is cheap
until it is hot or held long: a waiter holds a pooled connection while it
waits, so one row every request in an org queues on starves unrelated routes,
and a lock held across a network call is held for as long as somebody else's
server takes to answer. This module is where both kinds are taken, so the
rules live in one place.

**Row locks** (:func:`lock_rows`). Every row lock names a :class:`LockRank`,
and ranks are taken in ascending order only: a transaction that holds a rank
never reaches for a lower one, which is what keeps two transactions from each
holding what the other waits for. The ranks are registered here
(:func:`register_rank`), each with the reason it sits where it does. A rank
marked hot waits a short time rather than the profile's full lock timeout, and
its caller answers a ``55P03`` as a retryable ``503``.

**Advisory locks** (:func:`advisory_key`, :func:`advisory_xact_lock`). An
advisory lock is a mutex on something that is not a row: a count the next
insert must not race ("at most N tokens per org"), a creation that has no row
to lock yet, a job that must not run twice at once. Postgres identifies one by
a 64-bit number (or a pair of 32-bit ones), and two callers serialize only when
they derive the same number. So the derivation lives here, once.
:func:`advisory_key` turns a namespace and its parts into an
:class:`AdvisoryKey`. A namespace this module does not list gets the canonical
derivation: the first eight bytes of SHA-256 over the namespace, a colon and
the parts, read as a signed big-endian integer. The listed namespaces are the
keys that were derived some other way before this module existed. They keep
their old derivation so that a deployment rolling from the old code to this
one still has both halves colliding on the same lock while it rolls; a golden
test pins every one of them. A new caller never adds to that list.

Only transaction-scoped locks are taken here. A session-level lock lives on a
server connection, and a transaction pooler (PgBouncer in transaction mode)
hands that connection to the next borrower between transactions, so a session
lock either leaks or is released on someone else's turn. A caller that needs a
claim longer than one request transaction holds it on a dedicated connection
for as long as it needs (:func:`advisory_claim`), still inside one transaction.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import os
import weakref
import zlib
from collections.abc import AsyncIterator, Callable, Hashable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, ClassVar, Final, Literal, TypeVar, cast
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Executable,
    Integer,
    Result,
    Select,
    bindparam,
    func,
    literal_column,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import Insert
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.sql.expression import ColumnElement

from alkera_core.logging import get_logger
from alkera_core.observability.metrics import record_lock_rule_violation

log = get_logger("alkera.db.locking")

#: One part of an advisory key: an id, a name, or a number.
KeyPart = str | UUID | int

#: How the key's number is computed. ``bigint`` is computed here; the ``sql_*``
#: forms are computed by Postgres from ``text`` (the derivation some keys used
#: before this module); ``pair`` is the two-integer lock space
#: ``(klass, hashtext(text))``, which never collides with a one-integer key.
KeyForm = Literal["bigint", "sql_hashtext", "sql_hashtextextended", "pair"]


@dataclass(frozen=True, slots=True)
class AdvisoryKey:
    """The identity of one advisory lock. Built only by :func:`advisory_key`."""

    namespace: str
    form: KeyForm
    value: int | None = None
    text: str | None = None
    klass: int | None = None

    def arguments(self) -> tuple[ColumnElement[int], ...]:
        """The arguments ``pg_advisory_xact_lock`` and its siblings take."""
        if self.form == "bigint":
            return (bindparam("advisory_key", self.value, type_=BigInteger),)
        if self.form == "sql_hashtext":
            return (func.hashtext(bindparam("advisory_text", self.text)),)
        if self.form == "sql_hashtextextended":
            return (func.hashtextextended(bindparam("advisory_text", self.text), 0),)
        return (
            bindparam("advisory_class", self.klass, type_=Integer),
            func.hashtext(bindparam("advisory_text", self.text)),
        )


def _signed(digest: bytes) -> int:
    return int.from_bytes(digest[:8], "big", signed=True)


def _encode(parts: Sequence[KeyPart]) -> bytes:
    """The parts as bytes, concatenated with no separator.

    A UUID is its 16 bytes and an int its 8 signed big-endian bytes, so both
    are fixed width; a string is its UTF-8 and has no fixed width, which is why
    only the last part may be one. That keeps the concatenation unambiguous:
    ``("ab", "c")`` and ``("a", "bc")`` cannot both be spelled.
    """
    encoded = bytearray()
    for index, part in enumerate(parts):
        if isinstance(part, UUID):
            encoded += part.bytes
        elif isinstance(part, bool):
            raise TypeError("an advisory key part cannot be a bool")
        elif isinstance(part, int):
            encoded += part.to_bytes(8, "big", signed=True)
        elif isinstance(part, str):
            if index != len(parts) - 1:
                raise ValueError("only the last part of an advisory key may be a string")
            encoded += part.encode()
        else:
            raise TypeError(f"an advisory key part cannot be a {type(part).__name__}")
    return bytes(encoded)


def _uuid(part: KeyPart) -> UUID:
    if not isinstance(part, UUID):
        raise TypeError(f"this advisory key takes a UUID, not a {type(part).__name__}")
    return part


def _canonical(namespace: str, parts: Sequence[KeyPart]) -> AdvisoryKey:
    digest = hashlib.sha256(namespace.encode() + b":" + _encode(parts)).digest()
    return AdvisoryKey(namespace, "bigint", value=_signed(digest))


def _blake2b(namespace: str, payload: bytes) -> AdvisoryKey:
    digest = hashlib.blake2b(payload, digest_size=8).digest()
    return AdvisoryKey(namespace, "bigint", value=_signed(digest))


def _hashtext(namespace: str, payload: str) -> AdvisoryKey:
    return AdvisoryKey(namespace, "sql_hashtext", text=payload)


def _hashtextextended(namespace: str, payload: str) -> AdvisoryKey:
    return AdvisoryKey(namespace, "sql_hashtextextended", text=payload)


def _pair(namespace: str, klass: int, payload: str) -> AdvisoryKey:
    return AdvisoryKey(namespace, "pair", text=payload, klass=klass)


def _dotted(parts: Sequence[KeyPart]) -> str:
    return ".".join(str(part) for part in parts)


def _joined(parts: Sequence[KeyPart]) -> str:
    return ":".join(str(part) for part in parts)


#: The namespaces derived before this module, each in its old way, so a rolling
#: deployment's old and new halves still take one lock. Frozen: a new namespace
#: takes the canonical derivation and is never added here.
_LEGACY: Final[dict[str, Callable[[str, tuple[KeyPart, ...]], AdvisoryKey]]] = {
    # An org's audit chain: SHA-256 over the org id alone, no namespace.
    "audit-chain": lambda ns, p: AdvisoryKey(
        ns, "bigint", value=_signed(hashlib.sha256(_uuid(p[0]).bytes).digest())
    ),
    "ci-token-mint": lambda ns, p: _blake2b(ns, b"alkera:ci_token_mint:" + _uuid(p[0]).bytes),
    "realtime-doc-creation": lambda ns, p: _blake2b(ns, f"realtime_docs:{_joined(p)}".encode()),
    "compute-session-slot": lambda ns, p: _blake2b(ns, _joined(p).encode()),
    # The worker's job claims: CRC32, positive 31 bits.
    "worker-job": lambda ns, p: AdvisoryKey(
        ns, "bigint", value=zlib.crc32(f"alkera.billing.{p[0]}".encode()) & 0x7FFF_FFFF
    ),
    "billing-member-budget": lambda ns, p: _hashtext(ns, f"alkera.allocations.{p[0]}"),
    "gateway-covered": lambda ns, p: _hashtext(ns, f"alkera.gateway.covered.{p[0]}"),
    "billing-cap-slot": lambda ns, p: _hashtext(ns, "alkera.caps." + _dotted(p)),
    "org-machine-purchase": lambda ns, p: _hashtext(
        ns, f"alkera.org_machine.purchase.{_dotted(p)}"
    ),
    "chat-spare": lambda ns, p: _hashtext(ns, f"chat-spare:{_joined(p)}"),
    "files-drive-create": lambda ns, p: _hashtext(ns, f"files-drive:{p[0]}"),
    "deployment-health": lambda ns, p: _hashtext(ns, "deployment_health"),
    "files-object": lambda ns, p: _hashtextextended(ns, f"files.object:{_joined(p)}"),
    "notebook-kernels": lambda ns, p: _hashtextextended(ns, f"notebook-kernels:{p[0]}"),
    "crdt-doc": lambda ns, p: _hashtextextended(ns, f"crdt:{p[0]}"),
    "workspace-projects": lambda ns, p: _hashtextextended(ns, f"workspace.projects:{_joined(p)}"),
    "compute-admission": lambda ns, p: _pair(ns, 0x4F6D6164, str(p[0])),
    "compute-grant": lambda ns, p: _pair(ns, 0x436D7175, str(p[0])),
    "compute-register": lambda ns, p: _pair(ns, 0x436D7267, str(p[0])),
    "compute-power": lambda ns, p: _pair(ns, 0x50777231, str(p[0])),
    "chat-attach": lambda ns, p: _pair(ns, 0x43417474, str(p[0])),
}


def advisory_key(namespace: str, *parts: KeyPart) -> AdvisoryKey:
    """The advisory lock ``namespace`` takes for ``parts``.

    Two calls with the same namespace and parts name the same lock, and no
    other call does. ``namespace`` says what is being serialized (``team-tree``,
    ``kb-family``); the parts say which one (an org id, an item id).
    """
    if not namespace:
        raise ValueError("an advisory key needs a namespace")
    legacy = _LEGACY.get(namespace)
    if legacy is not None:
        return legacy(namespace, parts)
    return _canonical(namespace, parts)


Executor = AsyncSession | AsyncConnection


# --------------------------------------------------------------------------- #
# Row locks and their order
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True, order=True)
class LockRank:
    """One kind of row lock and its place in the order every transaction takes
    them in: a transaction holding a rank never takes a lower one.

    Built only by :func:`register_rank`. Values are never renumbered; a new
    rank takes a free value between the two it must sit between.
    """

    value: int
    name: str = field(compare=False)
    hot: bool = field(compare=False, default=False)
    reason: str = field(compare=False, default="")

    REFRESH_TOKEN: ClassVar[LockRank]
    USER_PREFERENCES: ClassVar[LockRank]
    ORG_SETTINGS: ClassVar[LockRank]
    WORKSPACE_MOVE: ClassVar[LockRank]
    ORG_MACHINE: ClassVar[LockRank]
    COMPUTE_ADMISSION: ClassVar[LockRank]
    ALLOCATION: ClassVar[LockRank]
    REALTIME_DOC: ClassVar[LockRank]
    WORKSPACE_OBJECT: ClassVar[LockRank]
    PROXY_REQUEST: ClassVar[LockRank]
    CREDIT_BALANCE: ClassVar[LockRank]
    CREDIT_REVERSAL: ClassVar[LockRank]
    CRDT_DOC: ClassVar[LockRank]
    FILES_NAMESPACE: ClassVar[LockRank]
    FILES_DRIVE: ClassVar[LockRank]
    FILES_NODE: ClassVar[LockRank]
    FILES_LEASE: ClassVar[LockRank]
    UPLOAD_SESSION: ClassVar[LockRank]


_RANKS: dict[str, LockRank] = {}

#: How long a hot rank waits for its row before answering ``55P03``.
HOT_LOCK_TIMEOUT_MS: Final = 500


def register_rank(name: str, value: int, *, hot: bool, reason: str) -> LockRank:
    """Declare a rank. Registering the same rank again returns it; a second
    rank with the same name or the same value is refused, because two kinds of
    row sharing a place in the order is an order nobody decided."""
    if not name or not reason:
        raise ValueError("a lock rank needs a name and the reason it sits where it does")
    made = LockRank(value=value, name=name, hot=hot, reason=reason)
    existing = _RANKS.get(name)
    if existing is not None:
        if (existing.value, existing.hot) != (value, hot):
            raise ValueError(f"lock rank {name!r} is already registered as {existing.value}")
        return existing
    for other in _RANKS.values():
        if other.value == value:
            raise ValueError(f"lock rank value {value} is already {other.name!r}")
    _RANKS[name] = made
    return made


def registered_ranks() -> tuple[LockRank, ...]:
    """Every rank, in the order they may be taken."""
    return tuple(sorted(_RANKS.values()))


LockRank.REFRESH_TOKEN = register_rank(
    "refresh_token",
    20,
    hot=False,
    reason="a session's refresh token and its successor, rotated before anything the "
    "session goes on to change",
)
LockRank.USER_PREFERENCES = register_rank(
    "user_preferences",
    30,
    hot=False,
    reason="a person's own settings: the identity row, then the org's, before anything they "
    "configure",
)
LockRank.ORG_SETTINGS = register_rank(
    "org_settings",
    40,
    hot=False,
    reason="an org's or a team's own settings row (SSO, model providers, a connection, an "
    "allowance, a storage override, a member's cap), and the cap slot that stands in for "
    "one before it exists, saved by an admin before anything it governs",
)
LockRank.WORKSPACE_MOVE = register_rank(
    "workspace_move",
    50,
    hot=False,
    reason="a move's steps lock the move first, then the chats and machines it moves",
)
LockRank.ORG_MACHINE = register_rank(
    "org_machine",
    100,
    hot=False,
    reason="the org's intent for a machine is decided before its allocation is touched",
)
LockRank.COMPUTE_ADMISSION = register_rank(
    "compute_admission",
    150,
    hot=False,
    reason="admission counts a grant's machines after the machine row and before any allocation",
)
LockRank.ALLOCATION = register_rank(
    "allocation",
    200,
    hot=False,
    reason="a machine's allocation, taken before the chats bound to it",
)
LockRank.REALTIME_DOC = register_rank(
    "realtime_doc",
    300,
    hot=False,
    reason="a socket's op locks the document before the transcript write locks the chat",
)
LockRank.WORKSPACE_OBJECT = register_rank(
    "workspace_object",
    400,
    hot=False,
    reason="a chat, workspace or notebook row: after its document, before its files",
)
LockRank.PROXY_REQUEST = register_rank(
    "proxy_request",
    450,
    hot=False,
    reason="a gateway request id, admitted and settled under it: before the balance its "
    "hold sits on",
)
LockRank.CREDIT_BALANCE = register_rank(
    "credit_balance",
    460,
    hot=False,
    reason="an account's balance in one credit class: after the machine whose use it pays "
    "for, before the reversal debts it settles",
)
LockRank.CREDIT_REVERSAL = register_rank(
    "credit_reversal",
    470,
    hot=False,
    reason="a refund or dispute's reversal row and the debt it records: after the balance it moves",
)
LockRank.CRDT_DOC = register_rank(
    "crdt_doc",
    520,
    hot=False,
    reason="a live document's row: after the object it belongs to, before the files its "
    "write back lands in",
)
LockRank.FILES_NAMESPACE = register_rank(
    "files_namespace",
    580,
    hot=False,
    reason="a drive's tree shape: shared by a write under a folder, exclusive for a "
    "write that rewrites a subtree's paths (a move, a trash, a restore)",
)
LockRank.FILES_DRIVE = register_rank(
    "files_drive",
    600,
    # Not hot while a byte reservation still holds it for its request: sixty
    # uploads opened at once must queue, not be refused. It becomes hot when
    # reservations are committed apart, through a quota ledger.
    hot=False,
    reason="the org's one drive row: only for the quota decision about the whole drive",
)
LockRank.FILES_NODE = register_rank(
    "files_node",
    700,
    hot=False,
    reason="a lease gate, then the parent, then the node, after any drive lock",
)
LockRank.FILES_LEASE = register_rank(
    "files_lease",
    750,
    hot=False,
    reason="the lease row is the last Files lock: the fence a write checks after its nodes",
)
LockRank.UPLOAD_SESSION = register_rank(
    "upload_session",
    800,
    hot=False,
    reason="an upload session's row, claimed after the nodes it lands under",
)


@dataclass(frozen=True, slots=True)
class _Held:
    rank: LockRank
    transaction: weakref.ref[Any]
    #: Which row of the rank, when the caller named it (:func:`hold_place`).
    what: Hashable | None = None
    #: Where it was taken, recorded only while the checks raise, so a broken
    #: order names both of its halves.
    where: str = ""


#: The ranks this task holds, each with the transaction that holds it. A
#: tuple, replaced rather than mutated, so a child task inherits what its
#: parent held when it started and its own locks never leak back.
_HELD: ContextVar[tuple[_Held, ...]] = ContextVar("alkera_db_locks_held", default=())


def _root_transaction(db: Executor) -> Any:
    if isinstance(db, AsyncSession):
        return db.sync_session.get_transaction()
    return db.sync_connection.get_transaction() if db.sync_connection is not None else None


def held_ranks() -> tuple[LockRank, ...]:
    """The row-lock ranks this task holds now, in the order they were taken.

    A rank is held until the transaction that took it ends; one whose
    transaction has committed or rolled back is forgotten here.
    """
    live = tuple(
        held
        for held in _HELD.get()
        if (transaction := held.transaction()) is not None and transaction.is_active
    )
    if len(live) != len(_HELD.get()):
        _HELD.set(live)
    return tuple(held.rank for held in live)


def _record(db: Executor, rank: LockRank, what: Hashable | None = None) -> None:
    transaction = _root_transaction(db)
    if transaction is None:
        return
    where = _caller() if checks_enforced() else ""
    _HELD.set((*_HELD.get(), _Held(rank, weakref.ref(transaction), what, where)))


def _caller() -> str:
    """The first frame outside this module: the code that asked for the lock."""
    frame = inspect.currentframe()
    while frame is not None and frame.f_code.co_filename == __file__:
        frame = frame.f_back
    if frame is None:
        return ""
    return f"{frame.f_code.co_filename.rsplit('/', 3)[-1]}:{frame.f_lineno} {frame.f_code.co_name}"


def hold_place(db: Executor, rank: LockRank, what: Hashable) -> None:
    """Count ``rank`` as taken for ``what`` in ``db``'s transaction, whether or
    not a row was there to lock.

    For a step of a fixed sequence whose row may not exist yet (a chat's
    document, before anybody has subscribed to it): the transaction has been
    through that step, so going through the sequence again, for this row or
    for another of its kind, is more of an order already taken and not a lower
    rank after a higher one.
    """
    _record(db, rank, what)


def holds_place(db: Executor, rank: LockRank, what: Hashable) -> bool:
    """Whether ``db``'s transaction has been through ``rank`` for ``what``
    (:func:`hold_place`), in this task."""
    transaction = _root_transaction(db)
    if transaction is None:
        return False
    return any(
        held.rank == rank and held.what == what and held.transaction() is transaction
        for held in _HELD.get()
    )


def held_mark() -> object:
    """What this task holds now, to hand back to :func:`savepoint_rolled_back`."""
    return _HELD.get()


def savepoint_rolled_back(mark: object) -> None:
    """Forget every rank taken since ``mark``: the savepoint that took them
    was rolled back, and Postgres let go of its row locks with it. Only for a
    caller that rolled a savepoint back itself; a savepoint that is released
    keeps its locks until the transaction ends."""
    if not isinstance(mark, tuple):
        raise TypeError("a mark comes from held_mark()")
    _HELD.set(mark)


# --------------------------------------------------------------------------- #
# The two rules, checked at run time
# --------------------------------------------------------------------------- #

#: Set to ``1`` (the test suite does, for every test) to raise on a broken
#: rule; anything else counts and logs it instead.
CHECKS_ENV: Final = "ALKERA_LOCK_CHECKS"


class LockRuleError(RuntimeError):
    """A lock taken out of order, or I/O awaited while a lock is held."""


class LockOrderError(LockRuleError):
    """A rank taken while a higher one is held: the shape of a deadlock."""


class IOUnderLockError(LockRuleError):
    """A call outside Postgres awaited while this task holds a row lock."""


def checks_enforced() -> bool:
    """Whether a broken rule raises (tests) or is counted and logged."""
    return os.environ.get(CHECKS_ENV) == "1"


def _violated(error: LockRuleError, *, rule: str) -> None:
    if checks_enforced():
        raise error
    record_lock_rule_violation(rule)
    log.warning("db.lock_rule_violated", rule=rule, detail=str(error))


def _check_order(db: Executor, rank: LockRank) -> None:
    """Refuse ``rank`` while a higher rank is held by ``db``'s transaction.

    Two transactions that take the same two ranks in opposite orders can each
    hold what the other waits for; taking them in ascending order only makes
    that impossible. A lock that never waits (``skip_locked``, a try) cannot
    be half of such a cycle and is not checked, and neither is a rank this
    task already holds: the order is between kinds of row, and a transaction
    that has taken one kind may take more of it (re-taking a row it holds
    costs nothing).

    Only ``db``'s own transaction counts: a task that opened another session
    (or inherited a parent's held locks when it was spawned) holds those in a
    transaction this lock does not belong to.
    """
    transaction = _root_transaction(db)
    mine = [one for one in _live_held() if one.transaction() is transaction]
    if any(one.rank == rank for one in mine):
        return
    above = [one for one in mine if one.rank.value > rank.value]
    if above:
        _violated(
            LockOrderError(
                f"took {rank.name} ({rank.value}) while holding "
                + ", ".join(
                    f"{one.rank.name} ({one.rank.value})"
                    + (f" from {one.where}" if one.where else "")
                    for one in above
                )
            ),
            rule="lock_order",
        )


def _live_held() -> tuple[_Held, ...]:
    held_ranks()
    return _HELD.get()


#: Why the current block may await I/O under a lock, when it declared one.
_IO_ALLOWED: ContextVar[str | None] = ContextVar("alkera_db_io_allowed", default=None)


class _IOOutsideLocks:
    """``with io_outside_locks("what"):`` around a call that leaves Postgres.

    Entered by the object store, the compute providers and the shared HTTP
    client, so the rule holds wherever they are called from: a row lock held
    while the call waits is held for as long as somebody else's server takes,
    and every request queued on that row waits with it. A path that must hold
    a lock across a call declares it with ``io_outside_locks.allow(reason=...)``,
    and the hygiene scan lists every such declaration.
    """

    @contextmanager
    def __call__(self, what: str) -> Iterator[None]:
        held = held_ranks()
        if held and _IO_ALLOWED.get() is None:
            _violated(
                IOUnderLockError(
                    f"{what} awaited while holding " + ", ".join(rank.name for rank in held)
                ),
                rule="io_under_lock",
            )
        yield

    @contextmanager
    def allow(self, *, reason: str) -> Iterator[None]:
        """Declare that the calls in this block wait under a lock, and why."""
        if not reason.strip():
            raise ValueError("an exception to the I/O rule needs its reason")
        token = _IO_ALLOWED.set(reason)
        try:
            yield
        finally:
            _IO_ALLOWED.reset(token)


io_outside_locks: Final = _IOOutsideLocks()

_Callable = TypeVar("_Callable", bound=Callable[..., Any])


def io_boundary(what: str) -> Callable[[_Callable], _Callable]:
    """Decorate a coroutine (or async generator) function that leaves Postgres,
    so every call to it is checked by :data:`io_outside_locks`."""

    def decorate(fn: _Callable) -> _Callable:
        if inspect.isasyncgenfunction(fn):

            @functools.wraps(fn)
            async def stream(*args: Any, **kwargs: Any) -> Any:
                with io_outside_locks(what):
                    async for item in fn(*args, **kwargs):
                        yield item

            return cast(_Callable, stream)

        @functools.wraps(fn)
        async def call(*args: Any, **kwargs: Any) -> Any:
            with io_outside_locks(what):
                return await fn(*args, **kwargs)

        return cast(_Callable, call)

    return decorate


_Class = TypeVar("_Class", bound=type)


def io_boundary_class(what: str) -> Callable[[_Class], _Class]:
    """:func:`io_boundary` on every public coroutine method a class defines,
    each named ``what.<method>``."""

    def decorate(cls: _Class) -> _Class:
        for name, member in list(vars(cls).items()):
            if name.startswith("_"):
                continue
            if inspect.iscoroutinefunction(member) or inspect.isasyncgenfunction(member):
                setattr(cls, name, io_boundary(f"{what}.{name}")(member))
        return cls

    return decorate


async def wait_at_most(db: Executor, wait_ms: int) -> None:
    """Bound every lock wait for the rest of ``db``'s transaction to
    ``wait_ms``, for a transaction whose every statement must give way fast
    (a reservation made beside a request, which falls back when it cannot)."""
    await db.execute(
        text("SELECT set_config('lock_timeout', :wait, true)"), {"wait": f"{int(wait_ms)}ms"}
    )


async def _execute_waiting_at_most(
    db: Executor, statement: Executable, wait_ms: int, params: Mapping[str, Any] | None = None
) -> Any:
    """Run ``statement`` with ``lock_timeout`` set to ``wait_ms`` for it alone."""
    prior = (
        await db.execute(
            text("SELECT current_setting('lock_timeout'), set_config('lock_timeout', :wait, true)"),
            {"wait": f"{int(wait_ms)}ms"},
        )
    ).first()
    result = await db.execute(statement, params)
    if prior is not None:
        await db.execute(
            text("SELECT set_config('lock_timeout', :prior, true)"), {"prior": prior[0]}
        )
    return result


#: The row type a locked statement selects.
_Row = TypeVar("_Row", bound=tuple[Any, ...])

Strength = Literal["update", "no_key_update", "share"]


async def lock_rows(
    db: Executor,
    rank: LockRank,
    statement: Select[_Row],
    *,
    strength: Strength = "update",
    skip_locked: bool = False,
    of: Any = None,
    timeout_ms: int | None = None,
) -> Result[_Row]:
    """Run ``statement`` with the rows it selects locked until the transaction
    ends, as ``rank``.

    ``strength`` is the row lock: ``update`` excludes every other locker and
    every foreign-key check; ``no_key_update`` excludes other writers but
    admits a transaction that only references the row through a foreign key;
    ``share`` admits other readers and excludes writers. ``skip_locked`` leaves
    out rows somebody else holds (a work queue's claim). ``of`` names the
    table to lock when the statement joins several. ``timeout_ms`` bounds the
    wait; a hot rank waits :data:`HOT_LOCK_TIMEOUT_MS` unless told otherwise,
    and everything else waits the connection's own ``lock_timeout``.

    Returns the statement's result. The rank counts as held only when a row
    was locked.
    """
    locked = statement.with_for_update(
        read=strength == "share",
        key_share=strength == "no_key_update",
        skip_locked=skip_locked,
        of=of,
    )
    return await _run_locking(db, rank, locked, None, timeout_ms, waits=not skip_locked)


#: The locking clause of each strength, for the statements written as SQL.
_CLAUSE: Final[dict[str, str]] = {
    "update": "FOR UPDATE",
    "no_key_update": "FOR NO KEY UPDATE",
    "share": "FOR SHARE",
}


async def lock_text(
    db: Executor,
    rank: LockRank,
    sql: str,
    params: Mapping[str, Any] | None = None,
    *,
    strength: Strength = "update",
    skip_locked: bool = False,
    of: str | None = None,
    timeout_ms: int | None = None,
) -> Result[Any]:
    """:func:`lock_rows` for a statement written as SQL text: the locking
    clause is appended here, so it is spelled nowhere else. ``of`` is the
    alias of the table to lock when ``sql`` joins several."""
    clause = _CLAUSE[strength]
    if of is not None:
        clause += f" OF {of}"
    if skip_locked:
        clause += " SKIP LOCKED"
    return await _run_locking(
        db, rank, text(f"{sql} {clause}"), params, timeout_ms, waits=not skip_locked
    )


async def lock_or_insert(
    db: Executor, rank: LockRank, statement: Select[_Row], insert: Insert
) -> tuple[Result[_Row], bool]:
    """The row ``statement`` selects, locked as ``rank``, after ``insert`` has
    made it if it did not exist.

    For a read-modify-write of a row that may not exist yet (a document of
    settings merged field by field): reading, then inserting when nothing was
    there, lets two first writers both insert, and reading, then writing back,
    lets the last of two writers drop what the first changed. The insert is
    tried first and does nothing on a conflict, so a second writer waits for
    the first one's row rather than colliding with it, and the row is then
    locked, so each writer reads what the one before it wrote.

    Returns the locked result and whether this call made the row.
    """
    made: Result[Any] = await db.execute(
        insert.on_conflict_do_nothing().returning(literal_column("1"))
    )
    created = made.first() is not None
    return await lock_rows(db, rank, statement), created


async def _run_locking(
    db: Executor,
    rank: LockRank,
    statement: Executable,
    params: Mapping[str, Any] | None,
    timeout_ms: int | None,
    *,
    waits: bool = True,
) -> Result[Any]:
    if waits:
        _check_order(db, rank)
    wait = timeout_ms if timeout_ms is not None else (HOT_LOCK_TIMEOUT_MS if rank.hot else None)
    if wait is None:
        result = await db.execute(statement, params)
    else:
        result = await _execute_waiting_at_most(db, statement, wait, params)
    frozen = result.freeze()
    if frozen.data:
        _record(db, rank)
    return frozen()


async def advisory_xact_lock(
    db: Executor,
    key: AdvisoryKey,
    *,
    try_only: bool = False,
    shared: bool = False,
    rank: LockRank | None = None,
    timeout_ms: int | None = None,
) -> bool:
    """Take ``key`` until ``db``'s transaction ends.

    Waits (up to ``timeout_ms``, else the connection's ``lock_timeout``)
    unless ``try_only``, which returns ``False`` at once when another
    transaction holds it. ``shared`` takes the lock in shared mode: shared
    holders admit each other and exclude an exclusive one. A lock that stands
    in for a row in the fixed order names that ``rank``. Returns whether the
    lock is held.
    """
    name = "pg_{}advisory_xact_lock{}".format(
        "try_" if try_only else "", "_shared" if shared else ""
    )
    statement = select(getattr(func, name)(*key.arguments()))
    if rank is not None and not try_only:
        _check_order(db, rank)
        if not shared:
            _check_no_upgrade(db, key)
    if timeout_ms is None or try_only:
        taken = (await db.execute(statement)).scalar_one()
    else:
        taken = (await _execute_waiting_at_most(db, statement, timeout_ms)).scalar_one()
    got = True if taken is None else bool(taken)
    if got and rank is not None:
        _record(db, rank, _SharedKey(key) if shared else key)
    return got


@dataclass(frozen=True, slots=True)
class _SharedKey:
    """What a shared hold of an advisory key is recorded as."""

    key: AdvisoryKey


def _check_no_upgrade(db: Executor, key: AdvisoryKey) -> None:
    """Refuse ``key`` exclusive while ``db``'s transaction holds it shared.

    The exclusive request waits for every other shared holder, and another
    holder making the same upgrade waits for this one: two writers doing it
    at once deadlock. A transaction takes the strongest mode it will need
    first."""
    transaction = _root_transaction(db)
    mine = [one for one in _live_held() if one.transaction() is transaction]
    if any(one.what == key for one in mine):
        return  # already held exclusive: asking again waits for nobody
    shared = [one for one in mine if one.what == _SharedKey(key)]
    if shared:
        where = f" from {shared[0].where}" if shared[0].where else ""
        _violated(
            LockOrderError(f"took {key.namespace} exclusive while holding it shared{where}"),
            rule="lock_order",
        )


def advisory_xact_lock_sync(bind: Connection, key: AdvisoryKey) -> None:
    """Take ``key`` until ``bind``'s transaction ends, on a synchronous
    connection (a migration). Unranked: a migration runs nothing else that
    takes a ranked lock."""
    bind.execute(select(func.pg_advisory_xact_lock(*key.arguments())))


def advisory_xact_lock_at_commit(session: Session, key: AdvisoryKey) -> None:
    """Take ``key`` until ``session``'s transaction ends, from a hook that runs
    as the transaction commits (a ``before_commit`` listener, where only the
    synchronous session is in hand). A lock taken there is held for the commit
    alone, which is what makes it the last lock any transaction takes; it is
    not ranked, because nothing can be taken after it."""
    session.execute(select(func.pg_advisory_xact_lock(*key.arguments())))


@asynccontextmanager
async def claimed_io(db: AsyncSession, key: AdvisoryKey) -> AsyncIterator[bool]:
    """Hold ``key``'s claim, and no row, while this task calls out of Postgres.

    The one shape a call to a provider takes: commit the intent, release every
    row lock, make the call under a claim, then apply the outcome in a short
    transaction. The claim is a transaction-scoped advisory lock that is only
    ever tried, never waited for, so two calls on one thing (a machine's stop
    and its start) cannot overlap, while everything else that touches the
    thing's rows (a message, a heartbeat, the next pass) goes on meanwhile.
    The block may lock rows after its call, to apply what the call did.

    Entered with no row lock held (a held one is the rule this exists to
    keep, and is reported as such). Yields whether the claim was taken. Ends
    the session's transaction on the way out: a commit after a clean block,
    a rollback after an error, and either way the claim goes with it.
    """
    held = held_ranks()
    if held:
        _violated(
            IOUnderLockError(
                f"a claimed call on {key.namespace} entered holding "
                + ", ".join(rank.name for rank in held)
            ),
            rule="io_under_lock",
        )
    try:
        got = await advisory_xact_lock(db, key, try_only=True)
        yield got
    except BaseException:
        await db.rollback()
        raise
    await db.commit()


@asynccontextmanager
async def advisory_claim(engine: AsyncEngine, key: AdvisoryKey) -> AsyncIterator[bool]:
    """Hold ``key`` for the whole block, on a connection of its own.

    For a claim that outlives any one unit of work (a job that must not run
    twice at once while its body commits many transactions on other
    connections). The claim is a transaction-scoped lock in a transaction this
    connection keeps open for the block, so it is released by that
    transaction's end, by the connection closing, or by the process dying, and
    never travels with a pooled connection to its next borrower. The idle
    limit is lifted for that one transaction: it runs nothing while the body
    works, by design.

    Never waits: yields ``False`` when another holder has the claim.
    """
    async with engine.connect() as conn:
        await conn.execute(text("SET LOCAL idle_in_transaction_session_timeout = 0"))
        got = await advisory_xact_lock(conn, key, try_only=True)
        try:
            yield got
        finally:
            await conn.rollback()


__all__ = [
    "CHECKS_ENV",
    "HOT_LOCK_TIMEOUT_MS",
    "AdvisoryKey",
    "IOUnderLockError",
    "KeyPart",
    "LockOrderError",
    "LockRank",
    "LockRuleError",
    "Strength",
    "advisory_claim",
    "advisory_key",
    "advisory_xact_lock",
    "advisory_xact_lock_at_commit",
    "advisory_xact_lock_sync",
    "checks_enforced",
    "claimed_io",
    "held_mark",
    "held_ranks",
    "hold_place",
    "holds_place",
    "io_boundary",
    "io_boundary_class",
    "io_outside_locks",
    "lock_or_insert",
    "lock_rows",
    "lock_text",
    "register_rank",
    "registered_ranks",
    "savepoint_rolled_back",
    "wait_at_most",
]
