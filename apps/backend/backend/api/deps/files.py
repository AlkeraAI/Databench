"""The dependencies every Files route declares, and nothing else.

Each one turns a header into a typed value or raises the typed library failure
the handler in ``backend.api.deps.files_errors`` already knows how to answer. A route that parsed
``If-Match`` itself would be a route that could forget the 428, so the parsing
lives here and the route only names the dependency.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from json import dumps as json_dumps
from typing import Annotated, Any, Final

from alkera_core.authz.decision import Decision
from alkera_core.authz.enums import Action
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource
from alkera_core.config import settings
from alkera_core.db.tenant_session import outer_role
from alkera_core.db.unwind import restore_while_unwinding
from alkera_core.files.authz.authorize import Enforcer
from alkera_core.files.clock import SystemClock
from alkera_core.files.db_retry import RetryBudget, with_db_retries
from alkera_core.files.errors import InvalidRequest, NotFound
from alkera_core.files.idempotency import MissingIdempotencyKey, StoredResponse, idempotent
from alkera_core.files.repo import APP_ROLE, ORG_SETTING, FilesRepo
from fastapi import BackgroundTasks, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps.files_context import FilesCtx
from backend.api.deps.files_errors import files_error_for
from backend.api.rate_limit import limited, limited_by_method
from backend.authz import enforce as platform_enforce
from backend.authz.enforce import DecisionSink
from backend.services.files.context import FilesContext, restart_attempt
from backend.services.realtime import flush_before_trash

#: A Files route handler, as the wrapper sees it: FastAPI calls an endpoint
#: with keyword arguments only, so the wrapper can name what it needs.
_Handler = Callable[..., Awaitable[Any]]

#: The header a fenced write carries its epoch in.
LEASE_EPOCH_HEADER = "X-Alkera-Lease-Epoch"
#: The header naming which instance of the holder is writing.
LEASE_INSTANCE_HEADER = "X-Alkera-Lease-Instance"
#: Sent by a holder whose ``If-Match`` names the version its bytes were made on
#: (a box from before that fix fenced an edit on the drive's head when it no
#: longer knew its base, so a live document cannot trust its precondition).
LEASE_BASE_HEADER = "X-Alkera-Lease-Base"

#: What a mutating Files route replays a `40001` / `40P01` under. Three total
#: attempts because the two collisions this answers are pairwise: a third
#: writer losing twice in a row is rarer than the client timeout, and `window`
#: is what keeps the ladder inside it rather than holding a request open.
FILES_RETRY_BUDGET: Final = RetryBudget(attempts=3, base_delay=0.02, max_delay=0.2)

#: The routes that are naturally idempotent and so need no key. A heartbeat is
#: state-based, and replaying it is the point. A live report is the same shape
#: at the same cadence: it says what the holder is doing to each node right now,
#: a resend clears nothing twice, and a report refused for a missing key is
#: worse than one applied twice — the drop a person made into the chat would
#: stay owed forever.
#: The tree report carries its own idempotency key in its body (``batch_id``),
#: remembered per lease, so a header key would be a second one to agree with.
#: The digest request is a read that happens to carry a body.
IDEMPOTENCY_EXEMPT_SUFFIXES: tuple[str, ...] = (
    "/lease/heartbeat",
    "/leases/heartbeat",
    "/lease/live",
    "/lease/tree",
    "/lease/tree/digests",
)


async def require_files_enabled() -> None:
    """The kill switch. Declared as a coroutine, like every dependency in this
       module that only reads settings or headers: FastAPI runs a plain function
       dependency on a worker thread, and a request that waits for a thread on
       every hop is a request that stalls whenever the pool is busy elsewhere.
    The routes are always mounted — so the OpenAPI surface,
       and therefore the SDKs, carry them — but with ``files_enabled`` off every
       one of them answers the same opaque 404 as a node that does not exist. A
       probe cannot tell a dark deployment from an empty one."""
    if not settings.files_enabled:
        raise NotFound()


#: Step out of the Files role for exactly the platform's own statements, to the
#: session's outer role (``tenant_session.outer_role``: the tenant role on a
#: bound request), and read back the org the Files stamp binds RLS to in the
#: same round trip. One statement, so the window's entry costs the same on every
#: path through it.
_STEP_OUT = "SELECT current_setting(:name, true), set_config('role', :role, true)"
#: Step back in. Two shapes, both a single statement: after a denial the
#: transaction the window opened in is gone, so the org setting went with it
#: and has to be restored beside the role — see :func:`restamp`.
_STEP_IN_ROLE = "SELECT set_config('role', :role, true)"
_STEP_IN_STAMP = "SELECT set_config('role', :role, true), set_config(:name, :org, true)"


async def restamp(session: AsyncSession, org: str | None) -> None:
    """Put the tenant stamp back after stepping out of it, in one statement.

    The role and the org GUC are both ``SET LOCAL``, so Postgres reverts both
    when the (sub)transaction they were set in aborts — and a refusal aborts
    one: a batch item's savepoint rolls back, and a route-level denial rolls
    the whole request back before the sink writes its row. Whatever runs next
    on that session would then read as nobody in particular and see nothing,
    which is a 404 for a node the caller owns.

    Restoring the role without the org beside it is worse than not restoring
    it at all: the reader is the Files role again, with no org for RLS to bind
    to, so it still sees nothing — and now silently. The two travel together.

    They travel together *in one statement* for a second reason. A refusal must
    cost exactly what a success costs minus its payload, and this call sits on
    both: a restore costing two statements after a refusal and one after a
    success would hand a caller who can count queries the very distinction the
    404 body refuses to make.
    """
    if org is None:
        await session.execute(text(_STEP_IN_ROLE), {"role": APP_ROLE})
        return
    await session.execute(text(_STEP_IN_STAMP), {"role": APP_ROLE, "name": ORG_SETTING, "org": org})


@asynccontextmanager
async def as_platform(session: AsyncSession) -> AsyncIterator[None]:
    """Run a few statements as the platform rather than as the tenant role.

    The Files transaction runs as ``alkera_files_app``, which by design can see
    nothing but the Files tables — that is what makes RLS bind. Two things a
    route needs are deliberately *not* Files tables: the team rows the platform
    role resolver reads, and the ``authz.decision`` row ``enforce`` writes,
    which belongs to the platform's audit stream and not to the tenant. Both
    step out of the role for exactly their own statements and step back in, so
    nothing else in the transaction gains a privilege.

    It lives here, not in one family, because every mutating handler now runs
    its whole body inside the idempotency claim's transaction — so every
    family's facts read and every family's decision happen under the Files
    role, and each one steps out through this same window.

    The window steps back in on *both* ways out, and pays the same single
    statement for it either way: the stamp restored carries the org beside the
    role, because a role with no org setting beside it reads nothing.
    """
    org = (
        await session.execute(text(_STEP_OUT), {"name": ORG_SETTING, "role": outer_role(session)})
    ).scalar_one()
    try:
        yield
    except BaseException as error:
        # A refusal leaves the transaction usable and is restamped; a database
        # error (a statement timeout, a failed flush) killed it, and restamping
        # would only replace the retryable error with 25P02 or
        # PendingRollbackError — a 500 where the client was owed a 503.
        await restore_while_unwinding(error, lambda: restamp(session, org))
        raise
    if not session.in_transaction():
        # A block that ended the transaction ended the window with it: both
        # `SET LOCAL`s died at the COMMIT, and restoring them now would open a
        # fresh transaction whose stamp dies with *it* — leaving the rest of
        # the request as the platform role with no org for RLS to bind to,
        # which is the fail-open direction. Refuse loudly instead.
        raise RuntimeError(
            "as_platform: the block ended the transaction the window was opened in, "
            "so the tenant role and org stamp cannot be restored"
        )
    await restamp(session, org)


def platform_wrap(session: AsyncSession) -> Callable[[], AbstractAsyncContextManager[None]]:
    """``as_platform`` in the shape :func:`files_enforcer` takes it."""
    return lambda: as_platform(session)


def files_enforcer(
    request: Request,
    db: AsyncSession,
    *,
    wrap: Callable[[], AbstractAsyncContextManager[None]] | None = None,
    sink: DecisionSink | None = None,
) -> Enforcer:
    """The platform decision engine as the Files library's ``Enforcer``.

    Every Files family needs the same three things and must not spell them
    differently: the engine bound to this request and session, the decision row
    it writes left exactly where it was, and its refusal re-raised as the typed
    Files failure so the one handler in ``backend.api.deps.files_errors`` renders it. A family that
    bound the engine itself would answer a policy denial in the platform
    envelope, which would be an existence oracle — so the binding
    lives here and a route only names it.

    ``wrap`` is for the families whose transaction runs as the restricted Files
    role: the engine reads team rows and writes an ``authz.decision`` row, and
    neither is a Files table, so those callers pass the context manager that
    steps out of the role for exactly the engine's statements.

    ``sink`` is where the decision row goes; the default writes an allow in the
    request's transaction and a denial in a session of its own, and never
    touches the request's transaction either way, so a batch that refuses one
    item keeps the items it already wrote.
    """

    async def run(
        ctx: ActingContext,
        action: Action,
        resource: Resource,
        attrs: Mapping[str, object],
    ) -> Decision:
        try:
            if wrap is None:
                return await platform_enforce(request, db, ctx, action, resource, attrs, sink=sink)
            async with wrap():
                return await platform_enforce(request, db, ctx, action, resource, attrs, sink=sink)
        except HTTPException as refused:
            raise files_error_for(refused) from None

    return run


@dataclass(frozen=True, slots=True)
class IdempotencyKey:
    """A validated key and the fingerprint of the request it was sent with."""

    key: str
    request_hash: bytes


async def idempotency(request: Request) -> IdempotencyKey | None:
    """The ``Idempotency-Key`` for a non-GET, with the body already hashed.

    ``None`` for a safe method, so a GET route may declare the dependency
    without a special case. A non-GET with no key is a 428: the request is
    otherwise valid and becomes acceptable the moment the header is added.

    The hash covers method, path and body, which is what makes a replay under
    the same key with a *different* request a 422 rather than a silent replay
    of an answer to a question nobody asked.
    """
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return None
    if request.url.path.endswith(IDEMPOTENCY_EXEMPT_SUFFIXES):
        return None
    raw = request.headers.get("Idempotency-Key")
    if not raw or not raw.strip():
        raise MissingIdempotencyKey()
    body = await request.body()
    digest = hashlib.sha256()
    digest.update(request.method.encode())
    digest.update(b"\0")
    digest.update(request.url.path.encode())
    digest.update(b"\0")
    digest.update(body)
    return IdempotencyKey(key=raw.strip(), request_hash=digest.digest())


async def idempotency_header(request: Request) -> str:
    """The ``Idempotency-Key`` a streamed request must carry, and never its body.

    For a route whose body has to stream and whose replay safety comes from
    somewhere else (a part's stored checksum): fingerprinting the body, as
    :func:`idempotency` does, reads every byte of it into memory before the
    route runs.
    """
    raw = request.headers.get("Idempotency-Key")
    if not raw or not raw.strip():
        raise MissingIdempotencyKey()
    return raw.strip()


async def if_match(request: Request) -> int | None:
    """The ``If-Match`` etag as the integer counter the node carries.

    ``None`` for a safe method. A mutation with no ``If-Match`` is a 428 for the
    same reason a missing idempotency key is: the caller must say which version
    it believes it is changing, and adding the header makes the request valid.
    A malformed etag is a 422 — it is a header the caller got wrong, not one
    they omitted.

    ``POST`` is required too, for the reason the rule exists: a grant, a
    restore or a conflict resolution written blind onto a node that moved
    under the caller is exactly what the precondition refuses. There is no
    mutating method the header is optional on.
    """
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        raw = request.headers.get("If-Match")
        return parse_etag(raw) if raw else None
    raw = request.headers.get("If-Match")
    if raw is None or not raw.strip():
        raise MissingIdempotencyKey(
            "files.if_match_required",
            "This mutation requires an If-Match header. Its value is the node's etag — "
            "the `ETag` response header a GET of the item returns, which is also the "
            "item body's `etag` field.",
        )
    return parse_etag(raw)


def parse_etag(raw: str) -> int:
    """One `If-Match` header as the integer counter a node's etag is.

    Public because the parsing is the contract, not an implementation detail:
    the upload completion path reads the same header on a body that makes it
    optional, and a second spelling of `W/"7"` is a second place to get it
    wrong.
    """
    text = raw.strip()
    if text.startswith("W/"):
        text = text[2:]
    text = text.strip('"')
    try:
        return int(text)
    except ValueError:
        raise InvalidRequest(
            "files.bad_if_match", "If-Match must be an etag this server issued"
        ) from None


#: The name `uploads.py` imported before the parser was public. Kept for this
#: batch so the two changes land independently; the alias goes when that import
#: is updated.
_parse_etag = parse_etag


@dataclass(frozen=True, slots=True)
class LeaseContext:
    """What a fenced write claims about the lease it is writing under.

    Both headers or neither: an epoch with no instance cannot be fenced (the
    library needs to know *which* holder), so a half-set pair is refused here
    rather than silently treated as unfenced.
    """

    epoch: int | None = None
    instance: str | None = None
    #: The holder said its ``If-Match`` is the version its bytes were made on.
    base_known: bool = False

    @property
    def fenced(self) -> bool:
        return self.epoch is not None


async def lease_context(request: Request) -> LeaseContext:
    """The lease headers, parsed. Absent on an ordinary write.

    Two headers and no third: a request cannot say that it is the push its
    release is applying. That exemption from the drive's ceilings belongs to
    the release, which the server applies itself, so there is nothing here for
    a holder to assert — and a holder that could assert it on every write
    would never be refused by a ceiling again.
    """
    raw_epoch = request.headers.get(LEASE_EPOCH_HEADER)
    instance = request.headers.get(LEASE_INSTANCE_HEADER)
    if raw_epoch is None and instance is None:
        return LeaseContext()
    if raw_epoch is None or instance is None or not instance.strip():
        raise InvalidRequest(
            "files.bad_lease_headers",
            "A fenced write carries both the lease epoch and the lease instance",
        )
    try:
        epoch = int(raw_epoch.strip())
    except ValueError:
        raise InvalidRequest(
            "files.bad_lease_headers", "The lease epoch must be an integer"
        ) from None
    return LeaseContext(
        epoch=epoch,
        instance=instance.strip(),
        base_known=request.headers.get(LEASE_BASE_HEADER, "").strip() == "agreed",
    )


#: Which rate-limit class each Files route family declares. A family not named
#: here takes the default for its method (``read`` for GET, ``mutation``
#: otherwise), so exhausting uploads never locks a caller out of listing their
#: own tree, and a family that lands without a row is still bounded.
_FAMILY_CLASSES: dict[str, str] = {
    # An upload-session open, an inline content write, a child or tree create
    # and the drag-and-drop batch all count as an upload the principal started.
    "uploads": "upload",
    "content_put": "upload",
    "creates": "upload",
    "bulk": "upload",
    # Parts, completes and aborts ride the session that admitted them.
    "upload_parts": "upload_part",
    # The batched item read is a POST only because its ids do not fit a query
    # string; it reads, so it spends the read budget rather than the method's
    # default mutation budget.
    "lookup": "read",
    # A drop polls one commit per file it landed; on the read budget those
    # polls alone refused the tail of a five-hundred-file drop. Only the POLL:
    # cancel and undo are ordinary writes a person makes one at a time, and the
    # whole family on this class would hand them fifty times the mutation
    # budget for a reason that is only ever about following queued work.
    "operations.poll": "operation",
    "operations.write": "mutation",
    # The daemon's lease heartbeat, its snapshot and delta feeds: machine
    # traffic, keyed by the machine credential so a busy box never starves its
    # owner's browser.
    "leases.heartbeat": "machine",
    "leases.snapshots": "machine",
    # The live plane is the busiest of the three: one report per debounce window
    # for every file the agent touches. Keyed per FOLDER rather than by the
    # machine credential the rest of the box's traffic rides — a box holds one
    # lease per chat, and on a single machine budget the chat writing hardest
    # would starve every other chat that box is running.
    "leases.live": "lease_node",
    # The tree report: the same per-folder key, on a budget of its own, so a
    # clone's burst of reports never spends what the folder's live reports and
    # drains run on.
    "leases.tree": "lease_tree",
    "delta": "machine",
    # A box reading and writing a file's live document as its text peer: one
    # call per agent save, keyed by the machine like the rest of its traffic.
    "live_text": "machine",
}


def ratelimited(name: str, *, limit: int | None = None) -> Callable[[], Coroutine[Any, Any, None]]:
    """The rate-limit class a Files route family declares (see
    ``backend.api.rate_limit``). ``limit`` is accepted for the families that
    used to carry their own number and is no longer read: the numbers live on
    the class, one knob per class."""
    del limit
    rate_class = _FAMILY_CLASSES.get(name)
    if rate_class is None:
        return limited_by_method()
    return limited(rate_class)


async def caller_drive(files: FilesCtx, drive_id: str) -> None:
    """Refuse a drive that is not the caller's org's, before anything is read.

    Most Files routes make this check on their first handler line, which is
    enough while every field of the request body is optional: FastAPI validates
    the body BEFORE it calls the endpoint, so a body the caller got wrong is
    answered 422 and the handler never runs. The moment a route has a required
    or bounded field, that 422 becomes the answer a stranger's request gets
    where every other route in the family answers the opaque 404 -- one route
    telling a different story about the same drive id, which is the seam a
    tenancy probe looks for.

    Declared as a DEPENDENCY so it runs first whatever the body is:
    ``solve_dependencies`` calls a route's dependencies before it validates any
    body parameter, so a drive that is not this org's is refused with the
    library's own ``NotFound`` and the request is over. A route with a required
    body wants this rather than the handler line.

    A coroutine, not a plain function: FastAPI resolves a plain-function
    dependency on a worker thread, and there is nothing here to wait for — a
    uuid is parsed and two of them are compared.
    """
    try:
        named = uuid.UUID(drive_id)
    except ValueError:
        # Not an id this server issues, so it names no drive of ours -- and it
        # must say so the same way a well-formed stranger's id does.
        raise NotFound() from None
    if files.drive.id != named:
        raise NotFound()


def idempotent_route(route: str, *, status: int = 200) -> Callable[[_Handler], _Handler]:
    """Run a mutating Files handler at most once per ``Idempotency-Key``.

    Without a replay, a client retrying after a commit-ambiguous timeout would
    run the effect a second time: a second version, a second subtree, a second
    batch, and the byte delta charged twice.

    One wrapper rather than the same six lines in twenty handlers: it names the
    route, hands the handler's own return value to
    :func:`alkera_core.files.idempotency.idempotent` as the answer to store,
    and replays those exact bytes for a repeat of the same key. A second
    request with the same key and a *different* body is the documented 422 the
    library raises; a key-less safe method (or a route the exemption list
    covers) runs the handler untouched.

    The handler is given a context whose repo is *joined*, exactly as
    ``uploads.py`` does: the claim, the effect and the stored answer have to be
    one unit of work — a collaborator that owned its own transaction would
    commit the effect before the answer was recorded, and a replay would then
    find a claim with no answer and run the effect again. The joined handle
    opens a SAVEPOINT and never commits, so the request's session decides, and
    a handler that opens ``files.repo.transaction()`` itself re-enters the same
    unit of work instead of starting a second one.

    ``status`` is the status the route declares, because a handler that returns
    a model never names one; a handler that returns its own ``Response`` keeps
    that response's status and headers.
    """

    def decorate(handler: _Handler) -> _Handler:
        @functools.wraps(handler)
        async def guarded(**kwargs: Any) -> Any:
            key = next((v for v in kwargs.values() if isinstance(v, IdempotencyKey)), None)
            if key is None:
                return await handler(**kwargs)
            found = next(((n, v) for n, v in kwargs.items() if isinstance(v, FilesContext)), None)
            if found is None:
                raise RuntimeError(
                    f"{route}: idempotent_route writes the claim and the answer through the "
                    "handler's own FilesContext, and this handler declares no FilesCtx parameter"
                )
            name, files = found
            session = files.repo.session
            attempts = 0
            # The request's `BackgroundTasks` is one object FastAPI injects
            # once, so a handler that queued inline work and then lost a
            # deadlock would queue it AGAIN on the replay and the customer
            # would get the same operation or notification twice from one
            # click. Each attempt collects into its own, and only the attempt
            # that answered hands its tasks to the request's instance.
            collectors = [n for n, v in kwargs.items() if isinstance(v, BackgroundTasks)]
            staged: dict[str, BackgroundTasks] = {}

            async def once() -> Response:
                nonlocal attempts
                attempts += 1
                if attempts > 1:
                    # Postgres rolled the losing transaction back whole, so the
                    # claim this attempt wrote went with it: the session is
                    # returned to a clean one and the replay re-claims the key
                    # rather than finding it spent with no answer behind it.
                    # The rollback expires the drive the context was built
                    # with, so it is reloaded before the handler reads it.
                    await restart_attempt(files)
                joined = replace(files, repo=FilesRepo.joined(session, files.repo.scope))
                call = {**kwargs, name: joined}
                staged.clear()
                for collector in collectors:
                    fresh = BackgroundTasks()
                    call[collector] = fresh
                    staged[collector] = fresh
                injected = next((v for v in call.values() if isinstance(v, Response)), None)

                async def run() -> StoredResponse:
                    return _stored(await handler(**call), status, injected)

                answered = await idempotent(
                    joined.repo,
                    joined.ctx,
                    route=route,
                    key=key.key,
                    request_hash=key.request_hash,
                    run=run,
                )
                return _replay(answered)

            answered = await with_db_retries(
                once,
                budget=FILES_RETRY_BUDGET,
                clock=SystemClock(),
                sleep=asyncio.sleep,
            )
            for collector, collected in staged.items():
                kwargs[collector].tasks.extend(collected.tasks)
            return answered

        return guarded

    return decorate


#: Headers that describe THIS hop's framing rather than the answer. Replaying a
#: stored ``content-length`` would hand a client the first request's byte count
#: over a body the server re-frames itself; the rest are per-connection.
_PER_HOP_HEADERS = frozenset({"content-length", "connection", "transfer-encoding", "keep-alive"})


def _stored(result: Any, status: int, injected: Response | None = None) -> StoredResponse:
    """A handler's return value as the bytes a replay must hand back.

    Whatever shape the handler answers in, what is stored is what the client
    saw — so a retry gets the same id, the same etag and the same ``Location``
    rather than a re-render of a tree that has moved on since.

    ``injected`` is the handler's own ``Response`` parameter when it declares
    one. FastAPI folds that response's status and headers into the answer it
    renders from a returned model, and it is how a handler says "this one is a
    202" without changing its return type — so the wrapper reads it too, or the
    replay of a queued batch would come back as the 200 the route declares.
    """
    if isinstance(result, Response):
        return StoredResponse(
            status=result.status_code,
            body=bytes(result.body),
            headers=_headers_of(result),
        )
    headers = _headers_of(injected) if injected is not None else {}
    answered = status if injected is None or injected.status_code is None else injected.status_code
    if isinstance(result, BaseModel):
        body = result.model_dump_json(by_alias=True).encode()
    elif isinstance(result, list):
        body = json_dumps([item.model_dump(mode="json", by_alias=True) for item in result]).encode()
    else:
        # A bare dict or ``None``, rendered exactly as FastAPI renders them, so
        # the first answer and its replay are the same bytes.
        body = json_dumps(result).encode()
    return StoredResponse(
        status=answered, body=body, headers={**headers, "content-type": "application/json"}
    )


def _headers_of(response: Response) -> dict[str, str]:
    return {k: v for k, v in response.headers.items() if k.lower() not in _PER_HOP_HEADERS}


def _replay(stored: StoredResponse) -> Response:
    headers = {k: v for k, v in stored.headers.items() if k.lower() != "content-type"}
    return Response(
        content=stored.body,
        status_code=stored.status,
        headers=headers or None,
        media_type=stored.headers.get("content-type", "application/json"),
    )


Idempotency = Annotated[IdempotencyKey | None, Depends(idempotency)]
IdempotencyHeader = Annotated[str, Depends(idempotency_header)]
IfMatch = Annotated[int | None, Depends(if_match)]


async def trash_if_match(
    request: Request, files: FilesCtx, item_id: str, if_match: IfMatch, permanent: bool = False
) -> int:
    """The ``If-Match`` a trash is fenced on (a mutation always carries one).
    Live edits to the item (or to a file under it) not on the drive yet are
    written back first, and that write is the session's own on the version the
    caller named, so the trash is fenced on the version it produced."""
    assert if_match is not None
    try:
        node = uuid.UUID(item_id)
    except ValueError:
        return if_match
    if permanent:
        return if_match
    return await flush_before_trash(request.app, files.repo.scope.org_team_id, node, if_match)


TrashIfMatch = Annotated[int, Depends(trash_if_match)]
Lease = Annotated[LeaseContext, Depends(lease_context)]
FilesEnabled = Depends(require_files_enabled)

__all__ = [
    "IDEMPOTENCY_EXEMPT_SUFFIXES",
    "LEASE_BASE_HEADER",
    "LEASE_EPOCH_HEADER",
    "LEASE_INSTANCE_HEADER",
    "Enforcer",
    "FilesEnabled",
    "Idempotency",
    "IdempotencyHeader",
    "IdempotencyKey",
    "IfMatch",
    "Lease",
    "LeaseContext",
    "TrashIfMatch",
    "as_platform",
    "files_enforcer",
    "idempotency",
    "idempotent_route",
    "if_match",
    "lease_context",
    "platform_wrap",
    "ratelimited",
    "require_files_enabled",
    "restamp",
]
