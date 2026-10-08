"""The Preconfigured-Connections reconcile PLANNER — a pure function.

``plan_reconcile`` compares the backend's authoritative list against the local
:class:`TeamConnectionsState` and returns exactly what must change. All the
policy lives here, side-effect free, so every case is unit-testable with plain
data; the I/O applier just executes the plan.

The load-bearing distinction: ``server=None`` means "no authoritative answer"
(offline, auth failure) and plans NOTHING — an empty list ``[]`` is an
authoritative "you have no team connections" and removes everything. Network
failure must never masquerade as membership loss.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from alkera_cli.plugins.plugin_base.team_connections import (
    TeamConnectionRecord,
    TeamConnectionsState,
    TeamMemberState,
    member_field_specs,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True)
class CredentialFetch:
    """Fetch the shared secret for one record and write it to disk."""

    record_id: str
    plugin: str
    local_handle: str
    credential_version: int


@dataclass(frozen=True)
class CredentialCleanup:
    """Delete retired local roles while preserving roles still owned by the member."""

    record_id: str
    plugin: str
    local_handle: str
    keep_primary: bool = False
    keep_named: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Removal:
    """Drop one record: its state, and (when it materialized) its local
    credential directory."""

    record_id: str
    plugin: str
    local_handle: str


@dataclass(frozen=True)
class Rename:
    """Move a record's local materialization to a new handle (a member-own
    connection appeared under the handle the team copy was using — the
    member's own connection always wins the plain name)."""

    record_id: str
    plugin: str
    old_local_handle: str
    new_local_handle: str


@dataclass(frozen=True)
class ReconcilePlan:
    """Everything one reconcile pass must do. ``authoritative=False`` plans
    nothing and the applier must not mutate any local state.

    ``assignments`` maps EVERY server record id to the local handle it must
    carry after this pass — the applier stamps it verbatim instead of
    re-deriving collision policy."""

    authoritative: bool
    upserts: list[TeamConnectionRecord] = field(default_factory=list)
    removals: list[Removal] = field(default_factory=list)
    renames: list[Rename] = field(default_factory=list)
    auto_adds: list[str] = field(default_factory=list)
    credential_fetches: list[CredentialFetch] = field(default_factory=list)
    credential_cleanups: list[CredentialCleanup] = field(default_factory=list)
    assignments: dict[str, str] = field(default_factory=dict)

    def is_noop(self) -> bool:
        return not (
            self.upserts
            or self.removals
            or self.renames
            or self.auto_adds
            or self.credential_fetches
            or self.credential_cleanups
        )


def _desired_local_handle(
    record: TeamConnectionRecord,
    current: str,
    taken: set[tuple[str, str]],
) -> str:
    """The handle this record should materialize under. A handle already
    granted stays stable unless it now collides; collisions walk a
    deterministic suffix chain (``wh`` → ``wh-team`` → ``wh-team2`` …)."""
    if current and (record.plugin, current) not in taken:
        return current
    if (record.plugin, record.handle) not in taken:
        return record.handle
    candidate = f"{record.handle}-team"
    n = 1
    while (record.plugin, candidate) in taken:
        n += 1
        candidate = f"{record.handle}-team{n}"
    return candidate


def _planned_removals(
    state: TeamConnectionsState, server_by_id: dict[str, TeamConnectionRecord]
) -> list[Removal]:
    """Every locally-known id the server no longer lists — an admin delete and
    a membership loss are indistinguishable here, and both mean the same thing:
    this workspace no longer gets the connection."""
    removals = []
    for record_id in state.records:
        if record_id not in server_by_id:
            member = state.member.get(record_id, TeamMemberState())
            old = state.records[record_id]
            removals.append(
                Removal(
                    record_id=record_id,
                    plugin=old.plugin,
                    local_handle=member.local_handle or old.handle,
                )
            )
    return removals


def _existing_claims(
    state: TeamConnectionsState, server_by_id: dict[str, TeamConnectionRecord]
) -> dict[str, tuple[str, str]]:
    """The ``(plugin, local_handle)`` each surviving team record already holds."""
    claims: dict[str, tuple[str, str]] = {}
    for record_id, member in state.member.items():
        if record_id in server_by_id and member.local_handle:
            claims[record_id] = (server_by_id[record_id].plugin, member.local_handle)
    return claims


def _planned_upsert(
    record: TeamConnectionRecord, known: TeamConnectionRecord | None
) -> TeamConnectionRecord | None:
    """A record new to this workspace, or different from its local copy, is
    re-persisted whole."""
    if known is None or known.model_dump(mode="json") != record.model_dump(mode="json"):
        return record
    return None


def _assigned_handle(
    record: TeamConnectionRecord,
    member: TeamMemberState,
    local_pairs: set[tuple[str, str]],
    team_claims: dict[str, tuple[str, str]],
) -> tuple[str, Rename | None]:
    """Each record's collision domain is the member's OWN pairs (never
    subtracted — a team copy can share its current name with a member-own
    connection, and the member always wins) plus every OTHER team record's
    claim. A changed grant plans the rename that moves the materialization."""
    current = member.local_handle
    taken = local_pairs | {pair for rid, pair in team_claims.items() if rid != record.id}
    desired = _desired_local_handle(record, current, taken)
    rename = None
    if current and desired != current:
        rename = Rename(
            record_id=record.id,
            plugin=record.plugin,
            old_local_handle=current,
            new_local_handle=desired,
        )
    return desired, rename


def _auto_adds_now(record: TeamConnectionRecord, member: TeamMemberState) -> bool:
    return record.enabled and record.auto_add and not member.added and not member.dismissed


def _member_roles_to_keep(
    record: TeamConnectionRecord, member: TeamMemberState
) -> tuple[bool, frozenset[str]]:
    """Return local primary/named roles that remain member-owned and enabled."""
    if not record.enabled or not member.has_member_secret:
        return False, frozenset()
    specs = member_field_specs(record)
    primary = any(spec.secret and not spec.credential_role for spec in specs)
    named = frozenset(
        spec.credential_role for spec in specs if spec.secret and spec.credential_role
    )
    return primary, named


def _shared_copy_requires_cleanup(record: TeamConnectionRecord, member: TeamMemberState) -> bool:
    """Return whether a previously materialized shared bundle must leave disk."""
    return member.fetched_credential_version >= 0 and (
        record.shared_custody == "lease"
        or not record.enabled
        or record.auth_mode == "per_user"
        or not record.has_shared_secret
    )


def _member_role_retired(
    record: TeamConnectionRecord,
    known: TeamConnectionRecord | None,
    member: TeamMemberState,
) -> bool:
    """Return whether the new form removed any role previously owned on disk."""
    if known is None:
        return False
    old_primary, old_named = _member_roles_to_keep(known, member)
    keep_primary, keep_named = _member_roles_to_keep(record, member)
    return (old_primary and not keep_primary) or bool(old_named - keep_named)


def _planned_cleanup(
    record: TeamConnectionRecord,
    known: TeamConnectionRecord | None,
    member: TeamMemberState,
    desired: str,
) -> CredentialCleanup | None:
    """Plan deletion of retired disk roles without deleting member-owned roles."""
    keep_primary, keep_named = _member_roles_to_keep(record, member)
    member_bundle_retired = member.has_member_secret and not keep_primary and not keep_named
    if not (
        _shared_copy_requires_cleanup(record, member)
        or member_bundle_retired
        or _member_role_retired(record, known, member)
    ):
        return None
    return CredentialCleanup(
        record_id=record.id,
        plugin=record.plugin,
        local_handle=desired,
        keep_primary=keep_primary,
        keep_named=keep_named,
    )


def _planned_fetch(
    record: TeamConnectionRecord, member: TeamMemberState, desired: str, auto_added_now: bool
) -> CredentialFetch | None:
    """A live-intent record whose shared secret is missing or stale gets a
    fetch — including the very pass that auto-adds it."""
    if (
        record.enabled
        and (member.added or auto_added_now)
        and not member.dismissed
        and record.auth_mode != "per_user"
        and record.has_shared_secret
        and record.shared_custody != "lease"
        and member.fetched_credential_version != record.credential_version
    ):
        return CredentialFetch(
            record_id=record.id,
            plugin=record.plugin,
            local_handle=desired,
            credential_version=record.credential_version,
        )
    return None


def plan_reconcile(
    state: TeamConnectionsState,
    server: Sequence[TeamConnectionRecord] | None,
    *,
    local_pairs: set[tuple[str, str]],
) -> ReconcilePlan:
    """Plan one reconcile pass — one named planner per plan field, walking the
    server records in a deterministic order.

    ``local_pairs`` is the member's OWN ``(plugin, handle)`` set (their
    ``connections.json``) — the names a team connection must never shadow.
    """
    if server is None:
        return ReconcilePlan(authoritative=False)

    server_by_id = {r.id: r for r in server}
    plan = ReconcilePlan(authoritative=True, removals=_planned_removals(state, server_by_id))

    team_claims = _existing_claims(state, server_by_id)
    for record in sorted(server_by_id.values(), key=lambda r: (r.plugin, r.handle, r.id)):
        known = state.records.get(record.id)
        member = state.member.get(record.id, TeamMemberState())

        upsert = _planned_upsert(record, known)
        if upsert is not None:
            plan.upserts.append(upsert)

        desired, rename = _assigned_handle(record, member, local_pairs, team_claims)
        plan.assignments[record.id] = desired
        if rename is not None:
            plan.renames.append(rename)
        team_claims[record.id] = (record.plugin, desired)

        auto_added_now = _auto_adds_now(record, member)
        if auto_added_now:
            plan.auto_adds.append(record.id)

        cleanup = _planned_cleanup(record, known, member, desired)
        if cleanup is not None:
            plan.credential_cleanups.append(cleanup)

        fetch = _planned_fetch(record, member, desired, auto_added_now)
        if fetch is not None:
            plan.credential_fetches.append(fetch)

    return plan


__all__ = [
    "CredentialCleanup",
    "CredentialFetch",
    "ReconcilePlan",
    "Removal",
    "Rename",
    "plan_reconcile",
]
