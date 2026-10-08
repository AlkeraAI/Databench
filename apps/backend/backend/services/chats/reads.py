"""How a chat reads on every surface that shows one: the chat rail, a single
chat, a box's own read, and a workspace's list of its chats.

Spelled once so a chat reads the same whichever list it is in: its machine's
status derived from the org's live machines, its Files folder, its sandbox
figures, and what the caller may do to it, each answered by the policy that
decides the action.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict
from uuid import UUID

from alkera_core.authz import Action, ResourceType
from alkera_core.authz.engine import authorize as decide_policy
from alkera_core.authz.principal import ActingContext
from alkera_core.chat_refusals import refusal_is_final, refusal_kind_of
from alkera_core.compute.machines import DRAINING, READY, RESTARTING, machine_name_for_reader
from alkera_core.compute.machines import machine_status as machine_row_status
from alkera_core.config import settings
from alkera_core.files import chat_workspace_id
from alkera_core.files.ids import OrgScope
from alkera_core.files.objects_bridge import (
    WORKSPACE_TYPE,
    working_folder_nodes,
)
from alkera_core.files.repo import FilesRepo
from alkera_core.models import User, WorkspaceObject
from alkera_core.models.compute import ComputeAllocation
from alkera_core.models.files.tree import FileNode
from alkera_core.objects.chat_session_state import chat_session_state
from alkera_core.objects.instants import instant
from alkera_core.permission_presentation import approval_refusal
from alkera_core.schemas.objects import ChatSessionRead, ChatSpec, MachineStatus
from alkera_core.status import ChatBounds, ChatEvidence, StatusFact, chat_status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.chats import chat_service, read_marks
from backend.services.compute import LiveMachines, chat_machine_status, live_machines
from backend.services.org import files_transaction
from backend.services.org import sandbox_limits as org_sandbox_limits
from backend.services.sharing import object_resource
from backend.utils.ids import uuid_or_none


@dataclass(frozen=True)
class ChatNode:
    """The Files folder a chat IS, the drive it is on, and whether it is in
    the trash."""

    id: UUID
    drive_id: UUID
    trashed: bool
    #: The native workspace folder this chat's folder lives in, and that
    #: folder's shared ``files/`` tree. Both ``None`` for a chat in a workspace
    #: of one, and for one whose folder is not inside its workspace's folder.
    workspace_node_id: UUID | None = None
    workspace_files_node_id: UUID | None = None
    #: Whether the chat's workspace owns a folder of its own (``native``).
    workspace_native: bool = False
    #: The chat's own working directory (``scratch/`` in its folder), where
    #: a chat on its own runs; a member of a native workspace runs in the
    #: workspace's shared tree instead.
    scratch_node_id: UUID | None = None


async def _with_workspaces(
    repo: FilesRepo, chats: Sequence[WorkspaceObject], found: dict[UUID, ChatNode]
) -> None:
    """Fill in each chat's native workspace folder and shared tree, in place.

    Two scoped reads for the page whatever its size, and none at all when no
    chat on it names a workspace that owns a folder (every workspace of one).
    A chat is placed in its workspace only when its own folder really is
    inside the workspace's folder: the spec says which workspace the chat
    belongs to, the tree says where its bytes are, and a box must lease the
    folder that holds them, never the one a stale spec names.
    """
    scratch = await working_folder_nodes(repo.session, [entry.id for entry in found.values()])
    for chat_id, entry in list(found.items()):
        found[chat_id] = replace(entry, scratch_node_id=scratch.get(entry.id))
    wanted: dict[UUID, UUID] = {}
    for chat in chats:
        workspace_id = chat_workspace_id(chat)
        if workspace_id is not None and chat.id in found:
            wanted[chat.id] = workspace_id
    if not wanted:
        return
    folders: dict[UUID, FileNode] = {}
    result: Any = await repo.execute_scoped(
        repo.select_nodes().where(
            FileNode.target_object_id.in_(set(wanted.values())),
            FileNode.subtype == WORKSPACE_TYPE,
            FileNode.kind == "folder",
            FileNode.trashed_at.is_(None),
        )
    )
    for node in result.scalars().all():
        if node.target_object_id is not None:
            folders[node.target_object_id] = node
    if not folders:
        return
    shared = await working_folder_nodes(
        repo.session, [UUID(str(node.id)) for node in folders.values()]
    )
    chat_paths: dict[UUID, str] = {}
    result = await repo.execute_scoped(
        repo.select_nodes().where(FileNode.id.in_([found[cid].id for cid in wanted]))
    )
    for node in result.scalars().all():
        chat_paths[UUID(str(node.id))] = str(node.path_ids)
    for chat_id, workspace_id in wanted.items():
        folder = folders.get(workspace_id)
        if folder is None:
            continue
        entry = found[chat_id]
        files_id = shared.get(UUID(str(folder.id)))
        inside = UUID(str(folder.drive_id)) == entry.drive_id and chat_paths.get(
            entry.id, ""
        ).startswith(f"{folder.path_ids}.")
        found[chat_id] = replace(
            entry,
            workspace_native=True,
            workspace_node_id=UUID(str(folder.id)) if inside and files_id else None,
            workspace_files_node_id=files_id if inside else None,
        )


async def chat_nodes(
    db: AsyncSession, ctx: ActingContext, chats: Sequence[WorkspaceObject]
) -> dict[UUID, ChatNode]:
    """The Files node each of these chats IS, by chat id.

    One scoped read for a whole page, the way the machines are read once: a
    list of fifty chats must not become fifty Files queries. It goes through the
    Files repo rather than a plain select, because the org predicate on those
    tables is the repo's job and a route reaching around it is how one org's
    node id ends up on another org's row.

    A chat with no node is simply absent from the map: Files can be disabled,
    and a chat created before the drive existed never got one. The caller reads
    a missing entry as ``None``, never as an error: the node id is a convenience
    for the box, not something a chat needs in order to work.

    A TRASHED node is read too, and kept apart from a live one. Dropping it here
    would make a chat whose working directory somebody threw away answer exactly
    like a chat that never had one, and those are different things to say to the
    person looking at the chat. A live node always wins over a trashed one: a
    chat that was trashed and re-projected holds both rows.
    """
    if not settings.files_enabled or not chats:
        return {}
    found: dict[UUID, ChatNode] = {}

    def _keep(node: FileNode) -> None:
        if node.target_object_id is None:
            return
        entry = ChatNode(
            id=UUID(str(node.id)),
            drive_id=UUID(str(node.drive_id)),
            trashed=node.trashed_at is not None,
        )
        standing = found.get(node.target_object_id)
        if standing is None or (standing.trashed and not entry.trashed):
            found[node.target_object_id] = entry

    if ctx.is_machine:
        # A box lists chats across the orgs it serves, and a chat's node lives
        # in its own org's drive: one scoped read per org on the page, and no
        # drive is ensured on the way; a machine provisions nothing.
        by_org: dict[UUID, list[UUID]] = {}
        for chat in chats:
            by_org.setdefault(chat.org_team_id, []).append(chat.id)
        for org_id, ids in by_org.items():
            repo = FilesRepo.joined(db, OrgScope(org_team_id=org_id))
            async with repo.transaction():
                result: Any = await repo.execute_scoped(
                    repo.select_nodes().where(FileNode.target_object_id.in_(ids))
                )
                for node in result.scalars().all():
                    _keep(node)
                await _with_workspaces(
                    repo, [chat for chat in chats if chat.org_team_id == org_id], found
                )
        return found
    ids = [chat.id for chat in chats]
    async with files_transaction(db, ctx) as repo:
        result = await repo.execute_scoped(
            repo.select_nodes().where(FileNode.target_object_id.in_(ids))
        )
        for node in result.scalars().all():
            _keep(node)
        await _with_workspaces(repo, chats, found)
        return found


async def links(db: AsyncSession, chat_id: UUID) -> dict[str, Any]:
    """The facets of ONE chat's read the listing computes per page: every
    linked id, in attach order, and how many there are; whether the chat owes
    a turn and when it was last written.

    A single chat answers with its ids because a reader who opened it is about
    to render them; a listing answers with the count alone. One statement
    either way: the ids are rows, so the read costs what the page costs and
    not what the chat's whole history costs.

    The turn facts are the listing's, word for word: a box re-reads a single
    chat when a frame names it, and a single read that said "nothing owed"
    over a message nobody answered sent that chat to the back of the box's
    queue.
    """
    ids, _next_page = await chat_service.linked_attachments(db, chat_id)
    activity = (await chat_service.chat_activity(db, [chat_id])).get(chat_id)
    return {"attachments": ids, "attachment_count": len(ids), "activity": activity}


#: ``(vcpu, memory_mb)`` as :func:`backend.services.org.sandbox_limits` resolves
#: them: the org's override, else its plan tier, else ``None`` (the box decides).
SandboxLimits = tuple[int | None, int | None]


async def sandbox_limits(db: AsyncSession, chat: WorkspaceObject) -> SandboxLimits:
    """The chat's sandbox figures, resolved from ITS org, never the caller's,
    which on a pool box serving many orgs is nobody's."""
    return await org_sandbox_limits(db, chat.org_team_id)


def chat_read(
    chat: WorkspaceObject,
    machines: LiveMachines,
    nodes: dict[UUID, ChatNode] | None = None,
    *,
    limits: SandboxLimits,
    can_send: bool,
    can_delete: bool = False,
    can_answer_always: bool = False,
    attachment_count: int = 0,
    attachments: Sequence[UUID] | None = None,
    activity: chat_service.ChatActivity | None = None,
    owner_display_name: str = "",
    read_state: read_marks.ReadState = read_marks.NO_READ_STATE,
) -> ChatSessionRead:
    """One chat as every read surface shows it. ``limits`` is required so no
    read (a create, a single get, a claim, a box's own read) can hand back a
    row the box would size at its floor instead of the org's plan."""
    spec = chat_service.chat_spec_of(chat)
    node = (nodes or {}).get(chat.id)
    vcpu, memory_mb = limits
    status = machine_status(spec, machines)
    refused = status == "refused"
    return ChatSessionRead(
        status=chat_status_fact(
            spec, status, machines.machine_for(spec.machine_id), activity, now=datetime.now(UTC)
        ),
        sandbox_vcpu=vcpu,
        sandbox_memory_mb=memory_mb,
        pending_turn=activity.pending_turn if activity is not None else False,
        last_activity_at=activity.last_activity_at if activity is not None else None,
        can_send=can_send,
        can_delete=can_delete,
        can_answer_always=can_answer_always,
        unread=read_state.unread,
        needs_you=read_state.needs_you,
        attachment_count=attachment_count,
        files_node_id=None if node is None or node.trashed else node.id,
        files_drive_id=None if node is None or node.trashed else node.drive_id,
        files_node_trashed=node is not None and node.trashed,
        attachments=[str(node_id) for node_id in attachments or ()],
        source_node_id=UUID(spec.source_node_id) if spec.source_node_id else None,
        source_object_id=UUID(spec.source_object_id) if spec.source_object_id else None,
        id=chat.id,
        title=chat.title,
        owner_user_id=chat.owner_user_id,
        owner_display_name=owner_display_name,
        org_id=chat.org_team_id,
        machine_id=spec.machine_id,
        machine_status=status,
        machine_refusal_reason=spec.publisher_refusal or None,
        machine_refusal_kind=refusal_kind_of(spec.publisher_refusal_kind) if refused else None,
        machine_refusal_final=refused and refusal_is_final(spec.publisher_refusal_kind),
        wake_requested_at=instant(spec.wake_requested_at),
        end_seq=spec.end_seq,
        created_at=chat.created_at,
        updated_at=chat.updated_at,
        last_seq=spec.last_seq,
        model=spec.model,
        permission_mode=spec.permission_mode,
        approval_refusal=approval_refusal(spec.permission_mode),
        workspace_id=uuid_or_none(spec.workspace_id),
        **_workspace_facets(spec, node),
        session_state=chat_session_state(spec, status, bool(activity and activity.pending_turn)),
    )


def chat_bounds() -> ChatBounds:
    """How long a chat's waits may run before its status says it has stalled."""
    return ChatBounds(
        turn_silence=timedelta(seconds=settings.chat_turn_silence_seconds),
        turn_start=timedelta(seconds=settings.chat_turn_start_seconds),
    )


def chat_status_fact(
    spec: ChatSpec,
    status: MachineStatus,
    machine: ComputeAllocation | None,
    activity: chat_service.ChatActivity | None,
    *,
    now: datetime,
) -> StatusFact | None:
    """The chat's status from every fact a read already holds: the spec, the
    machine's row and the transcript's activity."""
    row = machine_row_status(machine, now=now) if machine is not None else None
    answers = row in (READY, DRAINING, RESTARTING)
    return chat_status(
        ChatEvidence(
            machine_status=status,
            machine_answers=answers,
            machine_restarting=row == RESTARTING,
            machine_name=machine_name_for_reader(machine),
            refusal_kind=spec.publisher_refusal_kind,
            session_held=spec.mirror_state == "awake",
            wake_requested_at=instant(spec.wake_requested_at),
            slot_wait_at=instant(spec.slot_wait_at),
            turn_working=bool(activity and activity.turn_working),
            turn_stamped_at=activity.turn_stamped_at if activity else None,
            prompt_at=activity.prompt_at if activity else None,
            prompt_owed=bool(activity and activity.prompt_owed),
            turn_end_reason=spec.turn_end_reason,
            turn_end_at=instant(spec.turn_end_at),
            has_run=bool(activity and activity.last_activity_at is not None),
            ever_held=spec.mirror_state is not None,
        ),
        now=now,
        bounds=chat_bounds(),
    )


def _workspace_facets(spec: ChatSpec, node: ChatNode | None) -> dict[str, Any]:
    """The workspace half of a chat's read: what its workspace's folder is,
    for a native one the folder a box leases and the tree it roots at, and in
    every case the tree the chat's agent works in (``working_node_id``): the
    one a Files tab or a working copy opens, so what a person drops there is
    what the agent sees."""
    if node is None or node.trashed:
        return {"workspace_layout": "adopted"} if spec.workspace_id else {}
    if not spec.workspace_id or not node.workspace_native:
        facets: dict[str, Any] = {"working_node_id": node.scratch_node_id}
        if spec.workspace_id:
            facets["workspace_layout"] = "adopted"
        return facets
    return {
        "workspace_layout": "native",
        "workspace_node_id": node.workspace_node_id,
        "workspace_files_node_id": node.workspace_files_node_id,
        "working_node_id": node.workspace_files_node_id,
    }


def machine_status(spec: ChatSpec, machines: LiveMachines) -> MachineStatus:
    """What the chat says about the machine serving it, now: the one
    judgment every reader of a chat's binding makes
    (:func:`placement.chat_machine_status`), from the org's live machines."""
    return chat_machine_status(spec, machines.machine_for(spec.machine_id))


class ChatCaps(TypedDict):
    can_send: bool
    can_delete: bool
    can_answer_always: bool


def caps(ctx: ActingContext, chat: WorkspaceObject, attrs: Mapping[str, object]) -> ChatCaps:
    """What this caller may do to the chat, each answered by the policy that
    decides the action itself, so a control the reader cannot use is not
    offered to them."""
    return ChatCaps(
        can_send=may_send(ctx, chat, attrs),
        can_delete=decide_policy(
            ctx, Action.DELETE, object_resource(chat, type=ResourceType.CHAT), attrs
        ).allowed,
        # Whether an ask's "Always" is this reader's to give: the decision the
        # answer route makes on a standing option, so the card never offers
        # one the server would refuse.
        can_answer_always=decide_policy(
            ctx, Action.ANSWER_STANDING, object_resource(chat, type=ResourceType.CHAT), attrs
        ).allowed,
    )


def may_send(ctx: ActingContext, chat: WorkspaceObject, attrs: Mapping[str, object]) -> bool:
    """Whether this caller may SEND, answered by the policy that decides the
    send itself, over the very facts the send route resolves.

    ``authorize`` rather than ``enforce``: nothing was attempted, so nothing is
    filed. A read that wrote a SEND decision row per chat would turn opening a
    rail of fifty conversations into fifty entries on the audit lane that look
    like fifty attempts to speak.
    """
    return decide_policy(
        ctx, Action.SEND, object_resource(chat, type=ResourceType.CHAT), attrs
    ).allowed


async def chat_list_items(
    db: AsyncSession,
    ctx: ActingContext,
    page: Sequence[tuple[WorkspaceObject, Mapping[str, object]]],
) -> list[ChatSessionRead]:
    """A page of readable chats, each with the facts its read was decided on,
    as a listing shows them. Shared by the chat rail and a workspace's chat
    list, so a chat reads the same whichever list it is in."""
    visible = [chat for chat, _facts in page]
    # One read of each org's machines and limits for the whole page: a list of
    # fifty chats must not ask the compute plane fifty times. A person's page
    # is one org; a pool box's page spans the orgs it serves.
    orgs = {chat.org_team_id for chat in visible}
    machines_of = {org_id: await live_machines(db, org_id=org_id) for org_id in orgs}
    limits_of = {org_id: await org_sandbox_limits(db, org_id) for org_id in orgs}
    nodes = await chat_nodes(db, ctx, visible)
    # How many files a conversation carries, never which: the ids are the
    # single chat's to answer, and a rail of fifty chats that each held a
    # thousand of them would be a fifty-thousand-id response nothing renders.
    counts = await chat_service.attachment_counts(db, [chat.id for chat in visible])
    activity = await chat_service.chat_activity(db, [chat.id for chat in visible])
    owners = await owner_names(db, visible)
    caps_of = {chat.id: caps(ctx, chat, facts) for chat, facts in page}
    # Who may answer an ask is who may SEND: the gate the answer route decides
    # on, already answered for the row.
    states = await read_marks.read_states(
        db, ctx, [(chat_id, row["can_send"]) for chat_id, row in caps_of.items()]
    )
    return [
        chat_read(
            chat,
            machines_of[chat.org_team_id],
            nodes,
            limits=limits_of[chat.org_team_id],
            **caps_of[chat.id],
            attachment_count=counts.get(chat.id, 0),
            activity=activity.get(chat.id),
            owner_display_name=owners.get(chat.owner_user_id, ""),
            read_state=states.get(chat.id, read_marks.NO_READ_STATE),
        )
        for chat, _facts in page
    ]


async def owner_names(db: AsyncSession, chats: Sequence[WorkspaceObject]) -> dict[UUID, str]:
    """Each chat owner's name as people see it (their first and last name,
    else their email), in one read: a box names a chat's agent "Alkera agent
    for <name>" from it."""
    wanted = {chat.owner_user_id for chat in chats}
    if not wanted:
        return {}
    rows = await db.execute(
        select(User.id, User.first_name, User.last_name, User.email).where(User.id.in_(wanted))
    )
    return {
        user_id: f"{first or ''} {last or ''}".strip() or str(email or "")
        for user_id, first, last, email in rows
    }


__all__ = [
    "ChatCaps",
    "ChatNode",
    "LiveMachines",
    "SandboxLimits",
    "caps",
    "chat_list_items",
    "chat_nodes",
    "chat_read",
    "instant",
    "links",
    "live_machines",
    "machine_status",
    "may_send",
    "owner_names",
    "sandbox_limits",
]
