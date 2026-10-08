"""What the Files migration pair puts in the database, asserted against it.

The migration is the first of the isolation layers, so what it claims has to be
read back out of the catalogs rather than trusted: row-level security enabled
AND forced on every tenant table with the one policy, an application role that
cannot bypass it, the policy actually refusing another org's rows under that
role, the trigger that refuses a share to a principal outside the org, the
autovacuum settings on the churn tables, the two extensions, and every index the
models declare present under its declared name after the concurrent revision.

The exception set is asserted as a set, not a subset: a new tenant table that
forgets RLS fails here, and a new platform table has to be added to the list on
purpose. The round trip and ``alembic check`` are driven through Alembic itself,
the same way ``test_authz_migration_backfill`` does, so the upgrade path proven
is the production one.

The round trips and ``alembic check`` never run on the database the rest of the
session shares. Each takes a throwaway copy of the session's migration template
(:func:`tests.migration_harness.migration_scratch`) whose name nothing else in
the process knows, and the copy is dropped whatever happens inside it. Leftover
rows are one reason (a deep tree overflows the GiST index 0107's downgrade
rebuilds, a scratch table reads as drift), but not the main one.
``CREATE INDEX CONCURRENTLY`` waits for every transaction in its database that
holds a snapshot, whatever table that transaction reads, and the revisions bound
the wait with a lock timeout. On a database the session's shared session factory
reaches, one reader anywhere in the process (a pooled session, a background task
left on the session-scoped event loop) fails the upgrade half way and leaves the
schema below head for every later test. On a private copy nothing else can be
connected, and a trip that fails anyway fails one test.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio
from alembic import command
from alkera_core.config import settings
from alkera_core.db.base import Base
from alkera_core.db.schema_head import EXPECTED_SCHEMA_HEAD
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files.repo import SUBTREE_INDEX_DEPTH
from alkera_core.models.files import HISTORY_KINDS, OP_KINDS, VERSION_SOURCES
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, make_member
from tests.migration_harness import ScratchDatabase, migration_scratch

pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_VERSIONS = _BACKEND / "alembic" / "versions"
_CORE = next(iter(sorted(_VERSIONS.glob("*_files_core.py"))))
_INDEXES = next(iter(sorted(_VERSIONS.glob("*_files_indexes.py"))))
_PATH_INDEX = next(iter(sorted(_VERSIONS.glob("*_files_path_index.py"))))
_CATALOGUES = next(iter(sorted(_VERSIONS.glob("*_files_catalogues.py"))))

#: The three tables that hold no tenant rows. Spelled here, not imported from
#: the migration, so a change on either side has to be made on both.
PLATFORM_TABLES = frozenset({"file_stores", "file_sweep_shards", "file_platform"})
APP_ROLE = "alkera_files_app"
CHURN_TABLES = (
    "file_upload_sessions",
    "file_key_chunks",
    "file_idempotency_keys",
    "file_content_grants",
)
#: The Files tables in ``Base.metadata``: everything the subsystem named
#: ``file_*`` plus the three that do not carry the prefix.
_EXTRA_FILES_TABLES = frozenset({"dedup_domains", "manifest_terms", "storage_usage_snapshots"})


def files_tables() -> frozenset[str]:
    return frozenset(
        name
        for name in Base.metadata.tables
        if name.startswith("file_") or name in _EXTRA_FILES_TABLES
    )


async def _scalar(session: AsyncSession, sql: str, **params: Any) -> Any:
    return (await session.execute(text(sql), params)).scalar()


# --------------------------------------------------------------------- seeds


async def _seed_tree(session: AsyncSession, org_id: uuid.UUID) -> uuid.UUID:
    """A store, a dedup domain, a drive and one node for ``org_id``.

    Written as the migrating (superuser) login, so the rows exist regardless of
    the policy the tests then put in front of them.
    """
    store_id = uuid.uuid4()
    # uq_file_stores_driver_endpoint_bucket is real: two seeds that both take the
    # empty defaults collide, so each seed names its own bucket.
    await session.execute(
        text(
            "INSERT INTO file_stores (id, driver, bucket, capabilities, transfer_modes) "
            "VALUES (:id, 'filesystem', :bucket, '{}'::jsonb, ARRAY['proxied'])"
        ),
        {"id": store_id, "bucket": f"files-seed-{store_id.hex}"},
    )
    domain_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO dedup_domains (id, org_team_id, store_id, chunker_seed) "
            "VALUES (:id, :org, :store, '\\x00'::bytea)"
        ),
        {"id": domain_id, "org": org_id, "store": store_id},
    )
    drive_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO file_drives "
            "(id, org_team_id, store_id, dedup_domain_id, quota_bytes, quota_nodes, next_ino) "
            "VALUES (:id, :org, :store, :domain, 0, 0, 1)"
        ),
        {"id": drive_id, "org": org_id, "store": store_id, "domain": domain_id},
    )
    node_id = uuid.uuid4()
    await _insert_node(session, node_id=node_id, drive_id=drive_id, org_id=org_id)
    await session.commit()
    return node_id


async def _insert_node(
    session: AsyncSession,
    *,
    node_id: uuid.UUID,
    drive_id: uuid.UUID,
    org_id: uuid.UUID,
) -> None:
    await session.execute(
        text(
            "INSERT INTO file_nodes (id, ino, drive_id, org_team_id, kind, name, flags_names, "
            "path_ids, depth, mode, uid, gid, nlink, size, rdev, atime_ns, mtime_ns, ctime_ns, "
            "birthtime_ns, xattrs, etag, flags, traversal_only, metadata) VALUES "
            "(:id, 1, :drive, :org, 'folder', '\\x726f6f74'::bytea, '{}'::jsonb, "
            ":path, 0, 493, 0, 0, 1, 0, 0, 0, 0, 0, 0, '{}'::jsonb, 0, 0, false, '{}'::jsonb)"
        ),
        {
            "id": node_id,
            "drive": drive_id,
            "org": org_id,
            # ltree labels take [A-Za-z0-9_], so the uuid's dashes become underscores.
            "path": str(node_id).replace("-", "_"),
        },
    )


@pytest.fixture(autouse=True)
def _files_bridge_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rows below are hand-written SQL against the migrated schema.

    ``team_service.create_subteam`` carries a Files bridge behind
    ``FILES_ENABLED``: creating a team also ensures the org's drive, which
    allocates inodes from ``file_drives.next_ino``. The seeds here write a
    drive and its one node directly, so the allocator would hand out an inode
    the seed already used and the insert would die on
    ``uq_file_nodes_drive_ino`` — a collision that says nothing about the
    migration, the policy or the trigger under test. Pinned off so this module
    reads the same on a machine whose dotenv turns Files on as on one that does
    not; the bridge's own behaviour belongs to the service and route suites.
    """
    monkeypatch.setattr(settings, "files_enabled", False)


@pytest_asyncio.fixture
async def two_orgs(
    org_admin: OrgWithAdmin,
) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]]:
    """Org A with a seeded node, org B with a seeded node, both committed."""
    from backend.services.org import teams as team_service

    async with AsyncSessionLocal() as session:
        org_b, _ = await team_service.create_org_with_admin(
            session,
            org_name=f"files-rls-{uuid.uuid4().hex[:8]}",
            admin_email=f"files-rls-{uuid.uuid4().hex[:8]}@alkera.dev",
            admin_first_name="B",
            admin_last_name="Admin",
            admin_password="files-rls-pass-12345",
        )
        await session.commit()
        org_b_id = org_b.id
        node_a = await _seed_tree(session, org_admin.org_id)
        node_b = await _seed_tree(session, org_b_id)
    yield org_admin.org_id, node_a, org_b_id, node_b


# ------------------------------------------------------------------ (a) RLS


async def test_every_tenant_files_table_has_rls_forced_with_the_isolation_policy() -> None:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
                    "  EXISTS (SELECT 1 FROM pg_policy p "
                    "          WHERE p.polrelid = c.oid AND p.polname = 'files_tenant_isolation') "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relname = ANY(:names)"
                ),
                {"names": sorted(files_tables())},
            )
        ).all()
    seen = {r[0] for r in rows}
    assert seen == files_tables(), f"missing from the database: {files_tables() - seen}"
    unprotected = {name for name, rls, forced, policy in rows if not (rls and forced and policy)}
    # A set, not a subset: a new tenant table that forgets RLS shows up here,
    # and a new platform table has to be added to PLATFORM_TABLES on purpose.
    assert unprotected == PLATFORM_TABLES


async def test_the_isolation_policy_covers_reads_and_writes_on_every_tenant_table() -> None:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT c.relname, p.polcmd::text, p.polqual IS NOT NULL, "
                    "  p.polwithcheck IS NOT NULL "
                    "FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid "
                    "WHERE p.polname = 'files_tenant_isolation'"
                )
            )
        ).all()
    assert {r[0] for r in rows} == files_tables() - PLATFORM_TABLES
    # '*' is FOR ALL; a USING without a WITH CHECK would let a write plant a row
    # the same transaction cannot read back.
    assert {(r[1], r[2], r[3]) for r in rows} == {("*", True, True)}


# ------------------------------------------------------------- (b) the role


async def test_the_app_role_exists_without_superuser_or_bypassrls_and_the_login_is_a_member() -> (
    None
):
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT rolsuper, rolbypassrls, rolcanlogin, "
                    "  pg_has_role(current_user, oid, 'MEMBER') "
                    "FROM pg_roles WHERE rolname = :role"
                ),
                {"role": APP_ROLE},
            )
        ).one()
    superuser, bypass, canlogin, is_member = row
    assert (superuser, bypass, canlogin) == (False, False, False)
    assert is_member is True


async def test_the_app_role_holds_dml_on_every_files_table() -> None:
    async with AsyncSessionLocal() as session:
        missing = []
        for table in sorted(files_tables()):
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                granted = await _scalar(
                    session,
                    "SELECT has_table_privilege(:role, :table, :priv)",
                    role=APP_ROLE,
                    table=table,
                    priv=priv,
                )
                if not granted:
                    missing.append(f"{table}.{priv}")
    assert missing == []


# ------------------------------------------------------- (c) RLS actually bites


async def _as_app_role(session: AsyncSession, org_id: uuid.UUID | None) -> None:
    await session.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
    if org_id is not None:
        await session.execute(
            text("SELECT set_config('alkera.org_id', :org, true)"), {"org": str(org_id)}
        )


async def test_under_the_app_role_an_org_sees_only_its_own_nodes(
    two_orgs: tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    org_a, node_a, _org_b, node_b = two_orgs
    async with AsyncSessionLocal() as session:
        await _as_app_role(session, org_a)
        visible = (
            (
                await session.execute(
                    text("SELECT id FROM file_nodes WHERE id = ANY(:ids)"),
                    {"ids": [node_a, node_b]},
                )
            )
            .scalars()
            .all()
        )
        await session.rollback()
    assert list(visible) == [node_a]


async def test_under_the_app_role_an_unset_org_setting_sees_nothing(
    two_orgs: tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """An unset org id matches no row instead of raising.

    Both unset shapes, on one connection: never set at all, and set-then-ended
    — the shape a pooled connection actually hands the next borrower, where the
    GUC survives as the empty string rather than disappearing.
    """
    _org_a, node_a, _org_b, node_b = two_orgs
    async with AsyncSessionLocal() as session:
        await _as_app_role(session, None)
        assert (
            (
                await session.execute(
                    text("SELECT id FROM file_nodes WHERE id = ANY(:ids)"),
                    {"ids": [node_a, node_b]},
                )
            )
            .scalars()
            .all()
        ) == []
        # Set it, end the transaction: the GUC is now '' on this connection.
        await _as_app_role(session, _org_a)
        await session.rollback()
        await _as_app_role(session, None)
        rows = (
            (
                await session.execute(
                    text("SELECT id FROM file_nodes WHERE id = ANY(:ids)"),
                    {"ids": [node_a, node_b]},
                )
            )
            .scalars()
            .all()
        )
        await session.rollback()
    assert list(rows) == []


async def test_under_the_app_role_a_write_for_another_org_is_refused(
    two_orgs: tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """WITH CHECK: reading org B is not enough to stop a bug that WRITES a row
    stamped with org B's id from org A's transaction."""
    org_a, node_a, org_b, _node_b = two_orgs
    async with AsyncSessionLocal() as session:
        drive_a = await _scalar(
            session, "SELECT drive_id FROM file_nodes WHERE id = :id", id=node_a
        )
        await _as_app_role(session, org_a)
        with pytest.raises(Exception, match="row-level security"):
            await _insert_node(session, node_id=uuid.uuid4(), drive_id=drive_a, org_id=org_b)
        await session.rollback()


# --------------------------------------------------------- (d) share trigger


async def _share(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    node_id: uuid.UUID,
    kind: str,
    principal_id: uuid.UUID,
) -> None:
    await session.execute(
        text(
            "INSERT INTO file_shares "
            "(id, org_team_id, node_id, principal_kind, principal_id, role, granted_by) "
            "VALUES (:id, :org, :node, :kind, :principal, 'viewer', :org)"
        ),
        {
            "id": uuid.uuid4(),
            "org": org_id,
            "node": node_id,
            "kind": kind,
            "principal": principal_id,
        },
    )


async def test_a_share_to_a_member_of_the_org_is_accepted(
    two_orgs: tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    org_a, node_a, _org_b, _node_b = two_orgs
    async with AsyncSessionLocal() as session:
        member, _ = await make_member(session, org_id=org_a)
        await session.commit()
        await _share(session, org_id=org_a, node_id=node_a, kind="user", principal_id=member.id)
        await session.commit()
        assert (
            await _scalar(
                session,
                "SELECT count(*) FROM file_shares WHERE node_id = :n AND principal_id = :p",
                n=node_a,
                p=member.id,
            )
            == 1
        )


async def test_a_share_to_a_user_outside_the_org_is_refused_by_the_trigger(
    two_orgs: tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    org_a, node_a, org_b, _node_b = two_orgs
    async with AsyncSessionLocal() as session:
        stranger, _ = await make_member(session, org_id=org_b)
        await session.commit()
        with pytest.raises(Exception, match=re.escape("files.share_principal_not_in_org")):
            await _share(
                session, org_id=org_a, node_id=node_a, kind="user", principal_id=stranger.id
            )
        await session.rollback()


async def test_a_share_to_a_user_that_does_not_exist_is_refused(
    two_orgs: tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    org_a, node_a, _org_b, _node_b = two_orgs
    async with AsyncSessionLocal() as session:
        with pytest.raises(Exception, match=re.escape("files.share_principal_not_in_org")):
            await _share(
                session, org_id=org_a, node_id=node_a, kind="user", principal_id=uuid.uuid4()
            )
        await session.rollback()


async def test_a_share_to_a_team_of_another_org_is_refused(
    two_orgs: tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """The walk up parent_team_id has to reach org A's root, not merely find a
    team that exists."""
    org_a, node_a, org_b, _node_b = two_orgs
    from backend.services.org import teams as team_service

    async with AsyncSessionLocal() as session:
        foreign = await team_service.create_subteam(
            session, org_team_id=org_b, parent_team_id=org_b, name="foreign"
        )
        await session.commit()
        with pytest.raises(Exception, match=re.escape("files.share_principal_not_in_org")):
            await _share(
                session, org_id=org_a, node_id=node_a, kind="team", principal_id=foreign.id
            )
        await session.rollback()


async def test_a_share_to_a_descendant_team_of_the_org_is_accepted(
    two_orgs: tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    org_a, node_a, _org_b, _node_b = two_orgs
    from backend.services.org import teams as team_service

    async with AsyncSessionLocal() as session:
        child = await team_service.create_subteam(
            session, org_team_id=org_a, parent_team_id=org_a, name="child"
        )
        await session.commit()
        await _share(session, org_id=org_a, node_id=node_a, kind="team", principal_id=child.id)
        await session.commit()
        assert (
            await _scalar(
                session,
                "SELECT count(*) FROM file_shares WHERE principal_id = :p",
                p=child.id,
            )
            == 1
        )


async def test_an_org_share_must_name_the_owning_org(
    two_orgs: tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    org_a, node_a, org_b, _node_b = two_orgs
    async with AsyncSessionLocal() as session:
        await _share(session, org_id=org_a, node_id=node_a, kind="org", principal_id=org_a)
        await session.commit()
        with pytest.raises(Exception, match=re.escape("files.share_principal_not_in_org")):
            await _share(session, org_id=org_a, node_id=node_a, kind="org", principal_id=org_b)
        await session.rollback()


async def test_an_unknown_principal_kind_is_refused(
    two_orgs: tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """A kind nobody taught the trigger about is refused, so adding one is a
    deliberate migration rather than a silent hole."""
    org_a, node_a, _org_b, _node_b = two_orgs
    async with AsyncSessionLocal() as session:
        with pytest.raises(Exception, match=re.escape("files.share_principal_not_in_org")):
            await _share(session, org_id=org_a, node_id=node_a, kind="link", principal_id=org_a)
        await session.rollback()


async def test_an_update_that_moves_a_share_to_a_stranger_is_refused(
    two_orgs: tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """BEFORE INSERT OR UPDATE: an accepted row cannot be re-pointed afterwards."""
    org_a, node_a, org_b, _node_b = two_orgs
    async with AsyncSessionLocal() as session:
        member, _ = await make_member(session, org_id=org_a)
        stranger, _ = await make_member(session, org_id=org_b)
        await session.commit()
        await _share(session, org_id=org_a, node_id=node_a, kind="user", principal_id=member.id)
        await session.commit()
        with pytest.raises(Exception, match=re.escape("files.share_principal_not_in_org")):
            await session.execute(
                text("UPDATE file_shares SET principal_id = :new WHERE principal_id = :old"),
                {"new": stranger.id, "old": member.id},
            )
        await session.rollback()


# ------------------------------------------------- (e) (f) settings, extensions


@pytest.mark.parametrize("table", CHURN_TABLES)
async def test_a_churn_table_vacuums_at_one_percent_dead_tuples(table: str) -> None:
    async with AsyncSessionLocal() as session:
        reloptions = await _scalar(
            session, "SELECT reloptions FROM pg_class WHERE relname = :t", t=table
        )
    assert reloptions is not None, f"{table} carries no reloptions"
    assert "autovacuum_vacuum_scale_factor=0.01" in reloptions


@pytest.mark.parametrize("extension", ["ltree", "pg_trgm"])
async def test_the_extension_the_tree_and_the_search_need_is_installed(extension: str) -> None:
    async with AsyncSessionLocal() as session:
        assert (
            await _scalar(
                session, "SELECT count(*) FROM pg_extension WHERE extname = :e", e=extension
            )
            == 1
        )


# ------------------------------------------------------- (i) declared indexes


async def test_every_index_the_models_declare_exists_under_its_declared_name() -> None:
    """Catches an index dropped on the way from 0105 into the concurrent 0106."""
    declared = {
        index.name
        for name, table in Base.metadata.tables.items()
        if name in files_tables()
        for index in table.indexes
    }
    assert declared, "no Files indexes in the metadata — the harvest is wrong"
    async with AsyncSessionLocal() as session:
        present = set(
            (
                await session.execute(
                    text("SELECT indexname FROM pg_indexes WHERE indexname = ANY(:names)"),
                    {"names": sorted(declared)},
                )
            )
            .scalars()
            .all()
        )
    assert declared - present == set()


async def test_the_concurrent_revision_creates_every_non_unique_secondary_index() -> None:
    """The split itself: a unique index cannot be built concurrently, so it stays
    in 0105; everything else has to have moved, or 0106 is decoration."""
    core = _CORE.read_text()
    indexes = _INDEXES.read_text()
    assert "autocommit_block" in indexes
    assert indexes.count("postgresql_concurrently=True") == indexes.count("op.create_index(")
    for line in core.splitlines():
        if "op.create_index(" in line:
            assert "unique=True" in core.split(line, 1)[1][:400] or "uq_" in line, line


# ------------------------------------------------------ (g) (h) the round trip


async def test_the_migrations_are_a_consecutive_block_the_head_is_at_or_past() -> None:
    """The four Files revisions are one unbroken run — a gap means one was
    renumbered out from under the others and the chain no longer applies in the
    order it was written.

    The head is only required to be at or past the last of them: any other
    feature landing a revision moves it, and pinning equality here would fail
    this file for a change that has nothing to do with Files.
    """
    assert int(_INDEXES.name.split("_", 1)[0]) == int(_CORE.name.split("_", 1)[0]) + 1
    assert int(_PATH_INDEX.name.split("_", 1)[0]) == int(_INDEXES.name.split("_", 1)[0]) + 1
    assert int(_CATALOGUES.name.split("_", 1)[0]) == int(_PATH_INDEX.name.split("_", 1)[0]) + 1
    assert int(_CATALOGUES.name.split("_", 1)[0]) <= int(EXPECTED_SCHEMA_HEAD)


async def test_the_subtree_index_key_is_truncated_to_the_depth_the_repo_queries() -> None:
    """The index and ``FilesRepo.subtree_predicate`` must key on the same
    expression, or every subtree read silently becomes a sequential scan — and
    an index over the untruncated path is the shape that refuses inserts once a
    tree is deep."""
    async with AsyncSessionLocal() as session:
        definition = (
            await session.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
                {"name": "ix_file_nodes_path_ids"},
            )
        ).scalar_one()
    assert f"subpath(path_ids, 0, {SUBTREE_INDEX_DEPTH})" in definition
    assert "gist" in definition
    assert "gist_ltree_ops" in definition


async def test_the_app_role_may_append_to_the_outbox_and_read_back_nothing_else() -> None:
    """0107's grant, which is what let the announcement drop its
    ``SET LOCAL ROLE NONE`` dance: INSERT plus the two columns RETURNING reads,
    and no more — the role must not be able to read the log's payloads."""
    async with AsyncSessionLocal() as session:
        granted = dict(
            (
                await session.execute(
                    text(
                        "SELECT privilege_type, true FROM information_schema.table_privileges "
                        "WHERE table_name = 'event_outbox' AND grantee = :role"
                    ),
                    {"role": APP_ROLE},
                )
            ).all()
        )
        columns = set(
            (
                await session.execute(
                    text(
                        "SELECT column_name FROM information_schema.column_privileges "
                        "WHERE table_name = 'event_outbox' AND grantee = :role "
                        "AND privilege_type = 'SELECT'"
                    ),
                    {"role": APP_ROLE},
                )
            )
            .scalars()
            .all()
        )
        usable = (
            await session.execute(
                text("SELECT has_sequence_privilege(:role, 'event_outbox_id_seq', 'USAGE')"),
                {"role": APP_ROLE},
            )
        ).scalar_one()
    assert set(granted) == {"INSERT"}
    assert columns == {"id", "ts"}
    assert usable is True


async def _files_tables_present(session: AsyncSession) -> set[str]:
    return set(
        (
            await session.execute(
                text(
                    "SELECT relname FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relname = ANY(:names)"
                ),
                {"names": sorted(files_tables())},
            )
        )
        .scalars()
        .all()
    )


async def test_downgrade_removes_every_files_table_and_upgrade_restores_them() -> None:
    async with migration_scratch() as db:
        # Rows in the tables, so the downgrade drops tables that hold something.
        async with db.session() as session:
            await _seed_tree(session, uuid.uuid4())
            await _seed_tree(session, uuid.uuid4())
        await db.downgrade("0104")
        async with db.session() as session:
            assert await _files_tables_present(session) == set()
            assert (
                await _scalar(
                    session,
                    "SELECT count(*) FROM pg_policy WHERE polname = 'files_tenant_isolation'",
                )
                == 0
            )
            assert (
                await _scalar(
                    session,
                    "SELECT count(*) FROM pg_proc WHERE proname = 'files_share_principal_in_org'",
                )
                == 0
            )
        await db.upgrade()
        async with db.session() as session:
            assert await _files_tables_present(session) == files_tables()
            # The seed row the restore runbook increments comes back with it.
            assert (
                await _scalar(session, "SELECT restore_generation FROM file_platform WHERE id = 1")
                == 0
            )


async def test_alembic_check_is_clean_against_the_models() -> None:
    """The models and the revisions agree: no drift for the pr-gate to find.

    Checked on a copy of the template rather than the worker's database, where a
    table another module created and left behind would read as drift.
    """
    async with migration_scratch() as db:
        command.check(db.config)


async def test_every_files_constraint_and_index_carries_an_explicit_name() -> None:
    """A later revision can only ``drop_constraint(name=...)`` what was named.

    There is no ``naming_convention`` on ``Base.metadata``, so an unnamed
    constraint takes whatever Postgres invents (``file_x_col_key``,
    ``file_x_check``) and cannot be dropped by name in a follow-up migration.
    Primary keys are exempt either way: the invented ``<table>_pkey`` is
    deterministic and is what most of the schema carries, and a revision that
    names its own (``pk_<table>``) is more deterministic still.
    """
    async with AsyncSessionLocal() as session:
        anonymous_constraints = (
            (
                await session.execute(
                    text(
                        "SELECT c.relname || '.' || con.conname FROM pg_constraint con "
                        "JOIN pg_class c ON c.oid = con.conrelid "
                        "WHERE c.relname = ANY(:names) AND con.contype <> 'p' "
                        "  AND con.conname !~ '^(fk|uq|ck|ex)_'"
                    ),
                    {"names": sorted(files_tables())},
                )
            )
            .scalars()
            .all()
        )
        anonymous_indexes = (
            (
                await session.execute(
                    text(
                        "SELECT tablename || '.' || indexname FROM pg_indexes "
                        "WHERE schemaname = 'public' AND tablename = ANY(:names) "
                        "  AND indexname !~ '^(ix|uq|pk)_' AND indexname !~ '_pkey$'"
                    ),
                    {"names": sorted(files_tables())},
                )
            )
            .scalars()
            .all()
        )
    assert list(anonymous_constraints) == []
    assert list(anonymous_indexes) == []


async def test_the_files_tables_harvest_is_not_empty() -> None:
    assert len(files_tables()) >= 30
    assert PLATFORM_TABLES <= files_tables()


# ---------------------------------------------------- (i) 0108's catalogues


#: The values 0108 adds, per table, and so exactly the rows its ``downgrade()``
#: refuses to narrow.
NEW_CATALOGUE_VALUES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("file_history", "kind", ("conflict_resolved", "copy")),
    ("file_versions", "source", ("copy",)),
    ("file_ops", "kind", ("bulk",)),
)


async def _clear_catalogue_values(
    sessions: Callable[[], AsyncSession] = AsyncSessionLocal,
) -> None:
    async with sessions() as session:
        # A node's head points at a version, so the version cannot go while the
        # pointer does. Detaching first is safe here and only here: the rows are
        # being removed so a downgrade can run, and the tree they belonged to is
        # about to be dropped or is another test's already-finished setup.
        await session.execute(
            text(
                "UPDATE file_nodes SET head_version_id = NULL WHERE head_version_id IN "
                "(SELECT id FROM file_versions WHERE source = ANY(:values))"
            ),
            {"values": ["copy"]},
        )
        for table, column, values in NEW_CATALOGUE_VALUES:
            await session.execute(
                text(f"DELETE FROM {table} WHERE {column} = ANY(:values)"),
                {"values": list(values)},
            )
        await session.commit()


@pytest_asyncio.fixture
async def planted_rows() -> AsyncIterator[None]:
    """Take the rows a catalogue test plants back out again.

    The downgrade this section exercises refuses to narrow a catalogue any row
    still uses, which is the point — but it means a planted row left behind
    would decide a later test's result instead of that test's own setup.
    """
    yield
    await _clear_catalogue_values()


async def _drive_of(session: AsyncSession, node_id: uuid.UUID) -> uuid.UUID:
    return cast(
        uuid.UUID,
        await _scalar(session, "SELECT drive_id FROM file_nodes WHERE id = :id", id=node_id),
    )


async def _write_history(
    session: AsyncSession, *, org_id: uuid.UUID, node_id: uuid.UUID, kind: str, seq: int = 1
) -> None:
    await session.execute(
        text(
            "INSERT INTO file_history (id, org_team_id, node_id, seq, kind, acting_principal) "
            "VALUES (:id, :org, :node, :seq, :kind, :actor)"
        ),
        {
            "id": uuid.uuid4(),
            "org": org_id,
            "node": node_id,
            "seq": seq,
            "kind": kind,
            "actor": uuid.uuid4(),
        },
    )


async def _write_version(
    session: AsyncSession, *, org_id: uuid.UUID, node_id: uuid.UUID, source: str, seq: int = 1
) -> None:
    await session.execute(
        text(
            "INSERT INTO file_versions (id, org_team_id, node_id, seq, size_bytes, content_hash, "
            "source, keep_forever, held, lease_epoch, metadata) "
            "VALUES (:id, :org, :node, :seq, 0, '\\x00'::bytea, :source, false, false, 0, "
            "'{}'::jsonb)"
        ),
        {"id": uuid.uuid4(), "org": org_id, "node": node_id, "seq": seq, "source": source},
    )


async def _write_op(
    session: AsyncSession, *, org_id: uuid.UUID, drive_id: uuid.UUID, kind: str
) -> None:
    await session.execute(
        text(
            "INSERT INTO file_ops (id, org_team_id, drive_id, kind, actor, progress, errors, "
            "conflicts, result) VALUES (:id, :org, :drive, :kind, :actor, '{}'::jsonb, "
            "'[]'::jsonb, '[]'::jsonb, '{}'::jsonb)"
        ),
        {"id": uuid.uuid4(), "org": org_id, "drive": drive_id, "kind": kind, "actor": uuid.uuid4()},
    )


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param("conflict_resolved", id="conflict_resolved-17-bytes-past-the-old-column"),
        pytest.param("copy", id="copy"),
    ],
)
async def test_a_history_row_takes_the_kind_its_writer_actually_means(
    two_orgs: tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID],
    planted_rows: None,
    kind: str,
) -> None:
    """Before 0108 both of these were refused — ``conflict_resolved`` by the
    16-byte column as well as by the CHECK — which is why the two call sites
    wrote ``attrs`` and ``create`` instead."""
    org_a, node_a, _org_b, _node_b = two_orgs
    async with AsyncSessionLocal() as session:
        await _write_history(session, org_id=org_a, node_id=node_a, kind=kind)
        await session.commit()
        stored = await _scalar(
            session,
            "SELECT kind FROM file_history WHERE node_id = :node ORDER BY seq DESC LIMIT 1",
            node=node_a,
        )
    assert stored == kind


async def test_a_history_row_still_refuses_a_kind_that_is_in_no_catalogue(
    two_orgs: tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """Widening is not opening: the CHECK is still closed."""
    org_a, node_a, _org_b, _node_b = two_orgs
    async with AsyncSessionLocal() as session:
        with pytest.raises(IntegrityError):
            await _write_history(session, org_id=org_a, node_id=node_a, kind="conflict_resolve")
        await session.rollback()


async def test_a_copied_version_records_copy_as_its_own_provenance(
    two_orgs: tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID], planted_rows: None
) -> None:
    org_a, node_a, _org_b, _node_b = two_orgs
    async with AsyncSessionLocal() as session:
        await _write_version(session, org_id=org_a, node_id=node_a, source="copy")
        await session.commit()
        stored = await _scalar(
            session, "SELECT source FROM file_versions WHERE node_id = :node", node=node_a
        )
    assert stored == "copy"


async def test_a_queued_batch_is_recorded_under_its_own_operation_kind(
    two_orgs: tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID], planted_rows: None
) -> None:
    org_a, node_a, _org_b, _node_b = two_orgs
    async with AsyncSessionLocal() as session:
        drive = await _drive_of(session, node_a)
        await _write_op(session, org_id=org_a, drive_id=drive, kind="bulk")
        await session.commit()
        stored = await _scalar(
            session, "SELECT kind FROM file_ops WHERE drive_id = :drive", drive=drive
        )
    assert stored == "bulk"


async def _round_trip_the_catalogues(db: ScratchDatabase) -> None:
    """Down to 0107 and up again, asserting the catalogue columns at each end."""
    await db.downgrade("0107")
    async with db.session() as session:
        width = await _scalar(
            session,
            "SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_name = 'file_history' AND column_name = 'kind'",
        )
        narrowed = await _scalar(
            session,
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_file_ops_kind'",
        )
    assert width == 16
    assert "bulk" not in narrowed
    await db.upgrade()
    async with db.session() as session:
        width = await _scalar(
            session,
            "SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_name = 'file_history' AND column_name = 'kind'",
        )
        widened = await _scalar(
            session,
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_file_ops_kind'",
        )
    assert width == 32
    assert "bulk" in widened


async def test_the_catalogue_revision_round_trips_down_and_back_up() -> None:
    """Down to 0107 and up again on a database holding none of the new values
    (the harness parks any the template carries): the column narrows to what
    0105 sized it at, the widened value is gone from the CHECK while it is down,
    and the upgrade restores both."""
    async with migration_scratch() as db:
        await _round_trip_the_catalogues(db)


async def test_a_snapshot_held_elsewhere_in_the_session_does_not_stall_a_round_trip() -> None:
    """The upgrade back to head runs ``CREATE INDEX CONCURRENTLY``, which waits
    for every transaction in its database that holds a snapshot and gives up
    after the migrations' lock timeout. A trip that shared its database with the
    session's factory failed on a loaded runner for exactly that reason, half way
    up, and left the schema below head for every test after it. Here a reader on
    that factory pins a snapshot for the whole trip; the trip owns its database,
    so it never waits on the reader."""
    async with AsyncSessionLocal() as reader:
        await reader.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        await reader.execute(text("SELECT count(*) FROM teams"))
        reader_pid = await _scalar(reader, "SELECT pg_backend_pid()")
        async with migration_scratch() as db:
            async with db.session() as session:
                pinned = (
                    await session.execute(
                        text(
                            "SELECT state, backend_xmin IS NOT NULL AS holds_snapshot "
                            "FROM pg_stat_activity WHERE pid = :pid"
                        ),
                        {"pid": reader_pid},
                    )
                ).one()
            # Seen from another backend: the reader is parked mid-transaction
            # with its snapshot pinned, which is what the index build waits on.
            assert (pinned.state, pinned.holds_snapshot) == ("idle in transaction", True)
            await _round_trip_the_catalogues(db)
        await reader.rollback()


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        pytest.param("file_history", "kind", "conflict_resolved", id="history-conflict_resolved"),
        pytest.param("file_versions", "source", "copy", id="versions-copy"),
        pytest.param("file_ops", "kind", "bulk", id="ops-bulk"),
    ],
)
async def test_the_downgrade_refuses_to_narrow_a_catalogue_a_row_still_uses(
    table: str,
    column: str,
    value: str,
) -> None:
    """Each of the three is a fact somebody will read: an append-only audit row,
    a version\'s provenance, and the kind undo replays the inverse of. Rather
    than invent a narrower value for a row it did not write, the downgrade stops
    and names the table, the value and the count."""
    async with migration_scratch() as db:
        # Whatever the template carries would change the count the refusal names.
        await _clear_catalogue_values(db.session)
        org_id = uuid.uuid4()
        async with db.session() as session:
            node_id = await _seed_tree(session, org_id)
            if table == "file_history":
                await _write_history(session, org_id=org_id, node_id=node_id, kind=value)
            elif table == "file_versions":
                await _write_version(session, org_id=org_id, node_id=node_id, source=value)
            else:
                await _write_op(
                    session, org_id=org_id, drive_id=await _drive_of(session, node_id), kind=value
                )
            await session.commit()
        # Straight through Alembic rather than ``db.downgrade``: the harness
        # parks exactly the rows this test plants, and the refusal is the subject.
        with pytest.raises(RuntimeError, match=rf"cannot narrow {table}\.{column}: {value}=1"):
            command.downgrade(db.config, "0107")
        async with db.session() as session:
            # The refusal is total: nothing was narrowed on the way to the raise.
            survived = await _scalar(
                session,
                f"SELECT count(*) FROM {table} WHERE {column} = :value",
                value=value,
            )
            width = await _scalar(
                session,
                "SELECT character_maximum_length FROM information_schema.columns "
                "WHERE table_name = 'file_history' AND column_name = 'kind'",
            )
    assert survived == 1
    assert width == 32


@pytest.mark.parametrize(
    ("table", "column", "values"),
    [
        pytest.param("file_history", "kind", HISTORY_KINDS, id="history-kind"),
        pytest.param("file_versions", "source", VERSION_SOURCES, id="versions-source"),
        pytest.param("file_ops", "kind", OP_KINDS, id="ops-kind"),
    ],
)
async def test_every_value_a_closed_catalogue_admits_fits_the_column_that_holds_it(
    table: str, column: str, values: tuple[str, ...]
) -> None:
    """A CHECK that names a value the column is too narrow to store refuses the
    write with a truncation error instead of a constraint error, so the vocabulary
    reads as open while the write path is closed — which is how ``conflict_resolved``
    (17 bytes, in a 16-byte column) went recorded as ``attrs`` for a whole release.
    Widening a catalogue and widening its column are one change, and this asserts
    the pair against the live schema rather than against the model's own length."""
    async with AsyncSessionLocal() as session:
        width = await _scalar(
            session,
            "SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_name = :table AND column_name = :column",
            table=table,
            column=column,
        )
    assert width is not None, f"{table}.{column} is not a length-bounded type"
    too_long = sorted(one for one in values if len(one) > int(width))
    assert not too_long, (
        f"{table}.{column} is VARCHAR({width}) but the catalogue admits {too_long}: "
        "widen the column in the same revision that widens the CHECK."
    )
