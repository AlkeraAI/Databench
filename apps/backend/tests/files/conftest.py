"""Fixtures every Files route test builds on.

The store is a real filesystem store under ``tmp_path`` installed through the
production seam, so a route test exercises the same code path production does
and leaves no bytes behind. The org, its members and its tree come from the
backend factories and the Files library itself — never from hand-rolled rows,
so a test that passes here is testing the routes rather than its own setup.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest
import pytest_asyncio
from _files_kit import FilesFixtures, FilesOrgFixture
from alkera_core.authz.headers import agent_headers
from alkera_core.config import settings
from alkera_core.files.clock import SystemClock
from alkera_core.files.store.scoped import FilesystemScoped
from backend.services.files.store import set_store_factory
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, app_client, login, make_member


@pytest.fixture
def files_store(tmp_path: Path) -> AsyncIterator[Path]:
    """Point the whole backend at a filesystem store under ``tmp_path``.

    Installed through :func:`set_store_factory`, the same seam production
    fills from settings, and dropped on teardown so the next test cannot
    inherit a handle rooted at a directory that no longer exists.
    """
    root = tmp_path / "files-store"
    root.mkdir()
    set_store_factory(FilesystemScoped(root, clock=SystemClock()))
    yield root
    set_store_factory(None)


@pytest.fixture
def files_on(monkeypatch: pytest.MonkeyPatch, files_store: Path) -> None:
    """Files enabled, with a store behind it. The two always travel together:
    a route that is reachable but has no store fails in a way production never
    would."""
    monkeypatch.setattr(settings, "files_enabled", True)
    monkeypatch.setattr(settings, "files_store_provider", "filesystem")
    monkeypatch.setattr(settings, "files_store_root", files_store)
    # The suite has no Temporal worker, so a route that queues an upload
    # promote, a copy or an oversized move would leave it queued forever and
    # every test past that point would be testing half a product. The flag runs
    # it through the same library core the worker calls, in the request that
    # queued it -- the single-process shape a self-hosted install also runs.
    monkeypatch.setattr(settings, "files_inline_operations", True)


@pytest.fixture
def files_off(files_on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment configured exactly like ``files_on`` — real store behind it,
    operations inline — with the kill switch turned off.

    The flag is pinned rather than inherited from whatever the ambient dotenv
    happened to load: a developer whose ``.env.local`` sets ``FILES_ENABLED``
    would otherwise run the dark cases against a live deployment, where a 200 is
    the answer and the case that is supposed to prove darkness proves nothing.
    Built on ``files_on`` so the switch is the ONLY difference between the two
    worlds — a 404 seen here cannot be a missing store or an unconfigured
    provider wearing the kill switch's clothes.
    """
    monkeypatch.setattr(settings, "files_enabled", False)


@pytest_asyncio.fixture
async def files_org(real_session: AsyncSession, org_admin: OrgWithAdmin) -> FilesOrgFixture:
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    await real_session.commit()
    return FilesOrgFixture(
        org=org_admin,
        member=member,
        member_password=password or "",
        sub_team_id=None,
    )


@pytest_asyncio.fixture
async def files_client(client: AsyncClient, files_org: FilesOrgFixture) -> AsyncClient:
    """A client logged in as the org's admin."""
    return await login(client, files_org.org.admin_email, files_org.org.admin_password)


@pytest_asyncio.fixture
async def fx(real_session: AsyncSession, files_org: FilesOrgFixture) -> FilesFixtures:
    return FilesFixtures(real_session, files_org.org.org_id, files_org.org.admin_id)


@pytest.fixture
def idem() -> Callable[[], dict[str, str]]:
    """A fresh ``Idempotency-Key`` header per call, so two requests in one test
    are never accidentally each other's replay."""

    def make() -> dict[str, str]:
        return {"Idempotency-Key": uuid.uuid4().hex}

    return make


@pytest.fixture
def agent_session_id() -> uuid.UUID:
    """The chat session an agent-context client asserts it is running in.

    A UUID because that is the only shape a chat session id can have and still
    become a grant's binding: a non-UUID id binds nothing, so a fixture that
    used one would let an unbound mint pass for a bound one.
    """
    return uuid.uuid4()


@pytest_asyncio.fixture
async def agent_files_client(
    files_client: AsyncClient, agent_session_id: uuid.UUID
) -> AsyncIterator[AsyncClient]:
    """The org admin's own session, presented as an agent running inside it.

    The credential is the same server-authenticated one ``files_client`` holds
    -- the assertion headers are the only difference, and they come from
    :func:`agent_headers` so their names are never spelled a second time. A
    separate client rather than a mutated one, so a test can hold both and know
    which request carried the assertion.
    """
    async with app_client(
        cookies=files_client.cookies, headers=agent_headers(str(agent_session_id))
    ) as agent:
        yield agent


@pytest_asyncio.fixture
async def settle(real_session: AsyncSession) -> Callable[[], Awaitable[None]]:
    """End whatever transaction the test's own setup session still holds.

    The delta feed withholds any outbox row whose transaction is not strictly
    older than the oldest write transaction still in flight *in this database* —
    that is the no-gaps rule, and it is what stops a row whose ``bigserial`` id
    was allocated early but committed late from being skipped forever. A test
    process is one such writer: ``real_session`` takes an xid the moment the
    fixtures write through it, and SQLAlchemy keeps that transaction open until
    something commits or rolls it back. If that xid was assigned BEFORE the
    change under test, the feed is correct to hold the change back, and the test
    reads an empty page for a reason that has nothing to do with the route.

    So a test that reads the feed awaits this first: every sibling session
    settles, no local xid is in flight, and the page the route returns is the
    page a client would get. It is not a retry — the feed is read once, after
    the condition it documents is actually true.
    """

    async def settled() -> None:
        await real_session.commit()

    return settled
