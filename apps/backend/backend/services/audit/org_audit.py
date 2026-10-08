"""Per-org audit trail: record an event (within the action's transaction) + read
a page for the org admin.

``record`` adds the row to the CALLER's session, so the audit entry commits
atomically with the action it describes — no lost or orphaned audit rows. Keep
``detail`` small + JSON-serializable; secrets are scrubbed before write.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from alkera_core import audit_chain
from alkera_core.authz import ActingContext, is_server_only_event_type
from alkera_core.extensions import ExtensionPoint
from alkera_core.models import MembershipStatus, OrgAuditEvent, OrgMembership, User, WorkspaceObject
from alkera_core.models.audit_log import AuditLog
from alkera_core.schemas.system.org_audit import AgentAuditEventIn
from sqlalchemy import (
    ColumnElement,
    Select,
    String,
    and_,
    func,
    literal,
    or_,
    select,
    tuple_,
)
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.org import teams as team_service

# Case-insensitive field names whose values are redacted in `detail`.
_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "secret",
        "client_secret",
        "oauth_client_secret",
        "shared_secret",
        "oidc_client_secret",
        "token",
        "secret_token",
        "api_key",
        "access_token",
        "refresh_token",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
        "bearer_token",
    }
)
_REDACTED = "***"


# Trace-content field names (OWASP logging exclusions); never audit data.
_CONTENT_KEYS = frozenset(
    {
        "output",
        "stdout",
        "stderr",
        "command_output",
        "content",
        "file_content",
        "file_bytes",
        "diff",
        "sql",
        "sql_text",
        "statement",
        "statement_text",
        "query",
        "query_text",
        "rows",
        "result",
        "results",
        "result_rows",
        "prompt",
        "prompts",
        "response",
        "responses",
        "messages",
        "raw",
    }
)

# An agent target is a resource identifier; longer, or holding a newline or
# tab, it is content masquerading as one.
_AGENT_TARGET_MAX = 200

#: The only action namespace a client (a member's daemon) may report.
CLIENT_ACTION_PREFIX = "agent."


def client_reportable(action: str) -> bool:
    """Whether a client may report ``action`` into the org chain.

    Only the ``agent.`` vocabulary is client-writable; every other action in
    the audit vocabulary is server-emitted, and the authorization decision
    types are server-only by definition — a batch that names one is forged,
    whatever else it says. The wire schema already pins the agent vocabulary
    as a closed set; this is the guard that survives a future widening of it.
    """
    return action.startswith(CLIENT_ACTION_PREFIX) and not is_server_only_event_type(action)


def _redact_keys(value: Any, keys: frozenset[str]) -> Any:
    """Replace the value of any dict key in ``keys`` (case-insensitive) with the
    redaction marker, recursing through nested dicts and lists."""
    if isinstance(value, dict):
        return {
            key: (_REDACTED if key.lower() in keys else _redact_keys(val, keys))
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [_redact_keys(item, keys) for item in value]
    return value


def scrub_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """``detail`` with every secret-shaped key's value redacted, the same
    scrub the org chain applies to a server-written row."""
    return cast(dict[str, Any], _redact_keys(detail, _SENSITIVE_KEYS))


def _scrub_target(target: str | None) -> str | None:
    if target is None:
        return None
    if any(ch in target for ch in "\n\r\t"):
        return None
    return target[:_AGENT_TARGET_MAX] or None


# --------------------------------------------------------------------------- #
# Tamper-evident hash chain
# --------------------------------------------------------------------------- #


# The chain itself (hashing, the per-org lock, the tail, append and reseal) lives
# in ``alkera_core.audit_chain`` so the worker's account erasure writes the same
# chain the request path does.
_hash_event = audit_chain.hash_event


@dataclass(frozen=True, slots=True)
class ChainVerification:
    ok: bool
    checked: int
    broken_event_id: UUID | None
    broken_at: datetime | None
    head_hash: str | None  # the latest entry_hash — export this as an external checkpoint


def changes(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The fields whose value an update actually moved, as ``{field: {from, to}}``.

    For a settings write the audit row names what changed and from what, not
    the whole body the client sent: a save that re-sends every toggle but flips
    one must read as that one change, and a save that changes nothing records
    nothing. Only fields present in ``after`` are compared.
    """
    return {
        field: {"from": before.get(field), "to": value}
        for field, value in after.items()
        if before.get(field) != value
    }


async def record(
    db: AsyncSession,
    *,
    org_id: UUID,
    actor: User | None,
    action: str,
    target: str | None = None,
    detail: dict[str, Any] | None = None,
    acting: ActingContext | None = None,
) -> OrgAuditEvent:
    """Append one event to the org's chain, in the caller's transaction.

    ``acting`` is the resolved acting context when the route has one: its
    chain document lands in ``detail.actor`` so an agent or a personal access
    token acting for the user is distinguishable from the user themselves.
    ``actor`` stays the attributed user (the chain's subject) for the
    ``actor_id`` / ``actor_email`` columns the list and filters read.
    """
    actor_id = actor.id if actor is not None else None
    actor_email = actor.email if actor is not None else ""
    if acting is not None:
        detail = {**(detail or {}), "actor": acting.audit_dict()}
    # agent.* details are client-supplied, so trace-content keys redact too.
    keys = _SENSITIVE_KEYS | _CONTENT_KEYS if action.startswith("agent.") else _SENSITIVE_KEYS
    redacted = _redact_keys(detail, keys) if detail is not None else None
    return await audit_chain.append_row(
        db,
        org_id=org_id,
        actor_id=actor_id,
        actor_email=actor_email,
        action=action,
        target=target,
        detail=redacted,
    )


async def record_offboarding(
    db: AsyncSession, *, obj: WorkspaceObject, by: User, acting: ActingContext
) -> OrgAuditEvent:
    """Put an org admin's deletion of a departed member's private chat or
    workspace on the org's chain, under ``<type>.offboarded``: the one
    deletion of something the admin could not read, so the tenant can see who
    removed what and whose it was."""
    return await record(
        db,
        org_id=obj.org_team_id,
        actor=by,
        action=f"{obj.type}.offboarded",
        target=str(obj.id),
        detail={"owner_user_id": str(obj.owner_user_id)},
        acting=acting,
    )


async def record_agent_batch(
    db: AsyncSession,
    *,
    org_id: UUID,
    actor: User,
    events: Sequence[AgentAuditEventIn],
) -> int:
    """Append a daemon-reported batch of agent events to the org chain.

    The actor is the authenticated caller; the payload cannot change the
    attribution. ``record`` redacts content keys for agent actions, and the
    free-form ``target`` is scrubbed here. ``session_id`` and the client clock
    land in ``detail``; chain order stays server receipt time. Delivery is
    at-least-once, and ``detail.event_id`` makes a duplicate visible.

    Raises ``ValueError`` when any event names an action a client may not
    report (see :func:`client_reportable`); nothing from the batch is written.
    """
    forged = [event.action for event in events if not client_reportable(event.action)]
    if forged:
        raise ValueError(
            f"client-reported audit actions must be {CLIENT_ACTION_PREFIX}*; refused {forged[0]!r}"
        )
    for event in events:
        detail = dict(event.detail)
        detail["session_id"] = event.session_id
        detail["occurred_at"] = event.occurred_at.isoformat()
        await record(
            db,
            org_id=org_id,
            actor=actor,
            action=event.action,
            target=_scrub_target(event.target),
            detail=detail,
        )
    return len(events)


async def verify_chain(db: AsyncSession, *, org_id: UUID, cap: int = 100_000) -> ChainVerification:
    """Walk the org's chained events oldest-first, recomputing each hash and its
    link to the previous one. Returns the first row where the chain breaks (an
    edit, deletion, re-order, or insertion) or ``ok=True`` if intact. ``head_hash``
    is the latest entry_hash — anchor it externally to also catch a full rewrite.

    Rows from before the chain existed carry no hash and are skipped. Once the
    chain has begun, every committed row is sealed as it commits, so a row
    with no hash after the first sealed one is a break: a row written past the
    chain, or one whose hashes were wiped."""
    rows = (
        (
            await db.execute(
                select(OrgAuditEvent)
                .where(OrgAuditEvent.org_team_id == org_id)
                .order_by(OrgAuditEvent.created_at.asc())
                .limit(cap)
            )
        )
        .scalars()
        .all()
    )
    prev = ""
    checked = 0
    begun = False
    for row in rows:
        if row.entry_hash is None:
            if not begun:
                continue
            return ChainVerification(
                ok=False,
                checked=checked,
                broken_event_id=row.id,
                broken_at=row.created_at,
                head_hash=prev or None,
            )
        begun = True
        expected = _hash_event(
            org_id=row.org_team_id,
            actor_id=row.actor_id,
            actor_email=row.actor_email,
            action=row.action,
            target=row.target,
            detail=row.detail,
            created_at=row.created_at,
            prev_hash=prev,
        )
        if row.prev_hash != prev or row.entry_hash != expected:
            return ChainVerification(
                ok=False,
                checked=checked,
                broken_event_id=row.id,
                broken_at=row.created_at,
                head_hash=prev or None,
            )
        prev = row.entry_hash or ""
        checked += 1
    return ChainVerification(
        ok=True, checked=checked, broken_event_id=None, broken_at=None, head_hash=prev or None
    )


# --------------------------------------------------------------------------- #
# The platform's actions on this org
# --------------------------------------------------------------------------- #
#
# What Alkera's staff do to a tenant from the admin console — grant it credit,
# cap a member, change its plan or settings, give it a machine — is recorded in
# the platform's own ``audit_logs`` by the admin router, never in the org's
# chain. The org's Activity feed used to read the chain alone, so the actions
# most worth showing a customer were the ones it never showed. The page merges
# the platform rows that concern the org, read from ``audit_logs`` at read
# time and never written into the chain: the chain stays exactly what the org's
# own actions produced, and its hashes verify as before.
#
# A platform row names no org. Which org it concerns is derived from what the
# admin route recorded about the request — the org id in its path or body, or
# the team, member, pool or schedule it addressed, resolved back to the org —
# through the scopes registered below. A new admin route family that acts on an
# org is admitted by registering how its rows name the org; a row no scope
# claims is on no org's feed.

#: The prefix a platform action carries on an org's feed. It is the vocabulary
#: the chain already uses for what Alkera did to a tenant
#: (``platform.user_banned``), so a reader filtering on ``platform.`` gets every
#: such action, whether the route wrote it into the chain or the page merged it.
PLATFORM_ACTION_PREFIX = "platform."

#: Admin routes that already write their org-facing effect into the org's own
#: chain themselves. Their platform rows are left out of the merge, or one
#: action would show twice.
ROUTES_ALREADY_ON_THE_CHAIN: frozenset[str] = frozenset(
    {
        "set_org_storage",
        "clear_org_storage",
        "connect_org_slack",
        "disconnect_org_slack",
        "ban_user",
        "lift_user_ban",
        "reset_user_usage",
    }
)

GRANT_ROUTES: frozenset[str] = frozenset({"grant_credits", "create_recurring_grant"})
MEMBER_ROUTES: frozenset[str] = frozenset({"update_user", "set_platform_role"})


@dataclass(frozen=True, slots=True)
class OrgScopeFacts:
    """The org a page is about, resolved once: its id and every team in its
    tree, so a scope that addresses a team never walks the tree per row."""

    org_id: UUID
    team_ids: Sequence[UUID]

    @property
    def org_text(self) -> str:
        return str(self.org_id)

    @property
    def team_texts(self) -> list[str]:
        return [str(team_id) for team_id in self.team_ids]


#: How one family of admin routes names the org in its recorded request.
PlatformScope = Callable[[OrgScopeFacts], ColumnElement[bool]]


def path_param(key: str) -> ColumnElement[str]:
    # JSONB subscripting is untyped upstream; the ``astext`` accessor is text.
    return cast(ColumnElement[str], AuditLog.detail["path_params"][key].astext)


def _body(key: str) -> ColumnElement[str]:
    return cast(ColumnElement[str], AuditLog.detail["body"][key].astext)


def _user_texts_here(facts: OrgScopeFacts) -> Select[tuple[str]]:
    """The ids, as text, of the people who are active members of the org: whose
    platform-staff changes the org's admins see. A person's membership elsewhere
    does not bring another org's view of them here."""
    return select(sql_cast(OrgMembership.user_id, String)).where(
        OrgMembership.org_team_id == facts.org_id,
        OrgMembership.status == MembershipStatus.ACTIVE,
    )


def _org_in_path(facts: OrgScopeFacts) -> ColumnElement[bool]:
    """A route addressed to the org itself: settings, plan, compute, tokens."""
    return path_param("org_id") == facts.org_text


def _org_in_body(facts: OrgScopeFacts) -> ColumnElement[bool]:
    """A route told which org in its body: an enrollment, a dedicated machine."""
    return _body("org_id") == facts.org_text


def _grant_here(facts: OrgScopeFacts) -> ColumnElement[bool]:
    """Credit issued to the org's pool, to one of its teams, or to a member."""
    scope, target = _body("scope"), _body("target_id")
    return and_(
        AuditLog.action.in_(GRANT_ROUTES),
        or_(
            and_(scope == "org", target == facts.org_text),
            and_(scope == "team", target.in_(facts.team_texts)),
            and_(scope == "user", target.in_(_user_texts_here(facts))),
        ),
    )


def _member_here(facts: OrgScopeFacts) -> ColumnElement[bool]:
    """An edit to one of the org's members, addressed by the user."""
    return and_(
        AuditLog.action.in_(MEMBER_ROUTES), path_param("user_id").in_(_user_texts_here(facts))
    )


#: The ways a platform row names an org that the open admin routes write.
OPEN_PLATFORM_SCOPES: tuple[PlatformScope, ...] = (
    _org_in_path,
    _org_in_body,
    _grant_here,
    _member_here,
)

#: Further ways, registered by the extension that owns the admin routes whose
#: rows they place (billing's pool caps and standing grants). Registration is
#: what admits a new admin route family to the feed.
PLATFORM_SCOPES: ExtensionPoint[PlatformScope] = ExtensionPoint("org_audit_platform_scopes")


def platform_rows_about(facts: OrgScopeFacts) -> ColumnElement[bool]:
    """The platform audit rows that concern this org: any registered scope
    claims them, and the route did not already write them into the chain."""
    return and_(
        AuditLog.action.not_in(ROUTES_ALREADY_ON_THE_CHAIN),
        or_(*(scope(facts) for scope in (*OPEN_PLATFORM_SCOPES, *PLATFORM_SCOPES.items()))),
    )


def _platform_event(log: AuditLog, org_id: UUID) -> OrgAuditEvent:
    """A platform audit row as the org sees it: the shape of its own events,
    named in the chain's ``platform.`` vocabulary, carrying who on Alkera's
    side acted and the request as the admin route recorded it (already
    redacted there). Transient — never added to a session, never hashed."""
    detail: dict[str, Any] = {
        "platform": {
            "actor_role": log.actor_platform_role,
            "method": log.method,
            "path": log.path,
            "status_code": log.status_code,
        },
        **(log.detail or {}),
    }
    return OrgAuditEvent(
        id=log.id,
        org_team_id=org_id,
        actor_id=log.actor_id,
        actor_email=log.actor_email,
        action=f"{PLATFORM_ACTION_PREFIX}{log.action}",
        target=log.target or log.path,
        detail=detail,
        created_at=log.created_at,
        prev_hash=None,
        entry_hash=None,
    )


@dataclass(frozen=True, slots=True)
class AuditFilters:
    """Optional narrowing shared by the list page and the CSV export.
    ``action`` matches as a prefix (``agent.`` selects every agent row);
    the time bounds are inclusive."""

    action: str | None = None
    actor_email: str | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None

    def clauses(self, org_id: UUID) -> list[ColumnElement[bool]]:
        out: list[ColumnElement[bool]] = [OrgAuditEvent.org_team_id == org_id]
        if self.action:
            out.append(OrgAuditEvent.action.startswith(self.action, autoescape=True))
        if self.actor_email:
            # Stored emails are normalized (``user_service`` lowercases on write),
            # so match the needle the same way — a display-cased address still finds
            # its actor instead of silently matching nothing.
            out.append(OrgAuditEvent.actor_email == self.actor_email.strip().lower())
        if self.created_after is not None:
            out.append(OrgAuditEvent.created_at >= self.created_after)
        if self.created_before is not None:
            out.append(OrgAuditEvent.created_at <= self.created_before)
        return out

    def platform_clauses(self, facts: OrgScopeFacts) -> list[ColumnElement[bool]]:
        """The same narrowing over the platform rows that concern the org. The
        action prefix is matched against the name the feed shows, so
        ``platform.`` selects exactly what a reader sees under it."""
        out: list[ColumnElement[bool]] = [platform_rows_about(facts)]
        if self.action:
            shown = literal(PLATFORM_ACTION_PREFIX) + AuditLog.action
            out.append(shown.startswith(self.action, autoescape=True))
        if self.actor_email:
            out.append(AuditLog.actor_email == self.actor_email.strip().lower())
        if self.created_after is not None:
            out.append(AuditLog.created_at >= self.created_after)
        if self.created_before is not None:
            out.append(AuditLog.created_at <= self.created_before)
        return out


_OWN = "own"
_PLATFORM = "platform"


async def list_page(
    db: AsyncSession,
    *,
    org_id: UUID,
    offset: int,
    limit: int,
    filters: AuditFilters | None = None,
) -> tuple[Sequence[OrgAuditEvent], int]:
    """One newest-first page of the org's (optionally filtered) events plus the
    total count under the same filters — the org's own chain and the platform's
    actions on the org, merged in time order and paged as one listing.

    Platform rows are read, never written: they arrive as transient events in
    the chain's shape (see :func:`_platform_event`), so both consumers of the
    page render them as they render everything else and the chain's hashes
    are untouched.
    """
    narrowing = filters or AuditFilters()
    facts = OrgScopeFacts(
        org_id=org_id, team_ids=[org_id, *await team_service.descendant_ids(db, org_id)]
    )
    own = select(
        OrgAuditEvent.id.label("id"),
        OrgAuditEvent.created_at.label("created_at"),
        literal(_OWN).label("source"),
    ).where(*narrowing.clauses(org_id))
    platform = select(
        AuditLog.id.label("id"),
        AuditLog.created_at.label("created_at"),
        literal(_PLATFORM).label("source"),
    ).where(*narrowing.platform_clauses(facts))
    merged = own.union_all(platform).subquery("entries")
    total = (await db.execute(select(func.count()).select_from(merged))).scalar_one()
    page = (
        await db.execute(
            select(merged.c.id, merged.c.source)
            .order_by(merged.c.created_at.desc(), merged.c.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()

    own_ids = [row_id for row_id, source in page if source == _OWN]
    platform_ids = [row_id for row_id, source in page if source == _PLATFORM]
    own_rows: dict[UUID, OrgAuditEvent] = {}
    if own_ids:
        found = (
            await db.execute(select(OrgAuditEvent).where(OrgAuditEvent.id.in_(own_ids)))
        ).scalars()
        own_rows = {row.id: row for row in found}
    platform_rows: dict[UUID, AuditLog] = {}
    if platform_ids:
        logs = (await db.execute(select(AuditLog).where(AuditLog.id.in_(platform_ids)))).scalars()
        platform_rows = {log.id: log for log in logs}
    ordered = [
        own_rows[row_id] if source == _OWN else _platform_event(platform_rows[row_id], org_id)
        for row_id, source in page
    ]
    return ordered, int(total)


#: How many rows one export read fetches. Large enough that a long trail costs
#: few round trips, small enough that the batch in hand is never the thing that
#: decides how much memory an export takes.
EXPORT_BATCH = 1_000


async def iter_all(
    db: AsyncSession,
    *,
    org_id: UUID,
    filters: AuditFilters | None = None,
    batch: int = EXPORT_BATCH,
) -> AsyncIterator[OrgAuditEvent]:
    """Every event the filters match, newest first, a batch at a time.

    An audit trail is the one table that only grows, and an admin exporting it
    for a regulator wants all of it — so there is no cap here. The reads walk
    it on a seek rather than an offset: each batch asks for the rows strictly
    older than the last one handed out, which costs the same at the ten
    millionth row as at the first, and cannot skip or repeat a row when one is
    written while the export is running.
    """
    where = (filters or AuditFilters()).clauses(org_id)
    cursor: tuple[datetime, UUID] | None = None
    while True:
        statement = select(OrgAuditEvent).where(*where)
        if cursor is not None:
            statement = statement.where(
                tuple_(OrgAuditEvent.created_at, OrgAuditEvent.id)
                < tuple_(literal(cursor[0]), literal(cursor[1]))
            )
        rows = (
            (
                await db.execute(
                    statement.order_by(
                        OrgAuditEvent.created_at.desc(), OrgAuditEvent.id.desc()
                    ).limit(batch)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            yield row
        if len(rows) < batch:
            return
        cursor = (rows[-1].created_at, rows[-1].id)


#: The name the ``backend.services.audit`` package exports for :func:`record`.
record_org_audit = record
