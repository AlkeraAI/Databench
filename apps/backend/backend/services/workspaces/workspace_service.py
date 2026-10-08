"""Creating, finding, renaming and ending workspaces, and the chats in them.

Three rules this module holds in one place:

* **Every chat is in a workspace.** A chat created while a workspace may hold
  only one chat gets a workspace of one in the same transaction
  (:func:`adopt_chat`), which points at the chat's own folder instead of
  owning one, so nothing the box leases, mirrors or reads by path moves.
* **A member has one main workspace**, made the first time it is asked for
  (:func:`ensure_main`). Its logical id is the member's user id in a namespace
  of its own, so the table's unique constraint is what keeps it one.
* **A chat uses its workspace owner's connections**
  (:func:`connection_owner_id`): a collaborator's personal connections never
  enter a workspace they do not own.

Nothing here commits; the caller announces and commits.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from alkera_core.auth.tenancy import member_stands
from alkera_core.authz import Action, CredentialKind, ResourceType, authorize
from alkera_core.authz.chat_scope import SCOPE_PRIVATE
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.roles import RoleResolver
from alkera_core.compute.org_machines import new_workspace_pin
from alkera_core.config import settings
from alkera_core.db.cross_tenant import cross_tenant_write
from alkera_core.db.errors import LOCK_NOT_AVAILABLE, sqlstate_of
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.db.tenant_session import bound_org_ids
from alkera_core.files import chat_workspace_id, objects_bridge
from alkera_core.files.clock import SystemClock
from alkera_core.files.leases import end_leases_under
from alkera_core.files.namespace import Namespace
from alkera_core.files.objects_bridge import WORKSPACE_CHATS_FOLDER, WORKSPACE_TYPE
from alkera_core.files.repo import FilesRepo
from alkera_core.models import User, WorkspaceObject
from alkera_core.models.files.tree import FileNode
from alkera_core.models.workspace_object import DEFAULT_NAMESPACE
from alkera_core.objects import workspace_end
from alkera_core.objects.workspaces import (
    ADOPTED_NAMESPACE,
    MAIN_NAMESPACE,
    MAIN_TITLE,
    record_box_report,
    workspace_spec_of,
)
from alkera_core.schemas.objects import ChatSpec, SandboxState, WorkspaceSpec
from sqlalchemy import Select, func, literal, select, text, tuple_, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from backend.services import chats, objects, sharing
from backend.services.org import ancestor_chain, files_transaction
from backend.services.workspaces import project_cap

CHAT_TYPE = "chat"


class WorkspaceFolderMissingError(LookupError):
    """The workspace a chat was to be filed in has no ``.chats`` folder to file
    it in: its folder, or the records folder in it, is in the trash."""


class WorkspaceGoneError(LookupError):
    """The workspace a chat was to be filed in was deleted first."""


class WorkspaceClientIdTakenError(ValueError):
    """A workspace's ``client_id`` already names an object that is not one."""

    def __init__(self, held_by: str) -> None:
        super().__init__(f"client_id already names a {held_by}, not a workspace")
        self.held_by = held_by


def is_main(workspace: WorkspaceObject) -> bool:
    return workspace_spec_of(workspace.spec).kind == "main"


def is_adopted(workspace: WorkspaceObject) -> bool:
    return workspace_spec_of(workspace.spec).layout == "adopted"


def folder_object_id(workspace: WorkspaceObject) -> UUID:
    """The object whose Files node IS this workspace's folder.

    A native workspace owns its folder; a workspace of one adopted its chat's,
    so a rung on that chat's folder is a rung on the workspace, and sharing
    one shares the other by construction.
    """
    spec = workspace_spec_of(workspace.spec)
    if spec.layout == "adopted" and spec.adopted_chat_id:
        return UUID(spec.adopted_chat_id)
    return workspace.id


async def load(db: AsyncSession, workspace_id: UUID) -> WorkspaceObject | None:
    """The live workspace with this id, from any org (the engine's tenancy
    floor refuses another org's, on record).

    A request's session sees only its own orgs' rows, so a miss there is read
    once more across orgs, by id alone: another org's workspace has to reach
    the policy to be refused on record rather than answer an unrecorded 404."""
    stmt = select(WorkspaceObject).where(
        WorkspaceObject.id == workspace_id,
        WorkspaceObject.type == WORKSPACE_TYPE,
        WorkspaceObject.deleted_at == 0,
    )
    found = (await db.execute(stmt)).scalar_one_or_none()
    if found is not None or bound_org_ids(db) is None:
        return found
    async with cross_tenant_write(db, reason="workspaces.load_by_id.probe"):
        return (await db.execute(stmt)).scalar_one_or_none()


async def load_ended(db: AsyncSession, workspace_id: UUID) -> WorkspaceObject | None:
    """The deleted workspace with this id, from any org, read the way
    :func:`load` reads a live one (another org's reaches the policy to be
    refused on record)."""
    stmt = select(WorkspaceObject).where(
        WorkspaceObject.id == workspace_id,
        WorkspaceObject.type == WORKSPACE_TYPE,
        WorkspaceObject.deleted_at != 0,
    )
    found = (await db.execute(stmt)).scalar_one_or_none()
    if found is not None or bound_org_ids(db) is None:
        return found
    async with cross_tenant_write(db, reason="workspaces.load_ended_by_id.probe"):
        return (await db.execute(stmt)).scalar_one_or_none()


async def adopt_chat(
    db: AsyncSession, *, chat: WorkspaceObject, owner: User
) -> WorkspaceObject | None:
    """A workspace of one for ``chat``, and the chat's reference to it; ``None``
    when the chat is left with no workspace.

    The workspace owns no folder: it points at the chat's, so the chat's folder
    node, its working directory, its lease and every path a box resolves stay
    exactly where they were. Idempotent on the chat: a second call lands on
    the workspace the first one made.

    Nothing is adopted while ``workspaces_adoption_enabled`` is off, nor while
    the table refuses workspace rows (the workspaces revision is being rolled
    back): a chat with no workspace is served as it is, and the reconcile pass
    adopts it once adoption is back on.
    """
    if not settings.workspaces_adoption_enabled:
        return None
    spec = WorkspaceSpec(kind="project", layout="adopted", adopted_chat_id=str(chat.id))
    try:
        workspace, _created = await objects.object_service.create_object(
            db,
            owner=owner,
            org_id=chat.org_team_id,
            owner_user_id=chat.owner_user_id,
            type=WORKSPACE_TYPE,
            title=chat.title,
            spec=spec.model_dump(mode="json"),
            logical_id=str(chat.id),
            namespace=ADOPTED_NAMESPACE,
            team_id=chat.team_id,
            visibility_scope=chat.visibility_scope,
            with_node=False,
        )
    except IntegrityError as exc:
        # The insert ran in a savepoint of its own, so the caller's transaction
        # stands; only a CHECK refusing the type is the schema saying "no
        # workspaces here", anything else is a real fault.
        if sqlstate_of(exc) != CHECK_VIOLATION:
            raise
        return None
    await set_chat_workspace(db, chat=chat, workspace_id=workspace.id)
    return workspace


async def default_target(db: AsyncSession, *, owner: User, org_id: UUID) -> WorkspaceObject | None:
    """The workspace a chat started with no ``workspace_id`` lands in: the
    owner's main workspace in ``org_id`` while ``workspaces_multi_chat`` is on, else none
    (a workspace of one is adopted for it once it exists). Idempotent with the
    create that follows, which asks :func:`placement_for` the same question."""
    if not settings.workspaces_multi_chat:
        return None
    target, _made = await ensure_main(db, owner=owner, org_id=org_id)
    return target


async def placement_for(
    db: AsyncSession,
    *,
    owner: User,
    org_id: UUID,
    workspace: WorkspaceObject | None,
) -> tuple[WorkspaceObject | None, UUID | None]:
    """Where a new chat goes: ``(workspace, the .chats folder its folder is
    filed in)``, or ``(None, None)`` for a chat that gets a workspace of one
    (:func:`adopt_chat`, once the chat exists) and is filed where a chat's
    folder always was.

    A named ``workspace`` wins. Otherwise the chat goes in the owner's main
    workspace while ``workspaces_multi_chat`` is on, a browser chat and a
    Slack thread alike. The folder is
    ``None`` with Files off: the chat is in the workspace by reference and has
    no folder to file anywhere.
    """
    chosen = await _held_for_filing(db, workspace) if workspace is not None else None
    if chosen is None and settings.workspaces_multi_chat:
        chosen, _made = await ensure_main(db, owner=owner, org_id=org_id)
    if chosen is None or not settings.files_enabled:
        return chosen, None
    if is_adopted(chosen):
        raise WorkspaceFolderMissingError("This workspace holds one chat.")
    ctx = ActingContext.for_user(user_id=owner.id, org_id=chosen.org_team_id, email=owner.email)
    async with files_transaction(db, ctx) as repo:
        node = await objects_bridge.live_node_for(repo, chosen.id)
        chats = await objects_bridge.workspace_chats_folder(repo, node) if node else None
        if chats is None and is_main(chosen) and chosen.owner_user_id == owner.id:
            chats = await _remake_main_folder(repo, ctx, chosen, node)
    if chats is None:
        raise WorkspaceFolderMissingError(
            "Restore this workspace's folder from the trash to start a chat in it."
        )
    return chosen, UUID(str(chats.id))


async def _remake_main_folder(
    repo: FilesRepo, ctx: ActingContext, main: WorkspaceObject, node: FileNode | None
) -> FileNode | None:
    """The ``.chats`` folder of its owner's main workspace, made again when it
    went: the whole folder when it was trashed, moved off or purged, the
    records folder alone when only that went. A main workspace cannot be
    deleted and every chat started with no place named lands in it, so a
    folder of it that is gone is not the owner's to restore before they may
    start a chat; one restored from the trash later is an ordinary folder
    beside it."""
    if node is None:
        node = await objects_bridge.node_for_object(repo, ctx, main)
    else:
        namespace = Namespace(repo, ctx, SystemClock())
        await objects_bridge.ensure_working_folders(
            repo, ctx, namespace, node, only=WORKSPACE_CHATS_FOLDER
        )
    return await objects_bridge.workspace_chats_folder(repo, node)


async def adopt_strays(
    db: AsyncSession,
    chats_read: Sequence[WorkspaceObject],
    *,
    announce_as: dict[str, Any] | None = None,
) -> list[WorkspaceObject]:
    """Adopt every live chat here that names no workspace; the ones written.

    The reconcile pass adopts every chat that existed before workspaces, but a
    chat can still arrive without one: created by a task still running the
    previous build during a roll, created while adoption was off, or
    rewritten by a stale writer that replaced the whole spec. Reading or
    listing the chat (a person opening it, the box discovering it) heals it
    with the same idempotent adoption, so a stray lands on the workspace of
    one it would have had (or the one already minted for it). A chat whose
    workspace of one was deleted stays as it is: deleting a workspace ends
    its chats, so such a row is not one to bring back.

    With ``announce_as``, each healed chat is announced with ``chat.updated``
    as that actor, so anyone holding it re-reads the row.
    """
    if not settings.workspaces_adoption_enabled:
        return []
    stray = [
        chat
        for chat in chats_read
        if chat.type == CHAT_TYPE
        and chat.deleted_at == 0
        and not ChatSpec.model_validate(chat.spec or {}).workspace_id
    ]
    if not stray:
        return []
    # The heal runs in a short session of its own and commits there: a heal
    # that gives up (a lock it would wait on too long, a retired id) rolls
    # back only its own work and never expires a row the caller loaded and is
    # about to read. The caller re-reads the chats it healed, and only those.
    healed_ids = await _heal_in_own_session(
        [(chat.id, chat.owner_user_id) for chat in stray], announce_as=announce_as
    )
    healed = [chat for chat in stray if chat.id in healed_ids]
    for chat in healed:
        await db.refresh(chat)
    return healed


async def _heal_in_own_session(
    strays: Sequence[tuple[UUID, UUID]], *, announce_as: dict[str, Any] | None
) -> set[UUID]:
    """Adopt each ``(chat id, owner id)`` that still names no workspace, in a
    session of its own, committed; the ids healed.

    A read never waits on a heal: each one may wait for a lock this long, and
    one that would wait longer is left for the next read or the reconcile
    pass. A concurrent heal of the same chat is not contention: the create
    lands on the workspace the other request made.
    """
    healed: set[UUID] = set()
    async with AsyncSessionLocal() as heal:
        await heal.execute(
            text("SELECT set_config('lock_timeout', :wait, true)"), {"wait": HEAL_LOCK_TIMEOUT}
        )
        for chat_id, owner_id in strays:
            chat = await heal.get(WorkspaceObject, chat_id)
            owner = await heal.get(User, owner_id)
            if chat is None or owner is None or chat.deleted_at != 0:
                continue
            if ChatSpec.model_validate(chat.spec or {}).workspace_id:
                continue
            try:
                async with heal.begin_nested():
                    adopted = await adopt_chat(heal, chat=chat, owner=owner)
            except objects.object_service.LogicalIdRetiredError:
                continue
            except IntegrityError:
                # A constraint refused the write (a racing writer, a schema
                # being rolled back): the read is served without the heal.
                continue
            except DBAPIError as exc:
                if not _lock_not_available(exc):
                    raise
                continue
            if adopted is None:
                continue
            healed.add(chat_id)
            if announce_as is not None:
                await chats.announce_chat(heal, chat=chat, actor=announce_as)
        await heal.commit()
    return healed


#: How long healing one stray chat may wait for a lock inside a read.
HEAL_LOCK_TIMEOUT = "1s"


#: Postgres's SQLSTATE for a row a CHECK constraint refused.
CHECK_VIOLATION = "23514"


def _lock_not_available(exc: DBAPIError) -> bool:
    """Whether Postgres refused the statement for waiting past ``lock_timeout``."""
    return sqlstate_of(exc) == LOCK_NOT_AVAILABLE


async def set_chat_workspace(
    db: AsyncSession, *, chat: WorkspaceObject, workspace_id: UUID
) -> None:
    """Record which workspace ``chat`` is in. A derived fact like ``last_seq``:
    it does not bump the chat's version, so nobody editing the chat loses a
    race to it.

    The one key is merged into the stored spec in SQL rather than the spec
    this session read written back whole, so a field another writer committed
    since that read (a title, a binding, the stance) is not reverted by it.
    """
    if chat_workspace_id(chat) == workspace_id:
        return
    await db.flush()
    merged = (
        await db.execute(
            update(WorkspaceObject)
            .where(WorkspaceObject.id == chat.id)
            .values(
                spec=WorkspaceObject.spec.op("||")(
                    func.jsonb_build_object("workspace_id", str(workspace_id))
                )
            )
            .returning(WorkspaceObject.spec, WorkspaceObject.updated_at)
            .execution_options(synchronize_session=False)
        )
    ).one()
    set_committed_value(chat, "spec", merged.spec)
    set_committed_value(chat, "updated_at", merged.updated_at)


async def record_sandbox_report(
    db: AsyncSession, *, chat: WorkspaceObject, ctx: ActingContext, report: Any
) -> WorkspaceObject | None:
    """Write the box's report on the sandbox of ``chat``'s workspace, when
    ``report`` (a publisher-state update) carries one; the workspace written,
    or ``None`` when there is nothing to write.

    The reporter is the machine the caller proved, else the chat's own bound
    machine. Only a native workspace has a sandbox of its own: a workspace of
    one is the chat, whose own spec already says all of it, so its report is
    dropped rather than turning the workspace into a second copy. Like
    ``last_seq`` it does not bump the version: it is the box's observation,
    not anybody's edit.

    The caller already holds the chat's lock, and deleting a workspace takes
    the workspace's lock before each chat's, so waiting here could deadlock
    with a delete. A workspace somebody else holds is skipped instead (``None``):
    the box reports again on its next change, and a workspace being deleted
    needs no note.
    """
    sandbox_state: SandboxState | None = getattr(report, "workspace_sandbox", None)
    if sandbox_state is None:
        return None
    spec_of_chat = ChatSpec.model_validate(chat.spec or {})
    machine_id = str(ctx.acting_principal.id) if ctx.is_machine else spec_of_chat.machine_id
    if not spec_of_chat.workspace_id or not machine_id:
        return None
    try:
        key = UUID(spec_of_chat.workspace_id)
    except ValueError:
        return None
    workspace = (
        await db.execute(
            select(WorkspaceObject)
            .where(
                WorkspaceObject.id == key,
                WorkspaceObject.type == WORKSPACE_TYPE,
                WorkspaceObject.deleted_at == 0,
                WorkspaceObject.org_team_id == chat.org_team_id,
            )
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if workspace is None or is_adopted(workspace):
        return None
    spec = record_box_report(
        workspace_spec_of(workspace.spec),
        machine_id=machine_id,
        sandbox_state=sandbox_state,
        memory_used_mb=getattr(report, "workspace_memory_mb", None),
        at=datetime.now(UTC).isoformat(),
    )
    workspace.spec = spec.model_dump(mode="json")
    await db.flush()
    return workspace


def _main_rows(owner: User, org_id: UUID) -> Select[tuple[WorkspaceObject]]:
    return select(WorkspaceObject).where(
        WorkspaceObject.org_team_id == org_id,
        WorkspaceObject.namespace == MAIN_NAMESPACE,
        WorkspaceObject.type == WORKSPACE_TYPE,
        WorkspaceObject.owner_user_id == owner.id,
    )


async def find_main(db: AsyncSession, *, owner: User, org_id: UUID) -> WorkspaceObject | None:
    """The member's live main workspace in ``org_id``: one per person per org."""
    return (
        await db.execute(_main_rows(owner, org_id).where(WorkspaceObject.deleted_at == 0))
    ).scalar_one_or_none()


def _main_logical_id(owner: User, ended: int) -> str:
    """The logical id a member's next main workspace is minted under. The
    first is the member's id; one made after ``ended`` earlier ones were ended
    (an org admin offboarded a departed member who later came back) carries
    the count, because a tombstone keeps its logical id for good."""
    return str(owner.id) if ended == 0 else f"{owner.id}:{ended}"


async def ensure_main(
    db: AsyncSession, *, owner: User, org_id: UUID
) -> tuple[WorkspaceObject, bool]:
    """The member's main workspace in ``org_id``, made now if they have none:
    ``(it, made)``. One made now runs on the org's default machine for new
    workspaces when the member may use it.

    Two first requests racing each other make one: both count the same ended
    main workspaces and so mint under the same logical id, the loser's insert
    is refused by the unique ``(org, namespace, logical_id)`` inside its
    savepoint, and it reads the winner's.
    """
    found = await find_main(db, owner=owner, org_id=org_id)
    if found is not None:
        return found, False
    ended = int(
        await db.scalar(
            select(func.count()).select_from(
                _main_rows(owner, org_id).where(WorkspaceObject.deleted_at != 0).subquery()
            )
        )
        or 0
    )
    pin = await new_workspace_pin(db, org_id=org_id, user_id=owner.id)
    spec = WorkspaceSpec(
        kind="main", layout="native", machine_pin=str(pin) if pin is not None else None
    )
    try:
        async with db.begin_nested():
            workspace, created = await objects.object_service.create_object(
                db,
                owner=owner,
                org_id=org_id,
                type=WORKSPACE_TYPE,
                title=MAIN_TITLE,
                spec=spec.model_dump(mode="json"),
                logical_id=_main_logical_id(owner, ended),
                namespace=MAIN_NAMESPACE,
                visibility_scope=SCOPE_PRIVATE,
            )
    except IntegrityError:
        raced = await find_main(db, owner=owner, org_id=org_id)
        if raced is None:
            raise
        return raced, False
    return workspace, created


async def create_project(
    db: AsyncSession,
    *,
    owner: User,
    org_id: UUID,
    title: str,
    client_id: str | None,
    object_id: UUID | None = None,
) -> tuple[WorkspaceObject, bool]:
    """A new project workspace with a folder of its own: ``files/`` for the
    shared tree and ``.chats/`` for its chats' records. Idempotent on the
    caller's ``client_id``.

    Raises :class:`WorkspaceClientIdTakenError` when the id already names an
    object of another type, ``object_service.LogicalIdRetiredError`` when it
    named a workspace that was deleted, and
    :class:`~backend.services.workspaces.project_cap.WorkspaceProjectCapError`
    when the owner already has as many project workspaces as the org allows.
    A retry of a ``client_id`` that already made one is never refused by the
    cap: it makes nothing.
    """
    if not await _replays(db, org_id=org_id, client_id=client_id):
        try:
            await project_cap.admit_project(db, owner_id=owner.id, org_id=org_id)
        except project_cap.WorkspaceProjectCapError:
            # A concurrent retry of the same client_id may have made it while
            # this one waited on the count's lock: that is a replay too.
            if not await _replays(db, org_id=org_id, client_id=client_id):
                raise
    workspace, created = await objects.object_service.create_object(
        db,
        owner=owner,
        org_id=org_id,
        type=WORKSPACE_TYPE,
        title=title,
        spec=WorkspaceSpec(kind="project", layout="native").model_dump(mode="json"),
        logical_id=client_id,
        namespace=DEFAULT_NAMESPACE,
        object_id=object_id,
        visibility_scope=SCOPE_PRIVATE,
    )
    if not created and workspace.type != WORKSPACE_TYPE:
        raise WorkspaceClientIdTakenError(workspace.type)
    return workspace, created


async def _replays(db: AsyncSession, *, org_id: UUID, client_id: str | None) -> bool:
    """Whether ``client_id`` already names a live object in the org."""
    if not client_id:
        return False
    held = await objects.object_service.find_by_logical_id(
        db, org_team_id=org_id, namespace=DEFAULT_NAMESPACE, logical_id=client_id
    )
    return held is not None


def chats_in_statement(workspace_ids: Sequence[UUID]) -> Select[tuple[WorkspaceObject]]:
    """The live chats of these workspaces, oldest first. Naming the workspaces
    implies the ``workspace_id IS NOT NULL`` the workspace index is partial on,
    so this is the read that index serves."""
    keys = [str(workspace_id) for workspace_id in workspace_ids]
    return (
        select(WorkspaceObject)
        .where(
            WorkspaceObject.type == CHAT_TYPE,
            WorkspaceObject.deleted_at == 0,
            WorkspaceObject.spec["workspace_id"].astext.in_(keys),
        )
        .order_by(WorkspaceObject.created_at, WorkspaceObject.id)
    )


async def chats_page(
    db: AsyncSession,
    workspace_id: UUID,
    *,
    limit: int,
    after: tuple[datetime, UUID] | None,
) -> tuple[list[WorkspaceObject], bool]:
    """One page of a workspace's live chats, oldest first, after the keyset
    ``after``: ``(chats, whether more follow)``. Bounded however many chats the
    workspace holds."""
    stmt = chats_in_statement([workspace_id])
    if after is not None:
        stmt = stmt.where(
            tuple_(WorkspaceObject.created_at, WorkspaceObject.id)
            > tuple_(literal(after[0]), literal(after[1]))
        )
    rows = list((await db.execute(stmt.limit(limit + 1))).scalars().all())
    return rows[:limit], len(rows) > limit


#: How many of a workspace's most recently active chats a wake looks through
#: for the one to wake.
RECENT_CHATS_CONSIDERED: Final = 50


async def recent_chats(
    db: AsyncSession, workspace_id: UUID, *, limit: int = RECENT_CHATS_CONSIDERED
) -> list[WorkspaceObject]:
    """A workspace's live chats, the most recently active first."""
    stmt = (
        chats_in_statement([workspace_id])
        .order_by(None)
        .order_by(WorkspaceObject.updated_at.desc(), WorkspaceObject.id)
    )
    return list((await db.execute(stmt.limit(limit))).scalars().all())


async def chats_by_workspace(
    db: AsyncSession, workspace_ids: Sequence[UUID]
) -> dict[UUID, list[WorkspaceObject]]:
    """The live chats of each workspace, oldest first, in one indexed read."""
    if not workspace_ids:
        return {}
    rows = (await db.execute(chats_in_statement(workspace_ids))).scalars().all()
    found: dict[UUID, list[WorkspaceObject]] = {workspace_id: [] for workspace_id in workspace_ids}
    for chat in rows:
        raw = ChatSpec.model_validate(chat.spec or {}).workspace_id
        if raw:
            found.setdefault(UUID(raw), []).append(chat)
    return found


async def connection_owner_id(db: AsyncSession, chat: WorkspaceObject) -> UUID:
    """Whose connections the agent in ``chat`` uses: the workspace owner's.

    A chat a collaborator started in somebody else's workspace runs on the
    workspace owner's connections, never the collaborator's own, so one
    person's agent cannot write query results into files another person's
    agent reads under a credential that person never granted. For a
    workspace of one the owner is the chat's owner.

    Decided again on every call, never trusted off the spec: the workspace
    owner's connections apply only while the chat's owner IS the workspace
    owner or may still write the workspace. A collaborator whose share was
    revoked keeps their chat, and it falls back to their own connections. A
    chat whose workspace is not found (a row the adoption has not reached)
    uses its own owner's, which is what it used before workspaces existed.
    The turns such a chat runs are billed to the chat's owner either way.
    """
    raw = ChatSpec.model_validate(chat.spec or {}).workspace_id
    workspace = await load(db, UUID(raw)) if raw else None
    if workspace is None or workspace.org_team_id != chat.org_team_id:
        return chat.owner_user_id
    if workspace.owner_user_id == chat.owner_user_id:
        return workspace.owner_user_id
    if await may_write(db, workspace, chat.owner_user_id):
        return workspace.owner_user_id
    return chat.owner_user_id


async def multi_chat_workspace_of(
    db: AsyncSession, chat: WorkspaceObject
) -> WorkspaceObject | None:
    """The workspace ``chat`` is in when that workspace holds several chats (a
    project or its owner's main workspace), or ``None`` for a workspace of one.

    In such a workspace every chat's agent works in the shared tree, so who
    may drive the chat, and who a share may reach, is decided on the
    workspace rather than on the chat alone.
    """
    raw = ChatSpec.model_validate(chat.spec or {}).workspace_id
    workspace = await load(db, UUID(raw)) if raw else None
    if workspace is None or workspace.org_team_id != chat.org_team_id or is_adopted(workspace):
        return None
    return workspace


async def refuses_chat_share(db: AsyncSession, chat_id: UUID) -> bool:
    """Whether a share granted on the chat ``chat_id`` alone is refused: the chat
    is in a workspace that holds several chats, where a rung on one chat would
    let its holder watch an agent working in a tree they were never given. The
    workspace is what gets shared there."""
    chat = await db.get(WorkspaceObject, chat_id)
    if chat is None or chat.type != CHAT_TYPE:
        return False
    return await multi_chat_workspace_of(db, chat) is not None


async def may_write(db: AsyncSession, workspace: WorkspaceObject, user_id: UUID) -> bool:
    """Whether ``user_id`` passes ``workspace.access`` WRITE on the workspace
    now: the same facts and the same policy the add-a-chat door decides on,
    asked without a decision row (nothing was attempted)."""
    user = await db.get(User, user_id)
    if user is None or not await member_stands(db, user=user, org_team_id=workspace.org_team_id):
        return False
    ctx = ActingContext.for_user(user_id=user.id, org_id=workspace.org_team_id, email=user.email)
    roles = RoleResolver(db, ctx, ancestor_chain=ancestor_chain)
    reader = await sharing.resolve_reader(db, ctx=ctx, roles=roles, user=user)
    attrs = await sharing.workspace_attrs(db, workspace, reader)
    resource = sharing.object_resource(workspace, type=ResourceType.WORKSPACE)
    return authorize(ctx, Action.WRITE, resource, attrs).allowed


async def tombstone_adopted_for_chat(db: AsyncSession, *, chat_id: UUID) -> WorkspaceObject | None:
    """End the workspace of one a deleted chat was in, in the same
    transaction. A native workspace keeps standing when one of its chats goes;
    only the workspace that IS the chat goes with it."""
    workspace = (
        await db.execute(
            select(WorkspaceObject)
            .where(
                WorkspaceObject.type == WORKSPACE_TYPE,
                WorkspaceObject.namespace == ADOPTED_NAMESPACE,
                WorkspaceObject.logical_id == str(chat_id),
                WorkspaceObject.deleted_at == 0,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if workspace is None:
        return None
    await objects.object_service.tombstone(db, workspace, trash_node=False)
    return workspace


async def rename(
    db: AsyncSession, *, workspace: WorkspaceObject, title: str, expected_version: int
) -> WorkspaceObject:
    """Rename the workspace, naming the version read. A workspace of one IS
    its chat to the person looking at it, so the chat takes the same name;
    its version is not compared, because the caller read the workspace, not
    the chat."""
    updated = await objects.object_service.apply_update(
        db, obj=workspace, expected_version=expected_version, title=title
    )
    spec = workspace_spec_of(updated.spec)
    if spec.layout == "adopted" and spec.adopted_chat_id:
        chat = await objects.object_service.lock(db, UUID(spec.adopted_chat_id))
        if chat is not None and chat.title != title:
            await objects.object_service.apply_update(
                db, obj=chat, expected_version=chat.version, title=title
            )
    return updated


async def _held_for_filing(db: AsyncSession, workspace: WorkspaceObject) -> WorkspaceObject:
    """The named workspace, share-locked until the creating transaction ends.

    :func:`end` takes the row's update lock before it lists the chats it ends,
    so the two serialize: a chat filed first is one ``end`` then ends, and a
    workspace ended first is refused here, never left with a live chat in a
    folder that went to the trash.
    """
    held = (
        await db.execute(
            select(WorkspaceObject)
            .where(WorkspaceObject.id == workspace.id, WorkspaceObject.deleted_at == 0)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if held is None:
        raise WorkspaceGoneError("This workspace was deleted.")
    return held


async def end(
    db: AsyncSession, *, workspace: WorkspaceObject, actor: dict[str, Any] | None
) -> WorkspaceObject | None:
    """End a workspace and every chat in it, in the caller's transaction, in a
    bounded number of statements however many chats it holds. ``None`` when it
    was already ended.

    The workspace and every live chat in it are tombstoned here, so nothing can
    open, list, send to or file into any of them once the caller commits; each
    chat's doorbell rings with the deletion as the reason, so the box serving it
    drops it and leaves nothing of it on disk; and every lease under the
    workspace's folder ends, so that box is fenced on its next beat. What is
    left of each chat's ending (asleep, its folder trashed) and the workspace's
    own folder going to the trash is finished by the background pass
    (:mod:`alkera_core.objects.workspace_end`), which the caller nudges once it
    has committed. A workspace with no chats is finished here.

    The row's update lock is taken before the chats are ended, so a chat being
    filed into the workspace concurrently is either among them or refused
    (:func:`_held_for_filing`).
    """
    locked = await objects.object_service.lock(db, workspace.id)
    if locked is None:
        return None
    ended = await workspace_end.begin(db, workspace=locked, now=datetime.now(UTC), actor=actor)
    if ended:
        await _end_leases_under(db, locked)
    await objects.object_service.tombstone(
        db, locked, trash_node=not ended and not is_adopted(locked)
    )
    await objects.object_service.announce(db, obj=locked, actor=actor)
    return locked


async def _end_leases_under(db: AsyncSession, workspace: WorkspaceObject) -> None:
    """End every live lease on the workspace's folder and anything under it:
    the shared tree, the records folder and every chat's folder (for a
    workspace of one, the chat's own folder, which is the workspace's)."""
    if not settings.files_enabled:
        return
    ctx = ActingContext.for_service(
        token_id=workspace.owner_user_id,
        org_id=workspace.org_team_id,
        label="workspace_end",
        credential=CredentialKind.CI_TOKEN,
    )
    async with files_transaction(db, ctx) as repo:
        node = await objects_bridge.live_node_for(repo, folder_object_id(workspace))
        if node is not None:
            await end_leases_under(repo, node)


__all__ = [
    "WorkspaceClientIdTakenError",
    "WorkspaceFolderMissingError",
    "WorkspaceGoneError",
    "adopt_chat",
    "adopt_strays",
    "chats_by_workspace",
    "chats_in_statement",
    "chats_page",
    "connection_owner_id",
    "create_project",
    "end",
    "ensure_main",
    "find_main",
    "folder_object_id",
    "is_adopted",
    "is_main",
    "load",
    "load_ended",
    "may_write",
    "multi_chat_workspace_of",
    "placement_for",
    "recent_chats",
    "refuses_chat_share",
    "rename",
    "set_chat_workspace",
    "tombstone_adopted_for_chat",
]
