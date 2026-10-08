"""Saving a chat as the starting point for the next one.

A template is a chat distilled: the prose its author wants the next reader
handed, and the files that chat was working on. Both halves are written here so
the route stays a decision and an answer.

The two halves are deliberately asymmetric. The brief is a column on the object
row, so editing it is an ordinary object update. The files are a real folder in
the drive — a copy of the source chat's working directory, with new ids and the
saver as owner — so a template is shared, moved, renamed and duplicated by the
same machinery every other folder is, and nothing here needs a second answer
for "who may see these files".

What the copy deliberately leaves behind is the chat's own records. The source
of the copy is the working directory, never the chat folder above it, so the
manifest, the trace digest and the runtime directory beside it are not reachable
from the plan at all — a template cannot carry them even if a later caller asks
for the wrong node, because this module never names that node.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from alkera_core.authz import ActingContext
from alkera_core.authz.chat_scope import SCOPE_PRIVATE
from alkera_core.config import settings
from alkera_core.files.copy import run_copy, start_copy
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.namespace import Namespace
from alkera_core.files.objects_bridge import (
    CHAT_TEMPLATE_TYPE,
    chat_templates_folder,
    live_node_for,
    node_for_object,
    working_folder_node,
)
from alkera_core.files.ops import Operations
from alkera_core.files.quota import QuotaService
from alkera_core.files.repo import FilesRepo
from alkera_core.models import ChatMessage, User, WorkspaceObject
from alkera_core.models.files.tree import FileNode
from alkera_core.models.workspace_object import DEFAULT_NAMESPACE
from alkera_core.schemas.objects import (
    ChatModelPin,
    ChatSpec,
    ChatTemplateRead,
    ChatTemplateSpec,
)
from alkera_core.schemas.objects.template_brief import brief_from_transcript
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.files.context import FilesContext
from backend.services.org import teams as team_service
from backend.services.sharing import access, node_role

#: The object type this module writes. Named once so a caller cannot ask for a
#: template and be handed a chat.
TEMPLATE_TYPE = CHAT_TEMPLATE_TYPE


@dataclass(frozen=True, slots=True)
class SourceFiles:
    """The chat's working directory and what copying it would cost."""

    scratch: FileNode
    node_count: int
    byte_count: int


async def template_attrs(
    db: AsyncSession, template: WorkspaceObject, reader: access.Reader
) -> dict[str, object]:
    """Every fact ``chat_template.access`` reads for ``template``.

    The object facts, the team column the policy checks against the scope, and
    the rung the template's folder gives this caller — which is how someone
    outside the audience the template was saved into reads it at all. The owner
    never pays for that read: every branch that consults the rung admits them
    first.
    """
    caller = reader.ctx.effective_user_id
    is_owner = reader.owns(template.owner_user_id)
    role = (
        None
        if is_owner
        else await node_role.shared_object_role(
            db,
            org_id=template.org_team_id,
            object_id=template.id,
            user_id=caller,
            team_ids=reader.team_ids,
        )
    )
    return {
        **access.object_attrs(template, reader),
        "team_id": str(template.team_id) if template.team_id else "",
        "shared_role": role or "",
    }


def new_template_attrs(reader: access.Reader) -> dict[str, object]:
    """The facts for a template that does not exist yet.

    It has no folder, so there is no rung to resolve; ``CREATE`` consults the
    audience the saver is addressing — themself, a template being saved private
    — and their own membership of it.
    """
    return {
        **access.new_object_attrs(reader, visibility_scope=SCOPE_PRIVATE),
        "team_id": "",
        "shared_role": "",
    }


def template_read(obj: WorkspaceObject, *, files_node_id: uuid.UUID | None) -> ChatTemplateRead:
    """The wire shape, with the spec flattened onto it.

    The spec is not on the wire: a template's brief, pin and stance are fields a
    client reads and writes by name, and handing back the raw document would
    make every reader parse a versioned model to find them.
    """
    spec = ChatTemplateSpec.model_validate(obj.spec or {})
    return ChatTemplateRead(
        id=obj.id,
        title=obj.title,
        version=obj.version,
        owner_user_id=obj.owner_user_id or uuid.uuid4(),
        created_at=obj.created_at,
        updated_at=obj.updated_at,
        files_node_id=files_node_id,
        brief=spec.brief,
        model=spec.model,
        permission_mode=spec.permission_mode,
        source_chat_id=uuid.UUID(spec.source_chat_id) if spec.source_chat_id else None,
        saved_from_seq=spec.saved_from_seq,
    )


async def node_ids_for(
    db: AsyncSession, ctx: ActingContext, objects: Sequence[WorkspaceObject]
) -> dict[uuid.UUID, uuid.UUID]:
    """The live folder of each template, by object id.

    Resolved on the ROUTE's session rather than the Files dependency's: the two
    are different connections, and a read that ensured the drive on one while
    the other held an uncommitted insert of it would wait on a lock nobody is
    going to release. A template with no folder (Files disabled, or the folder
    trashed) is simply absent from the mapping — the client is told there is
    nothing to open, not that the template is gone.
    """
    if not settings.files_enabled or not objects or ctx.org_id is None:
        return {}
    found: dict[uuid.UUID, uuid.UUID] = {}
    async with team_service.files_transaction(db, ctx) as repo:
        for obj in objects:
            node = await live_node_for(repo, obj.id)
            if node is not None:
                found[obj.id] = uuid.UUID(str(node.id))
    return found


async def node_id_for(
    db: AsyncSession, ctx: ActingContext, obj: WorkspaceObject
) -> uuid.UUID | None:
    """The one template's live folder, on the route's own session."""
    return (await node_ids_for(db, ctx, [obj])).get(obj.id)


async def transcript_brief(db: AsyncSession, chat: WorkspaceObject) -> tuple[str, int]:
    """``(brief, last_seq)`` — the digest the server writes when nobody wrote one.

    The rows are read whole rather than paged: the digest is capped in
    characters, and a chat long enough for that to matter is a chat whose
    transcript the template is about to be cut from anyway.
    """
    rows = list(
        (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.chat_id == chat.id)
                .order_by(ChatMessage.seq.asc())
            )
        )
        .scalars()
        .all()
    )
    last_seq = rows[-1].seq if rows else 0
    return brief_from_transcript(rows, title=chat.title), last_seq


def spec_for(
    *, brief: str, chat: WorkspaceObject, source_chat_id: uuid.UUID, saved_from_seq: int
) -> dict[str, Any]:
    """The template's document, built from the chat it is being cut from.

    The model and the stance are carried as a SUGGESTION: what a chat started
    from this template actually runs as is decided there, against the starter's
    own ceiling, and this row can only narrow it.
    """
    chat_spec = ChatSpec.model_validate(chat.spec or {})
    pin: ChatModelPin | None = chat_spec.model
    return ChatTemplateSpec(
        brief=brief,
        model=pin,
        permission_mode=chat_spec.permission_mode,
        source_chat_id=str(source_chat_id),
        saved_from_seq=saved_from_seq,
    ).model_dump(mode="json")


async def source_files(files: FilesContext, chat_node: FileNode) -> SourceFiles | None:
    """The chat's working directory and the size of copying its live children.

    ``None`` when the chat has no working directory — an older chat whose folder
    predates it, or a chat whose folder was trashed. The caller answers that as
    a refusal rather than saving an empty template that looks like the chat's.
    """
    async with files.repo.transaction():
        scratch = await working_folder_node(files.repo, chat_node)
        if scratch is None:
            return None
        rows = [
            row
            for row in await files.repo.nodes_by_path_prefix(scratch.path_ids)
            if row.trashed_at is None and row.id != scratch.id
        ]
    return SourceFiles(
        scratch=scratch,
        node_count=len(rows),
        byte_count=sum(row.size for row in rows if row.kind == "file"),
    )


async def create_template_object(
    db: AsyncSession,
    repo: FilesRepo,
    ctx: ActingContext,
    *,
    owner: User,
    title: str,
    spec: dict[str, Any],
    object_id: uuid.UUID,
    logical_id: str | None,
    parent_id: NodeId | None,
) -> WorkspaceObject:
    """The template row and its folder, in the caller's transaction.

    The folder is minted where the saver asked rather than where the object
    bridge would file it unasked, which is the one reason this is not
    ``object_service.create_object``: a template saved into a shared folder has
    to land there, and moving it afterwards would leave a window in which it was
    filed somewhere else.
    """
    obj = WorkspaceObject(
        id=object_id,
        # The org the save acts in, from its credential; never the saver's own.
        org_team_id=ctx.org_id,
        logical_id=logical_id or str(uuid.uuid4()),
        namespace=DEFAULT_NAMESPACE,
        type=TEMPLATE_TYPE,
        title=title,
        version=1,
        status="ready",
        spec=spec,
        owner_user_id=owner.id,
        team_id=None,
        # Private, like a chat: the saver alone until they share it. Addressing
        # it to the org here would have the object bridge write a direct
        # org-wide grant on its folder that nobody asked for. Whoever may see
        # it beyond the owner comes from its folder — a rung granted on it, or
        # inherited from the folder it was saved into.
        visibility_scope=SCOPE_PRIVATE,
    )
    db.add(obj)
    await db.flush()
    if settings.files_enabled:
        # The node is minted on the FILES session, the row on the route's —
        # exactly as a duplicate does it. The two are different connections, and
        # ensuring the drive again from this one would block on the insert the
        # other is holding open.
        async with repo.transaction():
            await node_for_object(repo, ctx, obj, parent_id=parent_id)
    return obj


async def default_destination(files: FilesContext, home: FileNode) -> FileNode:
    """The caller's own ``Chat Templates`` folder, made on this visit if missing."""
    async with files.repo.transaction():
        return await chat_templates_folder(
            files.repo,
            Namespace(files.repo, files.ctx, files.clock, files.store, ceilings=files.ceilings),
            home,
        )


async def assert_room_for(
    files: FilesContext, *, drive: uuid.UUID, source: SourceFiles, destination: FileNode
) -> None:
    """Refuse the save before any row exists when it would not fit.

    The template's own folder and its working directory are counted alongside
    the files, because a save that fits the copy but not the two folders holding
    it is still a save that cannot finish.
    """
    await QuotaService(
        files.repo, files.ctx, files.clock, files.store, ceilings=files.ceilings
    ).assert_room(
        DriveId(drive),
        bytes=source.byte_count,
        nodes=source.node_count + 2,
        parent_path=destination.path_ids,
    )


async def copy_into(files: FilesContext, *, source: FileNode, destination: FileNode) -> int:
    """Copy the live children of ``source`` into ``destination``; the count made.

    Children, not the directory: the template's own working directory was minted
    by the object bridge with the flags a template's files wear, and copying the
    source directory itself would nest the chat's ``scratch`` inside the
    template's.
    """
    async with files.repo.transaction():
        children = [row for row in await files.repo.siblings(NodeId(source.id))]
    made = 0
    for child in children:
        if child.trashed_at is not None:
            continue
        async with files.repo.transaction():
            op_id = await start_copy(
                files.repo,
                Operations(files.repo, files.ctx, files.clock, files.store),
                node=child,
                dest_parent=destination,
            )
        await run_copy(files.repo, files.ctx, op_id, clock=files.clock)
        made += 1
    return made


async def working_folder_of(files: FilesContext, template_node: FileNode) -> FileNode | None:
    """The template folder's own working directory — where the copy lands."""
    async with files.repo.transaction():
        return await working_folder_node(files.repo, template_node)


async def node_of(files: FilesContext, object_id: uuid.UUID) -> FileNode | None:
    """The live folder of one object, read on the Files dependency's session."""
    async with files.repo.transaction():
        return await live_node_for(files.repo, object_id)


__all__ = [
    "TEMPLATE_TYPE",
    "SourceFiles",
    "assert_room_for",
    "copy_into",
    "create_template_object",
    "default_destination",
    "new_template_attrs",
    "node_id_for",
    "node_ids_for",
    "node_of",
    "source_files",
    "spec_for",
    "template_attrs",
    "template_read",
    "transcript_brief",
    "working_folder_of",
]
