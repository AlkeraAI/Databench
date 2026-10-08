"""A copy of a workspace object: a new row, its transcript or payload, no past.

Copying a file makes new node rows over the same chunks. Copying a chat, a
saved query or a report has to make a new OBJECT as well — the row the chat
page opens, the row the box publishes, the row a query is re-run from — and
the copy is a new object in every sense: a new id, the copier as owner, a
private audience until its node is shared, and none of the source's running
state.

A chat carries its transcript across (the whole point of resuming from a
duplicate) but none of the source's running state: no lease, no live session,
no publisher refusal and no wake. It is PLACED like a fresh chat instead —
bound to whichever machine the org is running, the way a chat started from the
composer is — because a copy is opened straight away and has to be able to take
a message. A query or a report carries its spec and its payload pages; ``node_for_object``
then projects the copy into the tree the way a brand-new object is projected,
so the members of a report folder are the copy's own, never the source's.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from alkera_core.authz.chat_scope import SCOPE_PRIVATE
from alkera_core.authz.principal import ActingContext
from alkera_core.files import objects_bridge
from alkera_core.files.ids import NodeId
from alkera_core.files.objects_bridge import WORKSPACE_TYPE
from alkera_core.files.repo import FilesRepo
from alkera_core.models import User, WorkspaceObject
from alkera_core.models.workspace_object import DEFAULT_NAMESPACE
from alkera_core.schemas.objects import ChatSpec, WorkspaceSpec
from alkera_core.schemas.objects.specs import MachineStatus as ChatMachineStatus
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services import workspaces
from backend.services.chats import chat_service
from backend.services.objects import object_service

#: What the owner's own duplicate is called. A copy made by somebody else keeps
#: the source's title: their list has no other chat of that name to collide with.
COPY_SUFFIX = " (copy)"


def duplicate_title(source: WorkspaceObject, *, copier_id: UUID) -> str:
    """The copy's title: the owner's duplicate says so, another reader's does not."""
    title = source.title
    if source.owner_user_id == copier_id:
        return f"{title}{COPY_SUFFIX}" if title else "Untitled" + COPY_SUFFIX
    return title


def _fresh_row(
    source: WorkspaceObject, *, owner: User, title: str, spec: dict[str, object]
) -> WorkspaceObject:
    return WorkspaceObject(
        id=uuid4(),
        # The source's org, which the callers have checked is the org the copy
        # is made in: an object is never copied across orgs.
        org_team_id=source.org_team_id,
        # A logical id of its own: the source's belongs to the client that
        # minted it, and a retried create must never land on the copy.
        logical_id=str(uuid4()),
        namespace=DEFAULT_NAMESPACE,
        type=source.type,
        title=title,
        version=1,
        status=source.status,
        spec=spec,
        owner_user_id=owner.id,
        team_id=None,
        visibility_scope=SCOPE_PRIVATE,
        content_updated_at=datetime.now(UTC).timestamp(),
    )


async def duplicate_folder_object(
    db: AsyncSession,
    *,
    source: WorkspaceObject,
    owner: User,
    org_id: UUID,
    title: str,
    machine_id: str | None = None,
    machine_status: ChatMachineStatus = "none",
) -> WorkspaceObject:
    """A new object of ``source``'s kind for a folder that has just been copied.

    Every object whose node is a folder is duplicated here — a conversation, a
    chat template, and whatever registers next — because what the copy needs is
    the same in each case: a new row of the same type carrying the source's
    spec, and the folder's own subtree copy to adopt it. The copy is written in
    the caller's transaction; nothing here commits, and nothing here touches the
    tree.

    A conversation is the one kind with running state, so it is the one kind
    with more to say. Its transcript is copied in one statement, row for row:
    the same ``seq`` (so ``last_seq`` still names the tail), the same
    ``event_id`` (unique per chat, so it collides with nothing) and the same
    payload. It is PLACED the way a chat started from the composer is placed:
    the caller resolves the org's machine and names it here. It never inherits
    the source's binding — the box holds a session per chat, and two chats
    pointing at one session would have the copy answer into the source's. But an
    unplaced copy is not the same thing as an org with no workspace, and leaving
    it unbound had the reader told their organization has no machine while the
    box that would take the copy was serving every other chat they own.

    ``org_id`` is the org the copy is made in (the request's); a source in any
    other org is refused.
    """
    if source.type not in objects_bridge.FOLDER_OBJECT_KINDS:
        raise ValueError("only an object whose node is a folder is duplicated with one")
    if source.org_team_id != org_id:
        raise ValueError("an object is copied inside its own org")
    is_chat = source.type == "chat"
    spec: dict[str, object] = dict(source.spec or {})
    if is_chat:
        current = chat_service.chat_spec_of(source)
        spec = ChatSpec(
            machine_id=machine_id,
            machine_status=machine_status,
            last_seq=current.last_seq,
            model=current.model,
            permission_mode=current.permission_mode,
            # The links are rows, copied below; the spec array is the previous
            # release's shape and the copy carries an empty one.
            attachments=[],
            source_node_id=current.source_node_id,
            source_object_id=current.source_object_id,
        ).model_dump(mode="json")
    elif source.type == WORKSPACE_TYPE:
        # A copied workspace is a project of the copier's: never a second main,
        # and never the source's binding.
        spec = WorkspaceSpec(kind="project", layout="native").model_dump(mode="json")
    made = _fresh_row(source, owner=owner, title=title, spec=spec)
    db.add(made)
    await db.flush()
    if is_chat:
        await db.execute(
            text(
                "INSERT INTO chat_attachments (chat_id, node_id, position) "
                "SELECT :target, node_id, position FROM chat_attachments WHERE chat_id = :source"
            ),
            {"target": made.id, "source": source.id},
        )
        await db.execute(
            text(
                "INSERT INTO chat_messages "
                "(id, chat_id, org_team_id, seq, role, kind, event_id, payload) "
                "SELECT gen_random_uuid(), :target, org_team_id, seq, role, kind, event_id, "
                "payload FROM chat_messages WHERE chat_id = :source"
            ),
            {"target": made.id, "source": source.id},
        )
        # A copy is a chat of its own, in a workspace of its own: it lands in
        # the copier's drive where any copied folder does.
        await workspaces.adopt_chat(db, chat=made, owner=owner)
    return made


async def duplicate_object(
    db: AsyncSession,
    repo: FilesRepo,
    ctx: ActingContext,
    *,
    source: WorkspaceObject,
    owner: User,
    title: str,
    parent_id: NodeId,
) -> WorkspaceObject:
    """A new query / report / result with ``source``'s spec and payload, filed
    under ``parent_id`` through the same projection a new object gets.

    The payload pages are copied by statement — a promoted result's rows are
    the object's, and a copy that pointed at the source's pages would vanish
    with the source. The node is created inside the caller's transaction, so
    there is no window in which the copy exists without a place in the tree.
    """
    if source.type in objects_bridge.FOLDER_OBJECT_KINDS:
        raise ValueError("an object that owns a folder is duplicated with it, not as a pointer")
    if source.org_team_id != ctx.org_id:
        raise ValueError("an object is copied inside its own org")
    made = _fresh_row(source, owner=owner, title=title, spec=dict(source.spec or {}))
    db.add(made)
    await db.flush()
    await db.execute(
        text(
            "INSERT INTO object_payload_rows "
            "(object_id, page, sha256, size, media_type, page_rows) "
            "SELECT :target, page, sha256, size, media_type, page_rows "
            "FROM object_payload_rows WHERE object_id = :source"
        ),
        {"target": made.id, "source": source.id},
    )
    async with repo.transaction():
        await objects_bridge.node_for_object(repo, ctx, made, parent_id=parent_id)
    await object_service.announce(db, obj=made, actor=ctx.audit_dict())
    return made


__all__ = ["COPY_SUFFIX", "duplicate_folder_object", "duplicate_object", "duplicate_title"]
