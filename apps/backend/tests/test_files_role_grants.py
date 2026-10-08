"""What ``alkera_files_app`` may touch, pinned against what the code touches.

Every Files transaction runs its statements as ``alkera_files_app``: an
unprivileged role that holds the Files tables and nothing else, which is what
makes the FORCEd row-level security on those tables bind. A statement against
another subsystem's table — the object row a chat lives on, the team rows the
role resolver walks, the org's audit trail — must step out of that role for
exactly its own statement and step back in, never be paid for by widening the
role.

The failure this pins is silent in review and loud in production: a new code
path reads ``workspace_objects`` while the role is in force, the reviewer fixes
the ``permission denied`` by adding a GRANT to a migration, and every Files
statement in the system is now one SQL injection away from the whole object
tree. So the grant set is asserted to be Files-only, and the denial it rests on
is exercised rather than assumed.
"""

from __future__ import annotations

import pytest
from alkera_core.files.repo import APP_ROLE
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import TextClause

#: Files tables whose names do not carry the ``file_`` prefix. Anything else the
#: role holds is either a new Files table (add it here) or the bug this module
#: exists to catch.
FILES_TABLES_WITHOUT_THE_PREFIX = frozenset(
    {"dedup_domains", "manifest_terms", "storage_usage_snapshots"}
)

#: The one platform table the role may write, and the only privilege it has on
#: it: a Files transaction announces its own changes on the shared outbox. It
#: cannot read the table at large — the delta feed's join steps out of the role
#: for that — so INSERT is the whole grant.
OUTBOX = "event_outbox"

#: Tables that role-scoped code paths have been caught reading or writing, each
#: of which must be reached by stepping OUT of the role rather than by granting
#: it. ``workspace_objects`` is the object a chat or a template is; ``teams``
#: and ``team_memberships`` are what the platform role resolver walks;
#: ``org_audit_events`` is the tenant's audit trail; ``users`` is the identity
#: the share trigger resolves.
MUST_STAY_FOREIGN = (
    "workspace_objects",
    "teams",
    "team_memberships",
    "org_audit_events",
    "users",
)

_GRANTS = text(
    """
    SELECT table_name, privilege_type
      FROM information_schema.role_table_grants
     WHERE grantee = :role AND table_schema = 'public'
    """
)

_COLUMN_GRANTS = text(
    """
    SELECT DISTINCT table_name, privilege_type
      FROM information_schema.role_column_grants
     WHERE grantee = :role AND table_schema = 'public'
    """
)


async def _granted(session: AsyncSession, *statements: TextClause) -> dict[str, set[str]]:
    """Every privilege the role holds, by table, across the given catalog views."""
    held: dict[str, set[str]] = {}
    for statement in statements:
        for table, privilege in (await session.execute(statement, {"role": APP_ROLE})).all():
            held.setdefault(table, set()).add(privilege)
    return held


def _is_files_table(table: str) -> bool:
    return table.startswith("file_") or table in FILES_TABLES_WITHOUT_THE_PREFIX


@pytest.mark.asyncio
async def test_the_files_role_holds_files_tables_and_the_outbox_insert_and_nothing_else(
    real_session: AsyncSession,
) -> None:
    """The role's grant set is the Files tables plus one outbox INSERT.

    A table that appears here is a table every Files statement can reach, so a
    new name in this diff is a design decision, not a migration detail: either
    it is a Files table, or the statement that wanted it belongs outside the
    role window.
    """
    held = await _granted(real_session, _GRANTS, _COLUMN_GRANTS)
    foreign = {
        table: sorted(privileges)
        for table, privileges in held.items()
        if not _is_files_table(table) and table != OUTBOX
    }
    assert foreign == {}, (
        f"{APP_ROLE} was granted a table outside the Files surface: {foreign}. "
        "A statement against another subsystem's table must step out of the role "
        "for its own statement (see `as_platform` in the files route deps, or the "
        "drop-and-restore fences in alkera_core.files) instead of widening the role "
        "every Files statement runs as."
    )
    assert _is_files_table("file_nodes")
    assert held.get(OUTBOX, set()) <= {"INSERT", "SELECT"}, (
        f"{APP_ROLE} may announce its changes on {OUTBOX} and read the columns the "
        f"delta feed needs; it must not gain more: {sorted(held.get(OUTBOX, set()))}"
    )
    assert "INSERT" in held.get(OUTBOX, set())


@pytest.mark.asyncio
@pytest.mark.parametrize("table", MUST_STAY_FOREIGN)
async def test_a_table_another_subsystem_owns_is_unreadable_under_the_files_role(
    real_session: AsyncSession, table: str
) -> None:
    """Postgres actually refuses, so the grant set above is not just bookkeeping.

    This is the error a role-scoped code path gets when it reaches for one of
    these tables — ``permission denied for table <name>`` — and the reason such
    a path has to be fenced rather than granted.
    """
    assert table not in await _granted(real_session, _GRANTS)
    await real_session.rollback()
    await real_session.begin()
    await real_session.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
    with pytest.raises(ProgrammingError) as refused:
        await real_session.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))
    assert "permission denied" in str(refused.value)
    await real_session.rollback()


@pytest.mark.asyncio
async def test_stepping_out_of_the_role_is_what_makes_the_foreign_read_work(
    real_session: AsyncSession,
) -> None:
    """The fence every role-scoped path uses, end to end on one transaction.

    ``SET LOCAL ROLE NONE`` for the foreign statement and the Files role back
    after it: the read succeeds inside the window and the role is in force
    again on the other side, which is the invariant every caller of that fence
    depends on.
    """
    await real_session.rollback()
    await real_session.begin()
    await real_session.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))

    await real_session.execute(text("SET LOCAL ROLE NONE"))
    await real_session.execute(text("SELECT 1 FROM workspace_objects LIMIT 1"))
    await real_session.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))

    assert (await real_session.execute(text("SELECT current_user"))).scalar_one() == APP_ROLE
    with pytest.raises(ProgrammingError):
        await real_session.execute(text("SELECT 1 FROM workspace_objects LIMIT 1"))
    await real_session.rollback()
