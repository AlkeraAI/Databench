"""Who is asking about a notebook, and what a request names.

A request resolves to a :class:`Caller` (the person, the agent acting in its
person's session, or a machine) and names a :class:`Target` the Files policy
admitted. A box speaks only for the chats bound to it, and follows only the
notebooks in a folder it holds.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from alkera_core.auth.tenancy import member_stands
from alkera_core.authz import ActingContext
from alkera_core.files import chat_works_at
from alkera_core.files.authz.authorize import Authorized
from alkera_core.files.errors import NotFound
from alkera_core.files.ids import NodeId
from alkera_core.files.leases import admits_unfenced_write
from alkera_core.models import User, WorkspaceObject
from alkera_core.models.files.tree import FileNode
from alkera_core.notebooks import edits as edit_log
from alkera_core.schemas.objects.specs import ChatSpec
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.crdt import DocRef, file_name, live_type_of, name_text
from backend.services.files import (
    FilesContext,
    FolderHolder,
    document_access,
    folder_holder,
    node_scope,
)
from backend.services.notebooks import names
from backend.services.notebooks.errors import AgentChatRefusedError
from backend.services.notebooks.refs import doc_ref

ActorKind = Literal["person", "agent", "system"]


@dataclass(frozen=True, slots=True)
class Where:
    """A notebook by its ids, for reads that need nothing else of it."""

    org_id: uuid.UUID
    item_id: uuid.UUID

    @property
    def ref(self) -> DocRef:
        return doc_ref(self.org_id, self.item_id)


@dataclass(frozen=True, slots=True)
class Caller:
    """The writer or runner a request resolved to."""

    ctx: ActingContext
    kind: ActorKind
    actor_key: str
    display: str
    user: User | None
    #: What the CRDT lane writes the edit as: the agent's chat session, or
    #: the machine of the lease a box holds.
    agent_id: str | None
    machine_id: str | None = None
    #: Whether the Files policy lets this caller edit the notebook, decided
    #: by the route for requests whose effect may be an edit (a notebook's
    #: own package list); ``None`` where no route decided it.
    may_edit: bool | None = None


@dataclass(frozen=True, slots=True)
class KernelScope:
    """The folder a ``notebook.run`` decision was made on: what the kernel
    binds (the folder its machine holds), or ``None`` when no machine held it.
    A request acts on this folder and no other, so a machine that takes the
    folder after the decision is not where the request goes."""

    holder: FolderHolder | None


@dataclass(frozen=True, slots=True)
class Target:
    """The notebook a request names, once the Files policy admitted it."""

    org_id: uuid.UUID
    drive_id: uuid.UUID
    item_id: uuid.UUID
    allowed: Authorized[str]
    #: Set once ``notebook.run`` decided on the kernel's folder.
    scope: KernelScope | None = None

    @property
    def ref(self) -> DocRef:
        return doc_ref(self.org_id, self.item_id)

    @property
    def node(self) -> FileNode:
        return self.allowed.node


def is_notebook(node: FileNode) -> bool:
    return (
        node.kind == "file"
        and node.trashed_at is None
        and live_type_of(name_text(node.name)) == "notebook"
    )


async def person_of(db: AsyncSession, ctx: ActingContext) -> User | None:
    user_id = ctx.effective_user_id
    return None if user_id is None else await db.get(User, user_id)


async def runner_of(db: AsyncSession, files: FilesContext) -> Caller:
    """The person or the agent (acting in its person's session) asking to act
    through the kernel. A machine on its own credential is answered as one;
    the ``notebook.run`` policy refuses it."""
    ctx = files.ctx
    if ctx.is_machine:
        machine = ctx.acting_principal.id
        return Caller(
            ctx=ctx,
            kind="system",
            actor_key=edit_log.actor_key(machine_id=machine),
            display=names.system_name(),
            user=None,
            agent_id=None,
            machine_id=machine,
        )
    user = await person_of(db, ctx)
    if user is None:
        raise NotFound()
    if ctx.is_agent:
        agent = ctx.acting_principal.id
        return Caller(
            ctx=ctx,
            kind="agent",
            actor_key=edit_log.actor_key(agent_id=agent),
            display=names.agent_name(names.person_name(user)),
            user=user,
            agent_id=agent,
        )
    return Caller(
        ctx=ctx,
        kind="person",
        actor_key=edit_log.actor_key(user_id=user.id),
        display=names.person_name(user),
        user=user,
        agent_id=None,
    )


async def chat_person(db: AsyncSession, target: Target, machine_id: str, chat_id: str) -> User:
    """The person whose chat ``chat_id`` the box ``machine_id`` says its agent
    acted in, on the notebook ``target``.

    Refused (:class:`AgentChatRefusedError`) unless the chat is a live chat
    of the notebook's org bound to that very machine, the chat belongs to the
    workspace holding the notebook, and its person is an active member of the
    org: a box speaks for the chats it serves and for nobody else."""
    try:
        chat_uuid = uuid.UUID(chat_id)
    except ValueError:
        raise AgentChatRefusedError("not a chat id") from None
    chat = await db.get(WorkspaceObject, chat_uuid)
    if (
        chat is None
        or chat.type != "chat"
        or chat.deleted_at != 0
        or chat.org_team_id != target.org_id
        or ChatSpec.model_validate(chat.spec or {}).machine_id != machine_id
    ):
        raise AgentChatRefusedError("the chat is not served by this machine")
    if not await agent_chat_in_workspace(db, target, chat_id):
        raise AgentChatRefusedError("the chat is not in the notebook's workspace")
    person = await db.get(User, chat.owner_user_id)
    if (
        person is None
        or not person.is_active
        or not await member_stands(db, user=person, org_team_id=target.org_id)
    ):
        raise AgentChatRefusedError("the chat's person no longer stands in the org")
    return person


async def agent_of_holder(db: AsyncSession, target: Target, holder: Caller, chat_id: str) -> Caller:
    """The holder box ``holder`` applying a batch its chat ``chat_id``'s agent
    made: the batch is the agent's, written for the chat's person (the person
    a write back of the notebook is attributed to), never the bare machine.
    Refused (:class:`AgentChatRefusedError`) as :func:`chat_person` refuses,
    and for anyone but the folder's holder."""
    if holder.machine_id is None or holder.user is not None:
        raise AgentChatRefusedError("only the folder's holder names the chat it writes for")
    person = await chat_person(db, target, holder.machine_id, chat_id)
    return Caller(
        ctx=holder.ctx,
        kind="agent",
        actor_key=edit_log.actor_key(agent_id=chat_id),
        display=names.agent_name(names.person_name(person)),
        user=person,
        agent_id=chat_id,
        machine_id=holder.machine_id,
    )


async def lease_admits_writes(files: FilesContext, node: FileNode) -> bool:
    async with files.repo.transaction():
        return await admits_unfenced_write(files.repo, node)


async def box_follows(db: AsyncSession, ctx: ActingContext, item_id: uuid.UUID) -> uuid.UUID | None:
    """The org of the notebook ``item_id`` when the box ``ctx`` speaks for may
    follow its live document on its own socket; ``None`` otherwise.

    The box that may is the one every other box path names: the machine
    holding the notebook's folder (:func:`folder_holder`, the machine its
    kernel runs on and its file is written back to), reading the file by the
    Files policy. Any other machine, a notebook in a folder the box does not
    hold, a file that is not a notebook and another org's notebook are all
    ``None``, which the socket answers as it answers a stranger."""
    if not ctx.is_machine or ctx.org_id is None:
        return None
    holder = await folder_holder(db, ctx, item_id)
    if holder is None or holder.machine_id != ctx.acting_principal.id:
        return None
    # The notebook's org is its drive's, which for a pool box is an org it
    # serves rather than its own.
    try:
        org_id = (await node_scope(db, ctx, NodeId(item_id))).org_team_id
    except NotFound:
        return None
    name = await file_name(db, org_id, str(item_id))
    if name is None or live_type_of(name) != "notebook":
        return None
    if not (await document_access(db, ctx, item_id)).can_read:
        return None
    return org_id


async def agent_chat_in_workspace(db: AsyncSession, target: Target, chat_id: str) -> bool:
    """Whether the agent of the chat ``chat_id`` works where the notebook is:
    the chat is a chat of the notebook's org and
    :func:`~alkera_core.files.workspace_identity.chat_works_at` the notebook
    (in the workspace whose folder holds it, or, a notebook in no workspace,
    inside the chat's own folder)."""
    try:
        chat_uuid = uuid.UUID(chat_id)
    except ValueError:
        return False
    chat = await db.get(WorkspaceObject, chat_uuid)
    if chat is None or chat.type != "chat" or chat.org_team_id != target.org_id:
        return False
    return chat_works_at(chat, [*target.allowed.chain, target.node])


__all__ = [
    "ActorKind",
    "Caller",
    "KernelScope",
    "Target",
    "Where",
    "agent_chat_in_workspace",
    "agent_of_holder",
    "box_follows",
    "chat_person",
    "is_notebook",
    "lease_admits_writes",
    "person_of",
    "runner_of",
]
