"""The drive and item routes: everything that reads or reshapes the tree.

Every handler here is the same four steps in the same order — resolve the
principal and the drive, resolve the node *through* ``authorize`` (which loads
via the repo and calls ``enforce``), call the library, render with ``to_item``
— and nothing else. That order is the "not yours" contract: a
nonexistent node, a node in another org and a node the caller may not read all
reach ``authorize``, all raise the same :class:`NotFound`, and none of them is
short-circuited by an early return before the policy has run. A handler that
checked existence itself would be an oracle for the second and third classes.

Statuses are never spelled in a handler: the library raises, and the one
exception handler in ``backend.api.deps.files_errors`` maps the class. The only literal statuses are
the *successful* ones FastAPI needs declared up front.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal

from alkera_core.authz.principal import ActingContext
from alkera_core.files import lease_snapshots, listing, ops, stars
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.authorize import Authorized
from alkera_core.files.authz.decider import CHAT_SUBTYPE, RECORD_BIT, AccessFacts
from alkera_core.files.authz.readable import access_by_id
from alkera_core.files.copy import run_copy, start_copy
from alkera_core.files.errors import InvalidRequest, NotFound, PreconditionFailed
from alkera_core.files.filters import (
    ListFilters,
    Marker,
)
from alkera_core.files.ids import DriveId, NodeId, OperationId, VersionId
from alkera_core.files.leases import fenced_create_into, fenced_write_for
from alkera_core.files.namespace import Namespace
from alkera_core.files.objects_bridge import (
    CHAT_TYPE,
    FOLDER_OBJECT_KINDS,
    adopt_copied_folder_node,
    drop_target_for,
    folder_object_kind,
    live_node_for,
    pointer_name,
)
from alkera_core.files.ops import Operations, OperationState
from alkera_core.files.providers.context_folder import CONTEXT_FOLDER_TYPES
from alkera_core.files.quota import QuotaService
from alkera_core.files.repo import FilesRepo
from alkera_core.files.trash import TRASH_WINDOW, Trash
from alkera_core.models.files.tree import FileNode
from alkera_core.models.user import User
from alkera_core.models.workspace_object import WorkspaceObject
from alkera_core.schemas.files.attrs import AttrsPatch
from alkera_core.schemas.files.item import Item, SymlinkKind
from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import files_nodes
from backend.api.deps.files import (
    Idempotency,
    IfMatch,
    Lease,
    TrashIfMatch,
    as_platform,
    caller_drive,
    idempotent_route,
    ratelimited,
)
from backend.api.deps.files_context import CHAT_QUERY, FilesCtx
from backend.api.deps.files_facts import facts_for
from backend.api.deps.files_nodes import _fence, _name_bytes, _order, _resolve, _uuid, files_txn
from backend.api.params import PathId
from backend.api.routes.files import PREFIX
from backend.api.routes.files.operations import OperationWire, queued_answer, to_wire
from backend.auth.dependencies import CurrentPrincipal, CurrentUser, DbSession
from backend.authz.enforce import role_resolver
from backend.services.audit import org_audit as org_audit_service
from backend.services.chats import chat_service
from backend.services.chats import duplicate as duplicate_service
from backend.services.compute.placement import resolve_machine_for
from backend.services.files.context import FilesContext
from backend.services.files.guards import (
    Decide,
    refuse_a_copy_over_hidden_chats,
    refuse_a_move_that_raises_the_mover,
)
from backend.services.files.home import caller_home, home_place
from backend.services.files.items import (
    to_item,
    with_lease_machines,
    with_object_facet,
    with_object_facets,
    with_owner_name,
    with_owner_names,
)
from backend.services.files.operations_runner import queue_inline
from backend.services.objects import object_service
from backend.services.sharing import access

router = APIRouter()

#: The published listing cap. A larger `limit` is refused by the
#: library rather than clamped, so a client that asked for 5,000 learns it.
MAX_LIMIT = listing.MAX_LIMIT

#: Query keys the children route consumes itself; everything else is handed to
#: `ListFilters.from_query`, which refuses what it does not know.
_LISTING_KEYS = frozenset({"orderBy", "marker", "limit"})


class DriveWire(BaseModel):
    """The org's drive as a client sees it."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    org_id: str
    root_id: str
    #: The caller's OWN ``/home/<me>`` folder — never the ``/home`` container,
    #: which holds every member's home and is a signpost rather than a place.
    #: ``None`` for a principal that has no home (a CI or proxy token).
    home_id: str | None = None
    quota_bytes: int = 0


class CreateChild(BaseModel):
    """The `POST …/children` body: one folder, symlink or special node.

    Files are absent on purpose — bytes arrive through a content PUT or an
    upload session, so a `kind: "file"` here would be a node with no version
    that a listing would render as an empty file nobody wrote.

    A body that asks for one anyway is REFUSED rather than quietly rounded to
    the default. ``kind`` used to leave a caller's ``"file"`` out of the
    literal, and a Drive-shaped body naming a ``file`` facet was simply an
    unknown key: either way the caller asked for a file, got a 201 describing a
    folder of that name, and discovered it only when the content PUT that
    followed answered 404 as though the file had vanished.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    name: str
    kind: Literal["folder", "symlink", "special", "file"] = "folder"
    #: Present only so a body that asks for a file is answered about the file
    #: it asked for. Nothing reads it — a file is made by writing its bytes.
    file: dict[str, Any] | None = None
    subtype: str | None = None
    symlink_target: str | None = None
    #: How the target is read back. Omitted means ``relative``, the only kind
    #: whose text means the same thing on every machine.
    symlink_kind: SymlinkKind | None = None
    # The namespace calls take `fail | rename` only: `replace` is a *content*
    # mode (a new version on the node that is already there), and a folder or a
    # symlink has no content to replace.
    conflict_behavior: Literal["fail", "rename"] = "fail"


class PatchItem(BaseModel):
    """The `PATCH` body: a rename, a move, an attrs change, or any combination."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    name: str | None = None
    parent_id: str | None = None
    attrs: AttrsPatch | None = None


class CopyItem(BaseModel):
    """The `POST …/copy` body."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    parent_id: str
    name: str | None = None
    conflict_behavior: Literal["fail", "rename", "replace"] = "rename"


class DuplicateItem(BaseModel):
    """The `POST …/duplicate` body.

    Both fields are optional: with no destination the copy lands in the
    caller's own drive — their home, or the place the source's own kind names
    inside it — and with no name it keeps the source's (a chat's copy is named
    for its new title).
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    destination_id: str | None = None
    name: str | None = None


class DuplicateResult(BaseModel):
    """What a duplicate answers: the new node, and the new object when the
    node stands for one — the chat id a client opens next."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    item: Item
    chat_id: uuid.UUID | None = None
    object_id: uuid.UUID | None = None


class TreeCreate(BaseModel):
    """The `POST …/tree` body: the folder skeleton of a dropped directory."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    paths: list[str] = Field(default_factory=list)


class ChildrenPage(BaseModel):
    """One page of a folder's children."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    value: list[Item]
    next_marker: str | None = None


#: The most ids one lookup may name. Sized to the edge: the WAF's 8 KB body
#: rule is not exempted for this route (exempting it would need a new
#: pre-buffer pattern and a WAF change for a read), and a hundred uuids with
#: their JSON is ~4 KB. A 500-file drop is five lookups instead of five hundred
#: item reads, which is the shape this route exists to replace.
MAX_LOOKUP_IDS = 100


class LookupRequest(BaseModel):
    """The ids a client already holds -- the nodes a drop's commits reported."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_LOOKUP_IDS)


class LookupPage(BaseModel):
    """The readable items among the ids asked for, in the order asked. An id
    that does not exist, is another org's, or that the caller may not read is
    left out -- the same silence the single read keeps with its 404."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    value: list[Item]


# --------------------------------------------------------------------------
# the four steps every handler shares
# --------------------------------------------------------------------------


async def _render(
    files: FilesContext,
    decided: Authorized[Any],
    *,
    request: Request,
    facts: AccessFacts | None = None,
) -> Item:
    """The wire item for a node the caller just authorized.

    The head version and the caller's star came back with the node from the one
    statement ``authorize`` loaded it with, so the content hash, mime type and
    scan state a client syncs on — and the star a page draws — are rendered
    here rather than left at their schema defaults.

    The lease is the one facet that cannot come off the node: it is held on an
    ancestor and governs the subtree, so it costs the single statement over the
    chain ``authorize`` already loaded. Without it a second member reading a
    file inside a mounted folder was told nothing was holding it — ``lease``
    null and ``stale`` false — while the holder was pushing to it.

    The chain is decided in its own batch for the same reason it is loaded at
    all: the path names every folder above the node, and a caller holding one
    grant on one file may not read those names. One statement for the whole
    chain, not one per ancestor.

    ``facts`` is a parameter so the handler that already resolved the caller's
    memberships for its own decision does not pay that read twice.
    """
    if facts is None:
        async with as_platform(files.repo.session):
            facts = await facts_for(request, files.repo.session, files.ctx, files.drive)
    snapshot = await lease_snapshots.lease_facet(
        files.repo,
        decided.node,
        decided.chain,
        ctx=files.ctx,
        now=files.clock.now(),
    )
    chain_access = await access_by_id(
        files.repo,
        files.ctx,
        [NodeId(ancestor.id) for ancestor in decided.chain],
        facts=facts,
    )
    return to_item(
        decided.node,
        decided.chain,
        decided.access,
        snapshot,
        version=decided.head,
        starred=decided.starred,
        readable_chain={
            node_id for node_id, access in chain_access.items() if access.allows(FilesAction.READ)
        },
    )


async def with_lease_chats(files: FilesContext, request: Request, items: list[Item]) -> list[Item]:
    """The same items, each lease held for a chat naming that chat's title and
    whether THIS caller may open it.

    A box takes a chat's folder under a lease, and a person browsing that
    folder in Files is looking at a folder the chat is writing: the page shows
    the saved copy read-only and says which conversation holds it, linking
    there for the people who may follow the link. The id is a lease fact and
    rides the snapshot; the title and the permission are the chat's, resolved
    here, once for the page, the way owner names and object titles are.

    The permission is :func:`access.readable_chats`: the chat policy's READ
    answer over the same facts the chat routes decide with, without a decision
    row (this is a listing, not a door). It is never derived from the reader's
    rung on the folder they are looking at: a person shared one file inside a
    chat's folder may read that file and may not open the chat.

    Runs as the platform, after the Files transaction: the chat rows and the
    caller's memberships are not Files tables. Costs nothing on a page with no
    chat lease on it.
    """
    wanted: set[uuid.UUID] = set()
    for item in items:
        if item.lease is not None and item.lease.chat_id:
            wanted.add(uuid.UUID(item.lease.chat_id))
    if not wanted:
        return items
    session = files.repo.session
    chats = list(
        (
            await session.execute(
                select(WorkspaceObject).where(
                    WorkspaceObject.id.in_(wanted),
                    WorkspaceObject.type == CHAT_TYPE,
                    WorkspaceObject.deleted_at == 0,
                )
            )
        )
        .scalars()
        .all()
    )
    titles = {chat.id: chat.title or "" for chat in chats}
    owned = {chat.id for chat in chats if chat.owner_user_id == files.ctx.effective_user_id}
    readable = await _readable_chat_ids(files, request, chats)
    filled: list[Item] = []
    for item in items:
        facet = item.lease
        chat_id = uuid.UUID(facet.chat_id) if facet is not None and facet.chat_id else None
        if facet is None or chat_id is None or chat_id not in titles:
            filled.append(item)
            continue
        update: dict[str, object] = {
            "chat_title": titles[chat_id],
            "can_open_chat": chat_id in readable,
        }
        if chat_id in owned and facet.yours != "you":
            # The reader's own chat is named as theirs even on somebody
            # else's box: the chat is what they know it by.
            update["yours"] = "chat"
        lease = facet.model_copy(update=update)
        filled.append(item.model_copy(update={"lease": lease}))
    return filled


async def _readable_chat_ids(
    files: FilesContext, request: Request, chats: list[WorkspaceObject]
) -> set[uuid.UUID]:
    """The chats among ``chats`` this caller may open, by the chat policy.

    A caller with no user behind it (a machine credential that delegates for
    nobody) has no rung, no ownership and no audience, so it may open none:
    the lease still names the chat, the link is simply withheld.
    """
    if not chats:
        return set()
    session = files.repo.session
    ctx = files.ctx
    user_id = ctx.effective_user_id
    user = (
        (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if user_id is not None
        else None
    )
    if user is None:
        return set()
    reader = await access.resolve_reader(
        session, ctx=ctx, roles=role_resolver(request, session, ctx), user=user
    )
    return {chat.id for chat in await access.readable_chats(session, chats, reader)}


# --------------------------------------------------------------------------
# drives
# --------------------------------------------------------------------------


@router.get("/drives", response_model=DriveWire, dependencies=[Depends(ratelimited("drives"))])
async def get_drive(
    request: Request,
    files: FilesCtx,
    background: BackgroundTasks,
    chat_id: Annotated[
        str | None,
        Query(
            alias=CHAT_QUERY,
            description=(
                "For a box on its machine credential: the chat whose drive it asks for. "
                "A person's drive is their own and this is ignored."
            ),
        ),
    ] = None,
) -> DriveWire:
    """The caller's org drive, and the node their own files live under.

    The drive itself is ensured by the context dependency. The home folder is
    ensured here because this is the first Files request any client makes and
    the org's founder has no membership event to have created hers — see
    :func:`backend.services.files.home.caller_home`. ``background`` is where a
    stray subtree too large to move inside this request is run after the
    response, the way the move route runs its oversized moves.

    ``homeId`` is answered by the server rather than derived by the client from
    the root listing: the root's ``home`` child is the org-wide container, and a
    client that takes it for "my files" shows an org admin every colleague's
    home folder as her own landing screen.

    A box on its machine credential has no drive of its own — it serves chats
    in orgs it is no member of — so for it this route answers the drive of the
    CHAT it names, decided the way every other Files read on that chat's
    folder is: through the policy, as the machine the chat is bound to. A
    chat it does not hold, a chat in an org it does not serve, a chat with no
    folder and no chat named at all are the same opaque not-found. A box has
    no home."""
    if files.ctx.is_machine:
        return await _drive_of_the_chat_a_box_named(request, files, chat_id)
    drive = files.drive
    home = await caller_home(files, background=background)
    return DriveWire(
        id=str(drive.id),
        org_id=str(drive.org_team_id),
        root_id=str(drive.root_node_id),
        home_id=str(home.id) if home is not None else None,
        quota_bytes=drive.quota_bytes,
    )


async def _drive_of_the_chat_a_box_named(
    request: Request, files: FilesContext, chat_id: str | None
) -> DriveWire:
    """The drive of the chat a box asked about, through the decision on the
    chat's folder.

    The context already scoped the request to that folder's drive and refused
    a chat in an org the credential does not serve before any node was read,
    so a drive the box holds no chat in leaves no row under its name. What is
    decided here is the binding — a chat run on another machine, or on none,
    is the same not-found — and the row it leaves names the chat's folder,
    the same node every other read on that folder is decided on.
    """
    if chat_id is None:
        raise NotFound()
    async with as_platform(files.repo.session):
        folder = await FilesRepo.chat_folder_anywhere(files.repo.session, _uuid(chat_id, "chat"))
    if folder is None:
        raise NotFound()
    async with files_txn(files):
        await _resolve(request, files, files.drive.id, folder.id, FilesAction.READ)
    drive = files.drive
    return DriveWire(
        id=str(drive.id),
        org_id=str(drive.org_team_id),
        root_id=str(drive.root_node_id),
        home_id=None,
        quota_bytes=drive.quota_bytes,
    )


# --------------------------------------------------------------------------
# reading one item
# --------------------------------------------------------------------------


def _echo_etag(response: Response, item: Item) -> None:
    """Put the node's etag where HTTP says it lives.

    It rides the body too, but every mutation on the node demands it back in
    `If-Match` and a caller reaching for a precondition looks for the header
    first — reading one out of a JSON field is a step nobody expects, and the
    `428` it costs reads like a bug in the request rather than a missing
    header. Quoted, which is the spelling `parse_etag` accepts.
    """
    if item.etag:
        response.headers["ETag"] = f'"{item.etag}"'


@router.get(
    "/drives/{drive_id}/items/{item_id}",
    response_model=Item,
    dependencies=[Depends(ratelimited("items"))],
)
async def get_item(
    request: Request, response: Response, files: FilesCtx, drive_id: PathId, item_id: PathId
) -> Item:
    async with files_txn(files):
        # Resolved once and handed to both steps: the decision on the node and
        # the batched decision over its chain are the same caller's.
        async with as_platform(files.repo.session):
            facts = await facts_for(request, files.repo.session, files.ctx, files.drive)
        decided = await _resolve(
            request,
            files,
            _uuid(drive_id, "drive"),
            _uuid(item_id, "item"),
            FilesAction.READ,
            facts=facts,
        )
        item = await _render(files, decided, request=request, facts=facts)
    _echo_etag(response, item)
    # Outside the transaction: inside it the session runs as the Files app role,
    # which has no read on `users` — the name is not a Files fact, so it is
    # resolved once the decision has committed rather than under its role.
    async with as_platform(files.repo.session):
        named = await with_object_facet(
            files.repo.session,
            await with_owner_name(
                files.repo.session, item, org_team_id=files.repo.scope.org_team_id
            ),
        )
        return (await with_lease_chats(files, request, await with_lease_machines(files, [named])))[
            0
        ]


@router.get(
    "/drives/{drive_id}/root:/{item_path:path}",
    response_model=Item,
    dependencies=[Depends(ratelimited("items"))],
)
async def get_item_by_path(
    request: Request, response: Response, files: FilesCtx, drive_id: PathId, item_path: str
) -> Item:
    """The path form. The id form is canonical; this one exists so a client
    that knows only where a thing is can start.

    The resolution is a plain lookup and its answer is then authorized exactly
    as the id form's is — an unreadable node on the way down leaves the same
    404 as a path that does not exist, and the path never becomes a download
    filename because this route returns an item, never bytes.
    """
    async with files_txn(files):
        drive = _uuid(drive_id, "drive")
        namespace = Namespace(
            files.repo, files.ctx, files.clock, files.store, ceilings=files.ceilings
        )
        found = await namespace.resolve_path(DriveId(drive), item_path, drive=files.drive)
        node_id = found.id if found is not None else uuid.UUID(int=0)
        async with as_platform(files.repo.session):
            facts = await facts_for(request, files.repo.session, files.ctx, files.drive)
        decided = await _resolve(request, files, drive, node_id, FilesAction.READ, facts=facts)
        item = await _render(files, decided, request=request, facts=facts)
    _echo_etag(response, item)
    async with as_platform(files.repo.session):
        named = await with_object_facet(
            files.repo.session,
            await with_owner_name(
                files.repo.session, item, org_team_id=files.repo.scope.org_team_id
            ),
        )
        return (await with_lease_chats(files, request, await with_lease_machines(files, [named])))[
            0
        ]


@router.get(
    "/drives/{drive_id}/items/{item_id}:/{item_path:path}",
    response_model=Item,
    dependencies=[Depends(ratelimited("items"))],
)
async def get_item_under(
    request: Request,
    response: Response,
    files: FilesCtx,
    drive_id: PathId,
    item_id: PathId,
    item_path: str,
) -> Item:
    """The path form, anchored: the item at ``item_path`` BELOW ``item_id``.

    A caller is told a folder's path only from the deepest ancestor it may read
    (:func:`backend.services.files.items.readable_path_bytes`), so a holder
    that reads nothing above the folder it holds — a box on a chat's lease —
    knows the folder's bare name and cannot spell an absolute path to anything
    under it. What is under a node it holds is addressed from that node, by
    the step down: the same lookup the root form makes, started at the anchor,
    and its answer authorized exactly as the id form's is. An anchor the
    caller may not read, a step that names nothing and a step that climbs
    (``..`` is no node's name) all leave the same 404; an empty step is the
    anchor itself.
    """
    async with files_txn(files):
        drive = _uuid(drive_id, "drive")
        namespace = Namespace(
            files.repo, files.ctx, files.clock, files.store, ceilings=files.ceilings
        )
        found = await namespace.resolve_path(
            DriveId(drive), item_path, drive=files.drive, below=NodeId(_uuid(item_id, "item"))
        )
        node_id = found.id if found is not None else uuid.UUID(int=0)
        async with as_platform(files.repo.session):
            facts = await facts_for(request, files.repo.session, files.ctx, files.drive)
        decided = await _resolve(request, files, drive, node_id, FilesAction.READ, facts=facts)
        item = await _render(files, decided, request=request, facts=facts)
    _echo_etag(response, item)
    async with as_platform(files.repo.session):
        named = await with_object_facet(
            files.repo.session,
            await with_owner_name(
                files.repo.session, item, org_team_id=files.repo.scope.org_team_id
            ),
        )
        return (await with_lease_chats(files, request, await with_lease_machines(files, [named])))[
            0
        ]


@router.post(
    "/drives/{drive_id}/items/lookup",
    response_model=LookupPage,
    dependencies=[Depends(ratelimited("lookup")), Depends(caller_drive)],
)
async def lookup_items(request: Request, files: FilesCtx, body: LookupRequest) -> LookupPage:
    """Many items by id, in the statements one item costs.

    The finish of a folder drop used to read every file it had just committed
    back one GET at a time -- five hundred files, five hundred reads, and the
    per-principal read budget refused the tail of the drop. The client already
    holds the ids (the commit operations name them), so it asks for them here
    in one request, and the page is rendered the way a folder listing renders
    its rows: every row from its OWN access decision, one statement per fact
    for the whole page -- plus one chain walk per DISTINCT parent, which for
    the drop this exists for is one, and for the worst case a caller can write
    is still no more than the reads it replaces.

    A listing, not a door: like the children page, rows are cut by the same
    READ decision the single read makes and no decision row is written per
    id -- an id the caller may not read is simply absent, indistinguishable
    from one that never existed, so the response is not an oracle over ids.

    The drive in the path is refused by :func:`caller_drive` before the body is
    even validated, so a stranger's drive id is the opaque 404 every other
    route in the family answers rather than a 422 about the body.
    """
    drive = files.drive.id
    wanted = [NodeId(node_id) for node_id in dict.fromkeys(body.ids)]
    async with files_txn(files):
        async with as_platform(files.repo.session):
            facts = await facts_for(request, files.repo.session, files.ctx, files.drive)
        rows = await files.repo.node_rows(wanted, starred_for=files.ctx.effective_user_id)
        # A node under another drive is not this drive's to describe, whatever
        # the caller may read of it.
        rows = {node_id: row for node_id, row in rows.items() if row.node.drive_id == drive}
        # The chain above each node, for its path and for cutting that path to
        # the folders the caller may name. A drop's files share a parent, so
        # the distinct parents are few; each chain is one statement.
        parents = await files.repo.nodes(
            [NodeId(row.node.parent_id) for row in rows.values() if row.node.parent_id]
        )
        chains: dict[uuid.UUID, list[FileNode]] = {
            parent.id: list(await files.repo.chain(parent)) for parent in parents
        }
        chain_ids = {ancestor.id for chain in chains.values() for ancestor in chain}
        decided = await access_by_id(
            files.repo,
            files.ctx,
            [*(NodeId(node_id) for node_id in chain_ids), *(NodeId(node_id) for node_id in rows)],
            facts=facts,
        )
        readable_chain = {
            node_id
            for node_id in chain_ids
            if node_id in decided and decided[node_id].allows(FilesAction.READ)
        }
        readable = [
            rows[node_id]
            for node_id in wanted
            if node_id in rows and node_id in decided and decided[node_id].allows(FilesAction.READ)
        ]
        snapshots = await lease_snapshots.lease_facets(
            files.repo,
            [
                (
                    row.node.id,
                    [
                        ancestor.id
                        for ancestor in chains.get(row.node.parent_id or uuid.UUID(int=0), [])
                    ],
                )
                for row in readable
            ],
            ctx=files.ctx,
            now=files.clock.now(),
        )
        items = [
            to_item(
                row.node,
                chains.get(row.node.parent_id or uuid.UUID(int=0), []),
                decided[row.node.id],
                snapshots.get(row.node.id),
                version=row.head,
                starred=row.starred,
                readable_chain=readable_chain,
            )
            for row in readable
        ]
    async with as_platform(files.repo.session):
        named = await with_lease_chats(
            files,
            request,
            await with_lease_machines(
                files,
                await with_object_facets(
                    files.repo.session,
                    await with_owner_names(
                        files.repo.session, items, org_team_id=files.repo.scope.org_team_id
                    ),
                ),
            ),
        )
    return LookupPage(value=named)


# --------------------------------------------------------------------------
# children
# --------------------------------------------------------------------------


@router.get(
    "/drives/{drive_id}/items/{item_id}/children",
    response_model=ChildrenPage,
    dependencies=[Depends(ratelimited("children"))],
)
async def list_children(
    request: Request,
    files: FilesCtx,
    drive_id: PathId,
    item_id: PathId,
    limit: int = 100,
    order_by: Annotated[str | None, Query(alias="orderBy")] = None,
    marker: Annotated[str | None, Query()] = None,
    kind: Annotated[str | None, Query()] = None,
    object_type: Annotated[str | None, Query(alias="objectType")] = None,
    mime_class: Annotated[str | None, Query(alias="mimeClass")] = None,
    owner: Annotated[str | None, Query()] = None,
    modified_after: Annotated[str | None, Query(alias="modifiedAfter")] = None,
    modified_before: Annotated[str | None, Query(alias="modifiedBefore")] = None,
    size_min: Annotated[str | None, Query(alias="sizeMin")] = None,
    size_max: Annotated[str | None, Query(alias="sizeMax")] = None,
    name_flag: Annotated[str | None, Query(alias="nameFlag")] = None,
    starred: Annotated[str | None, Query()] = None,
    shared: Annotated[str | None, Query()] = None,
    leased: Annotated[str | None, Query()] = None,
    trashed: Annotated[str | None, Query()] = None,
) -> ChildrenPage:
    """One keyset page of a folder's children.

    The readability predicate is handed to the library so the page is cut
    *after* authorization: a hidden sibling shortens nothing and so cannot be
    inferred from a page size.

    Every chip is declared here rather than read only off the query string, so
    the generated clients can send a filter through their typed call instead of
    smuggling it past them. They are declared as strings because
    `ListFilters.from_query` stays the one parser: it is what turns `sizeMin=x`
    into a `400 files.bad_filter` and an unknown key into a `400
    files.unknown_filter`, and a second parser in the signature would answer a
    different status for the same request.
    """
    async with files_txn(files):
        async with as_platform(files.repo.session):
            facts = await facts_for(request, files.repo.session, files.ctx, files.drive)
        parent = await _resolve(
            request,
            files,
            _uuid(drive_id, "drive"),
            _uuid(item_id, "item"),
            FilesAction.READ,
            facts=facts,
        )
        params = {k: v for k, v in request.query_params.items() if k not in _LISTING_KEYS}
        me = files.ctx.effective_user_id or uuid.UUID(int=0)
        filters = ListFilters.from_query(params, me=me)
        order = _order(order_by)
        marker_in = Marker.decode(marker, parent_id=parent.node.id, order=order) if marker else None
        page = await listing.children(
            files.repo,
            files.ctx,
            parent.node.id,
            order_by=order,
            filters=filters,
            marker=marker_in,
            limit=limit,
        )
        rows_out = list(page.items)
        # Every row is rendered from its OWN decision, never the listed folder's.
        # The decider reads a node's own `flags` and not its chain's, so a
        # `NO_DOWNLOAD` chat folder in a downloadable home — and the `ARTIFACT`
        # working directory inside that chat — differ from their parent in exactly
        # the capability a client gates its Download affordance on; stamping the
        # parent's access on the page told the client the opposite of what the
        # content route answers. Batched, so the page costs statements rather
        # than statements per row.
        # The listed folder's own chain rides in the SAME batch, because every
        # row's path is built from it: a caller who may read one folder deep in
        # somebody else's home must not be handed the names above it, and
        # deciding the chain separately would be a second statement per page.
        parent_chain_ids = [NodeId(ancestor.id) for ancestor in parent.chain]
        decided = await access_by_id(
            files.repo,
            files.ctx,
            [*parent_chain_ids, *(NodeId(row.id) for row in rows_out)],
            facts=facts,
        )
        readable_chain = {
            node_id
            for node_id in parent_chain_ids
            if node_id in decided and decided[node_id].allows(FilesAction.READ)
        }
        # A row that vanished between the keyset page and this decision has no
        # access to state, and is gone: rendering it with a borrowed one is the
        # lie this batch exists to remove.
        rows_out = [row for row in rows_out if row.id in decided]
        if parent.node.traversal_only:
            # A signpost is listable by every member of the org, so this is the
            # one listing whose parent grant says nothing about its children:
            # `home/` would otherwise publish the whole roster of member folders
            # to anyone who asked. Cut it by the same READ decision the direct
            # read makes, and cut it *after* the keyset page so a hidden sibling
            # still cannot be counted from a short one.
            rows_out = [row for row in rows_out if decided[row.id].allows(FilesAction.READ)]
        else:
            # A chat folder is the one child a folder's grant says nothing
            # about: the chat's own rule decides it, and a member's private
            # chat sits in a `Chats` folder an org admin may read while the
            # chat is not theirs. Rendering that row from its own decision
            # still handed over its id and its title, so it is cut here — and
            # only that kind, because every other child inherits the folder's
            # grants and a page of them must not shorten.
            rows_out = [
                row
                for row in rows_out
                if row.subtype != CHAT_SUBTYPE or decided[row.id].allows(FilesAction.READ)
            ]
        # ``authorize`` loads the chain with ``repo.chain``, which already ends with the
        # node itself; appending the parent again read every listed row as
        # ``/papers/papers/draft.txt``.
        chain = list(parent.chain)
        # One statement for the whole page: every row shares this chain, so the
        # leases that could govern them are the chain's plus each row's own.
        chain_ids = [ancestor.id for ancestor in chain]
        snapshots = await lease_snapshots.lease_facets(
            files.repo,
            [(row.id, chain_ids) for row in rows_out],
            ctx=files.ctx,
            now=files.clock.now(),
        )
        # One statement for the whole page, like the lease lookup above: a row's
        # head carries its `content_hash`, and without it a client that lists a
        # folder cannot tell whether it already holds the bytes — `alkera files
        # pull` would re-download an unchanged tree every time.
        heads = await files.repo.versions_by_id(
            [VersionId(row.head_version_id) for row in rows_out if row.head_version_id is not None]
        )
        rows = [
            to_item(
                row,
                chain,
                decided[row.id],
                snapshots.get(row.id),
                version=heads.get(row.head_version_id) if row.head_version_id else None,
                starred=row.id in page.starred_ids,
                readable_chain=readable_chain,
            )
            for row in rows_out
        ]
        marker = page.next_marker.encode() if page.next_marker is not None else None
    # One statement for the whole page, like the lease lookup above: a name per
    # row would be a statement per row. It runs once the transaction has closed
    # because inside it the session is the Files app role, which has no read on
    # `users` — and a name is not a Files fact.
    async with as_platform(files.repo.session):
        named = await with_lease_chats(
            files,
            request,
            await with_lease_machines(
                files,
                await with_object_facets(
                    files.repo.session,
                    await with_owner_names(
                        files.repo.session, rows, org_team_id=files.repo.scope.org_team_id
                    ),
                ),
            ),
        )
    return ChildrenPage(value=named, next_marker=marker)


#: What a caller is told when they ask this route for a file. The code names
#: the next call rather than the mistake: the bytes are what makes the file, so
#: the answer is "send them", not "that field is not allowed here".
FILE_NEEDS_CONTENT = "files.file_needs_content"


def _refuse_a_file(body: CreateChild) -> None:
    """A create naming a file — by ``kind`` or by carrying a ``file`` facet —
    is refused, never rounded down to a folder."""
    if body.kind != "file" and body.file is None:
        return
    raise InvalidRequest(
        FILE_NEEDS_CONTENT,
        "a file is created by writing its first version, not as a namespace entry",
        detail={"kind": "file"},
    )


@router.post(
    "/drives/{drive_id}/items/{item_id}/children",
    response_model=Item,
    status_code=201,
    dependencies=[Depends(ratelimited("creates"))],
)
@idempotent_route("files.items.create_child", status=201)
async def create_child(
    request: Request,
    files: FilesCtx,
    drive_id: PathId,
    item_id: PathId,
    body: CreateChild,
    idempotency: Idempotency,
    lease: Lease,
) -> Item:
    """Add one folder, symlink or special node under a folder.

    ``WRITE`` is decided on the *parent*, which is the node the caller is
    changing; the child does not exist yet and so has no access of its own.

    A body asking for a file is refused here rather than served as a folder:
    this route makes namespace entries, and a file only exists once its first
    version does.
    """
    _refuse_a_file(body)
    async with files_txn(files):
        drive = _uuid(drive_id, "drive")
        parent = await _resolve(request, files, drive, _uuid(item_id, "item"), FilesAction.WRITE)
        # Creating a child IS a write into the folder, so it is fenced like a
        # rename or a move: an unfenced POST into somebody else's mount must be
        # refused *before* the node exists, or the holder's next reconcile has
        # to discover a node it never wrote as a conflict. INTO the folder,
        # exactly as the library fences it below: a lease that admits other
        # people's writes admits them into the chat's working directory and
        # nowhere else, and a fence that read the create as a write ON the
        # folder refused a person the one drop the admission exists for.
        fence = _fence(lease, files)
        await fenced_create_into(files.repo, parent.node, fence)
        namespace = Namespace(
            files.repo, files.ctx, files.clock, files.store, ceilings=files.ceilings
        )
        # The library fences the create again on its own; handed no lease it
        # refuses the holder's own write as somebody else's mount.
        node = await namespace.create(
            DriveId(drive),
            NodeId(parent.node.id),
            body.kind,
            _name_bytes(body.name),
            conflict=body.conflict_behavior,
            symlink_target=(
                _name_bytes(body.symlink_target) if body.symlink_target is not None else None
            ),
            symlink_kind=body.symlink_kind,
            subtype=body.subtype,
            lease=fence,
        )
        return to_item(node, list(parent.chain), parent.access)


@router.post(
    "/drives/{drive_id}/items/{item_id}/tree",
    response_model=list[Item],
    status_code=201,
    dependencies=[Depends(ratelimited("creates"))],
)
@idempotent_route("files.items.create_tree", status=201)
async def create_tree(
    request: Request,
    files: FilesCtx,
    drive_id: PathId,
    item_id: PathId,
    body: TreeCreate,
    idempotency: Idempotency,
    lease: Lease,
) -> list[Item]:
    """The folder skeleton of a dropped directory, in one call.

    Idempotent by construction rather than only by key: a path that already
    exists is walked into, not re-created, so a retry that lost its answer and
    a re-drop of the same directory both leave one tree.

    The library's ``tree.create_tree`` is the seam this walk belongs behind,
    but it reserves exactly as many inos as it creates folders, so the drive's
    ``next_ino`` would move by the number of folders a caller just made, and a
    counter that moves with another caller's activity is an oracle on it. The
    walk stays until the reservation takes whole blocks.
    """
    async with files_txn(files):
        drive = _uuid(drive_id, "drive")
        top_id = _uuid(item_id, "item")
        # A signpost — the drive root, ``home/``, ``Teams/`` — is walked, never
        # written: every member may enter it and nobody may make anything
        # directly in it. A tree posted there needs READ to enter, and WRITE is
        # decided below, on the folder each new one is actually made in. That is
        # what lets a box push a chat folder from the root through
        # ``home/<owner>/`` as a plain member, while a path that would plant
        # something directly under a signpost is still refused by the library's
        # own rule and one that turns into a stranger's home by that home.
        async with as_platform(files.repo.session):
            facts = await facts_for(request, files.repo.session, files.ctx, files.drive)
        top = await files.repo.node(NodeId(top_id))
        entering = FilesAction.READ if top is not None and top.traversal_only else FilesAction.WRITE
        parent = await _resolve(request, files, drive, top_id, entering, facts=facts)
        fence = _fence(lease, files)
        # The tree's first level lands INTO the top, like any create; each
        # deeper folder is fenced by the library as it is made.
        await fenced_create_into(files.repo, parent.node, fence)
        namespace = Namespace(
            files.repo, files.ctx, files.clock, files.store, ceilings=files.ceilings
        )
        made: list[Item] = []
        # The folders WRITE has been decided on: the top when it took that
        # decision itself, and — below a signpost — each folder as the walk
        # first makes something in it. A decision covers everything under it.
        writable: dict[uuid.UUID, Authorized[Any]] = (
            {} if entering is FilesAction.READ else {parent.node.id: parent}
        )
        # ``authorize`` loads the chain with ``repo.chain``, which already ends with the
        # node itself; appending the parent again read every listed row as
        # ``/papers/papers/draft.txt``.
        chain = list(parent.chain)
        for wire_path in body.paths:
            cursor = parent.node
            cursor_chain = list(chain)
            for segment in [part for part in wire_path.split("/") if part]:
                name = _name_bytes(segment)
                existing = [
                    row for row in await files.repo.siblings(NodeId(cursor.id)) if row.name == name
                ]
                if existing:
                    node = existing[0]
                    # A folder the walk enters is decided on its own: a chat
                    # folder inside a folder the caller may write is not theirs
                    # to enter, and naming it in a path must not plant a folder
                    # in it. Refused as the by-id door refuses it.
                    entered = await access_by_id(
                        files.repo, files.ctx, [NodeId(node.id)], facts=facts
                    )
                    if not (node.id in entered and entered[node.id].allows(FilesAction.READ)):
                        await _resolve(
                            request, files, drive, node.id, FilesAction.READ, facts=facts
                        )
                else:
                    holder = _write_holder(writable, cursor_chain)
                    if holder is None:
                        holder = await _resolve(
                            request, files, drive, cursor.id, FilesAction.WRITE, facts=facts
                        )
                        writable[cursor.id] = holder
                    # Every folder made is a write inside the parent the caller
                    # holds; the library fences each one again on its own, and
                    # handed no lease it refused the holder's own skeleton.
                    node = await namespace.create(
                        DriveId(drive), NodeId(cursor.id), "folder", name, lease=fence
                    )
                    made.append(to_item(node, cursor_chain, holder.access))
                cursor_chain = [*cursor_chain, node]
                cursor = node
        return made


def _write_holder(
    writable: dict[uuid.UUID, Authorized[Any]], chain: Sequence[FileNode]
) -> Authorized[Any] | None:
    """The WRITE decision that covers making something at the end of ``chain``.

    A decision on a folder covers everything under it except a chat folder
    and a chat's record folder: the chat's own rule decides what is inside
    the one, and only the machine holding the chat's lease may put anything
    inside the other, so the nearest decision at or below the innermost of
    either is the only one that counts. Without the second boundary a person
    with edit on the chat could raise folders inside ``.runtime/`` on the
    strength of the decision taken at the chat's top, which the decision on
    the record folder itself refuses them.
    """
    for row in reversed(chain):
        if row.id in writable:
            return writable[row.id]
        if row.subtype == CHAT_SUBTYPE or row.flags & RECORD_BIT:
            return None
    return None


# --------------------------------------------------------------------------
# changing one item
# --------------------------------------------------------------------------


@router.patch(
    "/drives/{drive_id}/items/{item_id}",
    response_model=Item,
    # A move too large to run inline answers 202 with the operation instead of
    # the node; a client that only knows the 200 has no typed shape to poll.
    responses={202: {"model": OperationWire, "description": "The move runs as an operation"}},
    dependencies=[Depends(ratelimited("items"))],
)
@idempotent_route("files.items.patch_item")
async def patch_item(
    request: Request,
    files: FilesCtx,
    drive_id: PathId,
    item_id: PathId,
    body: PatchItem,
    if_match: IfMatch,
    idempotency: Idempotency,
    lease: Lease,
    response: Response,
    background: BackgroundTasks,
    conflict_behavior: Literal["fail", "rename"] = "fail",
) -> Any:
    """Rename, move, and set attributes — in that order, in one transaction.

    The etag rides *inside* each statement rather than being checked first, so
    a caller holding a stale one changes nothing at all: the update matches no
    row and the library raises the 412. The order matters because a rename and
    a move in one call must not race each other for the destination name; doing
    the rename first means the move carries the name the caller asked for.
    """
    async with files_txn(files):
        drive = _uuid(drive_id, "drive")
        decided = await _resolve(request, files, drive, _uuid(item_id, "item"), FilesAction.WRITE)
        assert if_match is not None  # a mutation without one is the dependency's 428
        namespace = Namespace(
            files.repo, files.ctx, files.clock, files.store, ceilings=files.ceilings
        )
        fence = _fence(lease, files)
        node = decided.node
        etag = if_match
        if body.name is not None:
            node = await namespace.rename(
                NodeId(node.id),
                _name_bytes(body.name),
                if_match=etag,
                conflict=conflict_behavior,
                lease=fence,
            )
            etag = node.etag
        if body.parent_id is not None:
            # The destination is a node the caller is writing INTO, so it is
            # decided exactly as the copy route decides its own: without this a
            # writer could graft their node into any folder in the org whose id
            # they knew — planting content there and, because a move re-derives
            # the subtree's inherited ACEs from its new parent, handing that
            # folder's principals a role on it. The same call is the drive
            # check, so a destination in a second drive is the opaque 404
            # rather than a node whose `drive_id` and `path_ids` disagree.
            destination = await _resolve(
                request, files, drive, _uuid(body.parent_id, "parentId"), FilesAction.WRITE
            )
            await refuse_a_move_that_raises_the_mover(
                decided, destination, decide=_by_id(request, files, drive)
            )
            moved = await namespace.move(
                NodeId(node.id),
                NodeId(destination.node.id),
                if_match=etag,
                conflict=conflict_behavior,
                lease=fence,
            )
            if isinstance(moved, OperationState):
                queue_inline(background, files, "large_move", OperationId(moved.id))
                return _queued_move(drive_id, moved)
            node = moved
            etag = node.etag
        if body.attrs is not None:
            await ops.set_attrs(
                files.repo, files.ctx, NodeId(node.id), _attr_columns(body.attrs), lease=fence
            )
            await files.repo.session.refresh(node)
        chain = await files.repo.chain(node)
        return to_item(node, chain, decided.access, version=decided.head, starred=decided.starred)


def _by_id(
    request: Request, files: FilesContext, drive: uuid.UUID, *, facts: AccessFacts | None = None
) -> Decide:
    """This request's by-id door, for the rules that decide one more node.

    The shared guards take a callable rather than a request so the batch can
    bind the engine its own way; here it is ``_resolve`` itself, so a node a
    guard decides is decided exactly as it would be named in the path.
    """

    async def decide(node_id: uuid.UUID, action: FilesAction) -> Authorized[Any]:
        return await _resolve(request, files, drive, node_id, action, facts=facts)

    return decide


def _queued_move(drive_id: str, state: OperationState) -> Response:
    """A subtree too large to move inline answers 202 with the operation.

    The tree has not changed yet, so returning the node would say it had. The
    body is the same ``Operation`` the operations routes render — one shape and
    one client progress path — and ``Location`` names where its progress is
    read.
    """
    return queued_answer(drive_id, state)


def _attr_columns(patch: AttrsPatch) -> dict[str, Any]:
    """The `AttrsPatch` reduced to what ``set_attrs`` accepts.

    ``xattrs`` is a whole-document replace, so an empty mapping is a *clear*
    and has to survive the "named nothing" filter that drops the unset fields —
    ``None`` is "the client said nothing about xattrs", ``{}`` is "remove them".
    """
    named: dict[str, Any] = {
        "mode": patch.mode,
        "uid": patch.uid,
        "gid": patch.gid,
        "atime_ns": patch.atime_ns,
        "mtime_ns": patch.mtime_ns,
        "xattrs": patch.xattrs,
    }
    chosen = {name: value for name, value in named.items() if value is not None}
    if not chosen:
        raise InvalidRequest("files.empty_attrs", "attrs named nothing this server can set")
    return chosen


@router.delete(
    "/drives/{drive_id}/items/{item_id}",
    response_model=None,
    status_code=200,
    responses={
        200: {"model": OperationWire, "description": "The trash, as an undoable operation"},
        204: {"description": "The purge; there is nothing left to undo"},
    },
    dependencies=[Depends(ratelimited("items"))],
)
@idempotent_route("files.items.delete_item")
async def delete_item(
    request: Request,
    files: FilesCtx,
    drive_id: PathId,
    item_id: PathId,
    if_match: TrashIfMatch,
    idempotency: Idempotency,
    lease: Lease,
    permanent: bool = False,
) -> Response:
    """Trash by default; ``?permanent=true`` purges.

    They are two different rungs, so they are two different decisions. The
    ladder puts trashing beside rename and move on the *writer* rung: it is
    undoable, the row stays, and a writer who may replace a file's bytes may
    obviously put it in the bin. ``DELETE`` is the owner rung and means the
    purge, bytes and row gone with nothing to restore. Deciding both as
    ``DELETE`` made a writer unable to trash their own upload; deciding both as
    ``WRITE`` would hand every writer the unrecoverable one.

    Trashing is undoable, so it answers with its ``Operation`` (one undo path);
    the purge has no inverse and answers 204. A trash first has the machines
    holding folders under the item push, so their work goes into the trash too.
    """
    if not permanent:
        await files_nodes.flush_holders_under(request, files, drive_id, item_id)
    async with files_txn(files):
        drive = _uuid(drive_id, "drive")
        action = FilesAction.DELETE if permanent else FilesAction.WRITE
        decided = await _resolve(request, files, drive, _uuid(item_id, "item"), action)
        trash = Trash(files.repo, files.ctx, files.clock, files.store)
        if permanent:
            await trash.purge(NodeId(decided.node.id), lease=_fence(lease, files))
            return Response(status_code=204)
        operations = Operations(files.repo, files.ctx, files.clock, files.store)
        # The window is the trash window: the undo *is* the restore, and a
        # restore is impossible the moment the purge sweeper takes the subtree,
        # so offering an undo past that point would be offering a 404.
        async with operations.perform(
            "trash",
            drive_id=DriveId(drive),
            undoable_until=files.clock.now() + TRASH_WINDOW,
        ) as started:
            await trash.trash(
                NodeId(decided.node.id),
                if_match=if_match,
                op=operations,
                lease=_fence(lease, files),
            )
        state = await operations.get(started.id)
    return Response(
        content=to_wire(state).model_dump_json(by_alias=True),
        status_code=200,
        media_type="application/json",
        headers={"Location": f"{PREFIX}/drives/{drive_id}/operations/{state.id}"},
    )


@router.post(
    "/drives/{drive_id}/items/{item_id}/copy",
    response_model=OperationWire,
    status_code=202,
    dependencies=[Depends(ratelimited("items"))],
)
@idempotent_route("files.items.copy_item", status=202)
async def copy_item(
    request: Request,
    files: FilesCtx,
    drive_id: PathId,
    item_id: PathId,
    body: CopyItem,
    idempotency: Idempotency,
    background: BackgroundTasks,
    lease: Lease,
) -> OperationWire:
    """Copy a node into another folder as a tracked operation.

    Always 202: a one-file copy and a 40,000-file copy take the same shape, so
    a client writes one progress path instead of two, and the server is free to
    move the work off the request without changing the contract.
    """
    async with files_txn(files):
        drive = _uuid(drive_id, "drive")
        async with as_platform(files.repo.session):
            facts = await facts_for(request, files.repo.session, files.ctx, files.drive)
        # COPY, not READ: the policy's copy refusals — a trashed or sealed
        # source, one under a "goes no further" folder, one read only through
        # a grant that runs out — exist for this verb, and a door that decided
        # READ walked past every one of them.
        source = await _resolve(
            request, files, drive, _uuid(item_id, "item"), FilesAction.COPY, facts=facts
        )
        target = await _resolve(
            request, files, drive, _uuid(body.parent_id, "parentId"), FilesAction.WRITE, facts=facts
        )
        # A copy aimed at a chat lands where a move does — in the chat's
        # sandbox, minted if the chat predates it — so the copied file is where
        # the conversation reads, not beside its working folders.
        landing = await drop_target_for(
            files.repo,
            files.ctx,
            Namespace(files.repo, files.ctx, files.clock, files.store, ceilings=files.ceilings),
            target.node,
        )
        # The destination is a folder the copy writes INTO, so it is fenced
        # like a create there: a copy is the verb that can put the most bytes
        # into somebody's mount, and a runner started here would otherwise
        # plant a whole tree inside a folder its holder is the only writer of.
        await fenced_write_for(files.repo, landing, _fence(lease, files), into=True)
        # A copy is new usage — for the drive and for the caller, who owns
        # every node it makes — so it is decided here, before the 202, the way
        # an upload is decided at open rather than by a worker nobody watches.
        copied = [
            row
            for row in await files.repo.nodes_by_path_prefix(source.node.path_ids)
            if row.trashed_at is None
        ]
        # The subtree is decided before it is queued: a chat folder the caller
        # cannot open must not ride out inside a parent they can.
        await refuse_a_copy_over_hidden_chats(
            files.repo,
            files.ctx,
            source.node,
            facts=facts,
            decide=_by_id(request, files, drive, facts=facts),
            action=FilesAction.COPY,
            rows=copied,
        )
        await QuotaService(
            files.repo, files.ctx, files.clock, files.store, ceilings=files.ceilings
        ).assert_room(
            DriveId(drive),
            bytes=sum(row.size for row in copied if row.kind == "file"),
            nodes=len(copied),
            parent_path=landing.path_ids,
        )
        operations = Operations(files.repo, files.ctx, files.clock, files.store)
        # `start_copy`, not `Operations.start`: the plan and the row are written
        # in one transaction, so the runner that picks the row up always finds
        # something to run. A bare `start` queues an operation whose only
        # possible end is `failed`.
        op_id = await start_copy(
            files.repo,
            operations,
            node=source.node,
            dest_parent=landing,
        )
        state = await operations.get(op_id)
    queue_inline(background, files, "copy", op_id)
    return to_wire(state)


#: How many chats and templates one copy may adopt. A copy of a folder makes a
#: new object for every one it carries, and each is a row, a projection and an
#: announcement: past this many the request is refused outright rather than run
#: until something gives, so a member who aimed a duplicate at their whole
#: drive learns it instead of half-copying it.
MAX_COPIED_OBJECTS = 200


def _as_uuid(raw: object) -> uuid.UUID | None:
    """``raw`` as a UUID, or ``None`` when it is not one."""
    try:
        return uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


async def _adopt_one(
    files: FilesContext,
    db: AsyncSession,
    *,
    ctx: ActingContext,
    user: User,
    node_id: uuid.UUID,
    found: WorkspaceObject,
    title: str,
) -> WorkspaceObject:
    """Make the copied folder at ``node_id`` a NEW object of ``found``'s kind.

    The object is created first and the node stamped second, both after the
    tree is whole: a partial copy never leaves a chat with half a folder.
    """
    # The object, its machine and its announcement all live on tables the Files
    # role cannot see, and this runs inside the idempotency claim's transaction
    # — so the role is in force. The window is exactly these statements; the
    # tree write below stays the tenant's.
    async with as_platform(db):
        # Placed the way a chat started from the composer is placed, and for the
        # same reason: the copy is opened immediately and has to be able to take
        # a message. Nothing else runs, so nothing else is placed.
        binding = (
            await resolve_machine_for(db, ctx=ctx, org_team_id=ctx.org_id, purpose="chat")
            if found.type == CHAT_TYPE
            else None
        )
        made = await duplicate_service.duplicate_folder_object(
            db,
            source=found,
            owner=user,
            org_id=ctx.org_id,
            title=title,
            machine_id=str(binding.machine_id) if binding else None,
            machine_status=binding.chat_status if binding else "none",
        )
        if made.type == CHAT_TYPE:
            await chat_service.announce_chat(db, chat=made, actor=ctx.audit_dict())
        else:
            await object_service.announce(db, obj=made, actor=ctx.audit_dict())
    async with files_txn(files):
        copied = await files.repo.node(NodeId(node_id))
        if copied is None:  # pragma: no cover - the copy just made it
            raise NotFound()
        await adopt_copied_folder_node(files.repo, files.ctx, copied, made, clock=files.clock)
    return made


async def _adopt_nested(
    files: FilesContext,
    db: AsyncSession,
    *,
    ctx: ActingContext,
    user: User,
    root_id: uuid.UUID,
    sources: Mapping[uuid.UUID, uuid.UUID],
) -> None:
    """Adopt every chat and template the copied subtree carries.

    A subtree copy brings the folders across and nothing that made them
    conversations: without this, a copied chat is a folder that opens as the
    SOURCE's chat, or as nothing at all. Each copy is matched to the node it
    was made from — the copy records that itself. A chat folder the caller
    could not read is not among them because the duplicate refused the whole
    copy before writing a row (``refuse_a_copy_over_hidden_chats``), not
    because anything here checks.

    The title is the source's, not a "(copy)": the folder around them is what
    was duplicated and what carries the new name, and suffixing everything
    inside it would rename a whole shelf.
    """
    made_from: dict[uuid.UUID, uuid.UUID] = {}
    async with files_txn(files):
        root = await files.repo.node(NodeId(root_id))
        if root is None:  # pragma: no cover - the copy just made it
            raise NotFound()
        for row in await files.repo.nodes_by_path_prefix(root.path_ids):
            if row.id == root.id or row.trashed_at is not None or row.kind != "folder":
                continue
            if (row.subtype or "") not in FOLDER_OBJECT_KINDS:
                continue
            came_from = (row.node_metadata or {}).get("copied_from")
            source_id = _as_uuid(came_from)
            if source_id is not None and source_id in sources:
                made_from[uuid.UUID(str(row.id))] = sources[source_id]
    for node_id, object_id in made_from.items():
        # `workspace_objects` is not a Files table, and the role is still in
        # force here: read the source as the platform, one object at a time.
        async with as_platform(db):
            found = await access.load_object(db, object_id)
        if found is None:  # pragma: no cover - deleted between the copy and here
            continue
        await _adopt_one(
            files,
            db,
            ctx=ctx,
            user=user,
            node_id=node_id,
            found=found,
            title=found.title,
        )


@router.post(
    "/drives/{drive_id}/items/{item_id}/duplicate",
    response_model=DuplicateResult,
    status_code=201,
    dependencies=[Depends(ratelimited("items"))],
)
@idempotent_route("files.items.duplicate_item", status=201)
async def duplicate_item(
    request: Request,
    files: FilesCtx,
    drive_id: PathId,
    item_id: PathId,
    body: DuplicateItem,
    idempotency: Idempotency,
    ctx: CurrentPrincipal,
    user: CurrentUser,
    db: DbSession,
) -> DuplicateResult:
    """Copy a node into the caller's own drive — or a folder they name — and
    answer the copy.

    One operation for everything the tree holds: a file, a folder with all
    beneath it, a chat, a chat template, a saved query, a report. The source is
    decided as ``COPY`` (reading it is all a copy takes; the policy refuses a
    trashed or a sealed source), the destination as ``WRITE``. With no
    destination a folder object lands in the place its own kind names inside
    the caller's home — a chat in ``Chats``, a template in ``Chat Templates``,
    created if missing — and anything else in their home.

    The copy is NEW: new ids, the copier as owner, the destination's sharing
    and nothing of the source's, stamps set now. A chat's copy is a new chat
    object holding the source's transcript and no machine; it opens and binds
    like a fresh chat. A template's copy is a new template holding the same
    starting files, and unsealed, because a template is material a member is
    meant to take away. A query's or a report's copy is a new object with its
    own payload, projected into the tree like a new one. A plain folder that
    HOLDS chats or templates copies each of them as one of its own too, so a
    copied conversation is never a second door onto the source's rows.

    Synchronous, in batches that each commit: a large folder's copy is the
    same resumable operation the tracked ``/copy`` route runs, driven here to
    its end so the answer can name the node. Should the request die mid-way,
    what was committed stays as a whole tree and the operation row is the
    worker's to resume — the chat object is only made once the tree is whole,
    so a partial copy never leaves a chat with half a folder.
    """
    drive = _uuid(drive_id, "drive")
    # The caller's home is where an unaddressed copy lands. Resolved before the
    # transaction: ensuring it runs transactions of its own.
    home = await caller_home(files) if body.destination_id is None else None
    async with files_txn(files):
        async with as_platform(files.repo.session):
            facts = await facts_for(request, files.repo.session, files.ctx, files.drive)
        source = await _resolve(
            request, files, drive, _uuid(item_id, "item"), FilesAction.COPY, facts=facts
        )
        node = source.node
        folder_kind = folder_object_kind(node)
        object_id = node.target_object_id
        as_object = object_id is not None and (
            node.kind == "object"
            or (node.kind == "folder" and node.subtype in CONTEXT_FOLDER_TYPES)
        )
        if body.destination_id is not None:
            dest_id = _uuid(body.destination_id, "destinationId")
        elif home is None:
            raise InvalidRequest("this caller has no home to copy into; name a destination")
        elif folder_kind is not None:
            place = await home_place(files, folder_kind, home)
            dest_id = uuid.UUID(str(place.id))
        else:
            dest_id = uuid.UUID(str(home.id))
        target = await _resolve(request, files, drive, dest_id, FilesAction.WRITE, facts=facts)
        landing = await drop_target_for(
            files.repo,
            files.ctx,
            Namespace(files.repo, files.ctx, files.clock, files.store, ceilings=files.ceilings),
            target.node,
        )
        title: str | None = None
        root_name: bytes | None = None
        op_id: OperationId | None = None
        nested: dict[uuid.UUID, uuid.UUID] = {}
        if object_id is not None and (folder_kind is not None or as_object):
            async with as_platform(files.repo.session):
                found = await access.load_object(files.repo.session, uuid.UUID(str(object_id)))
            if found is None:
                raise NotFound()
            # A copy the caller named takes that name. The client asks for it
            # before the copy is made — the copy is opened straight away, and a
            # name decided afterwards would mean renaming a chat that is
            # already on screen. With no name the source's own convention
            # stands: the owner's copy says so, another reader's does not.
            named = (body.name or "").strip()
            title = named or duplicate_service.duplicate_title(
                found, copier_id=uuid.UUID(str(user.id))
            )
            # The sanitizer the object bridge names every pointer with, over an
            # unsaved stand-in: the copy's title is what its folder is called.
            root_name = pointer_name(
                WorkspaceObject(type=found.type, title=title, logical_id=str(uuid.uuid4()))
            )
        if not as_object:
            copied = [
                row
                for row in await files.repo.nodes_by_path_prefix(node.path_ids)
                if row.trashed_at is None
            ]
            # Decided per chat folder before anything is written: the copy of
            # a parent must not carry out a conversation its copier cannot
            # open, and ``_adopt_nested`` below adopts everything the copy made.
            await refuse_a_copy_over_hidden_chats(
                files.repo,
                files.ctx,
                node,
                facts=facts,
                decide=_by_id(request, files, drive, facts=facts),
                action=FilesAction.COPY,
                rows=copied,
            )
            # Every conversation and template inside the copy becomes one of its
            # own, and that work is bounded before a row is written rather than
            # abandoned half-done: a member who copies their whole drive learns
            # so instead of minting objects until something gives.
            nested = {
                row.id: uuid.UUID(str(row.target_object_id))
                for row in copied
                if row.id != node.id and folder_object_kind(row) is not None
            }
            if len(nested) > MAX_COPIED_OBJECTS:
                raise InvalidRequest(
                    "files.too_many_objects_to_copy",
                    "that folder holds more chats and templates than one copy may make",
                    detail={"limit": MAX_COPIED_OBJECTS},
                )
            await QuotaService(
                files.repo, files.ctx, files.clock, files.store, ceilings=files.ceilings
            ).assert_room(
                DriveId(drive),
                bytes=sum(row.size for row in copied if row.kind == "file"),
                nodes=len(copied),
                parent_path=landing.path_ids,
            )
            op_id = await start_copy(
                files.repo,
                Operations(files.repo, files.ctx, files.clock, files.store),
                node=node,
                dest_parent=landing,
                name=root_name
                if root_name is not None
                else (body.name.encode("utf-8") if body.name else None),
                root_flags=folder_kind.root_flags if folder_kind is not None else 0,
            )
        landing_id = NodeId(landing.id)

    made_chat: WorkspaceObject | None = None
    made_object: WorkspaceObject | None = None
    if as_object:
        assert object_id is not None and title is not None
        # The object row and its payload pages are the platform's; the node the
        # service projects for them rides the same window because the write is
        # one unit of work with them.
        async with as_platform(db):
            found = await access.load_object(db, uuid.UUID(str(object_id)))
            if found is None:
                raise NotFound()
            made_object = await duplicate_service.duplicate_object(
                db,
                files.repo,
                files.ctx,
                source=found,
                owner=user,
                title=title,
                parent_id=landing_id,
            )
        async with files_txn(files):
            new_node = await live_node_for(files.repo, made_object.id)
        if new_node is None:  # pragma: no cover - the projection was just written
            raise NotFound()
        new_root_id = uuid.UUID(str(new_node.id))
    else:
        assert op_id is not None
        await run_copy(files.repo, files.ctx, op_id, clock=files.clock)
        state = await Operations(files.repo, files.ctx, files.clock, files.store).get(op_id)
        if state.result_node_id is None:
            raise PreconditionFailed(f"copy {op_id} ended {state.state} without a node")
        new_root_id = state.result_node_id
        if folder_kind is not None:
            assert object_id is not None and title is not None
            async with as_platform(db):
                source_object = await access.load_object(db, uuid.UUID(str(object_id)))
            if source_object is None:
                raise NotFound()
            made_root = await _adopt_one(
                files,
                db,
                ctx=ctx,
                user=user,
                node_id=new_root_id,
                found=source_object,
                title=title,
            )
            # A conversation is what the client opens straight away, so it is
            # named on its own field; every other kind answers as an object.
            if made_root.type == CHAT_TYPE:
                made_chat = made_root
            else:
                made_object = made_root
        if nested:
            await _adopt_nested(files, db, ctx=ctx, user=user, root_id=new_root_id, sources=nested)
    # The org's audit chain belongs to the platform, not to the tenant role.
    async with as_platform(db):
        await org_audit_service.record(
            db,
            org_id=ctx.org_id,
            actor=user,
            action="files.duplicate",
            target=str(new_root_id),
            detail={
                "source_node_id": str(node.id),
                "destination_id": str(landing_id),
                "chat_id": str(made_chat.id) if made_chat is not None else None,
                "object_id": str(made_object.id) if made_object is not None else None,
            },
            acting=ctx,
        )
    async with files_txn(files):
        decided = await _resolve(request, files, drive, new_root_id, FilesAction.READ)
        item = await _render(files, decided, request=request)
    return DuplicateResult(
        item=item,
        chat_id=made_chat.id if made_chat is not None else None,
        object_id=(
            made_object.id if made_object is not None else (made_chat.id if made_chat else None)
        ),
    )


# --------------------------------------------------------------------------
# star
# --------------------------------------------------------------------------


@router.put(
    "/drives/{drive_id}/items/{item_id}/star",
    response_model=Item,
    dependencies=[Depends(ratelimited("items"))],
)
@idempotent_route("files.items.star_item")
async def star_item(
    request: Request,
    files: FilesCtx,
    drive_id: PathId,
    item_id: PathId,
    if_match: IfMatch,
    idempotency: Idempotency,
) -> Item:
    return await _set_star(request, files, drive_id, item_id, if_match, starred=True)


@router.delete(
    "/drives/{drive_id}/items/{item_id}/star",
    response_model=Item,
    dependencies=[Depends(ratelimited("items"))],
)
@idempotent_route("files.items.unstar_item")
async def unstar_item(
    request: Request,
    files: FilesCtx,
    drive_id: PathId,
    item_id: PathId,
    if_match: IfMatch,
    idempotency: Idempotency,
) -> Item:
    return await _set_star(request, files, drive_id, item_id, if_match, starred=False)


async def _set_star(
    request: Request,
    files: FilesContext,
    drive_id: str,
    item_id: str,
    if_match: int | None,
    *,
    starred: bool,
) -> Item:
    """Set or clear the starred bit.

    A star is the caller marking their own view of a node, so it is decided as
    ``READ`` rather than ``WRITE``: starring a file you may read but not change
    is exactly the case a bookmark exists for. The bit itself is the library's
    compare-and-swap, so two clients racing to star the same node cannot lose
    one another's write and a no-op flip announces nothing.
    """
    async with files_txn(files):
        decided = await _resolve(
            request, files, _uuid(drive_id, "drive"), _uuid(item_id, "item"), FilesAction.READ
        )
        assert if_match is not None
        if decided.node.etag != if_match:
            raise PreconditionFailed(f"node {decided.node.id} is not at etag {if_match}")
        flip = stars.star if starred else stars.unstar
        await flip(files.repo, files.ctx, NodeId(decided.node.id))
        # The flip went out as an UPDATE statement rather than through the
        # identity map, which still holds the row as it was: expiring it is
        # what makes the returned etag the one a caller must send next.
        await files.repo.session.refresh(decided.node)
        # The star this request just set, not the one the read before it saw:
        # `decided` was loaded before the flip, so echoing its value would tell
        # the caller their own write did not happen.
        return to_item(
            decided.node,
            decided.chain,
            decided.access,
            version=decided.head,
            starred=starred,
        )


__all__ = ["router"]
