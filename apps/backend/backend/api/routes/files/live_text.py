"""A file's live document, read and written as text by its folder's holder.

While people edit a file live (``doc:file:<node>`` on the Loro lane) the agent
edits the same file on the box's disk. The box holding the folder joins the
document as a text peer (:mod:`backend.services.crdt.text_peers`) instead of
uploading the agent's edit for the session to merge from the drive:

* ``GET .../items/{node}/live`` answers the document's content and its version
  token (``{"live": false}`` when no session is live on the file);
* ``POST .../items/{node}/live`` merges a whole text made on the state a token
  names and answers the merged content, its token, the token of the state
  whose content is exactly the text sent, and whether the document's edits
  are reaching the drive (``saved``; when not, the box uploads the file).

Both are the holder's alone: the Files policy decides WRITE on the node for
the request's principal (the box's own credential, its machine verified), and
the lease covering the node must be the caller's at the epoch its headers
name, and take the file's writes. The update is written as that lease's
machine. A file larger than the document holds, one no session is open on,
or one in an org whose live editing is switched off
(:mod:`backend.services.crdt.switch`, answered with ``reason``) answers
``live: false`` and the box writes it the ordinary way.
"""

from __future__ import annotations

from typing import Annotated, Any

from alkera_core.db.session import get_db
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.errors import NotFound
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps.files import Lease, caller_drive, ratelimited
from backend.api.deps.files_context import FilesCtx
from backend.api.params import PathId
from backend.api.routes.files.content import authorized, fence, node_id_of
from backend.services.crdt import LIVE_EDITING_OFF, DocRef, PeerText, live_type_of, name_text
from backend.services.files import NotEditableError, holder_peer
from backend.services.realtime import crdt_lane

router = APIRouter(tags=["files"])

#: The largest text a submit carries: twice the largest file a session opens,
#: the document's own hard cap. Past it the box writes the file as a file.
MAX_TEXT_CHARS = 2 * 1024 * 1024


class LiveText(BaseModel):
    """The document as a text peer reads it."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    live: bool
    token: str | None = None
    text: str | None = None
    #: On a submit: the state the text sent was merged into the document at.
    at_token: str | None = None
    #: That state's content, when it is not exactly the text sent: a text
    #: naming no state is merged adding what it adds and removing nothing, so
    #: the state can hold more than was sent.
    at_text: str | None = None
    #: On a submit: a retry of one already merged (``at_token`` is unknown).
    repeat: bool = False
    #: On a submit: whether the document's write back is landing. ``False``
    #: while saving is paused (the drive does not hold this text and will not
    #: get it without a person): the box then uploads the file the ordinary
    #: way. A box from before this field reads it as absent, which means
    #: ``True``.
    saved: bool = True
    #: Why the answer is ``live: false`` when it is not simply that no session
    #: is open: ``live_editing_off`` while live editing is switched off for
    #: the org. A box reads ``live: false`` alone and writes the file the
    #: ordinary way either way.
    reason: str | None = None


class LiveTextSubmit(BaseModel):
    """A whole text, and the state it was made on."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    text: str = Field(max_length=MAX_TEXT_CHARS)
    submit_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,48}$")
    base_token: str | None = Field(default=None, max_length=64 * 1024)
    #: The drive etag the box last agreed the file at, for a token it does not
    #: hold (it joined with edits of its own not yet sent).
    base_etag: int | None = None


def _answer(found: PeerText | None) -> LiveText:
    if found is None:
        return LiveText(live=False)
    return LiveText(
        live=True,
        token=found.token,
        text=found.text,
        at_token=found.at_token,
        at_text=found.at_text,
        repeat=found.repeat,
        saved=found.saved,
    )


async def _holder(
    request: Request,
    db: AsyncSession,
    files: FilesCtx,
    item_id: str,
    lease: Lease,
) -> tuple[DocRef, str]:
    """The document and the machine a text peer of it writes as, once the
    policy and the fence have both said this caller is the holder."""
    node_id = node_id_of(item_id)
    allowed = await authorized(request, db, files, node_id, FilesAction.WRITE)
    try:
        machine = await holder_peer(db, files.ctx, node_id, lease=fence(lease, files))
    except NotEditableError:
        raise NotFound() from None
    # Nothing the lane does next needs this request's locks: the lease rows
    # the fence took are let go before the document is.
    await db.commit()
    # A notebook is co-edited as a notebook document, never as plain text
    # beside it: the box's whole-text edits of it merge cell by cell.
    doc_type = live_type_of(name_text(allowed.node.name))
    return DocRef(files.repo.scope.org_team_id, doc_type, str(node_id)), machine


@router.get(
    "/drives/{drive_id}/items/{item_id}/live",
    response_model=LiveText,
    response_model_by_alias=True,
    # The drive first, before the body is read: a stranger's request is the
    # same opaque 404 every other Files route answers, never a 422.
    dependencies=[Depends(caller_drive), Depends(ratelimited("live_text"))],
)
async def read_live_text(
    request: Request,
    drive_id: PathId,
    item_id: PathId,
    files: FilesCtx,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    lease: Lease,
) -> Any:
    """The file's live document as text, for its folder's holder."""
    ref, _machine = await _holder(request, db, files, item_id, lease)
    lane = crdt_lane(request.app)
    if lane is None:
        return LiveText(live=False)
    if not await lane.switch.on(ref.org_id):
        return LiveText(live=False, reason=LIVE_EDITING_OFF)
    return _answer(await lane.peers.read(ref))


@router.post(
    "/drives/{drive_id}/items/{item_id}/live",
    response_model=LiveText,
    response_model_by_alias=True,
    # The drive first, before the body is read: a stranger's request is the
    # same opaque 404 every other Files route answers, never a 422.
    dependencies=[Depends(caller_drive), Depends(ratelimited("live_text"))],
)
async def submit_live_text(
    request: Request,
    drive_id: PathId,
    item_id: PathId,
    files: FilesCtx,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    lease: Lease,
    body: LiveTextSubmit,
) -> Any:
    """Merge the agent's text into the file's live document. Idempotent by
    ``submitId``, so it carries no idempotency key."""
    ref, machine = await _holder(request, db, files, item_id, lease)
    lane = crdt_lane(request.app)
    if lane is None:
        return LiveText(live=False)
    if not await lane.switch.on(ref.org_id):
        # Nothing is merged: the box uploads the file, as for a closed session.
        return LiveText(live=False, reason=LIVE_EDITING_OFF)
    found = await lane.peers.submit(
        ref,
        text=body.text,
        submit_id=body.submit_id,
        agent_id=machine,
        base_token=body.base_token,
        base_etag=body.base_etag,
    )
    return _answer(found)


__all__ = ["LiveText", "LiveTextSubmit", "read_live_text", "router", "submit_live_text"]
