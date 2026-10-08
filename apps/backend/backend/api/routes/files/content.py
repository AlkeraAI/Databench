"""The API-host half of a file's bytes: write one version, get a URL for one.

Neither route puts a user byte on this origin. A ``PUT`` streams the request
body straight into :meth:`ContentService.put_version` — the body is never
buffered, so a 64 MiB upload costs a block of memory rather than a file of it —
and a ``GET`` answers ``302`` with a short-lived, single-use, session-bound URL
on the content domain, a separate hostname with no session cookie. The bytes
themselves are served by ``content_serve.py`` under that hostname.

Both routes are thin: resolve the principal and the
facts, hand the decision to ``alkera_core.files.authz.authorize`` with the
backend's ``enforce`` injected, then call the library. No status code is spelled
in a branch here — a refusal leaves as whatever :mod:`backend.api.deps.files_errors` maps the
library's exception to, which is what keeps a nonexistent node, another org's
node and a node the caller may not read byte-identical.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Final, Literal, get_args
from uuid import UUID

from alkera_core.authz.enums import PrincipalKind
from alkera_core.authz.principal import ActingContext
from alkera_core.db.session import get_db
from alkera_core.files import lease_snapshots, signed_urls
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.authorize import Authorized, authorize
from alkera_core.files.content import ContentService
from alkera_core.files.errors import Conflict, FilesError, InvalidRequest, NotFound
from alkera_core.files.freshness import LANDING_STATES, ContentState, content_state
from alkera_core.files.ids import DriveId, NodeId, SessionId, VersionId
from alkera_core.files.leases import LeaseContext as LibraryLease
from alkera_core.files.namespace import Namespace
from alkera_core.files.page_grants import mint_page_grant, page_grant_ttl
from alkera_core.files.promotion import PromoteOutcome
from alkera_core.models.compute import ComputeAllocation
from alkera_core.observability.envelope import ErrorEnvelope
from alkera_core.schemas.files.item import Item
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps.files import (
    Idempotency,
    IfMatch,
    Lease,
    LeaseContext,
    as_platform,
    files_enforcer,
    idempotent_route,
    platform_wrap,
    ratelimited,
)
from backend.api.deps.files_context import FilesCtx
from backend.api.deps.files_facts import facts_for
from backend.api.params import PathId
from backend.content_app import disposition_header
from backend.services.files.bytes_on_demand import lease_relative_path, promote_under
from backend.services.files.context import FilesContext
from backend.services.files.items import head_facts, holder_facet, to_item
from backend.services.objects.providers import rendered_providers
from backend.services.realtime.runtime import promoter_for

router = APIRouter(tags=["files"])

#: What ``?conflictBehavior=`` accepts on a content PUT.
#:
#: ``replace`` writes a new version onto the node the caller named — the default,
#: because a content PUT names a node rather than a name, so the caller has
#: already said which file they mean. ``fail`` refuses when that node already
#: holds bytes. ``rename`` renames the INCOMING bytes: they land on a fresh
#: sibling under the next free conflict name, and the node the caller addressed
#: is left exactly as it was. A write must never mutate a node the caller did
#: not name, and a client sending ``rename`` expects its own upload moved aside.
ConflictBehavior = Literal["fail", "replace", "rename"]


#: What ``kind: "page"`` will mint a grant for. A page grant relaxes two
#: headers — what the document may load beside itself, and who may frame it —
#: so it is offered only for the types a reader actually opens as a document.
#: Everything else is a download, which the single-use ``file`` grant already
#: serves; widening this set widens the relaxation, so it is a list rather
#: than a predicate.
#:
#: ``image/svg+xml`` is on it so a drawing can be rendered in a frame on the
#: content origin rather than inside the app: it is served there under the SVG
#: policy (``sandbox`` with no allowances, no script, no network of its own),
#: so the frame relaxation buys it nothing a drawing could use against anyone.
PAGE_MIME_TYPES: frozenset[str] = frozenset(
    {
        "text/html",
        "image/svg+xml",
        "application/pdf",
        "video/mp4",
        "video/webm",
        "audio/mpeg",
        "audio/wav",
    }
)


class ContentGrantRequest(BaseModel):
    """What kind of read the caller is asking to be granted.

    ``file`` is the single-use download URL every client already mints through
    the ``302``; ``page`` is the multi-use grant over the entry's own folder
    that lets a rendered document fetch the images beside it. They are one
    route because they are one decision — ``EXPORT`` on the node the caller
    named — and a client that asks for the wrong one for a node's type is told
    so rather than quietly handed the other.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    kind: Literal["file", "page"]
    disposition: Literal["inline", "attachment"] = "inline"


class ContentGrantResponse(BaseModel):
    """The minted URL, and what it was minted against.

    ``etag`` is the node's, so a client holding a stale item can tell that the
    bytes behind the URL are not the ones it rendered. ``expiresAt`` is the
    deadline the grant itself carries rather than a hint: there is no refresh,
    and a client past it mints again, which re-decides the access.

    ``contentState`` is what the minted bytes are against the disk of the
    machine holding the file's folder: ``on_drive`` (the machine's own copy),
    ``behind`` (the machine has a newer copy the drive could not fetch in time
    — the URL serves the store's older one, as of ``asOf``), or ``none`` for a
    file no machine reports on. A machine that is gone leaves ``on_drive`` and
    ``behind`` as they were: the drive's bytes did not leave with it.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    url: str
    expires_at: datetime
    kind: Literal["file", "page"]
    etag: str
    content_state: ContentState = "none"
    as_of: datetime | None = None


#: What ``Retry-After`` says on a ``files.live_pending`` answer: long enough for
#: a promoted file to have landed, short enough that the reader sees it soon
#: after it does.
PENDING_RETRY_SECONDS: Final = 2
#: The headers a read served from an older copy carries, on the redirect and on
#: the grant alike.
CONTENT_STATE_HEADER: Final = "X-Alkera-Content-State"
CONTENT_AS_OF_HEADER: Final = "X-Alkera-Content-As-Of"

PromoteOutcomeName = Literal[
    "landed",
    "accepted",
    "timed_out",
    "offline",
    "throttled",
    "not_holder",
    "missing",
    "changed",
    "busy",
    "requester",
]
PROMOTE_OUTCOME_NAMES: Final[tuple[str, ...]] = get_args(PromoteOutcomeName)


class LandingProgress(BaseModel):
    """How far the file's open upload has got, when one is open."""

    done_bytes: int
    total_bytes: int


class LivePendingDetail(BaseModel):
    """Why a reader is told the bytes are coming, and from where.

    ``holder`` is the machine the folder's lease names, by the name the lease
    facet shows on every row under it (the allocation's name for a registered
    box, else the lease's own word for the holder). ``outcome`` is what asking it came
    to: ``accepted`` (on its way), ``timed_out`` (no answer), ``offline`` (the
    machine is not serving the folder; nothing was asked), ``throttled`` /
    ``busy`` (asked too often, or too much in flight — retry), and
    ``not_holder`` / ``missing`` / ``changed`` (the machine cannot serve this
    copy), and ``requester`` (the reader is the holding machine itself, so
    nothing was asked). ``landing`` is the open upload's progress, or ``null``.
    """

    holder: str
    outcome: PromoteOutcomeName
    landing: LandingProgress | None = None


class NotAPage(InvalidRequest):
    """The node named is not something a page grant may be minted for (422)."""

    code = "files.not_a_page"
    status = 422


class LivePending(Conflict):
    """The node holds no bytes yet: the machine writing it has not synced (409).

    Its detail names the machine and what asking it came to. The caller has
    already passed the ``EXPORT`` decision on this very node, and the lease
    facet on every listing of it names the same machine, so the detail tells
    them nothing they could not read — which is what lets it through.
    """

    code = "files.live_pending"
    status = 409
    may_read_named_node = True

    def __init__(self, message: str, *, detail: LivePendingDetail | None = None) -> None:
        super().__init__(
            self.code,
            message,
            detail=None if detail is None else detail.model_dump(mode="json"),
        )
        self.headers = {"Retry-After": str(PENDING_RETRY_SECONDS)}


def fence(lease: LeaseContext, files: FilesContext) -> LibraryLease | None:
    """The route's lease headers as the library's fencing context.

    The caller is part of it: the headers name a lease, and the identity the
    server derived for this request — the credential's own principal, or the
    machine it PROVED it is — is what says the lease is theirs. Nothing the
    request chose enters it, which is why the context is built from
    ``files.holder`` rather than from the acting principal as sent: the agent
    assertion is a header, and a box's machine id is public.

    ``None`` for a caller that named no lease, which is not "unfenced": the
    library still refuses a write inside somebody else's mount, it just has no
    epoch to compare and answers ``files.leased`` rather than
    ``files.lease_fenced``.

    Nothing a request carries can spend the drive's ceilings: this is the
    conversion every write that carries BYTES goes through — the content PUT
    and the upload-session open — and the context it builds is a bounded one.
    The exemption belongs to the release, and the release is applied by the
    server under a context it builds itself.
    """
    if not lease.fenced:
        return None
    return LibraryLease(
        epoch=lease.epoch,
        instance_id=lease.instance,
        holder=files.holder,
        base_known=lease.base_known,
    )


class TooLarge(FilesError):  # noqa: N818 - matches the sibling error names in files.errors
    """The declared body exceeds the single-call PUT cap (413).

    Its own class rather than a status spelled in the route: the one exception
    handler maps every :class:`FilesError` subclass, so a limit gets a status
    without a route ever naming one.
    """

    code = "files.too_large"
    status = 413


async def authorized(
    request: Request,
    db: AsyncSession,
    files: FilesContext,
    node_id: NodeId,
    action: FilesAction,
) -> Authorized[str]:
    """Load, decide, and hand back the node — the only way this module gets one.

    A node that is not there, one in another org and one the caller may not read
    all raise the same :class:`NotFound` from inside ``authorize``, before any
    statement that could tell them apart.
    """
    async with files.repo.transaction():
        async with as_platform(db):
            facts = await facts_for(
                request, db, files.ctx, files.drive, machine_id=files.agent_machine_id
            )
        return await authorize(
            files.ctx,
            files.repo,
            node_id,
            action,
            facts=facts,
            enforce=files_enforcer(request, db, wrap=platform_wrap(db)),
        )


def node_id_of(raw: str) -> NodeId:
    """The path parameter as a node id.

    A malformed uuid answers the opaque 404 rather than a 422: telling a prober
    that their id was well-formed-but-absent versus malformed costs nothing to
    hide and is one bit more than they should get.
    """
    try:
        return NodeId(UUID(raw))
    except ValueError:
        raise NotFound() from None


def _declared_length(request: Request, cap: int) -> int:
    """The ``Content-Length`` the caller promised, refused if absent or too big.

    A single-call PUT must declare its size: the quota hold is taken before the
    first byte lands, and a hold cannot be taken against an unknown number. Over
    the cap is refused here rather than after 64 MiB have crossed the wire — the
    caller learns the limit for the price of a header. A body that then exceeds
    what it declared is caught mid-stream by the library (422), so the two
    limits are enforced at different moments on purpose.
    """
    raw = request.headers.get("Content-Length")
    if raw is None:
        raise InvalidRequest("files.length_required", "A content PUT must declare Content-Length")
    try:
        declared = int(raw)
    except ValueError:
        raise InvalidRequest("files.length_required", "Content-Length must be an integer") from None
    if declared < 0:
        raise InvalidRequest("files.length_required", "Content-Length may not be negative")
    if declared > cap:
        raise TooLarge(f"A single-call content PUT is capped at {cap} bytes")
    return declared


async def _request_stream(request: Request) -> AsyncIterator[bytes]:
    async for chunk in request.stream():
        yield chunk


async def _resolve_conflict(
    files: FilesContext,
    allowed: Authorized[str],
    behavior: ConflictBehavior,
    *,
    if_match: int,
    lease: LeaseContext,
) -> tuple[NodeId, int]:
    """Which node the bytes land on, and the etag to write against.

    ``replace`` is the identity. ``fail`` refuses a node that already holds
    content. ``rename`` leaves the occupant untouched and creates a fresh
    sibling under the next free conflict name for the incoming bytes, so both
    sets of bytes survive without a node the caller did not address being moved.

    The rename is a create inside the occupant's folder, so it carries the
    caller's lease: putting a new node into somebody else's mount is their
    write to make, and a conflict rename is no exception to it.
    """
    node = allowed.node
    if behavior == "replace" or node.head_version_id is None:
        return NodeId(node.id), if_match
    if behavior == "fail":
        raise Conflict("files.name_conflict", "This node already holds content")
    if node.parent_id is None:
        raise Conflict("files.name_conflict", "The drive root holds no content")
    namespace = Namespace(files.repo, files.ctx, files.clock, files.store, ceilings=files.ceilings)
    async with files.repo.transaction():
        # The drive before the nodes this create locks: the content write that
        # follows takes it for its quota hold, in this same transaction.
        await files.repo.lock_drive(DriveId(node.drive_id))
        fresh = await namespace.create(
            DriveId(node.drive_id),
            NodeId(node.parent_id),
            "file",
            bytes(node.name),
            conflict="rename",
            lease=fence(lease, files),
        )
    return NodeId(fresh.id), fresh.etag


@router.put(
    "/drives/{drive_id}/items/{item_id}/content",
    dependencies=[Depends(ratelimited("content_put", limit=120))],
)
@idempotent_route("files.content.put_content", status=201)
async def put_content(
    request: Request,
    response: Response,
    drive_id: PathId,
    item_id: PathId,
    files: FilesCtx,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    if_match: IfMatch,
    idempotency: Idempotency,
    lease: Lease,
    conflict_behavior: Annotated[ConflictBehavior, Query(alias="conflictBehavior")] = "replace",
) -> Item:
    """Stream a new version onto a file node.

    ``200`` when the node's head already held exactly these bytes — no version
    is written, and the store was asked to do the same work either way, so the
    answer is never an oracle for what the org already stores. ``201`` otherwise.
    """
    node_id = node_id_of(item_id)
    allowed = await authorized(request, db, files, node_id, FilesAction.WRITE)
    if if_match is None:  # pragma: no cover - the dependency already refuses this
        raise InvalidRequest("files.if_match_required", "A content PUT requires If-Match")
    declared = _declared_length(request, files.settings.files_single_put_max_bytes)
    target, etag = await _resolve_conflict(
        files, allowed, conflict_behavior, if_match=if_match, lease=lease
    )
    service = ContentService(
        files.repo, files.ctx, files.clock, files.store, ceilings=files.ceilings
    )
    info = await service.put_version(
        target,
        _request_stream(request),
        size_declared=declared,
        if_match=etag,
        mime_hint=request.headers.get("Content-Type"),
        lease=fence(lease, files),
    )
    response.status_code = 200 if info.unchanged else 201
    async with files.repo.transaction():
        written = await files.repo.node(target)
        version = None if info.unchanged else await files.repo.version(info.id)
        node = written if written is not None else allowed.node
        snapshot = await lease_snapshots.lease_facet(
            files.repo, node, allowed.chain, ctx=files.ctx, now=files.clock.now()
        )
    return to_item(
        node,
        allowed.chain,
        allowed.access,
        snapshot,
        version=version,
    )


@router.get(
    "/drives/{drive_id}/items/{item_id}/content",
    status_code=302,
    response_class=RedirectResponse,
    dependencies=[Depends(ratelimited("content_get", limit=600))],
    responses={
        409: {
            "model": ErrorEnvelope,
            "description": "The bytes are still on the machine holding the folder",
        }
    },
)
async def get_content(
    request: Request,
    drive_id: PathId,
    item_id: PathId,
    files: FilesCtx,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> Response:
    """Redirect to a signed, single-use URL for this node's head version.

    The target lives on the content domain and carries the whole credential; a
    ``Range`` on this request is folded into the grant, so the signed URL can
    only ever fetch the span it was minted for.

    A node whose content is *derived* has no head version to mint against, so
    it is served inline from here instead — see :func:`rendered_content`.
    """
    node_id = node_id_of(item_id)
    allowed = await authorized(request, db, files, node_id, FilesAction.EXPORT)
    if allowed.node.kind == "object" and allowed.node.head_version_id is None:
        return await rendered_content(request, files, allowed)
    # A row a person can see in their own listing whose bytes the machine
    # holding the folder has not pushed yet is not "not found" — and a browser
    # that follows this redirect is exactly the client that would otherwise
    # drop the row on a 404. The machine is asked for it first.
    fresh = await fresh_bytes(
        request, db, files, allowed, deadline=files.settings.files_promote_wait_seconds
    )
    return RedirectResponse(
        await mint_download_url(request, files, fresh.allowed),
        status_code=302,
        headers=fresh.headers(),
    )


@router.post(
    "/drives/{drive_id}/items/{item_id}/content-grants",
    status_code=201,
    dependencies=[Depends(ratelimited("content_grants", limit=600))],
    responses={
        409: {
            "model": ErrorEnvelope,
            "description": "The bytes are still on the machine holding the folder",
        }
    },
)
async def create_content_grant(
    request: Request,
    response: Response,
    drive_id: PathId,
    item_id: PathId,
    body: ContentGrantRequest,
    files: FilesCtx,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> ContentGrantResponse:
    """Mint a read URL for this node without redirecting to it.

    The ``302`` mint is the right shape for a download — the browser follows it
    and saves a file — and the wrong shape for a preview: a ``fetch`` that
    follows a cross-origin redirect sends ``Origin: null`` and fails CORS, and
    an ``<img src>`` of one is refused by the resource policy. A surface that
    wants the bytes *in* the page therefore asks for the URL as data and fetches
    the content origin itself, which is what this route answers.

    Not idempotent, deliberately: a mint is not a retry of the previous mint. A
    grant is a credential with its own deadline and its own counter, so replaying
    a key would hand back a URL closer to death than the caller thinks.
    """
    node_id = node_id_of(item_id)
    allowed = await authorized(request, db, files, node_id, FilesAction.EXPORT)
    wait = (
        files.settings.files_promote_page_wait_seconds
        if body.kind == "page"
        else files.settings.files_promote_wait_seconds
    )
    fresh = await fresh_bytes(request, db, files, allowed, deadline=wait)
    response.headers.update(fresh.headers())
    node = fresh.allowed.node
    if body.kind == "file":
        url = await mint_download_url(
            request, files, fresh.allowed, attachment=body.disposition == "attachment"
        )
        return ContentGrantResponse(
            url=url,
            expires_at=files.clock.now() + signed_urls.content_url_ttl(),
            kind="file",
            etag=str(node.etag),
            content_state=fresh.state,
            as_of=fresh.as_of,
        )
    minted = await _mint_page(request, files, fresh.allowed)
    return minted.model_copy(update={"content_state": fresh.state, "as_of": fresh.as_of})


@dataclasses.dataclass(frozen=True, slots=True)
class Freshness:
    """The node a read is served from, and what its bytes are against the
    machine holding its folder."""

    allowed: Authorized[str]
    state: ContentState
    #: When the drive's copy was last known to match the machine's — set only
    #: when the store's copy is older than the machine's.
    as_of: datetime | None = None

    def headers(self) -> dict[str, str]:
        """What a read served from an older copy says about it."""
        if self.state != "behind":
            return {}
        stamped = {CONTENT_STATE_HEADER: "behind"}
        if self.as_of is not None:
            stamped[CONTENT_AS_OF_HEADER] = self.as_of.isoformat()
        return stamped


async def fresh_bytes(
    request: Request,
    db: AsyncSession,
    files: FilesContext,
    allowed: Authorized[str],
    *,
    deadline: float,
) -> Freshness:
    """The node to serve, fetching its bytes from the machine first when the
    drive does not have them yet.

    A file no machine reports on costs nothing here: its row carries no holder
    facet, and it is served as it always was, with no further statement. A file
    under a live lease whose bytes the store already has (``on_drive``) is
    served from the store and the machine is never asked. Otherwise
    (``unlanded``, or ``behind`` the machine's newer copy) the machine is asked
    for that one file and this request waits — on the hub, holding no lock and
    no database connection — for the bytes to land through the ordinary fenced
    upload. What landed is re-read and served; what did not falls back to the
    store's older copy (``behind``) or to ``files.live_pending`` (nothing to
    serve yet), both of which say why.

    A node with no bytes and no lease is the opaque 404, as it always was. This
    caller has already passed the ``EXPORT`` decision on the node, so naming
    the machine tells them nothing the lease facet on its row does not.
    """
    node = allowed.node
    reported = holder_facet(node)
    if node.head_version_id is not None and reported is None:
        return Freshness(allowed, "none")
    async with files.repo.transaction():
        lease = await lease_snapshots.lease_facet(
            files.repo, node, allowed.chain, ctx=files.ctx, now=files.clock.now()
        )
    if lease is None:
        if node.head_version_id is None:
            raise NotFound()
        return Freshness(allowed, "none")
    state = content_state(head_facts(node, allowed.head), reported, lease_live=lease.live)
    if state not in LANDING_STATES:
        if node.head_version_id is None:
            outcome = (
                PromoteOutcome.MISSING
                if lease.live and lease.served == "live"
                else PromoteOutcome.OFFLINE
            )
            raise await _pending(files, allowed, lease, outcome)
        return Freshness(allowed, state)
    # The same ask the Slack relay makes for a chart it is about to post.
    outcome = await promote_under(
        db,
        allowed,
        lease,
        promoter=await promoter_for(request.app),
        deadline=deadline,
        asking_machine=files.agent_machine_id,
    )
    fresh = await _reread(files, allowed)
    if fresh.node.head_version_id is None:
        raise await _pending(files, fresh, lease, outcome)
    now_state = content_state(
        head_facts(fresh.node, fresh.head), holder_facet(fresh.node), lease_live=lease.live
    )
    if now_state == "behind":
        as_of = lease.last_sync_at or (fresh.head.created_at if fresh.head is not None else None)
        return Freshness(fresh, "behind", as_of=as_of)
    if now_state == "none":
        # The landing settled the holder's report, which is what clears it: the
        # bytes served are the machine's own.
        return Freshness(fresh, "on_drive")
    return Freshness(fresh, now_state)


async def _reread(files: FilesContext, allowed: Authorized[str]) -> Authorized[str]:
    """The node and its head as they stand now, after a wait.

    The access decision is not re-made: it was made for this request a few
    seconds ago, and a promotion changes the node's bytes, never who may read
    them."""
    node = allowed.node
    async with files.repo.transaction():
        await files.repo.session.refresh(node)
        head = (
            None
            if node.head_version_id is None
            else await files.repo.version(VersionId(node.head_version_id))
        )
    return dataclasses.replace(allowed, node=node, head=head)


async def _pending(
    files: FilesContext,
    allowed: Authorized[str],
    lease: lease_snapshots.LeaseFacet,
    outcome: PromoteOutcome,
) -> LivePending:
    """The ``files.live_pending`` answer, naming the machine, what asking it
    came to, and how far its upload of this file has got."""
    async with files.repo.transaction():
        row = (
            await files.repo.session.execute(
                # A single-call upload names the node it lands on; a multipart
                # one names the folder and the name until its commit resolves
                # the node, so both spellings of "this file" are asked for.
                text(
                    "SELECT bytes_received, declared_size FROM file_upload_sessions "
                    "WHERE org_team_id = :org "
                    "AND state IN ('open', 'uploading', 'committing') "
                    "AND (node_id = :node OR (node_id IS NULL AND parent_id = :parent "
                    "AND name = :name)) "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {
                    "org": files.repo.scope.org_team_id,
                    "node": allowed.node.id,
                    "parent": allowed.node.parent_id,
                    "name": bytes(allowed.node.name),
                },
            )
        ).first()
    # Outside the Files transaction: the allocation is not a Files table, and
    # the listing names the machine the same way, outside its own.
    holder = await _holder_name(files, lease.machine)
    landing = (
        None
        if row is None
        else LandingProgress(done_bytes=int(row.bytes_received), total_bytes=int(row.declared_size))
    )
    return LivePending(
        "The machine holding this folder has not synced these bytes yet",
        detail=LivePendingDetail(holder=holder, outcome=outcome.value, landing=landing),
    )


async def _holder_name(files: FilesContext, machine: str) -> str:
    """The machine a lease names, by the name a listing shows for it.

    A registered box's lease names it by its allocation id; the allocation's
    own name is what a person knows it by. Resolved in this drive's org only,
    so an id of another tenant's machine names nothing. A holder that named
    itself (a laptop mount), or a box with no name, keeps the lease's word."""
    try:
        machine_id = UUID(machine)
    except ValueError:
        return machine
    named = (
        await files.repo.session.execute(
            select(ComputeAllocation.name).where(
                ComputeAllocation.id == machine_id,
                ComputeAllocation.org_team_id == files.repo.scope.org_team_id,
            )
        )
    ).scalar_one_or_none()
    return named or machine


async def _mint_page(
    request: Request, files: FilesContext, allowed: Authorized[str]
) -> ContentGrantResponse:
    """A multi-use grant over the entry's own folder.

    The grant's root is the entry's parent rather than the entry: a page fetches
    the images, fonts and media beside it, and a grant naming only the entry
    could serve none of them. What keeps that from being a folder read is that
    every request under the grant is re-authorized against the node it names —
    the grant buys the right to ask, never the right to read.
    """
    node = allowed.node
    head = node.head_version_id
    async with files.repo.transaction():
        version = None if head is None else await files.repo.version(VersionId(head))
    if node.kind != "file" or version is None or version.mime_sniffed not in PAGE_MIME_TYPES:
        raise NotAPage("A page can only be minted for a document, not for this file")
    minter = signed_urls.minting_user(files.ctx)
    if minter is None or node.parent_id is None:
        # A credential naming no user — a CI or proxy token — has no access for
        # the per-request walk to resolve, so a grant minted on it would act as
        # nobody. The opaque refusal keeps that indistinguishable from a node
        # that is not there.
        raise NotFound()
    path = await mint_page_grant(
        files.repo,
        root_node_id=NodeId(node.parent_id),
        entry_node_id=NodeId(node.id),
        entry_name=bytes(node.name),
        user_id=minter,
        credential_id=files.ctx.credential_id,
        session_id=session_binding(files.ctx),
        machine_id=files.agent_machine_id,
        clock=files.clock,
        key=files.settings.effective_files_content_signing_key.encode(),
    )
    base = files.settings.files_content_base_url or str(request.base_url)
    return ContentGrantResponse(
        url=f"{base.rstrip('/')}{path}",
        expires_at=files.clock.now() + page_grant_ttl(),
        kind="page",
        etag=str(node.etag),
    )


async def rendered_content(
    request: Request, files: FilesContext, allowed: Authorized[str]
) -> Response:
    """The bytes of a node that is rendered on every read rather than stored.

    A replication context's ``spec.json`` and ``README.md`` are a pure function
    of the object's rows — nothing is written when the folder is created and
    nothing is rewritten when the spec changes — so there is no version to sign
    a content-domain URL for, and the mint below would answer the opaque 404
    that a node holding no bytes gets.

    They are served from the API host, which is safe for the one reason a
    stored byte is not: every byte here was produced by a renderer in this
    process, so there is nothing a caller could have uploaded and then talked a
    browser into executing on this origin. ``no-store`` because the rendering
    follows the rows, and a cached copy is a stale spec.
    """
    node = allowed.node
    provider = rendered_providers(files).resolve(node)
    info = await provider.head(node, None)
    payload = b"".join([chunk async for chunk in await provider.open(node, None)])
    return Response(
        content=payload,
        media_type=info.mime,
        headers={
            "Content-Disposition": content_disposition(
                bytes(node.name), info.mime, wants_attachment(request)
            ),
            "Cache-Control": "private, no-store",
        },
    )


#: Query values that ask for ``Content-Disposition: attachment``. ``download=1``
#: is the shorthand a link builder reaches for; ``disposition=attachment`` is the
#: explicit spelling, and ``disposition=inline`` names the unchanged default.
_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes"})


def wants_attachment(request: Request) -> bool:
    """Whether this mint was asked for a download rather than a rendered page.

    The disposition rule serves sniffed JSON, CSV, text and images ``inline``,
    which is right for a preview and wrong for a Download: the browser opens a
    tab instead of saving a file. The caller that knows which of the two it is
    asking for says so here, and the answer rides under the URL's signature —
    the default, and every URL that does not ask, is unchanged.
    """
    params = request.query_params
    disposition = params.get("disposition", "").strip().lower()
    if disposition == "attachment":
        return True
    if disposition == "inline":
        return False
    return params.get("download", "").strip().lower() in _TRUTHY


def content_disposition(node_name: bytes, mime: str, attachment: bool) -> str:
    """The ``Content-Disposition`` for a node, forced to ``attachment`` when asked.

    Spelled once here because both origins answer it: the content origin serves
    a stored version, this one serves a rendering, and a name that survived the
    sanitiser on one path and not the other would be a header the two hosts
    disagree about.
    """
    header = disposition_header(node_name, mime)
    if not attachment:
        return header
    _, _, parameters = header.partition(";")
    return f"attachment;{parameters}"


async def mint_download_url(
    request: Request,
    files: FilesContext,
    allowed: Authorized[str],
    *,
    attachment: bool | None = None,
) -> str:
    """The signed content URL for ``allowed``'s head version.

    Public because ``?select=downloadUrl`` on the item route is the same mint
    with the same TTL, single use and session binding: the item lane calls this
    rather than growing a second minting path that could drift from this one.

    ``attachment`` overrides what the query string asks for, for the one caller
    whose request carries its disposition in a body rather than in the URL.
    """
    head = allowed.node.head_version_id
    if head is None:
        raise NotFound()
    span = _range_header(request.headers.get("Range"), allowed.node.size)
    path = await signed_urls.mint_content_url(
        files.repo,
        version_id=VersionId(head),
        session_id=session_binding(files.ctx),
        range=span,
        clock=files.clock,
        key=files.settings.effective_files_content_signing_key.encode(),
        user_id=signed_urls.minting_user(files.ctx),
        attachment=wants_attachment(request) if attachment is None else attachment,
        # The machine this request PROVED it is, so the origin — which has no
        # credential to re-check an assertion with — can tell the box pulling
        # its own chat folder from a member who read its public id off a row.
        machine_id=files.agent_machine_id,
        # A box minting on its own credential has no user for the URL to act
        # as; the credential is what the origin rebuilds it from.
        credential_id=minting_credential(files.ctx),
    )
    # Always an absolute URL. With no separate content origin configured (local
    # dev, a small self-hosted install) the bytes are served by this same app
    # under /c on its own origin — a bare path would be resolved against the SPA
    # dev server, which answers index.html for it.
    base = files.settings.files_content_base_url or str(request.base_url)
    return f"{base.rstrip('/')}{path}"


def minting_credential(ctx: ActingContext) -> UUID | None:
    """The machine credential a URL is minted on, for a box minting as itself;
    ``None`` for every other caller. The credential's id is the machine
    principal's own, so the origin can re-ask its standing by id."""
    if not ctx.is_machine or ctx.credential_id is None:
        return None
    try:
        return UUID(ctx.credential_id)
    except ValueError:  # pragma: no cover - a machine principal's credential id is a uuid
        return None


def session_binding(ctx: ActingContext) -> SessionId | None:
    """The session a grant is bound to, when the credential names one.

    An agent's chat session id *is* its principal id (``Principal.id`` for
    ``AGENT``), which is the one session identifier an acting context carries
    today: a URL an agent minted is redeemable only under that agent's session,
    so it dies with the session rather than living on as a bearer token. A user
    acting on their own cookie has no session id on the context — resolving the
    JWT's ``jti`` onto the principal is what would give browser-minted URLs the
    same binding — and ``None`` is the honest answer for them rather than a
    binding that silently never applies.
    """
    if ctx.acting_principal.kind is not PrincipalKind.AGENT:
        return None
    try:
        return SessionId(UUID(ctx.acting_principal.id))
    except ValueError:  # pragma: no cover - non-uuid chat session identifiers
        return None


def _range_header(raw: str | None, size: int) -> tuple[int, int] | None:
    if raw is None or size <= 0:
        return None
    parsed = parse_range(raw, size)
    if parsed is None:
        raise InvalidRequest("files.bad_range", "Range must name one satisfiable span")
    return parsed


def parse_range(raw: str, size: int) -> tuple[int, int] | None:
    """``bytes=lo-hi`` / ``bytes=lo-`` / ``bytes=-suffix`` as an inclusive span.

    ``None`` means "not one satisfiable span": a bad unit, a multi-range list, a
    start past the end, or an inverted pair. Callers turn that into a 416 (on
    the content domain, where the header is the request) or a 422 (on the API
    host, where it is one parameter of a mint) — never into a silently widened
    grant.
    """
    text = raw.strip()
    if not text.lower().startswith("bytes="):
        return None
    spec = text[6:].strip()
    if "," in spec or "-" not in spec:
        return None
    lo_text, _, hi_text = spec.partition("-")
    if not lo_text:
        if not hi_text.isdigit():
            return None
        length = int(hi_text)
        if length == 0:
            return None
        return max(0, size - length), size - 1
    if not lo_text.isdigit():
        return None
    lo = int(lo_text)
    if lo >= size:
        return None
    if not hi_text:
        return lo, size - 1
    if not hi_text.isdigit():
        return None
    hi = min(int(hi_text), size - 1)
    if hi < lo:
        return None
    return lo, hi


__all__ = [
    "CONTENT_AS_OF_HEADER",
    "CONTENT_STATE_HEADER",
    "PAGE_MIME_TYPES",
    "PENDING_RETRY_SECONDS",
    "PROMOTE_OUTCOME_NAMES",
    "ConflictBehavior",
    "ContentGrantRequest",
    "ContentGrantResponse",
    "Freshness",
    "LandingProgress",
    "LivePending",
    "LivePendingDetail",
    "NotAPage",
    "PromoteOutcomeName",
    "TooLarge",
    "authorized",
    "create_content_grant",
    "fence",
    "fresh_bytes",
    "get_content",
    "lease_relative_path",
    "mint_download_url",
    "node_id_of",
    "parse_range",
    "put_content",
    "router",
    "session_binding",
    "wants_attachment",
]
