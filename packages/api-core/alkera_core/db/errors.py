"""What a database refusal means to the caller who caused it, or did not.

A request that reaches Postgres can be refused there for reasons of three
kinds, and each kind has one honest answer:

**Busy or away** (a retryable ``503`` with a code a client can branch on).
Nothing the request did caused it, and nothing was left behind:

``55P03`` ``lock_not_available``
    a statement waited for a row somebody else holds for longer than
    ``lock_timeout`` allows.
``57014`` ``query_canceled``
    a statement ran past ``statement_timeout``.
``sqlalchemy.exc.TimeoutError``
    no pooled connection came free within the pool's wait.
``57P01``/``57P02``/``57P03``, class ``08``, a refused socket, a closed connection
    the database is restarting or failing over: shut down, crashed, "starting
    up", a lost connection, nothing listening yet, or a pooled connection the
    restart closed under a request already holding it.

**The caller's input** (a ``4xx`` naming what was wrong):

``23505`` ``unique_violation``
    two requests created the same thing at once and this one lost; a ``409``.
    A constraint registered with :func:`register_unique` names its field and
    says what exists, in the words a sequential duplicate gets.
``23503`` ``foreign_key_violation``
    the request named a row that is gone, or removed one still in use; a
    ``409`` ``conflict.reference``.
class ``22`` (data exception)
    a value the column cannot hold: too long, out of range, not the type it
    claims to be; a ``422`` ``invalid_input``. The driver refusing a parameter
    before the server sees it is the same answer.

**A bug** (everything else: a NOT NULL or CHECK violation, a syntax error).
It stays unclassified and becomes the opaque ``500`` that pages: telling a
client to retry a bug, or that it sent something wrong, hides it.

The constraint name and the offending value never reach the caller; the
constraint goes to the log.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from asyncpg.exceptions import InterfaceError as DriverInterfaceError
from sqlalchemy.exc import InterfaceError as WrappedInterfaceError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from alkera_core.observability.errors import ErrorCode

LOCK_NOT_AVAILABLE: Final = "55P03"
#: A unique constraint refused the row. Not contention: callers that treat a
#: duplicate as "already done" compare :func:`sqlstate_of` against it.
UNIQUE_VIOLATION: Final = "23505"
QUERY_CANCELED: Final = "57014"
#: A row names another that does not exist, or one still named is removed.
FOREIGN_KEY_VIOLATION: Final = "23503"
#: The class-22 states a caller's value causes: too long for its column
#: (22001), out of its type's range (22003), not the shape its type reads
#: (22P02), or text Postgres cannot store (22P05, 22021). The rest of class 22
#: (a division by zero, a bad regular expression the server built) is a bug,
#: and stays one.
CALLER_DATA_STATES: Final = frozenset({"22001", "22003", "22P02", "22P05", "22021"})
#: The server is going away or not up yet: admin shutdown, crash shutdown and
#: cannot-connect-now ("the database system is starting up").
UNAVAILABLE_STATES: Final = frozenset({"57P01", "57P02", "57P03"})
#: SQLSTATE class 08: the connection itself failed or was lost.
CONNECTION_EXCEPTION_CLASS: Final = "08"
#: The modules whose frames mark an ``OSError`` as the database connection's.
_DRIVER_MODULES: Final = ("asyncpg", "psycopg", "sqlalchemy.pool", "sqlalchemy.engine")
#: What the driver says about a connection a restart closed under a request.
_CLOSED_CONNECTION_PHRASES: Final = ("connection is closed", "connection was closed")

#: What the loser of a create race is told when its constraint names no field.
ALREADY_EXISTS_MESSAGE: Final = "That already exists."
REFERENCE_MESSAGE: Final = "That refers to something that no longer exists or is still in use."
INVALID_INPUT_MESSAGE: Final = "A value in the request is too long, out of range or malformed."


@dataclass(frozen=True, slots=True)
class Contention:
    """What a busy database is called on the wire."""

    code: ErrorCode
    message: str
    #: Seconds a client is told to wait. Short for a held row — the holder is
    #: usually another request about to commit — longer for a drained pool,
    #: where an immediate retry only joins the queue that refused it.
    retry_after_seconds: int


@dataclass(frozen=True, slots=True)
class Refusal:
    """A database refusal as the answer a caller gets."""

    status: int
    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)
    #: Set only for the retryable answers; it becomes ``Retry-After``.
    retry_after_seconds: int | None = None
    #: The constraint the database named, for the log; never sent.
    constraint: str | None = None


@dataclass(frozen=True, slots=True)
class UniqueRule:
    """What a duplicate on one unique constraint is called."""

    field: str
    message: str
    code: str


_UNIQUE: dict[str, UniqueRule] = {}


def register_unique(
    constraint_name: str, field: str, message: str, *, code: str = ErrorCode.conflict
) -> None:
    """Name the field a duplicate on ``constraint_name`` is about.

    Registered beside the constraint, so the race a check-then-insert create
    cannot close is answered with the same field and sentence as the duplicate
    its check catches, with no ``except IntegrityError`` at the create. A second
    registration that disagrees with the first is a programming error.
    """
    rule = UniqueRule(field=field, message=message, code=str(code))
    existing = _UNIQUE.get(constraint_name)
    if existing is not None and existing != rule:
        raise ValueError(f"{constraint_name} is already registered as {existing}")
    _UNIQUE[constraint_name] = rule


def unique_rule(constraint_name: str) -> UniqueRule | None:
    """The registered rule for ``constraint_name``, if any."""
    return _UNIQUE.get(constraint_name)


_BY_SQLSTATE: Final[dict[str, Contention]] = {
    LOCK_NOT_AVAILABLE: Contention(
        code=ErrorCode.db_lock_timeout,
        message="Another change to the same data is still in progress. Please retry shortly.",
        retry_after_seconds=1,
    ),
    QUERY_CANCELED: Contention(
        code=ErrorCode.db_statement_timeout,
        message="That took too long to complete. Please retry shortly.",
        retry_after_seconds=2,
    ),
}

_UNAVAILABLE: Final = Contention(
    code=ErrorCode.db_unavailable,
    message="The service is briefly unavailable. Please retry shortly.",
    retry_after_seconds=5,
)

_POOL_EXHAUSTED: Final = Contention(
    code=ErrorCode.db_pool_exhausted,
    message="The service is busy. Please retry shortly.",
    retry_after_seconds=5,
)


def _chain(exc: BaseException, *, implicit: bool = False) -> Iterator[BaseException]:
    """``exc`` and the errors behind it, each visited once.

    The default follows what the error was raised *from*: SQLAlchemy's ``orig``
    (where the driver's error sits), else the explicit cause. ``implicit`` also
    follows ``__context__``, the error that was being handled when this one was
    raised; only the check for a refused socket reads that far, because an
    unrelated error handled earlier must not lend its SQLSTATE to this one.
    """
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        orig = getattr(current, "orig", None)
        if isinstance(orig, BaseException):
            pending.append(orig)
        elif current.__cause__ is not None:
            pending.append(current.__cause__)
        if implicit and current.__context__ is not None:
            pending.append(current.__context__)


def sqlstate_of(exc: BaseException) -> str | None:
    """The SQLSTATE ``exc`` carries anywhere along its chain, or ``None``.

    SQLAlchemy wraps the driver's error, so the code lives on ``orig`` or
    ``__cause__`` rather than on what the caller caught.
    """
    for current in _chain(exc):
        for attribute in ("sqlstate", "pgcode"):
            found = getattr(current, attribute, None)
            if isinstance(found, str) and found:
                return found
    return None


def constraint_of(exc: BaseException) -> str | None:
    """The constraint the driver names anywhere along ``exc``'s chain, if any."""
    for current in _chain(exc):
        name = getattr(current, "constraint_name", None)
        if isinstance(name, str) and name:
            return name
    return None


def is_unique_violation(exc: BaseException) -> bool:
    """Whether ``exc`` is a unique index refusing a second row with the same key."""
    return sqlstate_of(exc) == UNIQUE_VIOLATION


def _raised_by_the_driver(exc: BaseException) -> bool:
    """Whether a socket error in ``exc``'s chain was raised inside the database
    driver or pool, as opposed to any other connection in the process.

    The driver raises a bare ``ConnectionRefusedError`` when nothing listens on
    the database port, with nothing around it that says "database"; its frames
    are what do.
    """
    for current in _chain(exc, implicit=True):
        if not isinstance(current, OSError):
            continue
        tb = current.__traceback__
        while tb is not None:
            module = str(tb.tb_frame.f_globals.get("__name__", ""))
            if module.startswith(_DRIVER_MODULES):
                return True
            tb = tb.tb_next
    return False


def _connection_closed(exc: BaseException) -> bool:
    """Whether the driver refused because the connection it was handed is
    closed: a restart closed it under a request that already held it."""
    for current in _chain(exc):
        if isinstance(current, (DriverInterfaceError, WrappedInterfaceError)):
            text = str(current).lower()
            if any(phrase in text for phrase in _CLOSED_CONNECTION_PHRASES):
                return True
    return False


#: The generic data-exception state the driver gives a parameter it could not
#: encode. Postgres itself names the precise class-22 state instead.
_DRIVER_BIND_STATE: Final = "22000"

#: What the driver's encoder raises underneath a parameter it refused.
_ENCODE_FAILURES: Final = (OverflowError, ValueError, TypeError)


def _refused_parameter(exc: BaseException) -> bool:
    """Whether the driver refused a parameter before sending it: an integer
    past the column's range, a string where a UUID belongs. asyncpg raises
    these either as an interface error that is also a ``ValueError``, or as a
    data error carrying the generic state 22000 raised from the encoder's own
    error; a 22000 the server sent has no such cause."""
    for current in _chain(exc):
        if isinstance(current, DriverInterfaceError) and isinstance(current, ValueError):
            return True
        cause = current.__cause__
        if (
            getattr(current, "sqlstate", None) == _DRIVER_BIND_STATE
            and isinstance(cause, _ENCODE_FAILURES)
            and type(cause).__module__ == "builtins"
        ):
            return True
    return False


def contention(exc: BaseException) -> Contention | None:
    """The retryable answer for ``exc``, or ``None`` when it is not one: the
    database was busy or away, and the same request may simply be sent again."""
    if isinstance(exc, PoolTimeoutError):
        return _POOL_EXHAUSTED
    state = sqlstate_of(exc)
    if state is not None:
        if state in UNAVAILABLE_STATES or state.startswith(CONNECTION_EXCEPTION_CLASS):
            return _UNAVAILABLE
        return _BY_SQLSTATE.get(state)
    if _connection_closed(exc) or _raised_by_the_driver(exc):
        return _UNAVAILABLE
    return None


def _unique(exc: BaseException) -> Refusal:
    constraint = constraint_of(exc)
    rule = unique_rule(constraint) if constraint is not None else None
    if rule is None:
        return Refusal(409, ErrorCode.conflict, ALREADY_EXISTS_MESSAGE, constraint=constraint)
    return Refusal(409, rule.code, rule.message, {"field": rule.field}, constraint=constraint)


def classify(exc: BaseException) -> Refusal | None:
    """The answer a request that raised ``exc`` gets, or ``None`` for a bug."""
    busy = contention(exc)
    if busy is not None:
        return Refusal(
            503,
            busy.code,
            busy.message,
            {"retryable": True},
            retry_after_seconds=busy.retry_after_seconds,
        )
    state = sqlstate_of(exc)
    if state == UNIQUE_VIOLATION:
        return _unique(exc)
    if state == FOREIGN_KEY_VIOLATION:
        return Refusal(
            409, ErrorCode.conflict_reference, REFERENCE_MESSAGE, constraint=constraint_of(exc)
        )
    if state in CALLER_DATA_STATES or _refused_parameter(exc):
        return Refusal(422, ErrorCode.invalid_input, INVALID_INPUT_MESSAGE)
    return None


__all__ = [
    "ALREADY_EXISTS_MESSAGE",
    "CALLER_DATA_STATES",
    "CONNECTION_EXCEPTION_CLASS",
    "FOREIGN_KEY_VIOLATION",
    "INVALID_INPUT_MESSAGE",
    "LOCK_NOT_AVAILABLE",
    "QUERY_CANCELED",
    "REFERENCE_MESSAGE",
    "UNAVAILABLE_STATES",
    "UNIQUE_VIOLATION",
    "Contention",
    "Refusal",
    "UniqueRule",
    "classify",
    "constraint_of",
    "contention",
    "is_unique_violation",
    "register_unique",
    "sqlstate_of",
    "unique_rule",
]
