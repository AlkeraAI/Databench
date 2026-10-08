"""A replayed Files write reads the drive it was built with, and succeeds.

A mutating Files route replays itself when Postgres answers ``40001`` /
``40P01``: the losing attempt is rolled back and the handler runs again on the
same session. The drive on the request's ``FilesContext`` is loaded before the
first attempt, so that rollback expires it — and a handler that then reads one
of its columns (an agent's leased subtree is the drive root, an upload names
the drive it opens on) asks an async session for a synchronous refresh. That
is ``MissingGreenlet``: every replay of a content PUT from a box answered 500,
so the retry that exists to absorb a deadlock never absorbed one.

Every case here loses its first attempt for real — the injected failure runs
the real effect, aborts the transaction with a statement Postgres refuses, and
only then raises the driver error the ladder reads — and asserts what the
caller sees: the status the route promises, the bytes or rows that landed, and
that the replay applied the effect once.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
import pytest_asyncio
from _files_kit import FilesFixtures, FilesOrgFixture, node_etag
from alkera_core.authz.principal import ActingContext
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files import stars
from alkera_core.files.content import ContentService
from alkera_core.files.namespace import Namespace
from alkera_core.files.uploads import UploadService
from alkera_core.models.files.tree import FileNode
from asyncpg.exceptions import DeadlockDetectedError, SerializationError
from backend.services.files.context import build_files_context, restart_attempt
from httpx import AsyncClient
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/files"
CONTENT_HOST = "files.localhost:8000"
CONTENT_ORIGIN = f"http://{CONTENT_HOST}"
PAYLOAD = b"pushed from the box after a deadlock\n"

VERDICTS = [
    pytest.param(DeadlockDetectedError, id="deadlock-40P01"),
    pytest.param(SerializationError, id="serialization-40001"),
]


class LosesFirstAttempt:
    """Run the real call, then lose the attempt the way a deadlock loses it.

    The first call does the real work — reading the request body, writing its
    rows inside the attempt's transaction — so the replay has to start over
    from a session Postgres really aborted, not from one a mock merely claimed
    was. Every later call is the real call alone.
    """

    def __init__(
        self,
        real: Callable[..., Awaitable[Any]],
        session_of: Callable[..., AsyncSession],
        verdict: type[BaseException],
    ) -> None:
        self._real = real
        self._session_of = session_of
        self._verdict = verdict
        self.calls = 0

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        answer = await self._real(*args, **kwargs)
        if self.calls == 1:
            session = self._session_of(*args)
            try:
                await session.execute(text("SELECT 1 / 0"))
            except DBAPIError:
                pass
            raise DBAPIError("UPDATE file_drives", {}, self._verdict("collided"))
        return answer

    def as_method(self) -> Callable[..., Awaitable[Any]]:
        """This wrapper as a function a class attribute can hold, so the
        instance it is looked up on is bound as the first argument."""

        async def bound(*args: Any, **kwargs: Any) -> Any:
            return await self(*args, **kwargs)

        return bound


@pytest_asyncio.fixture
async def roomy(fx: FilesFixtures, real_session: AsyncSession) -> FileNode:
    """``/Shared`` on a drive with room in it (a new drive is born with none)."""
    drive = await fx.drive()
    await real_session.execute(
        text("UPDATE file_drives SET quota_bytes = :b, quota_nodes = :n WHERE id = :id"),
        {
            "b": settings.files_quota_default_bytes,
            "n": settings.files_quota_default_nodes,
            "id": drive.id,
        },
    )
    await real_session.commit()
    return await fx.shared()


async def _count(session: AsyncSession, statement: str, **params: Any) -> int:
    await session.commit()
    return int((await session.execute(text(statement), params)).scalar_one())


@pytest.mark.parametrize("verdict", VERDICTS)
async def test_an_agent_content_put_that_lost_its_first_attempt_lands_on_the_replay(
    verdict: type[BaseException],
    files_on: None,
    files_client: AsyncClient,
    agent_files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    roomy: FileNode,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED before the fix: the replay reads the agent's leased subtree off the
    expired drive and answers 500 with ``MissingGreenlet``, so no version lands
    however the client retries. The first attempt is lost AFTER the body was
    read, so the replay also proves the bytes are there to read again."""
    monkeypatch.setattr(settings, "files_content_base_url", CONTENT_ORIGIN)
    drive = await fx.drive()
    node = await fx.node(b"pushed.txt", parent=roomy)
    lose = LosesFirstAttempt(
        ContentService.put_version, lambda service, *_: service._repo.session, verdict
    )
    monkeypatch.setattr(ContentService, "put_version", lose.as_method())
    headers = {
        "Idempotency-Key": uuid.uuid4().hex,
        "If-Match": f'"{node.etag}"',
        "Content-Type": "application/octet-stream",
        "Content-Length": str(len(PAYLOAD)),
    }
    url = f"{BASE}/drives/{drive.id}/items/{node.id}/content"
    replace = {"conflictBehavior": "replace"}
    versions = "SELECT count(*) FROM file_versions WHERE node_id = :n"
    keys = "SELECT count(*) FROM file_idempotency_keys WHERE org_team_id = :o"

    written = await agent_files_client.put(url, content=PAYLOAD, params=replace, headers=headers)

    assert written.status_code == 201, written.text
    assert lose.calls == 2, "the wrapper did not replay the attempt that lost"
    assert await _count(real_session, versions, n=node.id) == 1, "the lost version survived"
    minted = await files_client.get(url)
    assert minted.status_code == 302, minted.text
    served = await files_client.get(
        minted.headers["location"].removeprefix(CONTENT_ORIGIN), headers={"Host": CONTENT_HOST}
    )
    assert served.status_code == 200, served.text
    assert served.content == PAYLOAD

    spent = await _count(real_session, keys, o=files_org.org.org_id)
    replayed = await agent_files_client.put(url, content=PAYLOAD, params=replace, headers=headers)

    assert replayed.status_code == 201
    assert replayed.content == written.content, "the stored answer did not win"
    assert lose.calls == 2, "a repeat of the key ran the effect again"
    assert await _count(real_session, versions, n=node.id) == 1
    assert await _count(real_session, keys, o=files_org.org.org_id) == spent


@pytest.mark.parametrize("verdict", VERDICTS)
async def test_an_agent_folder_create_that_lost_its_first_attempt_lands_on_the_replay(
    verdict: type[BaseException],
    files_on: None,
    agent_files_client: AsyncClient,
    fx: FilesFixtures,
    roomy: FileNode,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tree write through ``idempotent_route``: RED before the fix for the
    same read of the drive root on the replay."""
    drive = await fx.drive()
    lose = LosesFirstAttempt(Namespace.create, lambda ns, *_: ns._repo.session, verdict)
    monkeypatch.setattr(Namespace, "create", lose.as_method())

    created = await agent_files_client.post(
        f"{BASE}/drives/{drive.id}/items/{roomy.id}/children",
        json={"name": "after-the-deadlock", "kind": "folder"},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )

    assert created.status_code == 201, created.text
    assert lose.calls == 2
    landed = await _count(
        real_session,
        "SELECT count(*) FROM file_nodes WHERE parent_id = :p AND name = :n",
        p=roomy.id,
        n=b"after-the-deadlock",
    )
    assert landed == 1, "the lost attempt's folder survived, or the replay made none"


@pytest.mark.parametrize("verdict", VERDICTS)
async def test_an_agent_star_that_lost_its_first_attempt_stars_on_the_replay(
    verdict: type[BaseException],
    files_on: None,
    agent_files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED before the fix; the person's twin in ``test_files_db_retry_routes``
    never read the drive on its replay, which is how the defect stayed hidden."""
    drive = await fx.drive()
    node = await fx.node(b"starred.txt")
    lose = LosesFirstAttempt(stars.star, lambda repo, *_: repo.session, verdict)
    monkeypatch.setattr(stars, "star", lose)

    answered = await agent_files_client.put(
        f"{BASE}/drives/{drive.id}/items/{node.id}/star",
        headers={
            "Idempotency-Key": uuid.uuid4().hex,
            "If-Match": await node_etag(real_session, node.id),
        },
    )

    assert answered.status_code == 200, answered.text
    assert answered.json()["starred"] is True
    assert lose.calls == 2
    starred = "SELECT count(*) FROM file_stars WHERE node_id = :n"
    assert await _count(real_session, starred, n=node.id) == 1


@pytest.mark.parametrize("verdict", VERDICTS)
async def test_an_upload_open_that_lost_its_first_attempt_opens_on_the_replay(
    verdict: type[BaseException],
    files_on: None,
    files_client: AsyncClient,
    roomy: FileNode,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The upload family runs its own ladder, and its replay names the drive
    the session opens on: RED before the fix with ``MissingGreenlet``."""
    lose = LosesFirstAttempt(UploadService.open, lambda service, *_: service._repo.session, verdict)
    monkeypatch.setattr(UploadService, "open", lose.as_method())
    key = uuid.uuid4().hex
    body = {"declaredSize": 24, "name": "report.txt", "parentId": str(roomy.id)}

    opened = await files_client.post(f"{BASE}/uploads", json=body, headers={"Idempotency-Key": key})

    assert opened.status_code == 201, opened.text
    assert lose.calls == 2, "the upload ladder did not replay the attempt that lost"
    await real_session.commit()
    sessions = await real_session.execute(
        text("SELECT id FROM file_upload_sessions WHERE parent_id = :p"), {"p": roomy.id}
    )
    assert [str(row[0]) for row in sessions.all()] == [opened.json()["uploadId"]], (
        "the lost attempt's session survived, or the replay opened none"
    )

    replayed = await files_client.post(
        f"{BASE}/uploads", json=body, headers={"Idempotency-Key": key}
    )
    assert replayed.status_code == 201
    assert replayed.json()["uploadId"] == opened.json()["uploadId"]
    assert lose.calls == 2


async def test_restarting_an_attempt_leaves_the_same_drive_loaded(
    files_on: None, files_org: FilesOrgFixture
) -> None:
    """The mechanism itself: after the restart every column of the context's
    drive is loaded, so a synchronous read performs no IO — and it is the SAME
    instance, so whatever else closed over it (the ceilings resolver) sees the
    reloaded row too. A bare rollback leaves every column expired."""
    principal = ActingContext.for_user(
        user_id=files_org.org.admin_id,
        org_id=files_org.org.org_id,
        email=files_org.org.admin_email,
    )
    async with AsyncSessionLocal() as session:
        files = await build_files_context(session, principal)
        drive = files.drive
        root = drive.root_node_id

        # The attempt that loses is always inside a transaction.
        await session.execute(text("SELECT 1"))
        await session.rollback()
        assert sa_inspect(drive).expired_attributes, "a rollback no longer expires the drive"

        await restart_attempt(files)

        assert files.drive is drive
        assert not sa_inspect(drive).expired_attributes
        assert drive.root_node_id == root
        await session.rollback()
