"""``GET /api/v1/events`` — the portal's server-sent invalidation stream.

The portal learns that something changed from this stream instead of polling.
A frame names WHAT changed (type, entity, id, version) and never the change;
the client refetches through the REST surface it is authorized for, so the
stream cannot leak a field the reader is not entitled to.

The contract, in the order a connection experiences it:

* Authentication is the session cookie or a ``Bearer`` token, exactly as for
  every other route. There is no ``?token=``: a credential in a URL lands in
  every access log between the browser and the process.
* The process must actually be able to deliver. Frames reach a stream only
  through the outbox listener, so a process running without one — the listener
  disabled, or one that has not connected — answers ``503`` with a
  ``Retry-After`` rather than a stream of keepalives that would never carry an
  event. The portal turns its polling off while the stream is up, so a stream
  that reports healthy and delivers nothing stops the whole portal refreshing;
  a refusal keeps it polling. The socket gateway asks the same question of the
  same process (``realtime.runtime.can_deliver``) and refuses in its own
  vocabulary, so one replica is never healthy on one surface and dead on the
  other.
* Capacity is taken before anything else. Past the per-user or per-process cap
  the answer is ``429`` with ``Retry-After``.
* The client's cursor is ``Last-Event-ID`` (what the browser sends on its own
  reconnect) or else ``?after=<id>`` (for a reconnect the client owns, where a
  browser cannot set the header). The subscription to live events is taken
  BEFORE the catch-up read, so nothing committed in between is missed; rows the
  cursor missed are replayed from the outbox, filtered exactly like live ones.
  A cursor past the newest row, or so far behind that a replay would be
  unbounded, gets a ``reset`` instead and the client refetches everything.
* The stream opens with ``retry: 2000`` and ``: connected``, then frames
  ``id: <outbox id> / event: <type> / data: {thin body}``. Every keepalive
  interval it writes ``: keepalive`` and re-checks the session (expiry,
  revocation, deactivation, the verification gate) WITHOUT sliding the idle
  window — an open tab is not activity — and refreshes the entitlement
  snapshot so a membership change takes effect within one tick. A session
  that ended gets ``event: error / {"code": "unauthorized"}`` and the stream
  closes; the reconnect is refused with 401 and the client hands the user to
  login.
* A subscriber that fell behind gets ``event: reset / {"reason": "overflow"}``.
* Outbox ids do not arrive in order. A row that commits after a higher id was
  already read reaches the listener late, by its own id (a straggler), and is
  framed like any other row — with its own ``id:`` — because a connected
  client that never learns of it shows a stale screen. What keeps a row from
  being framed twice (the catch-up read and the hub can both deliver a row
  that committed between the subscribe and the read) is a bounded set of the
  ids this stream has framed, not a high-water mark. Right after a straggler
  the stream writes an ``id:``-only frame carrying the cursor it had, so the
  cursor a client resumes from never moves backwards: a reconnect replays
  only what came after the highest id the client already covers. A straggler
  that commits WHILE the client is disconnected is below that cursor and is
  not replayed; the listener's straggler window is bounded and a resume
  cannot recover it.
* After ``realtime_sse_max_stream_seconds`` the server closes the stream with
  ``retry: 1000`` so a rolling deploy never waits on a connection that would
  otherwise live forever; the browser resumes with its cursor.
* A process that is shutting down cancels its open connections once its
  graceful deadline passes (``--timeout-graceful-shutdown`` on every launch
  line). The stream reads that cancel the same way it reads its own deadline —
  one last ``retry: 1000`` and a clean end of body — so a restart costs the
  browser one reconnect rather than a truncated response.

The request's database session is used only for the catch-up read and is
released before the first byte: the generator runs for up to fifty minutes
and must never pin a pooled connection.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Collection
from dataclasses import dataclass, field
from functools import partial
from typing import Annotated
from uuid import UUID

from alkera_core.auth import (
    SessionClaims,
    TokenRevokedError,
    assert_token_active,
    family_alive,
)
from alkera_core.auth.machine_credential_standing import live_machine_of
from alkera_core.auth.tenancy import claims_stand
from alkera_core.authz import ActingContext, Action
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import (
    EventHub,
    HubEvent,
    ResetMarker,
    Subscription,
    latest_id,
    read_after,
)
from alkera_core.logging import get_logger
from alkera_core.machine_refusals import MACHINE_CREDENTIAL_REFUSED
from alkera_core.models import EventOutbox, User
from alkera_core.schemas.realtime import SseEventData
from alkera_core.verification import is_blocked
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps.machine_standing import decide_machine_standing
from backend.auth.dependencies import (
    CurrentPrincipal,
    DbSession,
    PrincipalUser,
    machine_context_if_standing,
    machine_own_standing,
    optional_session_claims,
)
from backend.services.realtime import runtime as realtime_runtime
from backend.services.realtime import sse
from backend.services.realtime.filters import (
    EntitlementRef,
    MachineScopeRef,
    load_entitlements,
    load_machine_scope,
    machine_routing_predicate,
    machine_stream_predicate,
    stream_predicate,
)
from backend.services.realtime.limits import ConnectionGate, connection_key
from backend.services.realtime.runtime import RealtimeRuntime

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["events"])

SSE_MEDIA_TYPE = "text/event-stream"
#: Rows per catch-up page.
CATCH_UP_PAGE = 500
#: The most rows one reconnect will replay. A cursor further behind than this
#: is answered with a ``reset`` instead: the client refetches what it shows,
#: which is cheaper for everyone than streaming an org's whole history.
MAX_CATCH_UP_ROWS = 5000
#: How long past the stream deadline the watchdog waits before it closes a
#: generator the client stopped reading (parked inside a ``yield`` under ASGI
#: back-pressure, where no code of ours runs again on its own).
WATCHDOG_GRACE_SECONDS = 60.0
#: How long the watchdog keeps retrying to close a generator that is mid-step.
EXPIRE_CLOSE_SECONDS = 10.0
EXPIRE_RETRY_SECONDS = 0.25
RETRY_AFTER_HEADER = {"Retry-After": "5"}


class _StreamLease:
    """What one stream request holds -- its capacity slot, its hub
    subscription, its body -- owned by the REQUEST, not by the body.

    The body generator cannot own them alone: a request can end before its
    body ever runs. After the route returns, the request's database session
    commits and closes (a Postgres round trip) before the first byte is sent,
    and a client that goes away inside that gap cancels the request with the
    response never started. An unstarted generator runs no cleanup, so a slot
    it was meant to give back stayed taken for the life of the process and the
    person's next tab was refused with 429. The lease is a request-scoped
    dependency: its teardown runs however the request ends, closes the body
    if one started, and gives back everything taken, each exactly once."""

    def __init__(self) -> None:
        self._releases: list[Callable[[], None]] = []
        self._body: AsyncGenerator[str, None] | None = None

    def hold(self, release: Callable[[], None]) -> None:
        """Take ownership of something to give back when the request ends."""
        self._releases.append(release)

    def attach(self, body: AsyncGenerator[str, None]) -> AsyncGenerator[str, None]:
        self._body = body
        return body

    def release(self) -> None:
        """Give back everything held, newest first; idempotent."""
        while self._releases:
            self._releases.pop()()

    async def end(self) -> None:
        body, self._body = self._body, None
        try:
            # Close the body BEFORE freeing the slot: a body parked mid-send
            # still holds its subscription and its watchdog.
            if body is not None:
                await body.aclose()
        finally:
            self.release()


async def _stream_lease() -> AsyncIterator[_StreamLease]:
    lease = _StreamLease()
    try:
        yield lease
    finally:
        await lease.end()


#: Request scope (the default): torn down after the response is sent, or when
#: the request is cancelled at any point before or during it.
StreamLease = Annotated[_StreamLease, Depends(_stream_lease)]


@router.get(
    "/events",
    response_model=None,
    response_class=StreamingResponse,
    summary="Subscribe to invalidation events",
    responses={
        200: {
            "model": SseEventData,
            "content": {SSE_MEDIA_TYPE: {"schema": {"type": "string"}}},
            "description": (
                "A text/event-stream of thin invalidation frames "
                "(see packages/api-core/alkera_core/schemas/realtime/README.md). "
                "Each `event: <type>` frame's `data:` is an SseEventData body and its `id:` "
                "is the outbox cursor to resume from; `event: reset` asks the client to "
                "refetch everything; `event: error` precedes a close the client must not retry."
            ),
        },
        429: {"description": "Too many concurrent event streams for this user or process"},
        503: {"description": "Realtime is not running in this process"},
    },
)
async def stream_events(
    request: Request,
    user: PrincipalUser,
    db: DbSession,
    ctx: CurrentPrincipal,
    lease: StreamLease,
    last_event_id: Annotated[
        str | None,
        Header(
            alias="Last-Event-ID",
            description="The browser's own resume cursor; wins over `after`.",
        ),
    ] = None,
    after: Annotated[
        str | None,
        Query(
            description=(
                "Resume after this outbox id when the client owns the reconnect and "
                "cannot set Last-Event-ID. A non-numeric or negative value is ignored."
            ),
        ),
    ] = None,
) -> StreamingResponse:
    runtime = realtime_runtime.runtime_of(request.app)
    if runtime is None or not await realtime_runtime.can_deliver(runtime):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "realtime_unavailable",
                "message": "The event stream is not running in this process.",
            },
            headers=RETRY_AFTER_HEADER,
        )
    if user is None:
        # A box on its own machine credential: no session to re-check, no org
        # to belong to. Its stream is the chats bound to its machine, decided
        # by the same predicate its socket decides with.
        return await _machine_stream(
            request, db, ctx, runtime, lease, last_event_id=last_event_id, after=after
        )
    claims = optional_session_claims(request)
    if claims is None:
        # ``CurrentUser`` decoded this very token a moment ago; a miss here is a
        # bug, and the honest answer to a stream that could not be re-checked
        # is to refuse it.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="session could not be re-read for the stream",
            headers={"WWW-Authenticate": "Cookie"},
        )
    # The stream's org is its credential's, verified by ``CurrentUser``.
    org_id = claims.org_team_id
    gate = ConnectionGate.sse()
    key = await connection_key(user.id, request.headers, org_id=org_id, credential_id=claims.jti)
    if not gate.try_acquire(key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "too_many_streams",
                "message": "Too many open event streams. Close one and try again.",
            },
            headers=RETRY_AFTER_HEADER,
        )
    lease.hold(partial(gate.release, key))
    hub = runtime.hub
    ref = EntitlementRef(await load_entitlements(db, user, org_id=org_id))
    sub = hub.subscribe(
        stream_predicate(user_id=user.id, org_id=org_id, ref=ref),
        label=f"sse:{user.id}",
    )
    lease.hold(partial(hub.unsubscribe, sub))
    cursor = (
        sse.parse_cursor(last_event_id) if last_event_id is not None else sse.parse_cursor(after)
    )
    caught_up = await _catch_up(db, org_id=org_id, cursor=cursor, accept=sub.predicate)
    user_id = user.id

    async def recheck() -> bool:
        return await _recheck(claims, user_id, ref)

    body = lease.attach(
        _bounded(
            _stream(
                hub=hub,
                sub=sub,
                recheck=recheck,
                initial=caught_up.frames,
                # The ids the client already holds: what it named as its cursor and
                # what was just replayed. The hub may deliver any of them again.
                framed=sse.FramedIds(
                    [*([cursor] if cursor is not None else []), *caught_up.framed]
                ),
                resume_id=caught_up.resume_id,
            ),
            release=lease.release,
            key=key,
        )
    )
    return StreamingResponse(
        body,
        media_type=SSE_MEDIA_TYPE,
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


async def _machine_stream(
    request: Request,
    db: AsyncSession,
    ctx: ActingContext,
    runtime: RealtimeRuntime,
    lease: _StreamLease,
    *,
    last_event_id: str | None,
    after: str | None,
    predicate: Callable[..., Callable[[HubEvent], bool]] = machine_stream_predicate,
    kind: str = "machine",
) -> StreamingResponse:
    """The event stream a box holds on its machine credential.

    Counted under the machine, on the key its socket is counted under: it is
    nobody's tab. The subscription and the catch-up read are cut by the one
    predicate the box's socket decides with — the chats bound to its machine,
    in the orgs its credential serves — and the catch-up spans the orgs it
    holds chats in, because a box has no org of its own to read. The tick
    re-asks the credential's standing and re-reads the binding of every chat
    it holds, so a revoked or rotated-away credential ends the stream within
    one keepalive and a chat rebound elsewhere stops reaching it.

    ``predicate`` narrows what the stream carries (the routing stream carries
    only chat doorbells and the machine's own row); ``kind`` keys its slot, so
    a box's routing stream and its full stream are counted apart.
    """
    if ctx.credential_id is None:  # pragma: no cover - a machine principal carries one
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
    credential_id = UUID(ctx.credential_id)
    # The credential must stand behind a live machine to hold a stream at all:
    # one that has claimed none has no chats to hear about, and one revoked
    # between the door and here is refused as the door refuses it.
    held = await live_machine_of(db, credential_id)
    if held is None or str(held) != ctx.acting_principal.id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": MACHINE_CREDENTIAL_REFUSED, "message": "Machine credential refused"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    machine_id = held
    gate = ConnectionGate.sse()
    key = f"{kind}:{ctx.acting_principal.id}"
    if not gate.try_acquire(key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "too_many_streams",
                "message": "Too many open event streams. Close one and try again.",
            },
            headers=RETRY_AFTER_HEADER,
        )
    lease.hold(partial(gate.release, key))
    hub = runtime.hub
    ref = MachineScopeRef(await load_machine_scope(db, ctx))
    sub = hub.subscribe(predicate(ref=ref), label=f"sse:{kind}:{machine_id}")
    lease.hold(partial(hub.unsubscribe, sub))
    cursor = (
        sse.parse_cursor(last_event_id) if last_event_id is not None else sse.parse_cursor(after)
    )
    caught_up = await _catch_up(
        db, org_id=None, cursor=cursor, accept=sub.predicate, org_ids=ref.value.org_ids
    )
    org_id = ctx.org_id
    org_bound = ctx.is_machine_worker

    async def recheck() -> bool:
        return await _recheck_machine(
            credential_id=credential_id,
            machine_id=machine_id,
            org_id=org_id,
            org_bound=org_bound,
            ref=ref,
        )

    body = lease.attach(
        _bounded(
            _stream(
                hub=hub,
                sub=sub,
                recheck=recheck,
                initial=caught_up.frames,
                framed=sse.FramedIds(
                    [*([cursor] if cursor is not None else []), *caught_up.framed]
                ),
                resume_id=caught_up.resume_id,
            ),
            release=lease.release,
            key=key,
        )
    )
    return StreamingResponse(
        body,
        media_type=SSE_MEDIA_TYPE,
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/events/routing",
    response_model=None,
    response_class=StreamingResponse,
    summary="Subscribe to a box's routing events",
    responses={
        200: {
            "model": SseEventData,
            "content": {SSE_MEDIA_TYPE: {"schema": {"type": "string"}}},
            "description": (
                "The event stream of a box's root process, on its machine credential: "
                "`chat.updated` for the chats bound to its machine and its machine's own "
                "row, as the same thin frames `/events` sends. Nothing else."
            ),
        },
        429: {"description": "Too many concurrent event streams for this machine or process"},
        503: {"description": "Realtime is not running in this process"},
    },
)
@machine_own_standing
async def stream_routing_events(
    request: Request,
    db: DbSession,
    ctx: CurrentPrincipal,
    lease: StreamLease,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    after: Annotated[str | None, Query()] = None,
) -> StreamingResponse:
    """The wake signal for a box's root process: which chat of which org has
    something for it, never what. Decided like the routing feed, on the
    machine credential alone; an org-bound worker credential is refused."""
    await decide_machine_standing(
        request,
        db,
        ctx,
        action=Action.READ,
        resource_id="routing",
        org_id=ctx.org_id,
        org_reached=True,
    )
    runtime = realtime_runtime.runtime_of(request.app)
    if runtime is None or not await realtime_runtime.can_deliver(runtime):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "realtime_unavailable",
                "message": "The event stream is not running in this process.",
            },
            headers=RETRY_AFTER_HEADER,
        )
    return await _machine_stream(
        request,
        db,
        ctx,
        runtime,
        lease,
        last_event_id=last_event_id,
        after=after,
        predicate=machine_routing_predicate,
        kind="machine-routing",
    )


async def _recheck_machine(
    *, credential_id: UUID, machine_id: UUID, org_id: UUID, org_bound: bool, ref: MachineScopeRef
) -> bool:
    """Whether the credential behind a box's stream still stands behind its
    machine — the rule every door shares — refreshing the box's scope (the
    orgs it serves, the chats bound to it) on the way. Runs in a short
    session of its own, like the person's re-check."""
    async with AsyncSessionLocal() as db:
        ctx = await machine_context_if_standing(
            db,
            credential_id=credential_id,
            machine_id=machine_id,
            org_id=org_id,
            org_bound=org_bound,
        )
        if ctx is None:
            return False
        ref.value = await load_machine_scope(db, ctx)
    return True


@dataclass(frozen=True, slots=True)
class CatchUp:
    """What a connecting client is sent first, and where it stands after.

    ``frames`` is the replay (or a single ``reset``); ``framed`` the outbox ids
    those frames carry, so the live loop never frames one of them again;
    ``resume_id`` the highest id the client covers once the replay is out —
    everything at or below it was either replayed now or already seen.
    """

    frames: list[str] = field(default_factory=list)
    framed: list[int] = field(default_factory=list)
    resume_id: int = 0


async def _catch_up(
    db: AsyncSession,
    *,
    org_id: UUID | None,
    cursor: int | None,
    accept: Callable[[HubEvent], bool],
    org_ids: Collection[UUID] | None = None,
) -> CatchUp:
    """The frames a reconnecting client missed. With no cursor nothing is
    replayed and the client stands at the current head.

    A person's replay is one org's rows. A box's (``org_ids``) spans the orgs
    it holds chats in — a box has no org of its own — read org by org and put
    back in id order, under the same replay ceiling as one org's."""
    head = await latest_id(db)
    if cursor is None:
        return CatchUp(resume_id=head)
    if cursor > head:
        return CatchUp(frames=[sse.reset_frame(sse.RESET_CURSOR_AHEAD)], resume_id=head)
    frames: list[str] = []
    framed: list[int] = []
    last = cursor
    if org_ids is not None:
        rows_of_all: list[EventOutbox] = []
        for org in org_ids:
            after_id = cursor
            while True:
                rows = await read_after(db, after_id=after_id, org_id=org, limit=CATCH_UP_PAGE)
                rows_of_all.extend(rows)
                if len(rows_of_all) > MAX_CATCH_UP_ROWS:
                    return CatchUp(
                        frames=[sse.reset_frame(sse.RESET_CURSOR_TOO_OLD)], resume_id=head
                    )
                if len(rows) < CATCH_UP_PAGE:
                    break
                after_id = rows[-1].id
        for row in sorted(rows_of_all, key=lambda row: row.id):
            last = row.id
            event = HubEvent.from_outbox(row)
            if accept(event):
                frames.append(sse.event_frame(event))
                framed.append(row.id)
        return CatchUp(frames=frames, framed=framed, resume_id=max(last, head))
    replayed = 0
    while True:
        rows = await read_after(db, after_id=last, org_id=org_id, limit=CATCH_UP_PAGE)
        replayed += len(rows)
        if replayed > MAX_CATCH_UP_ROWS:
            return CatchUp(
                frames=[sse.reset_frame(sse.RESET_CURSOR_TOO_OLD)], resume_id=max(last, head)
            )
        for row in rows:
            last = row.id
            event = HubEvent.from_outbox(row)
            if accept(event):
                frames.append(sse.event_frame(event))
                framed.append(row.id)
        if len(rows) < CATCH_UP_PAGE:
            break
    return CatchUp(frames=frames, framed=framed, resume_id=max(last, head))


async def _recheck(claims: SessionClaims, user_id: UUID, ref: EntitlementRef) -> bool:
    """Whether the session behind an open stream is still good. Runs in a short
    session of its own; refreshes the entitlement snapshot on the way.

    A stream opened with a valid access token stays open until its own
    deadline for as long as the session FAMILY behind that token is alive —
    the access token's own ``exp`` is not what ends it, because the browser
    keeps the session by rotating its refresh cookie, which never reaches an
    open stream. Revocation (the token's jti, and the family) is re-checked on
    every tick, so a revoked family closes the stream within one keepalive. A
    token with no family (a CLI token, a session from before families) falls
    back to its own ``exp`` — the socket makes the same check.

    The tick is NOT activity. Both idle windows are enforced here and neither
    is slid: a stream carries only what the server sends, so a tab left open
    on a desk looks exactly like one somebody is reading, and counting its
    keepalives would keep an unattended session alive to its absolute expiry.
    Only the person's own requests move an idle window.

    The membership the token names is re-verified too, so a deactivation or an
    epoch bump in the stream's org closes it within one keepalive even when
    nobody revoked the token itself."""
    async with AsyncSessionLocal() as db:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if user is None or not user.is_active or is_blocked(user):
            return False
        try:
            await assert_token_active(db, claims, user, slide_idle=False)
        except TokenRevokedError:
            return False
        if not await claims_stand(db, claims, user=user):
            return False
        alive = await family_alive(db, claims)
        if alive is False or (alive is None and time.time() >= claims.expires_at):
            return False
        ref.value = await load_entitlements(db, user, org_id=claims.org_team_id)
    return True


async def _stream(
    *,
    hub: EventHub,
    sub: Subscription,
    recheck: Callable[[], Awaitable[bool]],
    initial: list[str],
    framed: sse.FramedIds,
    resume_id: int,
) -> AsyncGenerator[str, None]:
    """The body: the opening frames, then the hub's events until the deadline,
    re-checking the holder's standing through ``recheck`` on every keepalive —
    a person's session, or a box's credential."""
    loop = asyncio.get_running_loop()
    keepalive = float(settings.realtime_sse_keepalive_seconds)
    deadline = loop.time() + float(settings.realtime_sse_max_stream_seconds)
    next_tick = loop.time() + keepalive
    yield sse.retry_frame(sse.RETRY_MS)
    yield sse.CONNECTED_COMMENT
    for frame in initial:
        yield frame
    try:
        while True:
            now = loop.time()
            if now >= deadline:
                yield sse.retry_frame(sse.RETRY_AFTER_DEADLINE_MS)
                return
            if now >= next_tick:
                yield sse.KEEPALIVE_COMMENT
                try:
                    alive = await recheck()
                except Exception as exc:  # the database, not the session, failed
                    log.warning("realtime.sse.recheck_failed", error=str(exc))
                    yield sse.retry_frame(sse.RETRY_MS)
                    return
                if not alive:
                    yield sse.error_frame(sse.ERROR_UNAUTHORIZED)
                    return
                next_tick = loop.time() + keepalive
                continue
            try:
                item = await asyncio.wait_for(
                    sub.queue.get(), timeout=min(next_tick, deadline) - now
                )
            except TimeoutError:
                continue
            if isinstance(item, ResetMarker):
                hub.ack_reset(sub)
                yield sse.reset_frame(sse.RESET_OVERFLOW)
                continue
            if item.id is None or not framed.add(item.id):
                continue
            yield sse.event_frame(item)
            if item.id > resume_id:
                resume_id = item.id
            else:
                # A straggler: the frame above carried an id below the cursor
                # the client held. Put the cursor back so a reconnect does not
                # replay everything between the two.
                yield sse.cursor_frame(resume_id)
    except asyncio.CancelledError:
        # The process is going away: the server stopped accepting, waited for
        # its open connections, and cancelled this one at its graceful
        # deadline. That is a close, not an error — end the body the way the
        # stream deadline does, so the client comes straight back with its
        # cursor (to the replacement process) instead of seeing a connection
        # reset mid-frame. Swallowing the cancel is what lets the last frame
        # out; the connection ends either way.
        yield sse.retry_frame(sse.RETRY_AFTER_DEADLINE_MS)


async def _bounded(
    gen: AsyncGenerator[str, None],
    *,
    release: Callable[[], None],
    key: str,
) -> AsyncGenerator[str, None]:
    """Free the subscription and the capacity slot (``release``, idempotent)
    as soon as the stream ends, the client disconnects (generator close), or
    the watchdog closes a generator the client stopped reading. The request's
    lease gives them back too, for a body that never ran."""
    loop = asyncio.get_running_loop()

    async def expire() -> None:
        # Close the generator BEFORE freeing the slot: a parked generator still
        # holds its subscription, and a replacement admitted into the freed slot
        # would run against capacity this one never gave back.
        limit = loop.time() + EXPIRE_CLOSE_SECONDS
        while True:
            try:
                await gen.aclose()
                break
            except RuntimeError:
                if loop.time() >= limit:
                    log.warning("realtime.sse.expired_while_running", key=key)
                    break
                await asyncio.sleep(EXPIRE_RETRY_SECONDS)
            except Exception:
                break
        release()

    expiry: asyncio.Task[None] | None = None

    def start_expiry() -> None:
        nonlocal expiry
        expiry = loop.create_task(expire())

    watchdog = loop.call_later(
        max(1.0, float(settings.realtime_sse_max_stream_seconds) + WATCHDOG_GRACE_SECONDS),
        start_expiry,
    )
    try:
        async for chunk in gen:
            yield chunk
    finally:
        watchdog.cancel()
        if expiry is not None and not expiry.done():
            expiry.cancel()
        release()


__all__ = ["router", "stream_events"]
