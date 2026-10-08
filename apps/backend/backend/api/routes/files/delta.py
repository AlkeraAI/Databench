"""The change feed as an HTTP surface: one page, one link, one token.

The route resolves the token, asks ``DeltaService`` for one page and returns
what it built. It decides nothing about *what* changed — that is the library's
watermark and its latest-state-by-id folding — and it decides who may see a row
from the same access the rest of Files decides it from, taken fresh on every
page, so a node the caller lost access to between two pages is gone from the
very next one.

**The feed mentions only what the caller may see.** A node the caller has no
claim to is left off the page entirely, never sent as a tombstone: a tombstone
per private node gave every member the ids of everyone else's files and a signal
each time one changed. A tombstone goes to a caller who could read the node (a
trash, or a purge vouched for by the folder the node was in) or whose own grant
on it was just withdrawn. The library spells the rule
(``alkera_core.files.delta.shown_to``) and records the one gap it leaves.

Authorizing a page needs a decision per node, and the library's own predicate is
synchronous by design (it is called inside the row loop, so one await per row
would be one query per row). So the page is read ONCE, its ids are authorized
in one batch, and the rows the batch refused are dropped here.

Reading it a second time instead — the shape this used to have — was wrong, and
not only wasteful. The two reads carry the same cursor and the same limit, but
they are two statements under READ COMMITTED, so they take two snapshots; and
what the feed may deliver is decided per snapshot ("strictly below the oldest
in-flight transaction"), not by the outbox being append-only. A row that became
deliverable between them was therefore in the second page and in nobody's
authorized set, and it went to the client as ``{"id", "deleted": true}`` — a
live node announced as a deletion, which is the one item a sync client applies
by removing its local copy. One read cannot disagree with itself.

The batch is the point. A loop calling ``authorized()`` per row was three
queries and one platform decision row per item — 30,000 statements for a page
of 10,000, an N+1 the delta performance budget could not honestly
admit. ``readable_ids`` resolves the whole page in a
constant number of statements and answers exactly what the per-item call
answered: for ``READ`` the ``files.access`` policy is "in the org, with READ
among the allowed actions", which is the effective access the seam computes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Annotated, Any
from uuid import UUID

from alkera_core.db.session import get_db
from alkera_core.files.authz.decider import names_caller as names
from alkera_core.files.authz.grants import Principal
from alkera_core.files.authz.readable import readable_ids
from alkera_core.files.delta import (
    DEFAULT_LIMIT,
    DELTA_MAX_LIMIT,
    DeltaExpired,
    DeltaPage,
    DeltaService,
    DeltaToken,
    shown_to,
)
from alkera_core.files.errors import NotFound
from alkera_core.files.ids import DriveId
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps.files import ratelimited
from backend.api.deps.files_context import FilesCtx
from backend.api.deps.files_errors import refusal_body
from backend.api.deps.files_lane import access_facts

router = APIRouter(tags=["files"])

#: The literal a client sends to mean "start from now, tell me nothing that
#: already happened" — the one token value that is not an opaque string.
LATEST = "latest"


def _drive(ctx: Any, drive_id: UUID) -> DriveId:
    """The drive the caller named, or the opaque 404.

    A drive id that is not this org's own drive is not distinguishable, from
    outside, from one that does not exist at all.
    """
    if ctx.drive.id != drive_id:
        raise NotFound()
    return DriveId(ctx.drive.id)


def _expired_response(request: Request, exc: DeltaExpired, drive_id: UUID) -> JSONResponse:
    """The 410 a stale token gets: what to do, where to start, and when.

    ``Retry-After`` is the library's, not a constant chosen here, so a fleet
    whose tokens all expired together is spread out by the same policy that
    decided they were stale.
    """
    resync = str(request.url.replace(query=f"token={LATEST}"))
    return JSONResponse(
        status_code=exc.status,
        content=refusal_body(status=exc.status, code=exc.code, message=str(exc)),
        headers={"Retry-After": str(exc.retry_after), "Location": resync},
    )


def _authorized(
    page: DeltaPage, readable: set[UUID], names_caller: Callable[[str, UUID], bool]
) -> DeltaPage:
    """The page as this caller may have it.

    ``readable`` was decided for each item's ``vouch_id``, and the library's
    own rule decides from it, so the route and the library cannot drift. The
    links are untouched: the cursor covers the rows that were READ, including
    the ones dropped here, and those rows are never owed to this caller.
    """
    decided = (shown_to(item, readable.__contains__, names_caller) for item in page.items)
    return replace(page, items=tuple(item for item in decided if item is not None))


def _rendered(page: DeltaPage, *, key: str) -> dict[str, Any]:
    """The page on the wire: items, and exactly one of the two links."""
    body: dict[str, Any] = {"items": [item.as_dict() for item in page.items]}
    if page.next_link is not None:
        body["nextLink"] = page.next_link.encode(key=key)
    if page.delta_link is not None:
        body["deltaLink"] = page.delta_link.encode(key=key)
    return body


@router.get("/drives/{drive_id}/delta", dependencies=[Depends(ratelimited("delta"))])
async def read_delta(
    request: Request,
    drive_id: UUID,
    ctx: FilesCtx,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    token: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=DELTA_MAX_LIMIT)] = DEFAULT_LIMIT,
) -> Any:
    """One page of this drive's changes after ``token``."""
    drive = _drive(ctx, drive_id)
    service = DeltaService(ctx.repo, ctx.ctx, ctx.clock)

    if token == LATEST:
        async with ctx.repo.transaction():
            made = await service.latest_token(drive)
        return {"items": [], "deltaLink": made.encode(key=service.key)}

    parsed: DeltaToken | None = None
    if token:
        parsed = DeltaToken.decode(token, key=service.key)

    facts = await access_facts(request, db, ctx.ctx)
    try:
        async with ctx.repo.transaction():
            page = await service.read(drive, token=parsed, limit=limit)
            vouchers = {item.vouch_id for item in page.items if item.vouch_id is not None}
            readable = await readable_ids(ctx.repo, ctx.ctx, list(vouchers), facts=facts)

            def names_caller(kind: str, principal_id: UUID) -> bool:
                return names(Principal(kind=kind, id=principal_id), ctx.ctx, facts)

            body = _rendered(_authorized(page, readable, names_caller), key=service.key)
    except DeltaExpired as exc:
        return _expired_response(request, exc, drive_id)
    return body


__all__ = ["LATEST", "read_delta", "router"]
