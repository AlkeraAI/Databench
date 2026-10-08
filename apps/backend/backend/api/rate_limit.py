"""Per-caller request throttling: one registry of rate-limit classes, every
route resolved to exactly one of them.

The edge WAF carries a rate-based rule, but AWS WAF aggregates over a rolling
five minutes with a minimum useful limit in the hundreds — so a hundred
requests inside one second, or one minute, sail through it. An external
assessment did exactly that: 100 CI-token mints in ~1 s, then 100 KB writes in
~1 min, and nothing throttled. The WAF answers volume; this answers burst and
pace, close to the endpoint that cares, sized per class of endpoint.

A CLASS is a burst (what a caller may spend at once), a sustained per-minute
rate, and a KEY — the identity the spend is counted against: the caller IP for
anonymous calls, the acting principal for authenticated ones, the machine
credential for the workspace box (so a busy box never starves its owner's
browser), the source address for a webhook (and, once its signature has
verified it, the tenant the delivery names), the account named by a
credential attempt. A route declares its class with ``limited("<class>")`` in
its ``dependencies`` (or its router's); a route that declares nothing gets the
default for its method — ``read`` for GET / HEAD, ``mutation`` for the rest —
so nothing is ever unlimited. ``install(app)`` walks every route once, at
startup, and pins the resolution; a hygiene test walks the same routes.

Mechanics. Each (class, key) has a token bucket of ``burst`` tokens refilling
at ``per_minute / 60`` a second AND a fixed one-minute window of ``per_minute``:
the bucket is what stops a same-instant flood, the window is what makes "the
121st request within a minute is refused" literally true. Buckets are
in-process and therefore per task; the deployment-wide ceiling is ``limit x
tasks`` with the WAF as the global backstop. The two strict classes
(``credential``, ``mint``) also count the hour in Postgres (one upsert per
attempt, see ``alkera_core.models.RateLimitWindow``), so a multi-task
deployment cannot be split-brained on exactly the classes an attacker spreads.

Every refusal is a 429 with ``Retry-After`` and the ``RateLimit-*`` headers,
the house envelope ``{code: "rate_limited", message, details.retry_after}``,
and a structured log line carrying the class, the route and a digest of the
key — never the credential.

The clock is injected. A burst timed by the wall clock refills as fast as a
slow request loop drains it, which turns "did it throttle?" into a question
about how loaded the machine is; a test pins the clock and moves it.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Coroutine, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from ipaddress import ip_address
from typing import Any, Final

from alkera_core.auth import COOKIE_NAME, InvalidTokenError, SessionClaims, decode_session_token
from alkera_core.auth.ci_token import looks_like_ci_token
from alkera_core.auth.machine_token import looks_like_machine_token
from alkera_core.auth.pat_token import looks_like_pat_token
from alkera_core.auth.proxy_token import looks_like_proxy_token
from alkera_core.config import settings
from alkera_core.db.errors import LOCK_NOT_AVAILABLE, sqlstate_of
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.logging import get_logger
from alkera_core.models import RateLimitWindow
from asyncpg import PostgresError
from fastapi import FastAPI, HTTPException, status
from fastapi.routing import APIRoute, APIWebSocketRoute
from sqlalchemy import delete, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import HTTPConnection, Request
from starlette.routing import BaseRoute, Mount

from backend.services.compute import assertion as machine_assertion

log = get_logger(__name__)

#: Ceiling on distinct keys held per limiter. Keys are caller-controlled (an IP
#: per attacker host), so this must be bounded or a spray becomes a memory leak.
#: Evicting a key only forgives its history, and the evicted key is by
#: construction the least recently seen — i.e. not the one attacking.
_MAX_KEYS: Final = 16_384

#: Reads a monotonically increasing number of seconds.
Clock = Callable[[], float]
#: Reads the wall clock, for the durable hourly window.
WallClock = Callable[[], datetime]

#: The durable window's length, and how the escalating wait for a key past its
#: hourly ceiling grows: doubling from a minute, never past the lockout's own
#: fifteen minutes, and never past the end of the window it lives in — so the
#: worst a stranger can impose on an account they name is a bounded wait that
#: expires on its own.
_DURABLE_WINDOW: Final = timedelta(hours=1)
_BACKOFF_BASE_SECONDS: Final = 60.0
_BACKOFF_CEILING_SECONDS: Final = 900.0
#: Every this-many durable writes, rows older than the window are purged.
_PURGE_EVERY: Final = 256
#: How long one durable hit may wait for its counter row. A key under
#: concurrent attempts has its row held by the attempt ahead of it until that
#: one commits; under a slow commit an unbounded wait queued every attempt on
#: the key behind it and ended in the request's lock timeout. Short, so the
#: wait is never what a caller notices; past it the class's busy policy answers.
_DURABLE_LOCK_WAIT_MS: Final = 100

REFUSAL_CODE: Final = "rate_limited"


class KeyKind(StrEnum):
    """What a class counts its spend against."""

    #: The proxy-attested caller address.
    IP = "ip"
    #: The authenticated principal (a user session, a token digest); the caller
    #: address when anonymous.
    PRINCIPAL = "principal"
    #: The account a credential attempt names — an email, a token, a device
    #: code — as a digest; the principal when the call is already authenticated.
    ACCOUNT = "account"
    #: The machine credential (the agent assertion) when the call carries one;
    #: the principal otherwise.
    MACHINE = "machine"
    #: The org, the holder's machine and the leased folder the route names — one
    #: budget per folder a machine holds, rather than one per machine.
    LEASE_NODE = "lease_node"
    #: The principal, the machine acting for it and the resource the route names
    #: — one budget per chat or node a box works on, never the owner's own.
    AGENT_RESOURCE = "agent_resource"
    #: The org of the principal, for org-wide operations where one org must not
    #: starve another.
    ORG = "org"
    #: The tenant a webhook delivery names -- a Slack workspace, a Stripe
    #: account -- once its signature has verified it. Charged by the route
    #: itself, never computed by the enforcer: the enforcer runs before the
    #: signature is checked, and an unverified body may name any tenant it likes.
    TENANT = "tenant"
    #: The webhook route and the address it was posted from: the only key a
    #: public webhook is counted against before its signature is checked.
    SOURCE = "source"


@dataclass(frozen=True, slots=True)
class Leg:
    """One key a class counts against, with its numbers. Every leg of a class
    must admit the request. The numbers are read live from settings so a
    deployment tunes them by env var and a test pins them by monkeypatch."""

    kind: KeyKind
    per_minute: Callable[[], int]
    burst: Callable[[], int]
    #: The durable hourly ceiling, counted in Postgres; ``None`` for the classes
    #: whose per-task multiplication is acceptable.
    per_hour: Callable[[], int] | None = None


@dataclass(frozen=True, slots=True)
class RateLimitClass:
    name: str
    legs: tuple[Leg, ...]
    #: What a refusal says. Product copy, no numbers.
    message: str
    #: True for the sentinel that is never counted.
    exempt: bool = False
    #: True for a class a route charges itself, at the point in its handler
    #: where the key becomes trustworthy; ``limited()`` refuses to declare it,
    #: so the enforcer never computes its key from an unverified request.
    charged_by_route: bool = False
    #: What a durable hourly leg answers when its counter row is held past
    #: ``_DURABLE_LOCK_WAIT_MS`` (:class:`DurableBusy`): ``True`` refuses the
    #: attempt with a one-second wait, ``False`` admits it on the in-memory
    #: legs alone. A row is held only while attempts on that one key arrive
    #: concurrently, so a class guarding a secret refuses (concurrency on one
    #: account is what guessing looks like) and the rest admit. A store that is
    #: unreachable or failing for any other reason always admits: the durable
    #: leg is a second opinion, never an availability dependency.
    refuse_when_busy: bool = False


def _leg(kind: KeyKind, name: str, *, hourly: bool = False, ip: bool = False) -> Leg:
    """A leg reading ``rate_limit_<name>[_ip]_{per_minute,burst,per_hour}``."""
    stem = f"rate_limit_{name}_ip" if ip else f"rate_limit_{name}"
    return Leg(
        kind=kind,
        per_minute=lambda: int(getattr(settings, f"{stem}_per_minute")),
        burst=lambda: int(getattr(settings, f"{stem}_burst")),
        per_hour=(lambda: int(getattr(settings, f"{stem}_per_hour"))) if hourly else None,
    )


_WAIT = "Too many requests. Please wait a moment and try again."

#: The classes, in the order the report lists them. A new class is a new entry
#: here plus its settings; a route names it by string.
CLASSES: Final[dict[str, RateLimitClass]] = {
    "credential": RateLimitClass(
        "credential",
        (
            _leg(KeyKind.ACCOUNT, "credential", hourly=True),
            _leg(KeyKind.IP, "credential", hourly=True, ip=True),
        ),
        "Too many sign-in attempts. Please wait before trying again.",
        refuse_when_busy=True,
    ),
    # The device grant's token poll: a cadence the server itself issues, so it
    # is bounded for volume and never counted by the hour (see the settings).
    "device_poll": RateLimitClass(
        "device_poll",
        (
            _leg(KeyKind.ACCOUNT, "device_poll"),
            _leg(KeyKind.IP, "device_poll", ip=True),
        ),
        _WAIT,
    ),
    "mint": RateLimitClass(
        "mint",
        (_leg(KeyKind.PRINCIPAL, "mint", hourly=True),),
        "Too many credentials issued in a short time. Please wait before minting another.",
    ),
    "mutation": RateLimitClass("mutation", (_leg(KeyKind.PRINCIPAL, "mutation"),), _WAIT),
    "read": RateLimitClass("read", (_leg(KeyKind.PRINCIPAL, "read"),), _WAIT),
    "chat": RateLimitClass(
        "chat",
        (_leg(KeyKind.PRINCIPAL, "chat"),),
        "You are sending messages very quickly. Please pause for a moment.",
    ),
    "upload": RateLimitClass(
        "upload",
        (_leg(KeyKind.PRINCIPAL, "upload"),),
        "Too many uploads started at once. Please wait a moment and try again.",
    ),
    "upload_part": RateLimitClass(
        "upload_part",
        (_leg(KeyKind.PRINCIPAL, "upload_part"),),
        "Too many upload parts at once. Please wait a moment and try again.",
    ),
    # Following queued work: a poll per commit, copy or download the principal
    # started, so it scales like the parts and not like a person reading.
    "operation": RateLimitClass(
        "operation",
        (_leg(KeyKind.PRINCIPAL, "operation"),),
        "Too many progress reads at once. Please wait a moment and try again.",
    ),
    "stream_open": RateLimitClass(
        "stream_open",
        (_leg(KeyKind.PRINCIPAL, "stream_open"),),
        "Reconnecting too quickly. Please wait a moment before reconnecting.",
    ),
    "ws_ticket": RateLimitClass("ws_ticket", (_leg(KeyKind.PRINCIPAL, "ws_ticket"),), _WAIT),
    "machine": RateLimitClass(
        "machine",
        (
            # The machine's own budget -- only for an assertion that verified as
            # a machine this principal registered on this very credential; any
            # other request is counted as its principal here.
            _leg(KeyKind.MACHINE, "machine"),
            # The ceiling: everything the principal's machines do, together.
            # Keyed on the person rather than the address, so boxes that share
            # one egress never share a budget, and no id a request writes can
            # step outside it.
            _leg(KeyKind.PRINCIPAL, "agent_principal"),
        ),
        _WAIT,
    ),
    "lease_node": RateLimitClass(
        "lease_node",
        (
            _leg(KeyKind.LEASE_NODE, "lease_node"),
            # The ceiling. The folder in the per-folder key is a path segment the
            # caller writes, so on that leg alone a caller naming a different
            # folder every time would never spend a budget at all; the address
            # is the one part of the key nothing in the request can rotate.
            _leg(KeyKind.IP, "lease_node", ip=True),
        ),
        _WAIT,
    ),
    # The holder's tree report, keyed like the live plane -- per folder, with
    # the address as the ceiling -- on numbers of its own.
    "lease_tree": RateLimitClass(
        "lease_tree",
        (
            _leg(KeyKind.LEASE_NODE, "lease_tree"),
            _leg(KeyKind.IP, "lease_tree", ip=True),
        ),
        _WAIT,
    ),
    # The public webhooks. Before the signature is checked nothing in the
    # request can be trusted but the address it came from, so that is all the
    # enforcer keys on -- per route, so one provider's traffic never spends
    # another's. The tenant a delivery names is charged by the route once the
    # signature has verified it (`webhook_tenant`), and a failed signature is
    # charged to its source (`webhook_reject`): a forged body naming a tenant
    # costs that tenant nothing.
    "webhook": RateLimitClass("webhook", (_leg(KeyKind.SOURCE, "webhook", ip=True),), _WAIT),
    "webhook_tenant": RateLimitClass(
        "webhook_tenant", (_leg(KeyKind.TENANT, "webhook"),), _WAIT, charged_by_route=True
    ),
    "webhook_reject": RateLimitClass(
        "webhook_reject",
        (_leg(KeyKind.SOURCE, "webhook_reject"),),
        _WAIT,
        charged_by_route=True,
    ),
    # A box acting for a person on the person's own routes -- reading a chat's
    # transcript, listing a folder, opening the event stream -- on behalf of
    # every chat it runs. On the person's budget, thirty idle chats' polling
    # spent the owner's own reads and the owner's browser lost its live
    # updates; on one machine budget, the chat polling hardest starved the
    # rest. So a request whose agent assertion VERIFIES as the principal's own
    # registered machine is counted here instead: per (principal, machine,
    # resource) at the machine's numbers, with a per-principal ceiling that no
    # machine id or path segment can rotate. The person keeps their own budget
    # untouched. An assertion that does not verify changes nothing: the
    # request stays on the person's own class.
    "agent": RateLimitClass(
        "agent",
        (
            _leg(KeyKind.AGENT_RESOURCE, "machine"),
            _leg(KeyKind.PRINCIPAL, "agent_principal"),
        ),
        _WAIT,
    ),
    # A box on its own machine credential, on the classes a person's requests
    # fall into (see the `box` settings). The principal key of a machine
    # credential is its digest and the address, so both are bounded.
    "box": RateLimitClass("box", (_leg(KeyKind.PRINCIPAL, "box"),), _WAIT),
    "admin": RateLimitClass("admin", (_leg(KeyKind.PRINCIPAL, "admin"),), _WAIT),
    "exempt": RateLimitClass("exempt", (), "", exempt=True),
    # Counted by the route itself rather than by the middleware: its key is the
    # grant nonce paired with the caller address, and a nonce is a path segment
    # of a separate ASGI app — nothing the key kinds above can see. The numbers
    # are fixed rather than settings-backed because the budget belongs to one
    # grant (a page and its assets is tens of requests), not to a deployment.
    "page_grant": RateLimitClass(
        "page_grant",
        (Leg(kind=KeyKind.IP, per_minute=lambda: 600, burst=lambda: 600),),
        _WAIT,
    ),
}

#: The principal-keyed classes a request whose agent assertion verifies is
#: counted on the ``agent`` class for instead. The strict classes (credential, mint) and
#: the admin budget are never re-keyed: a box has no business on them.
AGENT_REKEYED: Final = frozenset(
    {"read", "mutation", "chat", "upload", "upload_part", "operation", "stream_open", "ws_ticket"}
)

#: The classes a route falls into when it declares none: by method.
DEFAULT_READ: Final = "read"
DEFAULT_MUTATION: Final = "mutation"
_READ_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})

#: Paths that are never counted: liveness must answer under attack, and the
#: content mount is a separate ASGI app serving signed bytes — its budget is
#: the edge's Files rule, not an API class.
EXEMPT_PREFIXES: Final = ("/health",)


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float
    window_start: float
    window_count: int


@dataclass(frozen=True, slots=True)
class Decision:
    """What one leg said about one request."""

    allowed: bool
    limit: int
    remaining: int
    #: Seconds until the caller may send one more request (0 when allowed).
    retry_after: float
    #: Seconds until the minute window resets.
    reset: float


class TokenBucketLimiter:
    """A bounded LRU of token buckets plus a fixed minute window, one per key.

    Not thread-safe by lock, and does not need to be: the app is asyncio and
    every method here runs to completion without an await, so no two coroutines
    interleave inside one.
    """

    def __init__(
        self,
        *,
        per_minute: Callable[[], int],
        burst: Callable[[], int],
        max_keys: int = _MAX_KEYS,
        clock: Clock = time.monotonic,
    ) -> None:
        self._per_minute = per_minute
        self._burst = burst
        self.max_keys = max_keys
        self.clock = clock
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()

    @property
    def per_minute(self) -> int:
        return max(1, self._per_minute())

    @property
    def burst(self) -> int:
        return max(1, min(self._burst(), self.per_minute))

    def consume(self, key: str, *, now: float | None = None, spend: bool = True) -> Decision:
        """Spend one token for `key`, or say how long until one is available.

        With ``spend=False`` the answer is the same but nothing is taken — the
        registry asks every key of a class first and spends only when all of
        them admit, so a request one key refuses costs the others nothing.
        """
        now = self.clock() if now is None else now
        per_minute = self.per_minute
        burst = self.burst
        rate = per_minute / 60.0
        window_start = math.floor(now / 60.0) * 60.0
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(
                tokens=float(burst), updated_at=now, window_start=window_start, window_count=0
            )
            self._buckets[key] = bucket
        else:
            bucket.tokens = min(float(burst), bucket.tokens + (now - bucket.updated_at) * rate)
            bucket.updated_at = now
            if bucket.window_start != window_start:
                bucket.window_start = window_start
                bucket.window_count = 0
        self._buckets.move_to_end(key)
        while len(self._buckets) > self.max_keys:
            self._buckets.popitem(last=False)

        reset = max(0.0, window_start + 60.0 - now)
        if bucket.window_count >= per_minute:
            return Decision(False, per_minute, 0, max(1.0, reset), reset)
        if bucket.tokens < 1.0:
            wait = (1.0 - bucket.tokens) / rate
            remaining = per_minute - bucket.window_count
            return Decision(False, per_minute, remaining, max(1.0, wait), reset)
        if not spend:
            remaining = min(int(bucket.tokens) - 1, per_minute - bucket.window_count - 1)
            return Decision(True, per_minute, remaining, 0.0, reset)
        bucket.tokens -= 1.0
        bucket.window_count += 1
        remaining = min(int(bucket.tokens), per_minute - bucket.window_count)
        return Decision(True, per_minute, remaining, 0.0, reset)

    def reset(self) -> None:
        self._buckets.clear()


class DurableBusy(Exception):  # noqa: N818 - a state, not a failure: the row is in use
    """The counter row stayed held past ``_DURABLE_LOCK_WAIT_MS``."""


class DurableWindow:
    """The hourly count in Postgres for the strict classes.

    One upsert per attempt on a short session of its own — it must survive the
    request's rollback (a refused login rolls back, and the refusal is exactly
    what has to be counted) and must never hold a second pooled connection
    while the request's is open, which is why it runs from the app-level
    dependency, before the request session exists.
    """

    def __init__(self, session_factory: Callable[[], AsyncSession] = AsyncSessionLocal) -> None:
        self.session_factory = session_factory
        self._writes = 0

    async def hit(self, rate_class: str, key: str, *, now: datetime) -> int:
        """Count one attempt and return the hour's total including it.

        One atomic upsert, so concurrent attempts on a key each count once and
        none is lost. It waits at most ``_DURABLE_LOCK_WAIT_MS`` for the row and
        raises :class:`DurableBusy` past that, rather than queueing the request.
        """
        window_start = now.replace(minute=0, second=0, microsecond=0)
        stmt = (
            insert(RateLimitWindow)
            .values(rate_class=rate_class, key=key, window_start=window_start, count=1)
            .on_conflict_do_update(
                constraint="pk_rate_limit_windows",
                set_={"count": RateLimitWindow.count + 1},
            )
            .returning(RateLimitWindow.count)
        )
        async with self.session_factory() as session:
            await session.execute(text(f"SET LOCAL lock_timeout = '{_DURABLE_LOCK_WAIT_MS}ms'"))
            try:
                count = int((await session.execute(stmt)).scalar_one())
            except DBAPIError as exc:
                if sqlstate_of(exc) == LOCK_NOT_AVAILABLE:
                    raise DurableBusy(rate_class) from exc
                raise
            await session.commit()
        self._writes += 1
        if self._writes % _PURGE_EVERY == 0:
            await self._purge(window_start)
        return count

    async def _purge(self, window_start: datetime) -> None:
        """Drop rows older than the window, in a transaction of its own and
        best effort: housekeeping must never cost an attempt its count."""
        try:
            async with self.session_factory() as session:
                await session.execute(text(f"SET LOCAL lock_timeout = '{_DURABLE_LOCK_WAIT_MS}ms'"))
                await session.execute(
                    delete(RateLimitWindow).where(
                        RateLimitWindow.window_start < window_start - _DURABLE_WINDOW
                    )
                )
                await session.commit()
        except (SQLAlchemyError, OSError):
            log.warning("ratelimit.durable_purge_skipped", exc_info=True)

    def reset(self) -> None:
        self._writes = 0


def caller_key(request: HTTPConnection) -> str:
    """The identity an anonymous limit is counted against: the proxy-attested
    client IP.

    Attested, not merely present. A caller can write any left-hand
    `X-Forwarded-For` entries it likes, while each trusted proxy APPENDS the peer
    it actually saw — so counting the leftmost hop would let an attacker mint a
    fresh quota per request by rotating a header. Reading
    `forwarded_for_trusted_hops` from the RIGHT gives the address our own chain
    vouched for. Same rule the signup/login forensics path uses.

    A header shorter than our chain means the chain was bypassed; nothing in it
    is attestable, so the key falls back to a single shared bucket rather than to
    a forgeable value. Shared is the safe direction here: it over-throttles a
    misconfigured deployment instead of silently not throttling at all.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        trusted = max(1, settings.forwarded_for_trusted_hops)
        raw = hops[-trusted] if len(hops) >= trusted else None
    else:
        raw = request.client.host if request.client else None
    if not raw:
        return "unattributed"
    try:
        return str(ip_address(raw))
    except ValueError:
        return "unattributed"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _bearer(conn: HTTPConnection) -> str | None:
    header = conn.headers.get("authorization")
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def _session_claims(conn: HTTPConnection) -> SessionClaims | None:
    token = conn.cookies.get(COOKIE_NAME) or _bearer(conn)
    if not token:
        return None
    try:
        return decode_session_token(token)
    except InvalidTokenError:
        return None


def _session_user(conn: HTTPConnection) -> tuple[str, str] | None:
    """``(user_id, org_id)`` when the request carries a VALID session token —
    signed by us and unexpired. A forged or expired token yields nothing, so
    rotating garbage cannot mint fresh keys: the caller is then just its IP."""
    claims = _session_claims(conn)
    if claims is None:
        return None
    return str(claims.user_id), str(claims.org_team_id)


def principal_keys(conn: HTTPConnection) -> list[str]:
    """The keys an authenticated identity is counted against — every one of
    them must admit the request.

    A verified session JWT is one key: the user. A bearer in one of our token
    shapes (a CI token, a proxy token, a personal access token) is counted by
    its digest — one budget per credential, never the credential itself in
    memory or in a log line — AND by the caller address, because the enforcer
    runs before the token is verified: a spray of made-up tokens would
    otherwise mint a fresh budget per request. Anything else is its address.
    """
    user = _session_user(conn)
    if user is not None:
        return [f"user:{user[0]}"]
    address = f"ip:{caller_key(conn)}"
    bearer = _bearer(conn)
    if bearer and (
        looks_like_ci_token(bearer)
        or looks_like_proxy_token(bearer)
        or looks_like_pat_token(bearer)
        or looks_like_machine_token(bearer)
    ):
        return [f"tok:{_digest(bearer)}", address]
    return [address]


#: Where the enforcer leaves the machine a request verified as, for the key
#: functions below (which are synchronous and run per leg).
_VERIFIED_MACHINE: Final = "alkera.rate_limit.verified_machine"


async def resolve_verified_machine(conn: HTTPConnection) -> str | None:
    """Verify the request's agent assertion once and remember the answer on
    the connection: the machine id when the assertion is a machine the
    session's user registered on this very session token, ``None`` for
    everything else (see :mod:`backend.services.compute.assertion`). Only a
    session JWT can speak for a machine."""
    claims = _session_claims(conn)
    machine = (
        None
        if claims is None
        else await machine_assertion.verified_machine_id(
            conn.headers,
            user_id=claims.user_id,
            org_id=claims.org_team_id,
            credential_id=claims.jti,
        )
    )
    conn.scope[_VERIFIED_MACHINE] = machine
    return machine


def verified_agent_id(conn: HTTPConnection) -> str | None:
    """The machine this request verified as, or ``None``: no assertion, a
    malformed pair (the route itself refuses it), or one that did not verify.
    Only what :func:`resolve_verified_machine` recorded counts; a header alone
    never does."""
    value = conn.scope.get(_VERIFIED_MACHINE)
    return value if isinstance(value, str) else None


def machine_keys(conn: HTTPConnection) -> list[str]:
    """The machine when the call's agent assertion verified; the principal
    otherwise. A box's budget is separate from its owner's; a header that
    merely names a machine is the person."""
    machine = verified_agent_id(conn)
    if machine is not None:
        return [f"machine:{machine}"]
    return principal_keys(conn)


def agent_resource_keys(conn: HTTPConnection) -> list[str]:
    """One budget per resource a machine works on for a principal: the
    principal's own key, the asserted machine session, and the last id-shaped
    path segment the route names (the chat, the node) -- the drive alone for a
    drive-level route, the route itself when it names nothing.

    Only a uuid-shaped segment reaches the key, for the reason ``lease_node_keys``
    gives: this runs before the endpoint coerces it. The resource is
    caller-written, so this leg alone bounds nothing against a caller that
    rotates it; the class pairs it with the per-principal leg.
    """
    session = verified_agent_id(conn)
    if session is None:
        return principal_keys(conn)
    resource = "-"
    for raw in conn.path_params.values():
        try:
            resource = str(uuid.UUID(str(raw)))
        except (ValueError, AttributeError, TypeError):
            continue
    principal = principal_keys(conn)[0]
    return [f"{principal}|agent:{session}|res:{resource}"]


def lease_node_keys(conn: HTTPConnection) -> list[str]:
    """One budget per folder a machine holds: the org, the machine and the node
    the route names.

    The live plane is the one family whose load scales with how MANY folders a
    box holds at once — a report per debounce window per file, per chat — so on
    the machine's own budget the busiest chat starves every other chat that box
    is running. The holder's *instance* is deliberately not part of the key: a
    mirror that restarts, or a second instance of the same holder, is still the
    same folder's traffic and must not buy itself a fresh budget for it.

    Only a node id shaped like one reaches the key, and it reaches it in the one
    spelling a uuid has: this runs before the endpoint coerces the segment, so
    free text — or the same folder written sixteen ways — would otherwise be a
    fresh budget each time and a fresh entry in a bounded table of keys. A route
    with no folder in its path, and a segment that is not a node id, both fall
    back to the machine's own budget, so a request this key cannot describe is
    still bounded by the caller it came from.

    This leg alone does not bound a caller that names a NEW folder every time —
    every such request is a first one. The class pairs it with the address leg,
    which nothing in the request can rotate, and that is what makes the budget
    shared across whatever folder the caller names.
    """
    raw = conn.path_params.get("item_id")
    try:
        node = uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return machine_keys(conn)
    org = org_keys(conn)[0]
    return [f"{org}|{holder}|node:{node}" for holder in machine_keys(conn)]


def org_keys(conn: HTTPConnection) -> list[str]:
    user = _session_user(conn)
    if user is not None:
        return [f"org:{user[1]}"]
    return principal_keys(conn)


async def _json_body(request: Request) -> dict[str, Any] | None:
    if "json" not in request.headers.get("content-type", ""):
        return None
    try:
        parsed = json.loads(await request.body())
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


async def _form_field(request: Request, name: str) -> str | None:
    if "application/x-www-form-urlencoded" not in request.headers.get("content-type", ""):
        return None
    try:
        # Read the bytes first so they are cached on the request: parsing the
        # form straight off the stream would consume it, and the route that
        # verifies the signature over the raw body still has to read it.
        try:
            await request.body()
        except RuntimeError:
            # A route that declares `Form()` parameters: FastAPI parsed the form
            # off the stream before any dependency ran, so there are no bytes
            # left to cache — and `form()` below returns that same parse.
            pass
        form = await request.form()
    except Exception:
        return None
    value = form.get(name)
    return value if isinstance(value, str) and value else None


async def account_key(conn: HTTPConnection) -> str:
    """The account a credential attempt names, as a digest.

    An email in the body (login, signup, reset request), a device code in the
    form (the device-grant poll), a token in the path (reset confirm, email
    verification); an already-authenticated call (CLI-token mint, device
    approve) counts against the session's user. With nothing to name, the leg
    collapses onto the caller address — still bounded, just less precisely. That
    key is spelled apart from the address leg's own: the durable hour is one row
    per (class, key), and a shared spelling would count the request twice and
    hold everything the address did against the smaller account ceiling.
    """
    if isinstance(conn, Request):
        body = await _json_body(conn)
        email = body.get("email") if body else None
        if isinstance(email, str) and email.strip():
            return f"acct:{_digest(email.strip().lower())}"
        device_code = await _form_field(conn, "device_code")
        if device_code:
            return f"acct:dev:{_digest(device_code)}"
        token = conn.path_params.get("token")
        if isinstance(token, str) and token:
            return f"acct:tok:{_digest(token)}"
    user = _session_user(conn)
    if user is not None:
        return f"user:{user[0]}"
    return f"acct:ip:{caller_key(conn)}"


async def keys_for(kind: KeyKind, conn: HTTPConnection, *, route_path: str) -> list[str]:
    """Every key one leg of a class counts this request against."""
    if kind is KeyKind.IP:
        return [f"ip:{caller_key(conn)}"]
    if kind is KeyKind.PRINCIPAL:
        return principal_keys(conn)
    if kind is KeyKind.ACCOUNT:
        return [await account_key(conn)]
    if kind is KeyKind.MACHINE:
        return machine_keys(conn)
    if kind is KeyKind.LEASE_NODE:
        return lease_node_keys(conn)
    if kind is KeyKind.AGENT_RESOURCE:
        return agent_resource_keys(conn)
    if kind is KeyKind.ORG:
        return org_keys(conn)
    if kind is KeyKind.TENANT:
        # Never read off the request: a tenant key is trustworthy only after the
        # route verified the delivery, so it is the route that charges it. A
        # class that reached here anyway is counted as its address.
        return [f"ip:{caller_key(conn)}"]
    return [f"source:{route_path}|ip:{caller_key(conn)}"]


# --------------------------------------------------------------------------
# Declaring a class on a route
# --------------------------------------------------------------------------

_MARKER_ATTR: Final = "rate_limit_class"


def limited(name: str) -> Callable[[], Coroutine[Any, Any, None]]:
    """The dependency a route (or a router include) declares its class with:
    ``dependencies=[Depends(limited("chat"))]``. It does nothing at request
    time — the app-level enforcer reads it off the route — so declaring it
    twice, or on a router and again on one of its routes, is harmless: the
    most specific declaration wins. A coroutine, not a plain function: FastAPI
    runs a plain function dependency on a worker thread, and this one is on
    every route, so it would have cost every request a thread hop for nothing.
    """
    if name not in CLASSES:
        raise LookupError(f"{name!r} is not a registered rate-limit class")
    if CLASSES[name].charged_by_route:
        raise LookupError(f"{name!r} is charged by its route, not declared on one")

    async def _marker() -> None:
        return None

    setattr(_marker, _MARKER_ATTR, name)
    _marker.__name__ = f"rate_limit_{name}"
    return _marker


def limited_by_method() -> Callable[[], Coroutine[Any, Any, None]]:
    """An explicit "the default for my method" — a family that declares
    nothing on purpose, so a reader sees a decision rather than an omission."""

    async def _marker() -> None:
        return None

    _marker.__name__ = "rate_limit_by_method"
    return _marker


def declared_class(route: APIRoute | APIWebSocketRoute) -> str | None:
    """The class the route (or its router) declared, most specific last."""
    found: str | None = None
    for dep in route.dependant.dependencies:
        name = getattr(dep.call, _MARKER_ATTR, None)
        if isinstance(name, str):
            found = name
    return found


@dataclass(frozen=True, slots=True)
class ResolvedRoute:
    method: str
    path: str
    rate_class: RateLimitClass
    endpoint: Callable[..., Any] | None


def _default_for(method: str) -> str:
    return DEFAULT_READ if method in _READ_METHODS else DEFAULT_MUTATION


def resolve_routes(routes: list[BaseRoute], *, prefix: str = "") -> Iterator[ResolvedRoute]:
    """Every route of the app with the class it resolves to, mounts included."""
    for route in routes:
        if isinstance(route, Mount):
            # A mounted app has its own middleware stack and no enforcer of
            # ours; the content origin is the one mount, and it is exempt by
            # design (signed bytes, budgeted at the edge).
            for inner in resolve_routes(list(route.routes), prefix=prefix + route.path):
                yield ResolvedRoute(inner.method, inner.path, CLASSES["exempt"], inner.endpoint)
            continue
        if isinstance(route, APIWebSocketRoute):
            path = prefix + route.path
            name = declared_class(route) or _default_for("GET")
            yield ResolvedRoute("WS", path, CLASSES[name], route.endpoint)
            continue
        if isinstance(route, APIRoute):
            path = prefix + route.path
            exempt = path.startswith(EXEMPT_PREFIXES)
            for method in sorted(route.methods or ()):
                if exempt:
                    name = "exempt"
                else:
                    name = declared_class(route) or _default_for(method)
                yield ResolvedRoute(method, path, CLASSES[name], route.endpoint)
            continue
        # A plain Starlette route: the docs and the raw OpenAPI (local only),
        # `/metrics` (bearer-guarded, a scraper's cadence). Not an API call.
        yield ResolvedRoute("*", prefix + getattr(route, "path", "?"), CLASSES["exempt"], None)


# --------------------------------------------------------------------------
# The registry: buckets, clocks, and the route table the enforcer reads
# --------------------------------------------------------------------------


class RateLimitRegistry:
    """The process's throttling state: one limiter per (class, leg), the
    clocks every limiter reads, the durable window, and the endpoint table
    ``install`` pins."""

    def __init__(
        self,
        *,
        clock: Clock = time.monotonic,
        wall: WallClock = lambda: datetime.now(UTC),
        durable: DurableWindow | None = None,
    ) -> None:
        self.clock = clock
        self.wall = wall
        self.durable = durable if durable is not None else DurableWindow()
        self._limiters: dict[tuple[str, int], TokenBucketLimiter] = {}
        self._by_endpoint: dict[tuple[Callable[..., Any], str], RateLimitClass] = {}
        self._route_paths: dict[Callable[..., Any], str] = {}

    def limiter(self, rate_class: str, leg: int = 0) -> TokenBucketLimiter:
        cls = CLASSES[rate_class]
        found = self._limiters.get((rate_class, leg))
        if found is None:
            spec = cls.legs[leg]
            found = TokenBucketLimiter(
                per_minute=spec.per_minute, burst=spec.burst, clock=lambda: self.clock()
            )
            self._limiters[(rate_class, leg)] = found
        return found

    def install(self, app: FastAPI) -> list[ResolvedRoute]:
        """Pin every route's class. Called once, after the last include."""
        resolved = list(resolve_routes(app.routes))
        for entry in resolved:
            if entry.endpoint is None:
                continue
            self._by_endpoint[(entry.endpoint, entry.method)] = entry.rate_class
            self._route_paths.setdefault(entry.endpoint, entry.path)
        return resolved

    def class_for(self, endpoint: Callable[..., Any] | None, method: str) -> RateLimitClass:
        """The class for a request: what ``install`` pinned, else the default
        for the method — a route added after install is still never unlimited."""
        if endpoint is not None:
            found = self._by_endpoint.get((endpoint, method))
            if found is not None:
                return found
        return CLASSES[_default_for(method)]

    def route_path(self, endpoint: Callable[..., Any] | None, *, fallback: str) -> str:
        """The route template (``/api/v1/chats/{chat_id}``) for a log line, never
        the concrete path — an id in a path is somebody's data."""
        if endpoint is None:
            return fallback
        return self._route_paths.get(endpoint, fallback)

    def reset(self) -> None:
        """Forget every bucket, for tests that must not inherit each other's
        spend. The route table is kept — it is a property of the app."""
        for limiter in self._limiters.values():
            limiter.reset()
        self.durable.reset()

    async def check(
        self, cls: RateLimitClass, conn: HTTPConnection, *, route_path: str
    ) -> Refusal | None:
        """Spend one request across every key of every leg of ``cls``, or say
        which refused.

        Every key is asked first and spent only when all of them admit: a
        request one key refuses costs the others nothing, so a spray from an
        exhausted address at an account it names does not also drain that
        account's own budget. The durable hour is counted after the in-memory
        keys admit, for the same reason.
        """
        if cls.exempt:
            return None
        now = self.clock()
        wall_now = self.wall()
        planned: list[tuple[int, Leg, str, Decision]] = []
        worst: Refusal | None = None
        for index, leg in enumerate(cls.legs):
            limiter = self.limiter(cls.name, index)
            for key in await keys_for(leg.kind, conn, route_path=route_path):
                decision = limiter.consume(key, now=now, spend=False)
                if decision.allowed:
                    planned.append((index, leg, key, decision))
                    continue
                refusal = Refusal(cls, key, decision.retry_after, decision)
                if worst is None or refusal.retry_after > worst.retry_after:
                    worst = refusal
        if worst is not None:
            return worst
        for index, _leg, key, _ in planned:
            self.limiter(cls.name, index).consume(key, now=now)
        for _, leg, key, decision in planned:
            if leg.per_hour is None:
                continue
            hourly = await self._durable_check(cls, leg, key, decision, now=wall_now)
            if hourly is not None and (worst is None or hourly.retry_after > worst.retry_after):
                worst = hourly
        return worst

    async def _durable_check(
        self,
        cls: RateLimitClass,
        leg: Leg,
        key: str,
        decision: Decision,
        *,
        now: datetime,
    ) -> Refusal | None:
        try:
            count = await self.durable.hit(cls.name, key, now=now)
        except DurableBusy:
            log.warning("ratelimit.durable_busy", rate_class=cls.name, refused=cls.refuse_when_busy)
            if not cls.refuse_when_busy:
                return None
            return Refusal(cls, key, 1.0, Decision(False, decision.limit, 0, 1.0, decision.reset))
        except (SQLAlchemyError, PostgresError, OSError):
            log.warning("ratelimit.durable_unavailable", rate_class=cls.name, exc_info=True)
            return None
        ceiling = max(1, leg.per_hour()) if leg.per_hour is not None else 0
        if count <= ceiling:
            return None
        window_end = now.replace(minute=0, second=0, microsecond=0) + _DURABLE_WINDOW
        remaining_in_window = max(1.0, (window_end - now).total_seconds())
        overflow = count - ceiling
        backoff = min(
            _BACKOFF_CEILING_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** min(overflow - 1, 10))
        )
        wait = min(remaining_in_window, backoff)
        return Refusal(
            cls,
            key,
            wait,
            Decision(False, decision.limit, 0, wait, decision.reset),
            hourly=True,
        )


@dataclass(frozen=True, slots=True)
class Refusal:
    rate_class: RateLimitClass
    key: str
    retry_after: float
    decision: Decision
    hourly: bool = False

    @property
    def retry_after_seconds(self) -> int:
        return max(1, math.ceil(self.retry_after))

    def headers(self) -> dict[str, str]:
        return {
            "Retry-After": str(self.retry_after_seconds),
            "RateLimit-Limit": str(self.decision.limit),
            "RateLimit-Remaining": str(max(0, self.decision.remaining)),
            "RateLimit-Reset": str(max(1, math.ceil(self.decision.reset))),
        }

    def exception(self) -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": REFUSAL_CODE,
                "message": self.rate_class.message,
                "retry_after": self.retry_after_seconds,
            },
            headers=self.headers(),
        )


REGISTRY: Final = RateLimitRegistry()


def _log_refusal(refusal: Refusal, *, method: str, path: str) -> None:
    log.warning(
        "ratelimit.refused",
        rate_class=refusal.rate_class.name,
        key_digest=_digest(refusal.key)[:16],
        hourly=refusal.hourly,
        method=method,
        route=path,
        retry_after=refusal.retry_after_seconds,
    )


async def enforce_rate_limit(conn: HTTPConnection) -> None:
    """The app-level dependency: runs first on every HTTP route, before any
    session is opened. A socket handshake is not counted here — the socket
    route charges its open itself once the ticket is admitted, so the budget
    is the user's, and its frames never reach a bucket.
    """
    if not settings.rate_limit_enabled or conn.scope.get("type") != "http":
        return
    method = str(conn.scope.get("method", "GET")).upper()
    endpoint = conn.scope.get("endpoint")
    cls = REGISTRY.class_for(endpoint, method)
    if cls.exempt:
        return
    if cls.name in AGENT_REKEYED and _machine_credential(conn):
        cls = CLASSES["box"]
    elif cls.name in AGENT_REKEYED or _keys_on_the_machine(cls):
        machine = await resolve_verified_machine(conn)
        if machine is not None and cls.name in AGENT_REKEYED:
            cls = CLASSES["agent"]
    path = REGISTRY.route_path(endpoint, fallback=str(conn.scope.get("path", "")))
    refusal = await REGISTRY.check(cls, conn, route_path=path)
    if refusal is None:
        return
    _log_refusal(refusal, method=method, path=path)
    raise refusal.exception()


def _machine_credential(conn: HTTPConnection) -> bool:
    """Whether the request carries a machine credential as its bearer."""
    bearer = None if conn.cookies.get(COOKIE_NAME) else _bearer(conn)
    return bearer is not None and looks_like_machine_token(bearer)


def _keys_on_the_machine(cls: RateLimitClass) -> bool:
    return any(
        leg.kind in (KeyKind.MACHINE, KeyKind.LEASE_NODE, KeyKind.AGENT_RESOURCE)
        for leg in cls.legs
    )


async def charge_stream_open(conn: HTTPConnection, *, key: str) -> Refusal | None:
    """Charge one ``stream_open`` for an admitted socket, keyed by the user it
    was admitted as. Returns the refusal for the route to answer in its own
    vocabulary (a close code, not a 429)."""
    if not settings.rate_limit_enabled:
        return None
    cls = CLASSES["stream_open"]
    decision = REGISTRY.limiter(cls.name).consume(key, now=REGISTRY.clock())
    if decision.allowed:
        return None
    refusal = Refusal(cls, key, decision.retry_after, decision)
    _log_refusal(refusal, method="WS", path=str(conn.scope.get("path", "")))
    return refusal


def page_grant_admits(conn: HTTPConnection, nonce: str) -> bool:
    """Whether one more request may be served under ``nonce`` from this address.

    Keyed on the pair rather than on either half. The nonce alone would let one
    reader's open page throttle every other reader of the same grant, and the
    address alone is the whole content origin's budget — which a page legitimately
    spends in a burst, one request per asset. The pair bounds what a *leaked* URL
    buys: a holder of somebody else's page URL cannot walk the folder behind it
    faster than a real reader loads a document.

    A refusal is not a ``429`` here: the content origin answers every refusal with
    the same opaque ``404``, and a throttle that identified itself would tell a
    prober their nonce is real.
    """
    return REGISTRY.limiter("page_grant").consume(f"{nonce}|{caller_key(conn)}").allowed


def _webhook_source(conn: HTTPConnection, provider: str) -> str:
    return f"{provider}|ip:{caller_key(conn)}"


def charge_webhook_rejection(conn: HTTPConnection, *, provider: str) -> Refusal | None:
    """Charge one failed signature to the source it came from -- never to the
    tenant its body named -- and answer the refusal once that source has spent
    its failures for ``provider``.

    Called only AFTER the signature has been checked and has failed. A valid
    signature never consults this budget: a provider posts every customer's
    webhooks from one shared pool of addresses, and anyone can point their own
    Stripe or GitHub webhook at this URL, so a budget that refused before the
    signature would let a stranger's forgeries from that pool shut out our
    genuine deliveries from the same address. Keyed per provider rather than
    per route: Slack's two endpoints share one secret, so they share one budget.
    """
    if not settings.rate_limit_enabled:
        return None
    cls = CLASSES["webhook_reject"]
    key = _webhook_source(conn, provider)
    decision = REGISTRY.limiter(cls.name).consume(key, now=REGISTRY.clock())
    if decision.allowed:
        return None
    refusal = Refusal(cls, key, decision.retry_after, decision)
    _log_refusal(refusal, method="POST", path=str(conn.scope.get("path", "")))
    return refusal


def charge_webhook_tenant(conn: HTTPConnection, *, provider: str, tenant: str) -> Refusal | None:
    """Spend one delivery from the budget of the tenant a VERIFIED delivery
    names; the refusal when that budget is spent.

    Called only after the signature has checked out, so the tenant is the one
    the provider vouched for. Not keyed on the address: a provider posts from a
    pool, and a tenant's budget must not grow with the size of that pool.
    """
    if not settings.rate_limit_enabled:
        return None
    cls = CLASSES["webhook_tenant"]
    key = f"{provider}:{tenant}"
    decision = REGISTRY.limiter(cls.name).consume(key, now=REGISTRY.clock())
    if decision.allowed:
        return None
    refusal = Refusal(cls, key, decision.retry_after, decision)
    _log_refusal(refusal, method="POST", path=str(conn.scope.get("path", "")))
    return refusal


def install(app: FastAPI) -> list[ResolvedRoute]:
    return REGISTRY.install(app)


def reset_all_limiters() -> None:
    """Forget every bucket in every limiter. Called by an autouse test fixture so
    one test's burst cannot throttle the next one."""
    REGISTRY.reset()
    machine_assertion.CACHE.reset()


__all__ = [
    "CLASSES",
    "DEFAULT_MUTATION",
    "DEFAULT_READ",
    "EXEMPT_PREFIXES",
    "REFUSAL_CODE",
    "REGISTRY",
    "Clock",
    "Decision",
    "DurableWindow",
    "KeyKind",
    "Leg",
    "RateLimitClass",
    "RateLimitRegistry",
    "Refusal",
    "ResolvedRoute",
    "TokenBucketLimiter",
    "WallClock",
    "account_key",
    "caller_key",
    "charge_stream_open",
    "charge_webhook_rejection",
    "charge_webhook_tenant",
    "declared_class",
    "enforce_rate_limit",
    "install",
    "keys_for",
    "lease_node_keys",
    "limited",
    "limited_by_method",
    "machine_keys",
    "org_keys",
    "page_grant_admits",
    "principal_keys",
    "reset_all_limiters",
    "resolve_routes",
    "resolve_verified_machine",
    "verified_agent_id",
]
