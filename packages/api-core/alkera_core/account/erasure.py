"""Erasing one account: the wind-down, then the erasure itself.

Two transactions, in this order, both driven by the worker's sweep once a
deletion request's grace window has ended:

1. :func:`wind_down` ends every credential and every live chat the person has,
   and commits. Their pooled chat machines then leave the plane on their own
   (the chat endings release them), which is why it is its own step.
2. :func:`erase` recomputes the plan and refuses (:class:`ErasureBlocked`) if
   anything still needs settling: live compute, a renewing paid plan, a sole
   admin seat that appeared during the grace window. Otherwise, in one
   transaction: shared items move to each org's recipient and private ones are
   deleted with their folders; memberships end; orgs the person was alone in
   close; every registered disposition runs; the audit trails are scrubbed and
   resealed; the identity row becomes the tombstone; the request records its
   certificate.

Nothing here talks to the network except the Files store, and that only
through the Trash service. What a contributor keeps for the person in the
account archive store is deleted by the caller after the commit
(:attr:`ErasureResult.archive_keys`).
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import CursorResult, String, delete, func, select, text, update
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core import audit_chain
from alkera_core.account import dispositions
from alkera_core.account.contributors import (
    ContentHandover,
    ExternalErasure,
    OrgClosing,
    OwnedContent,
    contributors,
)
from alkera_core.account.dispositions import TOMBSTONE_LABEL, tombstone_email
from alkera_core.account.files_erasure import empty_drive, purge_object_folders
from alkera_core.account.plan import (
    compute_plan,
    shared_object_ids,
    transfer_recipient,
)
from alkera_core.auth.revocation import revoke_all_for_user, revoke_memberships
from alkera_core.authz.enums import PrincipalKind
from alkera_core.db.cross_tenant import cross_tenant_write
from alkera_core.events import Entity, EventType, actor_system, emit
from alkera_core.logging import get_logger
from alkera_core.models import (
    AccountDeletionRequest,
    AuditLog,
    CiToken,
    Invitation,
    InvitationStatus,
    MachineCredential,
    MembershipStatus,
    OrgAuditEvent,
    OrgMembership,
    PersonalAccessToken,
    ProxyToken,
    RoleAssignment,
    Team,
    TeamMembership,
    User,
    WorkspaceObject,
)
from alkera_core.models.crdt_doc import CrdtDoc
from alkera_core.models.realtime_doc import RealtimeDoc
from alkera_core.models.sso_link_request import SsoLinkRequest
from alkera_core.objects import chat_end
from alkera_core.schemas.account import DeletionPlan, ErasureCertificate, ResealedChain

log = get_logger(__name__)

#: The actor every event the erasure emits is recorded under.
ACTOR_LABEL = "account.erasure"

#: The name a closed org is left with: the root row stays only to hold the
#: billing records the ledger must keep, and the org's own name may be personal.
CLOSED_ORG_NAME = "Deleted organization"


class ErasureBlocked(Exception):  # noqa: N818 - a state the sweep records, not a failure
    """The account cannot be erased yet; ``code`` is the first blocker's."""

    def __init__(self, plan: DeletionPlan) -> None:
        self.plan = plan
        self.code = plan.blockers[0].code
        super().__init__(f"erasure blocked: {self.code}")


@dataclass
class ErasureContext:
    """What every stage and disposition step reads and counts into."""

    db: AsyncSession
    user_id: uuid.UUID
    email: str
    plan: DeletionPlan
    now: datetime
    rows: Counter[str] = field(default_factory=Counter)

    def count(self, key: str, n: int) -> None:
        if n:
            self.rows[key] += n


@dataclass(frozen=True)
class ErasureResult:
    request_id: uuid.UUID
    certificate: ErasureCertificate
    #: The address the account had, for the completion email sent after commit.
    email: str
    first_name: str
    #: Objects in the account archive store to delete once the erasure commits,
    #: as the contributors named them (:attr:`AccountContributor.stored_objects`).
    archive_keys: tuple[str, ...]


def _rowcount(result: object) -> int:
    return int(result.rowcount) if isinstance(result, CursorResult) else 0


def _actor() -> dict[str, Any]:
    return actor_system(ACTOR_LABEL)


# --------------------------------------------------------------------------- #
# Wind-down
# --------------------------------------------------------------------------- #


async def revoke_credentials(db: AsyncSession, user_id: uuid.UUID) -> None:
    """End every credential the person holds: sessions and refresh families
    and the machine credentials of boxes on their own hardware
    (``revoke_all_for_user``), and personal access tokens. Device grants redeem
    into the session registry, so they end with it."""
    now = datetime.now(UTC)
    await revoke_all_for_user(db, user_id)
    await db.execute(
        update(PersonalAccessToken)
        .where(PersonalAccessToken.user_id == user_id, PersonalAccessToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await db.flush()


async def wind_down(db: AsyncSession, user_id: uuid.UUID) -> int:
    """End every credential and every live chat. Returns how many chats ended.
    Idempotent; the caller commits."""
    async with cross_tenant_write(db, reason="account.wind_down"):
        await revoke_credentials(db, user_id)
        chats = await chat_end.live_chats_owned_by(db, user_id)
        await chat_end.end_chats(db, chats, chat_end.ChatEndReason.ACCESS_REMOVED, actor=_actor())
    return len(chats)


# --------------------------------------------------------------------------- #
# Stages
# --------------------------------------------------------------------------- #


async def _content_in_leaving_org(
    ctx: ErasureContext, org_id: uuid.UUID, recipient: uuid.UUID
) -> None:
    """Shared items move to ``recipient``; private ones are deleted, folders first."""
    db = ctx.db
    shared_objects = sorted(await shared_object_ids(db, org_id, ctx.user_id), key=str)
    if shared_objects:
        await db.execute(
            update(WorkspaceObject)
            .where(WorkspaceObject.id.in_(shared_objects))
            .values(owner_user_id=recipient, version=WorkspaceObject.version + 1)
            .execution_options(synchronize_session=False)
        )
    ctx.count("workspace_objects.owner_user_id:transfer", len(shared_objects))
    if shared_objects:
        handover = ContentHandover(
            db=db,
            org_id=org_id,
            user_id=ctx.user_id,
            recipient=recipient,
            object_ids=shared_objects,
        )
        for contributor in contributors():
            if contributor.content_handover is not None:
                for key, n in (await contributor.content_handover(handover)).items():
                    ctx.count(key, n)
        ctx.count(
            "realtime_docs.owner_user_id:transfer",
            _rowcount(
                await db.execute(
                    update(RealtimeDoc)
                    .where(
                        RealtimeDoc.owner_user_id == ctx.user_id,
                        RealtimeDoc.doc_id.in_([str(i) for i in shared_objects]),
                    )
                    .values(owner_user_id=recipient)
                )
            ),
        )
        for object_id in shared_objects:
            await emit(
                db,
                org_id=org_id,
                type=EventType.WORKSPACE_OBJECT_CHANGED,
                entity=Entity.WORKSPACE_OBJECT,
                entity_id=str(object_id),
                payload={"owner_transferred": True},
                actor=_actor(),
            )
    await _delete_owned(ctx, org_id, recipient=recipient)


async def _delete_owned(
    ctx: ErasureContext, org_id: uuid.UUID, *, recipient: uuid.UUID | None = None
) -> None:
    """Delete every item the person still owns in the org (the private ones,
    once shared ones have moved; all of them in an org that closes). Each
    contributor hands its shared items to ``recipient``, when there is one,
    before erasing the rest."""
    db = ctx.db
    object_ids = list(
        (
            await db.execute(
                select(WorkspaceObject.id).where(
                    WorkspaceObject.org_team_id == org_id,
                    WorkspaceObject.owner_user_id == ctx.user_id,
                )
            )
        )
        .scalars()
        .all()
    )
    await _delete_objects(ctx, org_id, object_ids)
    owned = OwnedContent(db=db, org_id=org_id, user_id=ctx.user_id, recipient=recipient)
    for contributor in contributors():
        if contributor.owned_content is not None:
            for key, n in (await contributor.owned_content(owned)).items():
                ctx.count(key, n)


async def _delete_objects(
    ctx: ErasureContext, org_id: uuid.UUID, object_ids: list[uuid.UUID]
) -> None:
    if not object_ids:
        return
    db = ctx.db
    files = await purge_object_folders(db, org_id, ctx.user_id, object_ids)
    ctx.count("file_nodes:purged", files.purged)
    ctx.count("file_nodes:held", files.held)
    doc_ids = [str(i) for i in object_ids]
    await db.execute(delete(RealtimeDoc).where(RealtimeDoc.doc_id.in_(doc_ids)))
    await db.execute(delete(CrdtDoc).where(CrdtDoc.org_id == org_id, CrdtDoc.doc_id.in_(doc_ids)))
    removed = _rowcount(
        await db.execute(
            delete(WorkspaceObject)
            .where(WorkspaceObject.id.in_(object_ids))
            .execution_options(synchronize_session=False)
        )
    )
    ctx.count("workspace_objects.owner_user_id:erase", removed)
    for object_id in object_ids:
        await emit(
            db,
            org_id=org_id,
            type=EventType.WORKSPACE_OBJECT_CHANGED,
            entity=Entity.WORKSPACE_OBJECT,
            entity_id=str(object_id),
            payload={"deleted": True},
            actor=_actor(),
        )


async def end_membership(ctx: ErasureContext, membership: OrgMembership) -> None:
    """Take the person out of one org for good, with the outcome
    ``org_memberships.remove`` produces: credentials there ended, role
    assignments revoked (kept as history), team seats gone with the membership
    (the composite key cascades), machine audience grants gone (their
    disposition), and the change announced in the org."""
    db = ctx.db
    org_id = membership.org_team_id
    await revoke_memberships(db, [membership], reason="account_deleted")
    ctx.count(
        "role_assignments.principal_id:revoke",
        _rowcount(
            await db.execute(
                update(RoleAssignment)
                .where(
                    RoleAssignment.org_team_id == org_id,
                    RoleAssignment.principal_kind == PrincipalKind.USER,
                    RoleAssignment.principal_id == ctx.user_id,
                    RoleAssignment.revoked_at.is_(None),
                )
                .values(revoked_at=ctx.now)
            )
        ),
    )
    ctx.count(
        "team_memberships.user_id:erase",
        int(
            await db.scalar(
                select(func.count())
                .select_from(TeamMembership)
                .where(
                    TeamMembership.org_team_id == org_id,
                    TeamMembership.user_id == ctx.user_id,
                )
            )
            or 0
        ),
    )
    await db.execute(
        delete(OrgMembership)
        .where(OrgMembership.id == membership.id)
        .execution_options(synchronize_session=False)
    )
    ctx.count("org_memberships.user_id:erase", 1)
    await emit(
        db,
        org_id=org_id,
        type=EventType.MEMBERSHIP_CHANGED,
        entity=Entity.MEMBERSHIP,
        entity_id=f"{org_id}:{ctx.user_id}",
        payload={"team_id": str(org_id), "user_id": str(ctx.user_id), "removed": True},
        actor=_actor(),
    )


async def close_org(ctx: ErasureContext, org_id: uuid.UUID) -> None:
    """Erase an org the person was alone in. Its content, connections and
    integration credentials go; pending seats are withdrawn; the root row is
    renamed and stays, holding the billing records."""
    db = ctx.db
    now = ctx.now
    object_ids = list(
        (await db.execute(select(WorkspaceObject.id).where(WorkspaceObject.org_team_id == org_id)))
        .scalars()
        .all()
    )
    await _delete_objects(ctx, org_id, object_ids)
    drive = await empty_drive(db, org_id, ctx.user_id)
    ctx.count("file_nodes:purged", drive.purged)
    ctx.count("file_nodes:held", drive.held)
    closing = OrgClosing(db=db, org_id=org_id, user_id=ctx.user_id)
    for contributor in contributors():
        if contributor.org_closing is not None:
            for key, n in (await contributor.org_closing(closing)).items():
                ctx.count(key, n)
    for model in (ProxyToken, CiToken, MachineCredential):
        await db.execute(
            update(model)
            .where(model.org_team_id == org_id, model.revoked_at.is_(None))
            .values(revoked_at=now)
        )
    await db.execute(
        delete(OrgMembership).where(
            OrgMembership.org_team_id == org_id,
            OrgMembership.status == MembershipStatus.PENDING,
        )
    )
    await db.execute(update(Team).where(Team.id == org_id).values(name=CLOSED_ORG_NAME))


async def _scrub_addresses(ctx: ErasureContext) -> None:
    """Personal data keyed by the address rather than the id."""
    db = ctx.db
    address = ctx.email.lower()
    tomb = tombstone_email(ctx.user_id)
    ctx.count(
        "invitations.email:scrub",
        _rowcount(
            await db.execute(
                update(Invitation).where(func.lower(Invitation.email) == address).values(email=tomb)
            )
        ),
    )
    ctx.count(
        "invitations.invited_by_id:revoke",
        _rowcount(
            await db.execute(
                update(Invitation)
                .where(
                    Invitation.invited_by_id == ctx.user_id,
                    Invitation.status == InvitationStatus.PENDING,
                )
                .values(status=InvitationStatus.REVOKED, resolved_at=ctx.now)
            )
        ),
    )
    ctx.count(
        "sso_link_requests.email:erase",
        _rowcount(
            await db.execute(
                delete(SsoLinkRequest).where(func.lower(SsoLinkRequest.email) == address)
            )
        ),
    )


async def _scrub_json(ctx: ErasureContext, table: str, column: str, where: str = "") -> int:
    """Rewrite the address inside a JSON column wherever it appears. The
    address sits in actor documents as a label (and in a few audit details as a
    target); a plain text replace reaches every place without knowing each
    document's shape."""
    clause = f"{column}::text LIKE :pattern"
    if where:
        clause = f"{clause} AND {where}"
    params: dict[str, Any] = {
        "address": ctx.email,
        "label": TOMBSTONE_LABEL,
        "pattern": f"%{_like_escape(ctx.email)}%",
    }
    if ":orgs" in where:
        params["orgs"] = [o.org_id for o in ctx.plan.orgs]
    result = await ctx.db.execute(
        text(
            f"UPDATE {table} SET {column} = replace({column}::text, :address, :label)::jsonb "  # noqa: S608 - table and column are this module's literals
            f"WHERE {clause}"
        ),
        params,
    )
    return _rowcount(result)


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _scrub(value: Any, address: str) -> Any:
    if isinstance(value, str):
        return value.replace(address, TOMBSTONE_LABEL)
    if isinstance(value, dict):
        return {k: _scrub(v, address) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, address) for v in value]
    return value


async def _scrub_audit(ctx: ErasureContext) -> list[ResealedChain]:
    """Rewrite the person's address in both audit trails and reseal every org
    chain the rewrite touched, then put the reseal itself on that chain."""
    db = ctx.db
    address = ctx.email
    resealed: list[ResealedChain] = []
    pattern = f"%{_like_escape(address)}%"
    touched = (
        await db.execute(
            select(OrgAuditEvent.org_team_id, func.min(OrgAuditEvent.created_at))
            .where(
                (OrgAuditEvent.actor_id == ctx.user_id)
                | (func.lower(OrgAuditEvent.actor_email) == address.lower())
                | (func.lower(OrgAuditEvent.target) == address.lower())
                | sql_cast(OrgAuditEvent.detail, String).like(pattern)
            )
            .group_by(OrgAuditEvent.org_team_id)
        )
    ).all()
    for org_id, first_at in touched:
        await audit_chain.lock_chain(db, org_id)
        rows = (
            (
                await db.execute(
                    select(OrgAuditEvent).where(
                        OrgAuditEvent.org_team_id == org_id,
                        OrgAuditEvent.created_at >= first_at,
                    )
                )
            )
            .scalars()
            .all()
        )
        rewritten = 0
        for row in rows:
            before = (row.actor_email, row.target, row.detail)
            if row.actor_id == ctx.user_id or row.actor_email.lower() == address.lower():
                row.actor_email = TOMBSTONE_LABEL
            if row.target is not None and row.target.lower() == address.lower():
                row.target = TOMBSTONE_LABEL
            if row.detail is not None:
                row.detail = _scrub(row.detail, address)
            if (row.actor_email, row.target, row.detail) != before:
                rewritten += 1
        await db.flush()
        if not rewritten:
            continue
        outcome = await audit_chain.reseal(db, org_id, since=first_at)
        marker = await audit_chain.append_row(
            db,
            org_id=org_id,
            actor_id=None,
            actor_email="",
            action="audit.chain_resealed",
            target=None,
            detail={
                "reason": "account_erasure",
                "rows_rewritten": rewritten,
                "rows_rehashed": outcome.rows,
                "old_head": outcome.old_head,
                "new_head": outcome.new_head,
            },
        )
        # The marker's hash is read now, so it is sealed now, under the lock
        # this transaction already holds.
        await audit_chain.seal_now(db, org_id)
        resealed.append(
            ResealedChain(
                org_id=org_id,
                rows_rewritten=rewritten,
                old_head=outcome.old_head,
                new_head=marker.entry_hash,
            )
        )
        ctx.count("org_audit_events.actor_id:scrub", rewritten)
    ctx.count(
        "audit_logs.actor_id:scrub",
        _rowcount(
            await db.execute(
                update(AuditLog)
                .where(AuditLog.actor_id == ctx.user_id)
                .values(actor_email=TOMBSTONE_LABEL)
            )
        ),
    )
    ctx.count("audit_logs.detail:scrub", await _scrub_json(ctx, "audit_logs", "detail"))
    # The outbox is append-only; erasure is its one sanctioned rewrite, opened
    # for this transaction alone and only for the actor and payload documents.
    await db.execute(text("SELECT set_config('alkera.outbox_erasure', 'on', true)"))
    ctx.count(
        "event_outbox.actor:scrub",
        await _scrub_json(ctx, "event_outbox", "actor", "org_id = ANY(:orgs)"),
    )
    ctx.count(
        "event_outbox.payload:scrub",
        await _scrub_json(ctx, "event_outbox", "payload", "org_id = ANY(:orgs)"),
    )
    await db.execute(text("SELECT set_config('alkera.outbox_erasure', 'off', true)"))
    ctx.count(
        "compute_allocation_events.actor:scrub",
        await _scrub_json(ctx, "compute_allocation_events", "actor"),
    )
    return resealed


async def _tombstone(ctx: ErasureContext) -> User:
    user = await ctx.db.get(User, ctx.user_id, with_for_update=True)
    if user is None:
        raise LookupError(f"no user {ctx.user_id}")
    user.email = tombstone_email(ctx.user_id)
    user.email_domain = "deleted.invalid"
    user.first_name = ""
    user.last_name = ""
    user.password_hash = None
    user.mfa_enabled = False
    user.mfa_secret_encrypted = None
    user.mfa_backup_codes = None
    user.mfa_last_used_counter = None
    user.scim_external_id = None
    user.email_verification_token = None
    user.email_verification_expires_at = None
    user.password_reset_token = None
    user.password_reset_expires_at = None
    user.signup_ip = None
    user.last_login_ip = None
    user.locked_until = None
    user.last_failed_login_at = None
    user.failed_login_count = 0
    user.is_active = False
    user.platform_role = None
    user.deleted_at = ctx.now
    user.token_epoch = ctx.now
    await ctx.db.flush()
    return user


# --------------------------------------------------------------------------- #
# The erasure
# --------------------------------------------------------------------------- #


async def _for_reerasure(db: AsyncSession, plan: DeletionPlan, user_id: uuid.UUID) -> DeletionPlan:
    """The plan a re-erasure acts on. The erasure was already decided and done,
    so nothing blocks it: an org the restored rows show the person as its last
    admin of is left, its shared items going to whoever else is active there."""
    orgs = []
    for planned in plan.orgs:
        if planned.fate == "blocked":
            recipient = await transfer_recipient(db, planned.org_id, user_id)
            planned = planned.model_copy(update={"fate": "leave", "transfer_to_user_id": recipient})
        orgs.append(planned)
    return plan.model_copy(update={"orgs": orgs, "blockers": []})


async def _stored_objects(db: AsyncSession, user_id: uuid.UUID) -> tuple[str, ...]:
    """Every installed contributor's objects in the account archive store, read
    before the dispositions erase the rows that name them."""
    keys: list[str] = []
    for contributor in contributors():
        if contributor.stored_objects is not None:
            keys.extend(key for key in await contributor.stored_objects(db, user_id) if key)
    return tuple(keys)


async def _table_exists(db: AsyncSession, table: str) -> bool:
    return await db.scalar(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": table}) is True


async def erase(
    db: AsyncSession,
    request: AccountDeletionRequest,
    *,
    now: datetime | None = None,
    reerasure: bool = False,
) -> ErasureResult:
    """Erase the account ``request`` names, in the caller's transaction.

    Raises :class:`ErasureBlocked` (writing nothing) when the recomputed plan
    has a blocker, unless this is a ``reerasure`` of an identity the erasure
    ledger says was already erased (a restore brought it back). Every installed
    contributor's ``external_erasure`` runs last, inside the transaction; one
    that raises rolls the whole erasure back. The caller writes the ledger
    entry, commits, then deletes ``archive_keys`` from the store and (for a
    first erasure) sends the completion email to ``email``."""
    at = now or datetime.now(UTC)
    plan = await compute_plan(db, request.user_id, now=at)
    if reerasure:
        plan = await _for_reerasure(db, plan, request.user_id)
    elif plan.blockers:
        raise ErasureBlocked(plan)
    async with cross_tenant_write(db, reason="account.erase"):
        user = await db.get(User, request.user_id, with_for_update=True)
        if user is None or user.deleted_at is not None:
            raise LookupError(f"user {request.user_id} is gone or already erased")
        email, first_name = user.email, user.first_name
        ctx = ErasureContext(db=db, user_id=user.id, email=email, plan=plan, now=at)
        await revoke_credentials(db, user.id)
        chats = await chat_end.live_chats_owned_by(db, user.id)
        await chat_end.end_chats(db, chats, chat_end.ChatEndReason.ACCESS_REMOVED, actor=_actor())

        memberships = {
            m.org_team_id: m
            for m in (
                await db.execute(select(OrgMembership).where(OrgMembership.user_id == user.id))
            )
            .scalars()
            .all()
        }
        cert = ErasureCertificate(erased_at=at)
        for planned in plan.orgs:
            if planned.fate == "leave":
                if planned.transfer_to_user_id is not None:
                    await _content_in_leaving_org(ctx, planned.org_id, planned.transfer_to_user_id)
                else:
                    await _delete_owned(ctx, planned.org_id)
                cert.orgs_left.append(planned.org_id)
            elif planned.fate == "close":
                await _delete_owned(ctx, planned.org_id)
                await close_org(ctx, planned.org_id)
                cert.orgs_closed.append(planned.org_id)
        # Content the person owns in an org they are no longer a standing member
        # of (left earlier, pending seat) follows the same rule: private goes.
        for org_id in {
            o
            for (o,) in (
                await db.execute(
                    select(WorkspaceObject.org_team_id)
                    .where(WorkspaceObject.owner_user_id == user.id)
                    .distinct()
                )
            ).all()
        }:
            await _delete_owned(ctx, org_id)
        for contributor in contributors():
            if contributor.remaining_content is not None:
                await contributor.remaining_content(db, user.id)
        for membership in memberships.values():
            await end_membership(ctx, membership)
        archive_keys = await _stored_objects(db, user.id)
        for disposition in dispositions.stepped():
            assert disposition.step is not None
            # A table a deployment does not have (a domain it never installed)
            # holds nobody to erase.
            if await _table_exists(db, disposition.table):
                ctx.count(disposition.key, await disposition.step(ctx))
        await _scrub_addresses(ctx)
        cert.resealed = await _scrub_audit(ctx)
        await _tombstone(ctx)
        job = ExternalErasure(db=db, user_id=user.id, closed_org_ids=tuple(cert.orgs_closed))
        for contributor in contributors():
            if contributor.external_erasure is not None:
                for key, n in (await contributor.external_erasure(job)).items():
                    ctx.count(key, n)
        cert.rows = dict(sorted(ctx.rows.items()))
        cert.archives_deleted = len(archive_keys)
        request.status = "completed"
        request.completed_at = at
        request.blocked_reason = None
        request.plan = plan.model_dump(mode="json")
        request.certificate = cert.model_dump(mode="json")
        await db.flush()
    log.info(
        "account.erased",
        user_id=str(request.user_id),
        orgs_left=len(cert.orgs_left),
        orgs_closed=len(cert.orgs_closed),
    )
    return ErasureResult(
        request_id=request.id,
        certificate=cert,
        email=email,
        first_name=first_name,
        archive_keys=archive_keys,
    )


__all__ = [
    "ACTOR_LABEL",
    "CLOSED_ORG_NAME",
    "ErasureBlocked",
    "ErasureContext",
    "ErasureResult",
    "end_membership",
    "erase",
    "revoke_credentials",
    "wind_down",
]
