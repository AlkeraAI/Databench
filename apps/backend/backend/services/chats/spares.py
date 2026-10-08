"""Warming a chat ahead of a person's first message, and taking it back.

The wait on the first message of a new chat is everything the box does
before the prompt can run: take the folder's lease, spawn the agent, inject
its config. All of that can happen before anybody types. While a person is on
the chat page, this keeps one chat warmed for them in the org the page is in
— a real row, placed like any chat, which the box opens a session for the
moment it lists it — and the empty composer's first send takes it instead of
creating one. A person in several orgs keeps one spare in each.

Nothing here is ever an error the page shows. A warm that cannot happen
(no live machine, the drive refusing a create, the catalog unreachable) is
``none``, and the first message then takes the ordinary path; the page
neither knows nor says which happened.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import structlog
from alkera_core.authz import Action, Resource, ResourceType
from alkera_core.authz.principal import ActingContext
from alkera_core.compute.machines import live_workspace_machines, machine_state
from alkera_core.config import settings
from alkera_core.db.locking import advisory_key, advisory_xact_lock
from alkera_core.files.errors import FilesError
from alkera_core.models import User, WorkspaceObject
from alkera_core.objects import chat_spares
from alkera_core.schemas.objects import ChatSpareState
from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.dependencies import CurrentPrincipal
from backend.authz import enforce, role_resolver
from backend.services import workspaces
from backend.services.chats import catalog as chat_catalog
from backend.services.chats import chat_service
from backend.services.compute.placement import resolve_machine_for
from backend.services.org import preferences as preferences_service
from backend.services.org import teams as team_service
from backend.services.sharing import access

logger = structlog.get_logger(__name__)

#: The machine states a spare is worth keeping on. ``starting`` counts: the box
#: is coming up and will list the spare on its first pass.
LIVE = frozenset({"ready", "starting"})


async def _machine_is_live(db: AsyncSession, chat: WorkspaceObject) -> bool:
    spec = chat_service.chat_spec_of(chat)
    if spec.publisher_refusal or not spec.machine_id:
        return False
    try:
        bound = UUID(spec.machine_id)
    except ValueError:
        return False
    rows = (await db.execute(live_workspace_machines(chat.org_team_id))).scalars().all()
    alloc = next((row for row in rows if row.id == bound), None)
    return machine_state(alloc) in LIVE


async def _hold_owner(db: AsyncSession, owner_id: UUID, org_id: UUID) -> None:
    """Serialize this owner's warms in ``org_id`` for the rest of the transaction.

    "At most one spare per owner in each org" is a partial unique index, so two warms
    racing — two tabs, or a page that mounts the warmer twice — both read no
    spare, both insert, and the loser gets a unique violation the page can do
    nothing with. The lock makes the read-and-warm one step per owner: the
    second caller waits, then finds the first one's spare standing and stamps
    it. It is an *xact* lock, so it goes when the transaction does and a
    caller that dies mid-warm blocks nobody.
    """
    await advisory_xact_lock(db, advisory_key("chat-spare", owner_id, org_id))


async def _ensure_drive(db: AsyncSession, user: User, *, org_id: UUID) -> None:
    """Make ``org_id``'s drive, and the dedup domain under it, before the lock.

    A chat's folder is created inside the warm, and on the org's very first one
    that also makes the drive and a brand-new dedup domain — which is marked in
    the OBJECT STORE as it is made. An outbound write is the one thing that
    must never sit inside the per-owner lock: a second tab would wait on the
    bucket and come back as a lock timeout on a call the page cannot act on.
    The ensure is idempotent, so the create under the lock finds it standing
    and writes nothing but rows.
    """
    if not settings.files_enabled:
        return
    from backend.services.files.context import ensure_store_row, stamp_new_domain

    # Read what is needed off the row first. Losing the race for the store row
    # or the drive rolls this session back, which expires every instance loaded
    # on it, and a lazy re-read of `user` from async code is a hard error.
    user_id, email = user.id, user.email
    ctx = ActingContext.for_user(user_id=user_id, org_id=org_id, email=email)
    store = await ensure_store_row(db, settings)
    await db.commit()
    made: list[UUID] = []

    async def record(domain_id: UUID) -> None:
        made.append(domain_id)

    # An org makes its drive once, and two first warms would otherwise race to
    # make it and collide on its unique row. Serialize on the ORG here, where
    # the wait costs one org one time and is not in any owner's way — and keep
    # the marker OUT of it: the lock covers rows only.
    await advisory_xact_lock(db, advisory_key("files-drive-create", org_id))
    async with team_service.files_transaction(db, ctx, on_domain_created=record):
        pass
    await db.commit()
    # The lock went with that transaction. The marker is written now, holding
    # nothing: it is a write to the object store, and a store that is slow or
    # refusing must never be something another request waits behind. An
    # unmarked domain is already the safe failure — the collector reports it
    # and never collects it, and the janitor's marker sweeper settles it.
    stamp = stamp_new_domain(store.id, settings, db)
    for domain_id in made:
        await stamp(domain_id)
    # What the stamp recorded about each domain is on this session and nothing
    # above has committed it.
    await db.commit()
    # Whatever the ensure and the stamp rolled back on the way, the caller goes
    # on to create a chat owned by this row: load it back here rather than
    # letting the next attribute read try it from async code.
    await db.refresh(user)


async def usable(db: AsyncSession, chat: WorkspaceObject) -> bool:
    """Whether a spare can still be handed to its owner: its box is live and
    has not refused it. Anything else is reaped rather than claimed — a spare
    on a dead machine would only hand the reader the "no workspace" wait the
    spare exists to remove."""
    return chat_spares.is_spare(chat) and await _machine_is_live(db, chat)


async def warm(
    request: Request, db: AsyncSession, ctx: CurrentPrincipal, user: User
) -> ChatSpareState:
    """The page's heartbeat: keep one spare standing for this person.

    A spare already standing on a live box has its idle clock moved and
    nothing else happens — the common case, one indexed read and one write.
    A spare whose box is gone is reaped and replaced. With none, one is
    warmed: placed by the same resolver a create uses, pinned to the same
    model/effort/mode the composer would preselect, its folder made in the
    owner's ``Chats`` like any chat's. It is not announced and no box lists
    it: an agent starts when the owner's first send claims it, not before.
    The decision that admits it is the ordinary CREATE, filed under the id the
    spare has, so the audit trail of a claimed chat starts at its warm.
    """
    now = datetime.now(UTC)
    await _hold_owner(db, user.id, ctx.org_id)
    # One spare per person in each org: another org's spare is that org's
    # page's to keep, and this read never sees it, so never stamps or reaps it.
    standing = await chat_spares.find_spare(db, owner_id=user.id, org_id=ctx.org_id)
    if standing is not None:
        if await usable(db, standing):
            await chat_spares.stamp_active(db, standing, now=now)
            await db.commit()
            return ChatSpareState(state="warm")
        await chat_spares.reap(db, standing)
    await db.commit()
    # The commit dropped the lock with its transaction. What follows is not the
    # read-and-insert the lock exists for: the placement, and above all the
    # catalog's round trip to the gateway, hold nothing. A second tab waits on
    # this owner's database work and never on an outbound call to another
    # service — a gateway slower than `lock_timeout` would otherwise come back
    # to the page as the same unanswerable error the lock was added to remove.
    binding = await resolve_machine_for(db, ctx=ctx, org_team_id=ctx.org_id, purpose="chat")
    if binding is None:
        return ChatSpareState(state="none")
    try:
        pin = await chat_catalog.default_pin_for(db, user, ctx.org_id)
    except (chat_catalog.CatalogUnavailableError, chat_catalog.NoModelOfferedError) as exc:
        logger.info("chat.spare.unpinnable", user_id=str(user.id), reason=str(exc))
        return ChatSpareState(state="none")
    mode = await preferences_service.starting_cloud_mode(db, user_id=user.id, org_id=ctx.org_id)
    reader = await access.resolve_reader(
        db, ctx=ctx, roles=role_resolver(request, db, ctx), user=user
    )
    object_id = uuid4()
    await _ensure_drive(db, user, org_id=ctx.org_id)
    # Take the lock again for the half that must not race — the read that finds
    # no spare and the insert that makes one — and re-read under it: another
    # caller may have warmed one while this one was resolving a model.
    await _hold_owner(db, user.id, ctx.org_id)
    raced = await chat_spares.find_spare(db, owner_id=user.id, org_id=ctx.org_id)
    if raced is not None:
        await db.commit()
        return ChatSpareState(state="warm")
    try:
        await enforce(
            request,
            db,
            ctx,
            Action.CREATE,
            Resource(ResourceType.CHAT, id=str(object_id), org_id=ctx.org_id),
            access.new_chat_attrs(reader),
        )
    except HTTPException:
        # An unverified email, or a reader the policy would not let start a
        # chat: their first message meets the same refusal, in words, there.
        await db.commit()
        return ChatSpareState(state="none")
    owner_id = str(user.id)  # read now: the rollback below expires the row
    try:
        _chat, _created = await chat_service.create_chat(
            db,
            owner=user,
            org_id=ctx.org_id,
            title=None,
            client_id=None,
            machine_id=str(binding.machine_id),
            machine_status=binding.chat_status,
            model=pin,
            permission_mode=mode,
            object_id=object_id,
            spare=True,
        )
    except (FilesError, workspaces.WorkspaceFolderMissingError) as refused:
        # The drive would not make the folder (the storage safety mode, the
        # node ceiling, a lease the write is not admitted under) or the main
        # workspace's folder is in the trash. Nothing is warmed, and the first
        # message meets the same refusal where it can be read: warming never
        # answers 500.
        await db.rollback()
        logger.info("chat.spare.folder_refused", user_id=owner_id, reason=str(refused))
        return ChatSpareState(state="none")
    await db.commit()
    return ChatSpareState(state="warm")


async def reap_for_user(db: AsyncSession, *, user_id: UUID) -> bool:
    """Signing out ends the person's spares: nobody is on the page any more.
    The session a sign-out ends spans every org it switched to, so every org's
    spare goes. Returns whether there was one."""
    standing = await chat_spares.spares_of_owner(db, owner_id=user_id)
    for chat in standing:
        await chat_spares.reap(db, chat)
    return bool(standing)


__all__ = ["LIVE", "reap_for_user", "usable", "warm"]
