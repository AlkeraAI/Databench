"""Every column that names a person, and what erasure does to it.

One registry, keyed ``(table, column)``. Each entry says what happens to the
rows that name the erased person and why:

* ``erase``: the rows are personal (a credential, a preference, a link to an
  outside account) and are deleted;
* ``scrub``: the row is kept but the personal fields in it are rewritten
  (audit actor addresses, actor documents);
* ``transfer``: owned content in an org that keeps going moves to another
  member; private content is deleted;
* ``detach``: the reference is set to null and the row kept;
* ``keep``: the row is kept as it is. The id it holds now names the tombstone
  identity ("Deleted user"), which carries nothing personal. ``why`` names the
  retention basis (a legal obligation for money, the security and legal-claims
  basis for audit, or plain authorship of org content).

An entry either carries a ``step`` (the statement that applies it) or names the
erasure ``stage`` that applies it (``credentials``, ``content``,
``memberships``, ``audit``), because some dispositions are not one statement.

The registry is the contract a test holds the schema to: every foreign key to
``users.id``, and every column whose name says it holds a person, must have an
entry, or the suite fails. A table added later cannot leak a person by being
forgotten; its author has to say what erasure does to it.

A private domain's tables register their entries where the domain defines its
models, through :func:`register` and :func:`register_not_a_person`, so the
registry describes exactly the tables this process has loaded.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import CursorResult, text

if TYPE_CHECKING:
    from alkera_core.account.erasure import ErasureContext


class Kind(StrEnum):
    ERASE = "erase"
    SCRUB = "scrub"
    TRANSFER = "transfer"
    DETACH = "detach"
    KEEP = "keep"


Step = Callable[["ErasureContext"], Awaitable[int]]


@dataclass(frozen=True, slots=True)
class Disposition:
    table: str
    column: str
    kind: Kind
    why: str
    #: The statement that applies it; ``None`` for ``keep`` or a staged entry.
    step: Step | None = field(default=None, compare=False)
    #: The erasure stage that applies it when one statement cannot.
    stage: str | None = None
    #: Order among the stepped entries: lower runs first.
    order: int = 50

    @property
    def key(self) -> str:
        return f"{self.table}.{self.column}:{self.kind.value}"


_REGISTRY: dict[tuple[str, str], Disposition] = {}

#: Columns whose name reads like a person but which hold something else. The
#: coverage test refuses a person-shaped column that is in neither this set
#: nor the registry.
_NOT_A_PERSON: set[tuple[str, str]] = {
    ("auth_refresh_tokens", "user_agent"),
    ("identity_security_events", "user_agent"),
    ("compute_allocations", "ssh_user"),
    # The login on an org's own SSH machine, not an Alkera account.
    ("ssh_machine_endpoints", "username"),
    ("compute_grants", "per_user_max"),
    ("device_authorizations", "user_code"),
    # A socket id, not a person.
    ("crdt_peers", "held_by"),
    # A free-text "what displaced it" note on an edit conflict.
    ("file_conflicts", "displaced_by"),
}


def register(disposition: Disposition) -> Disposition:
    key = (disposition.table, disposition.column)
    if key in _REGISTRY:
        raise ValueError(f"{disposition.table}.{disposition.column} already has a disposition")
    if disposition.kind is Kind.KEEP and disposition.step is not None:
        raise ValueError("a kept column has no step")
    if disposition.kind is not Kind.KEEP and disposition.step is None and not disposition.stage:
        raise ValueError(f"{key} needs a step or a stage")
    _REGISTRY[key] = disposition
    return disposition


def registry() -> dict[tuple[str, str], Disposition]:
    return dict(_REGISTRY)


def register_not_a_person(table: str, column: str) -> None:
    """Record that ``table.column`` reads like a person but holds something else."""
    if (table, column) in _REGISTRY:
        raise ValueError(f"{table}.{column} has a disposition; it cannot also be not a person")
    _NOT_A_PERSON.add((table, column))


def not_a_person() -> frozenset[tuple[str, str]]:
    """Every column recorded as person-shaped but holding something else."""
    return frozenset(_NOT_A_PERSON)


def stepped() -> Iterator[Disposition]:
    """The entries the engine applies one statement each, in order."""
    yield from sorted(
        (d for d in _REGISTRY.values() if d.step is not None),
        key=lambda d: (d.order, d.table, d.column),
    )


# --------------------------------------------------------------------------- #
# Statement builders
# --------------------------------------------------------------------------- #


def _count(result: object) -> int:
    return int(result.rowcount) if isinstance(result, CursorResult) else 0


def delete_rows(table: str, column: str) -> Step:
    async def step(ctx: ErasureContext) -> int:
        result = await ctx.db.execute(
            text(f"DELETE FROM {table} WHERE {column} = :uid"),  # noqa: S608
            {"uid": ctx.user_id},
        )
        return _count(result)

    return step


def null_column(table: str, column: str) -> Step:
    async def step(ctx: ErasureContext) -> int:
        result = await ctx.db.execute(
            text(f"UPDATE {table} SET {column} = NULL WHERE {column} = :uid"),  # noqa: S608
            {"uid": ctx.user_id},
        )
        return _count(result)

    return step


def relabel_actor(table: str, column: str, label_column: str) -> Step:
    """Keep the row and its id (which now names the tombstone) and rewrite the
    name it recorded for the person to the tombstone's label."""

    async def step(ctx: ErasureContext) -> int:
        result = await ctx.db.execute(
            text(f"UPDATE {table} SET {label_column} = :label WHERE {column} = :uid"),  # noqa: S608
            {"uid": ctx.user_id, "label": TOMBSTONE_LABEL},
        )
        return _count(result)

    return step


def _erase(table: str, column: str, why: str, *, order: int = 50) -> None:
    register(
        Disposition(table, column, Kind.ERASE, why, step=delete_rows(table, column), order=order)
    )


def _detach(table: str, column: str, why: str) -> None:
    register(Disposition(table, column, Kind.DETACH, why, step=null_column(table, column)))


def _keep(table: str, column: str, why: str) -> None:
    register(Disposition(table, column, Kind.KEEP, why))


def _staged(table: str, column: str, kind: Kind, stage: str, why: str) -> None:
    register(Disposition(table, column, kind, why, stage=stage))


# Reasons spelled once.
AUTHOR_REASON = "authorship of org content or configuration; the id now names the tombstone"
STAFF_REASON = "a platform or org admin's action on record; the id now names the tombstone"
MONEY_REASON = "financial record kept for tax and accounting obligations"
ABUSE_REASON = "anti-abuse record keyed to the identity; holds no contact data once scrubbed"
_FILES_AUTHOR = "who created or changed an org's file; the org keeps its files"

# ---- credentials and sign-in: erased (after the credentials stage revokes) --
_erase("auth_tokens", "user_id", "a session credential", order=10)
_erase("auth_refresh_tokens", "user_id", "a session credential", order=10)
_erase("personal_access_tokens", "user_id", "a credential", order=10)
_erase("device_authorizations", "user_id", "a device sign-in grant", order=10)
_erase("oauth_identities", "user_id", "a link to an outside sign-in account", order=10)

# ---- personal settings and state ------------------------------------------
_erase("user_preferences", "user_id", "personal settings")
_erase("user_org_preferences", "user_id", "personal settings per org")
_erase("chat_workspace_states", "user_id", "personal view state of a chat")
_erase("chat_read_marks", "user_id", "personal read state of a chat")
_erase("realtime_presence", "user_id", "short-lived presence")
_erase("crdt_peers", "user_id", "an editing peer binding")
_erase("crash_reports", "user_id", "a crash report with device context")
_erase("identity_security_events", "user_id", "the person's own security log")
_erase("org_machine_audiences", "user_id", "a grant to use an org machine")
_erase("user_storage_limits", "user_id", "a per-person quota")
_erase("file_stars", "user_id", "a personal bookmark")
_erase("file_shares", "principal_id", "a share to a person who no longer exists")
_erase("account_export_requests", "user_id", "the person's own export requests")

# ---- notebook activity: kept as authorship, the recorded name scrubbed ------
# A run and an edit tally say who ran or edited a cell of an org's notebook;
# the org keeps that history, under the tombstone's name.
for _table, _column in (("notebook_runs", "requested_by_user_id"), ("notebook_edits", "user_id")):
    register(
        Disposition(
            _table,
            _column,
            Kind.SCRUB,
            "who ran or edited a cell of an org's notebook; the name it recorded is rewritten",
            step=relabel_actor(_table, _column, "actor_display"),
        )
    )

# ---- owned content: transferred or deleted per org -------------------------
_staged("workspace_objects", "owner_user_id", Kind.TRANSFER, "content", "owned chats and objects")
_staged("realtime_docs", "owner_user_id", Kind.TRANSFER, "content", "follows its chat")

# ---- memberships: ended by the memberships stage ---------------------------
_staged("org_memberships", "user_id", Kind.ERASE, "memberships", "standing in an org")
_staged("team_memberships", "user_id", Kind.ERASE, "memberships", "a team seat")
_staged(
    "role_assignments",
    "principal_id",
    Kind.KEEP,
    "memberships",
    "revoked, kept as authorization history; the id now names the tombstone",
)

# ---- audit: scrubbed by the audit stage ------------------------------------
_staged(
    "org_audit_events",
    "actor_id",
    Kind.SCRUB,
    "audit",
    "kept for security and legal claims (Art. 17(3)(e)); address rewritten, chain resealed",
)
_staged(
    "audit_logs",
    "actor_id",
    Kind.SCRUB,
    "audit",
    "platform staff audit kept; address rewritten",
)
_staged(
    "event_outbox",
    "actor",
    Kind.SCRUB,
    "audit",
    "actor documents carry the address as a label; rewritten",
)
_staged(
    "compute_allocation_events",
    "actor",
    Kind.SCRUB,
    "audit",
    "actor documents carry the address as a label; rewritten",
)

# ---- detached ---------------------------------------------------------------
_detach("crdt_updates", "author_user_id", "edits stay with the shared document")
_detach("crash_reports", "read_by_user_id", "staff triage marker")

# ---- kept: money ------------------------------------------------------------
_keep("compute_allocations", "user_id", MONEY_REASON)
_keep("identity_org_creations", "user_id", ABUSE_REASON)
_keep("user_bans", "user_id", ABUSE_REASON)

# ---- kept: the lifecycle records themselves ---------------------------------
_keep("account_deletion_requests", "user_id", "the record that the erasure happened")
_keep("account_deletion_requests", "requested_by_id", STAFF_REASON)
_keep("account_export_requests", "requested_by_id", STAFF_REASON)
# The claim is the org's; the staff member who assigned it is only on record.
_keep("sso_domain_claims", "assigned_by_id", STAFF_REASON)

# ---- kept: an actor on someone else's row -----------------------------------
for _table, _column in (
    ("ci_tokens", "created_by_id"),
    ("compute_grants", "created_by"),
    ("compute_offerings", "created_by"),
    ("email_domain_bans", "created_by_id"),
    ("email_domain_bans", "lifted_by_id"),
    ("entitlement_grants", "issued_by_id"),
    ("invitations", "invited_by_id"),
    ("machine_credentials", "created_by"),
    ("model_provider_configs", "updated_by_id"),
    ("org_compute_assignments", "assigned_by"),
    ("org_machine_audiences", "created_by"),
    ("org_machines", "created_by"),
    ("org_storage_limits", "created_by_id"),
    ("proxy_tokens", "created_by_id"),
    ("role_assignments", "granted_by_id"),
    ("team_allocations", "created_by_id"),
    ("user_bans", "created_by_id"),
    ("user_bans", "lifted_by_id"),
    ("user_storage_limits", "created_by_id"),
    ("workspace_machine_moves", "requested_by"),
):
    _keep(_table, _column, STAFF_REASON if "ban" in _table or "grant" in _table else AUTHOR_REASON)

# ---- kept: files metadata (no foreign keys; uuid ids) -----------------------
for _table, _column in (
    ("file_nodes", "created_by"),
    ("file_versions", "created_by"),
    ("file_upload_sessions", "created_by"),
    ("file_upload_sessions", "lease_holder"),
    ("file_history", "acting_principal"),
    ("file_history", "delegating_user"),
    ("file_conflicts", "actor"),
    ("file_conflicts", "resolved_by"),
    ("file_trash_ops", "actor_id"),
    ("file_ops", "actor"),
    ("file_idempotency_keys", "principal_id"),
    ("file_locks", "holder_principal"),
    ("file_leases", "holder_principal_id"),
    ("file_page_grants", "minted_by_user_id"),
    ("file_shares", "granted_by"),
    ("file_holds", "placed_by"),
):
    _keep(_table, _column, _FILES_AUTHOR)
_keep(
    "file_acl_members",
    "principal_id",
    "a derived ACL cache naming an id no credential can carry again; the ACL rewrite owns it",
)


#: Pseudo-columns the engine writes that no foreign key reaches: the identity
#: row itself, and personal data keyed by address rather than id.
TOMBSTONE_FIELDS: tuple[str, ...] = (
    "email",
    "email_domain",
    "first_name",
    "last_name",
    "password_hash",
    "mfa_secret_encrypted",
    "mfa_backup_codes",
    "mfa_last_used_counter",
    "scim_external_id",
    "email_verification_token",
    "email_verification_expires_at",
    "password_reset_token",
    "password_reset_expires_at",
    "signup_ip",
    "last_login_ip",
    "locked_until",
    "last_failed_login_at",
)


def tombstone_email(user_id: uuid.UUID) -> str:
    """The address an erased identity is left with: unique (the column is), not
    deliverable (``.invalid`` is reserved by RFC 2606), and naming nobody."""
    return f"deleted-{user_id.hex}@deleted.invalid"


TOMBSTONE_LABEL = "Deleted user"


__all__ = [
    "ABUSE_REASON",
    "AUTHOR_REASON",
    "MONEY_REASON",
    "STAFF_REASON",
    "TOMBSTONE_FIELDS",
    "TOMBSTONE_LABEL",
    "Disposition",
    "Kind",
    "Step",
    "delete_rows",
    "not_a_person",
    "null_column",
    "register",
    "register_not_a_person",
    "registry",
    "stepped",
    "tombstone_email",
]
