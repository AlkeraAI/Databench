"""Stepping out of a role and back in, on a session bound to its orgs.

Two windows change the role a bound request's statements run as:

* a narrower role (the Files role) steps out for a few statements on tables it
  holds no grant on (:func:`alkera_core.db.tenant_session.stepped_out`). On a
  bound request it steps out to the tenant role, so the content tables' policy
  still binds; ``NONE`` would be the login, which row security does not bind;
* a cross-tenant window (:func:`alkera_core.db.cross_tenant.cross_tenant_write`)
  runs its block as the login. It lasts as long as the block, even when the
  block commits part way through, and flushes the block's writes before the
  tenant role comes back.

Every case reads what the statements ran as off the database itself
(``current_user``), or what a write through the window left behind.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager

import pytest
from alkera_core.db.cross_tenant import cross_tenant_write
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.db.tenant_session import INFO_KEY, TENANT_ROLE, bind_tenant, stepped_out
from alkera_core.files import acl, delta, objects_bridge
from alkera_core.files.repo import APP_ROLE, FilesRepo
from alkera_core.models import WorkspaceObject
from backend.api.deps.files import as_platform
from backend.services.files.context import build_files_context
from backend.services.org import teams as team_service
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401 -- a fixture this module uses
from tests.conftest import OrgWithAdmin
from tests.crdt.file_world import acting, file_world
from tests.test_content_row_security import Orgs, _seed

pytestmark = [pytest.mark.asyncio]


@pytest.fixture
async def orgs(real_session: AsyncSession) -> Orgs:
    """Two orgs, each with a row in every content table."""
    made = []
    for label in ("a", "b"):
        org, admin = await team_service.create_org_with_admin(
            real_session,
            org_name=f"Windows {label} {uuid.uuid4().hex[:8]}",
            admin_email=f"windows-{label}-{uuid.uuid4().hex[:8]}@example.com",
            admin_first_name="W",
            admin_last_name=label.upper(),
            admin_password="admin-pass-12345",
        )
        await real_session.commit()
        await _seed(real_session, org.id, admin.id)
        made.append((org.id, admin.id))
    return Orgs(a=made[0][0], b=made[1][0], user_a=made[0][1], user_b=made[1][1])


@pytest.fixture
async def bound() -> AsyncIterator[AsyncSession]:
    """A session that the test binds; closed afterwards."""
    async with AsyncSessionLocal() as session:
        yield session


async def _user(session: AsyncSession) -> str:
    return str((await session.execute(text("SELECT current_user"))).scalar_one())


async def _login(session: AsyncSession) -> str:
    return str((await session.execute(text("SELECT session_user"))).scalar_one())


async def _titles(session: AsyncSession, org: uuid.UUID) -> set[str]:
    rows = await session.execute(
        text("SELECT title FROM workspace_objects WHERE org_team_id = :o"), {"o": org}
    )
    return {str(row[0]) for row in rows.all()}


# ---- the helper itself --------------------------------------------------------


@pytest.mark.parametrize(
    ("binding", "in_window", "expected"),
    [
        pytest.param(True, False, "tenant", id="a-bound-request-steps-out-to-the-tenant-role"),
        pytest.param(False, False, "login", id="an-unbound-sweep-steps-out-to-the-login"),
        pytest.param(True, True, "login", id="inside-a-cross-tenant-window-steps-out-to-the-login"),
    ],
)
async def test_stepping_out_goes_to_the_outer_role_and_puts_the_narrower_one_back(
    orgs: Orgs, bound: AsyncSession, binding: bool, in_window: bool, expected: str
) -> None:
    if binding:
        bind_tenant(bound, [orgs.b])
    login = await _login(bound)

    async def _probe() -> tuple[str, str]:
        await bound.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
        async with stepped_out(bound):
            inside = await _user(bound)
        return inside, await _user(bound)

    if in_window:
        async with cross_tenant_write(bound, reason="test.stepped_out"):
            inside, after = await _probe()
    else:
        inside, after = await _probe()
    assert inside == (TENANT_ROLE if expected == "tenant" else login)
    assert after == APP_ROLE


async def test_a_failure_inside_the_step_out_still_puts_the_narrower_role_back(
    orgs: Orgs, bound: AsyncSession
) -> None:
    bind_tenant(bound, [orgs.b])
    await bound.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
    with pytest.raises(LookupError):
        async with stepped_out(bound):
            raise LookupError("a refusal the caller handles")
    assert await _user(bound) == APP_ROLE


# ---- the Files windows --------------------------------------------------------

Window = Callable[[FilesRepo], AbstractAsyncContextManager[None]]


def _platform(repo: FilesRepo) -> AbstractAsyncContextManager[None]:
    return as_platform(repo.session)


#: Every place Files code steps out of its role inside a request.
WINDOWS = [
    pytest.param(acl.as_login_role, id="acl-reads-users-and-teams"),
    pytest.param(objects_bridge._writing_a_grant, id="a-grant-insert"),
    pytest.param(objects_bridge._recording_the_reference, id="a-chat-reference-write"),
    pytest.param(delta._reading_the_outbox, id="the-change-feed-outbox-join"),
    pytest.param(_platform, id="the-route-decision-window"),
]


@pytest.fixture
async def files_session(real_session: AsyncSession) -> AsyncIterator[AsyncSession]:
    yield real_session
    real_session.info.pop(INFO_KEY, None)
    await real_session.rollback()


@pytest.mark.usefixtures("files_on")
@pytest.mark.parametrize("window", WINDOWS)
@pytest.mark.parametrize("binding", [True, False], ids=["a-bound-request", "an-unbound-sweep"])
async def test_a_files_window_runs_as_the_role_its_request_was_in(
    files_session: AsyncSession, org_admin: OrgWithAdmin, window: Window, binding: bool
) -> None:
    fw = await file_world(files_session, org_admin)
    login = await _login(files_session)
    if binding:
        bind_tenant(files_session, [fw.ref.org_id])
    context = await build_files_context(files_session, acting(fw.world.owner))
    async with context.repo.transaction():
        async with window(context.repo):
            inside = await _user(files_session)
        after = await _user(files_session)
    assert inside == (TENANT_ROLE if binding else login)
    assert after == APP_ROLE


# ---- the cross-tenant window --------------------------------------------------


async def test_a_window_survives_a_commit_inside_its_block(orgs: Orgs, bound: AsyncSession) -> None:
    """A platform handler that commits part way through (to make a write
    visible to a worker) still reads every org afterwards: the transaction it
    begins inside the window starts as the login, not as its caller's org."""
    bind_tenant(bound, [orgs.b])
    await bound.execute(text("SELECT 1"))
    login = await _login(bound)
    async with cross_tenant_write(bound, reason="test.commit_inside"):
        before = await _titles(bound, orgs.a)
        await bound.commit()
        after_commit = await _titles(bound, orgs.a)
        who = await _user(bound)
    outside = await _user(bound)
    still_bound = await _titles(bound, orgs.a)
    assert before, "the seed gave org a rows"
    assert after_commit == before
    assert who == login
    assert outside == TENANT_ROLE
    assert still_bound == set()


async def test_an_inner_window_that_commits_leaves_the_outer_one_standing(
    orgs: Orgs, bound: AsyncSession
) -> None:
    bind_tenant(bound, [orgs.b])
    login = await _login(bound)
    async with cross_tenant_write(bound, reason="test.outer"):
        async with cross_tenant_write(bound, reason="test.inner"):
            await bound.commit()
        between = await _user(bound)
        seen = await _titles(bound, orgs.a)
    after = await _user(bound)
    assert between == login
    assert seen
    assert after == TENANT_ROLE


async def test_a_window_that_commits_and_raises_leaves_the_next_transaction_bound(
    orgs: Orgs, bound: AsyncSession
) -> None:
    bind_tenant(bound, [orgs.b])
    with pytest.raises(LookupError):
        async with cross_tenant_write(bound, reason="test.raises"):
            await bound.commit()
            await bound.execute(text("SELECT 1"))
            raise LookupError("a refusal after the commit")
    await bound.rollback()
    assert await _user(bound) == TENANT_ROLE
    assert await _titles(bound, orgs.a) == set()


async def test_a_window_flushes_another_orgs_rows_before_the_role_comes_back(
    orgs: Orgs, bound: AsyncSession
) -> None:
    """A block that changes another org's row and leaves it dirty: the write
    lands with the caller's commit rather than flushing then under the tenant
    role, whose policy would hide the row (a stale update) or refuse it."""
    bind_tenant(bound, [orgs.b])
    marker = f"flushed-{uuid.uuid4().hex[:8]}"
    async with cross_tenant_write(bound, reason="test.flush") as session:
        row = (
            await session.execute(
                select(WorkspaceObject).where(WorkspaceObject.org_team_id == orgs.a).limit(1)
            )
        ).scalar_one()
        row.title = marker
    await bound.commit()
    async with AsyncSessionLocal() as login:
        written = await login.scalar(
            text("SELECT count(*) FROM workspace_objects WHERE title = :t"), {"t": marker}
        )
    assert written == 1
