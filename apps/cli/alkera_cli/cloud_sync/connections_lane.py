"""The Preconfigured-Connections reconcile APPLIER — the I/O half.

Executes a :class:`ReconcilePlan` against the on-disk store + credential
files. Ordering is the crash-safety argument:

1. credential directories are removed / moved / written FIRST,
2. the state document is saved ONCE, atomically, at the end.

A crash anywhere in between leaves either the old state (files may be ahead —
the next pass re-plans the same steps and converges: rmtree and re-fetch are
both idempotent) or the new state. Never a torn document, never a state that
claims a credential version the disk doesn't hold.

Every credential fetch is fault-isolated: one failure logs, leaves that
record's ``fetched_credential_version`` trailing (so the next pass retries),
and never blocks the rest of the plan.
"""

from __future__ import annotations

import shutil
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from alkera_core.atomic_io import write_text_atomic
from alkera_core.extensions import ExtensionPoint
from alkera_core.naming import is_safe_handle

from alkera_cli.plugins.plugin_base.team_connections import (
    TeamConnectionsState,
    TeamConnectionsStore,
    TeamMemberState,
    team_credential_dir,
)

if TYPE_CHECKING:
    from alkera_core.project.directory import ProjectDirectory

    from alkera_cli.cloud_sync.reconcile import (
        CredentialCleanup,
        CredentialFetch,
        ReconcilePlan,
        Removal,
        Rename,
    )
    from alkera_cli.plugins.plugin_base.team_connections import TeamConnectionRecord

logger = structlog.get_logger(__name__)

FetchedCredential = tuple[str, int, dict[str, str]]
CredentialFetcher = Callable[[str], Awaitable[FetchedCredential]]
"""Fetch one versioned primary-and-named credential bundle."""

RowAnnouncer = Callable[[str, bool], None]
"""``(record_id, removed)`` for one row this pass changed.

Sync is the only writer that changes a team row without anybody asking it to —
an admin rotates a credential, adds a connection, takes one away — so it is the
one path where a surface has no reason to re-read. Announcing what it changed is
what turns that into a row that updates on its own. ``removed`` distinguishes a
row that is gone (the editor has to drop it) from one that merely moved."""


#: Run after each sync pass with the project it reconciled.
CONNECTIONS_RECONCILED: ExtensionPoint[Callable[[ProjectDirectory], object]] = ExtensionPoint(
    "connections.reconciled"
)

#: Other lease caches a removed record's credential may sit in, dropped with it.
LEASE_INVALIDATORS: ExtensionPoint[Callable[[str], None]] = ExtensionPoint(
    "connections.lease_invalidators"
)


def _write_secret(
    directory: Path, secret: str, named_secrets: dict[str, str] | None = None
) -> None:
    """Write an exact primary-and-named credential bundle owner-only."""
    unsafe = [name for name in named_secrets or {} if not is_safe_handle(name)]
    if unsafe:
        raise ValueError(f"unsafe named credential role {unsafe[0]!r}")
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    primary_path = directory / "credential"
    if secret:
        write_text_atomic(primary_path, secret, mode=0o600)
    else:
        primary_path.unlink(missing_ok=True)
    named_dir = directory / "credentials"
    shutil.rmtree(named_dir, ignore_errors=True)
    if not named_secrets:
        return
    named_dir.mkdir(mode=0o700)
    for name, value in sorted(named_secrets.items()):
        write_text_atomic(named_dir / name, value, mode=0o600)


async def run_connections_lane(
    project: ProjectDirectory, *, announce: RowAnnouncer | None = None
) -> None:
    """One reconcile pass: pull the authoritative set, plan, apply.

    Logged out → a clean no-op (the cached team set keeps serving; a login is
    not a membership answer and a logout is not a removal). A project pinned to
    another org than the sign-in is the same no-op: a switch is not a membership
    answer either. Any non-answer from the backend flows through as
    ``server=None`` and mutates nothing. The first pass pins the project to the
    org it reconciled against.

    ``announce`` is told about every row this pass changed, so an admin's change
    reaches an open editor without waiting for its next poll. Optional: the CLI
    beat has nobody to tell.
    """
    from alkera_cli.cloud_sync.client import resolve_connections_client
    from alkera_cli.cloud_sync.reconcile import plan_reconcile
    from alkera_cli.plugins.plugin_base.connections_store import AddedConnectionsStore

    client = resolve_connections_client(project, pin=True)
    if client is None:
        return
    store = TeamConnectionsStore(project.team_connections_path)
    local_pairs = {
        (c.plugin, c.handle) for c in AddedConnectionsStore(project.connections_path).load()
    }
    server = await client.fetch_records()
    plan = plan_reconcile(store.load(), server, local_pairs=local_pairs)
    await apply_reconcile(
        plan,
        store,
        plugins_root=project.plugins_path,
        fetch_credential=client.fetch_credential,
        announce=announce,
    )
    # Whatever is described from the connections (the agent's source cards in
    # the product) is refreshed off the set this pass just reconciled, so a
    # connection an admin adds reaches the next turn without a restart.
    for reconciled in CONNECTIONS_RECONCILED.items():
        reconciled(project)


def _quarantined_records(plan: ReconcilePlan) -> set[str]:
    """The record ids whose server-supplied plugin/handle isn't a safe path
    component. A quarantined record never enters local state at all, so no
    later pass (nor a member action, nor the health lane) can materialize a
    path from it. Every sink still asks ``team_credential_dir`` for its path,
    which stands on its own for a record an older client already persisted."""
    rejected = {
        record.id
        for record in plan.upserts
        if not (is_safe_handle(record.plugin) and is_safe_handle(record.handle))
    }
    for record_id, handle in plan.assignments.items():
        if not is_safe_handle(handle):
            rejected.add(record_id)
    return rejected


async def apply_reconcile(
    plan: ReconcilePlan,
    store: TeamConnectionsStore,
    *,
    plugins_root: Path,
    fetch_credential: CredentialFetcher,
    announce: RowAnnouncer | None = None,
) -> TeamConnectionsState | None:
    """Execute ``plan``. Returns the saved state, or ``None`` when the plan
    was non-authoritative or a no-op — in which case NOTHING was written (the
    store file stays byte-identical)."""
    if not plan.authoritative or plan.is_noop():
        return None

    # Quarantine before anything else touches the plan.
    rejected = _quarantined_records(plan)
    if rejected:
        logger.warning("cloud_sync.connections.records_rejected", count=len(rejected))

    # Fetch every planned credential FIRST — these are the applier's ONLY
    # awaits. With them out of the way, the state mutation below is one
    # synchronous load→mutate→save block, so a member action (a remove /
    # dismiss / authorize RPC — everything shares one event loop) can never
    # land between our load and our save and be clobbered by a stale snapshot.
    # An ADVISORY pre-fetch load skips rows the member already tombstoned (no
    # wasted secret round-trip for a dismissal that landed since planning) —
    # dismissal-only, so a not-yet-added auto-add row still gets its fetch. A
    # write landing DURING a fetch is merely a wasted fetch: the authoritative
    # re-load below never writes it.
    advisory = store.load()
    fetched: dict[str, FetchedCredential] = {}
    for fetch in plan.credential_fetches:
        if fetch.record_id in rejected:
            continue
        advisory_member = advisory.member.get(fetch.record_id)
        if advisory_member is not None and advisory_member.dismissed:
            continue
        try:
            fetched[fetch.record_id] = await fetch_credential(fetch.record_id)
        except Exception:
            logger.warning(
                "cloud_sync.connections.credential_fetch_failed",
                record_id=fetch.record_id,
                plugin=fetch.plugin,
                exc_info=True,
            )

    # Load fresh AFTER the awaits, and hold the store's write lock for the whole
    # synchronous mutate→save: a member action in ANOTHER process (a second
    # editor window, the TUI beside the daemon) can no longer land between this
    # load and the save below and be reverted by it. In-process actions share
    # the event loop and already can't interleave a synchronous block.
    with store.locked():
        state = store.load()
        before = _row_snapshot(state)
        _apply_plan_locked(
            plan, state, plugins_root=plugins_root, rejected=rejected, fetched=fetched
        )
        store.save(state)
        after = _row_snapshot(state)
    # Announced from the document, not from the plan: a plan step the quarantine
    # dropped, or one that re-wrote a row byte-for-byte, changed nothing a
    # surface could show, and an announcement for it is a re-render for nothing.
    _announce_changed_rows(before, after, announce)
    return state


def _row_snapshot(state: TeamConnectionsState) -> dict[str, tuple[Any, Any]]:
    """Every row as it reads right now, for comparing against itself after."""
    return {
        record_id: (
            record.model_dump(mode="json"),
            member.model_dump(mode="json") if (member := state.member.get(record_id)) else None,
        )
        for record_id, record in state.records.items()
    }


def _announce_changed_rows(
    before: dict[str, tuple[Any, Any]],
    after: dict[str, tuple[Any, Any]],
    announce: RowAnnouncer | None,
) -> None:
    """Tell ``announce`` about each row that actually moved. Best-effort: a
    listener that raises must not fail the sync pass that fed it."""
    if announce is None:
        return
    for record_id in before.keys() - after.keys():
        with _quiet_announcement(record_id):
            announce(record_id, True)
    for record_id, row in after.items():
        if before.get(record_id) != row:
            with _quiet_announcement(record_id):
                announce(record_id, False)


@contextmanager
def _quiet_announcement(record_id: str) -> Iterator[None]:
    try:
        yield
    except Exception:
        logger.debug("cloud_sync.connections.announce_failed", record_id=record_id, exc_info=True)


def _apply_plan_locked(
    plan: ReconcilePlan,
    state: TeamConnectionsState,
    *,
    plugins_root: Path,
    rejected: set[str],
    fetched: dict[str, FetchedCredential],
) -> None:
    """Mutate ``state`` per the plan, one pass per plan field, in the
    documented crash-safety order. Pure of awaits by design — the caller holds
    the store lock around this whole block plus the save."""
    _apply_removals(plan.removals, state, plugins_root)
    _apply_renames(plan.renames, plugins_root)
    _apply_upserts(plan.upserts, state, rejected)
    _apply_credential_cleanups(plan.credential_cleanups, state, plugins_root)
    _apply_handle_assignments(plan.assignments, state, rejected)
    _apply_auto_adds(plan.auto_adds, state, rejected)
    _apply_fetched_credentials(plan.credential_fetches, state, plugins_root, fetched)


def wipe_credential_and_leases(
    plugins_root: Path, plugin: str, local_handle: str, record_id: str
) -> None:
    """One removal, everywhere the credential lives: the on-disk directory AND
    both lease caches. No cached lease may outlive an authoritative removal
    (admin deleted the connection / the member left the team) — a per-user
    OAuth token or a shared service credential would both still work otherwise.
    Only the DELETE is conditional on the path being ours; a directory this
    machine never names holds nothing it wrote."""
    from alkera_cli.cloud_sync.shared_lease import invalidate_shared_lease

    directory = team_credential_dir(plugins_root, plugin, local_handle)
    if directory is not None:
        shutil.rmtree(directory, ignore_errors=True)
    invalidate_shared_lease(record_id)
    for invalidate in LEASE_INVALIDATORS.items():
        invalidate(record_id)


def _apply_removals(
    removals: list[Removal], state: TeamConnectionsState, plugins_root: Path
) -> None:
    """The state entries go regardless — a record we refuse to materialize is
    exactly one we want gone."""
    for removal in removals:
        wipe_credential_and_leases(
            plugins_root, removal.plugin, removal.local_handle, removal.record_id
        )
        state.records.pop(removal.record_id, None)
        state.member.pop(removal.record_id, None)


def _apply_renames(renames: list[Rename], plugins_root: Path) -> None:
    for rename in renames:
        old_dir = team_credential_dir(plugins_root, rename.plugin, rename.old_local_handle)
        new_dir = team_credential_dir(plugins_root, rename.plugin, rename.new_local_handle)
        if old_dir is None or new_dir is None:
            continue
        if old_dir.is_dir():
            shutil.rmtree(new_dir, ignore_errors=True)
            new_dir.parent.mkdir(parents=True, exist_ok=True)
            old_dir.rename(new_dir)


def _apply_upserts(
    upserts: list[TeamConnectionRecord], state: TeamConnectionsState, rejected: set[str]
) -> None:
    for record in upserts:
        if record.id in rejected:
            continue
        state.records[record.id] = record


def _delete_retired_roles(directory: Path, cleanup: CredentialCleanup) -> None:
    """Delete only disk roles absent from the cleanup's member-owned keep set."""
    if not cleanup.keep_primary:
        (directory / "credential").unlink(missing_ok=True)
    named_dir = directory / "credentials"
    if not named_dir.is_dir():
        return
    for path in named_dir.iterdir():
        if path.name in cleanup.keep_named:
            continue
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


def _settle_cleanup_member(member: TeamMemberState, cleanup: CredentialCleanup) -> None:
    """Reset shared-copy state and retire member completion only when no role remains."""
    member.fetched_credential_version = -1
    if not member.has_member_secret or cleanup.keep_primary or cleanup.keep_named:
        return
    member.has_member_secret = False
    member.authorized = False


def _apply_credential_cleanups(
    cleanups: list[CredentialCleanup], state: TeamConnectionsState, plugins_root: Path
) -> None:
    """Apply role-selective cleanup after any handle rename."""
    from alkera_cli.cloud_sync.shared_lease import invalidate_shared_lease

    for cleanup in cleanups:
        directory = team_credential_dir(plugins_root, cleanup.plugin, cleanup.local_handle)
        if directory is not None:
            _delete_retired_roles(directory, cleanup)
        invalidate_shared_lease(cleanup.record_id)
        member = state.member.get(cleanup.record_id)
        if member is not None:
            _settle_cleanup_member(member, cleanup)


def _apply_handle_assignments(
    assignments: dict[str, str], state: TeamConnectionsState, rejected: set[str]
) -> None:
    for record_id, local_handle in assignments.items():
        if record_id in rejected or record_id not in state.records:
            continue  # upsert list can trail a racing removal; never resurrect
        member = state.member.setdefault(record_id, TeamMemberState())
        member.local_handle = local_handle


def _apply_auto_adds(auto_adds: list[str], state: TeamConnectionsState, rejected: set[str]) -> None:
    for record_id in auto_adds:
        if record_id in rejected:
            continue
        auto_member = state.member.get(record_id)
        if auto_member is None or auto_member.dismissed:
            continue  # a dismissal that raced the planning pass wins
        auto_member.added = True


def _apply_fetched_credentials(
    fetches: list[CredentialFetch],
    state: TeamConnectionsState,
    plugins_root: Path,
    fetched: dict[str, FetchedCredential],
) -> None:
    for fetch in fetches:
        got = fetched.get(fetch.record_id)
        if got is None:
            continue  # the fetch failed — the version trails and the next pass retries
        fetch_member = state.member.get(fetch.record_id)
        if fetch_member is None or fetch_member.dismissed or not fetch_member.added:
            continue
        fetch_dir = team_credential_dir(plugins_root, fetch.plugin, fetch_member.local_handle)
        if fetch_dir is None:
            continue
        secret, version, named_secrets = got
        _write_secret(fetch_dir, secret, named_secrets)
        fetch_member.fetched_credential_version = version


__all__ = ["CredentialFetcher", "RowAnnouncer", "apply_reconcile", "run_connections_lane"]
