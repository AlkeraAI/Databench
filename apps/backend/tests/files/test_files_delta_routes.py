"""The delta surface: one page, one link, and what a stale token gets.

The feed's own invariants (the watermark, latest-state-by-id) belong to the
library's tests. What these pin is the HTTP contract on top of it: the token
round-trips, a rename is ONE item rather than a delete plus a create, a caller
who cannot read a node sees a tombstone with no name in it, and an expired token
answers 410 with the three things a client needs to resync.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from alkera_core.authz.principal import ActingContext
from alkera_core.config import settings
from alkera_core.db.session import engine
from alkera_core.files.authz.decider import AccessFacts
from alkera_core.files.authz.readable import readable_ids
from alkera_core.files.clock import SystemClock
from alkera_core.files.delta import (
    DELTA_RETENTION,
    RESYNC_APPLY,
    DeltaToken,
)
from alkera_core.files.history import emit_node_changed
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.path_labels import ino_label
from alkera_core.files.repo import ID_BATCH
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import login, make_member
from tests.files._files_kit import delta_until
from tests.files._oracle import counting
from tests.files.conftest import FilesFixtures, FilesOrgFixture

#: Transaction control is not work a page-cost budget is about.
_CONTROL = frozenset({"BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE"})


def _is_work(statement: str) -> bool:
    return statement.strip().split(" ", 1)[0].upper() not in _CONTROL


pytestmark = pytest.mark.asyncio


async def _announce(fx: FilesFixtures, org: FilesOrgFixture, node: Any, *, version: int) -> None:
    """Emit the row a real mutation would, through the library's emit point."""
    ctx = ActingContext.for_user(
        user_id=org.org.admin_id, org_id=org.org.org_id, email=org.org.admin_email
    )
    async with fx.repo.transaction():
        await emit_node_changed(
            fx.repo,
            ctx,
            node_id=NodeId(node.id),
            drive_id=DriveId(node.drive_id),
            version=version,
        )
    await fx._session.commit()


async def _cursor_past(client: AsyncClient, drive_id: uuid.UUID, node_id: str) -> str:
    """The cursor a real client holds after a first sync, earned rather than minted.

    ``?token=latest`` is the boundary of what the feed may *deliver*, not of
    what has committed: a setup row whose transaction is not yet strictly older
    than the oldest write transaction in flight in this database is held back,
    and the cursor is minted underneath it — so it arrives later, in the page
    the test is about to assert on, beside the mutation. Under a loaded
    database the writer holding it back is usually somebody else's.

    So the cursor is taken from the feed only once the feed has carried
    ``node_id``, the last node the setup announced: everything the setup wrote
    took an older transaction than that one, so everything the setup wrote is
    behind the cursor too, and the next page can only be the mutation.
    """
    _, token = await delta_until(client, drive_id, carries=node_id)
    return token


def _skeleton(drive: FileDrive) -> str:
    """The drive's root: the setup's last announcement for a test that seeds rows
    rather than creating them through a route.

    ``ensure_org_drive`` writes the root and every signpost under it in ONE
    transaction, so the whole skeleton shares the root's ``xmin`` and the feed
    either withholds all of it or delivers all of it. Seeding a node afterwards
    (``fx.node``) announces nothing, so the root is the newest row the feed has
    to carry before a cursor may be taken.
    """
    assert drive.root_node_id is not None
    return str(drive.root_node_id)


async def _settle(fx: FilesFixtures) -> None:
    """End the fixture session's own transaction before the feed is asked anything.

    The test process is a writer like any other: the fixtures take an xid
    through this session and hold it until something commits or rolls back, and
    the feed is right to withhold every row at or above it — including the ones
    this test is waiting for, which would leave it waiting on itself.
    """
    await fx._session.rollback()


async def _drive_and_home(client: AsyncClient) -> tuple[str, str]:
    """The drive and the caller's home, as the drive read names them.

    A first visit ensures the home, and that ensure is itself an announced
    create — the setup's last one, and so the node a cursor has to be earned
    past (:func:`_cursor_past`) before the page under assertion can be only the
    mutation.
    """
    response = await client.get("/api/v1/files/drives")
    assert response.status_code == 200, response.text
    body = response.json()
    return body["id"], body["homeId"]


async def _made_under_home(
    client: AsyncClient, drive_id: str, home_id: str, headers: dict[str, str]
) -> str:
    """One folder created through the route, straight into the caller's home."""
    created = await client.post(
        f"/api/v1/files/drives/{drive_id}/items/{home_id}/children",
        json={"name": "under-home", "kind": "folder"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    node_id: str = created.json()["id"]
    return node_id


async def test_files_delta_lists_what_a_member_makes_under_their_home(
    files_on: None,
    client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    idem: Callable[[], dict[str, str]],
) -> None:
    """A plain member's home is granted to them alone — nothing on the root,
    nothing on ``home/`` — and it is where all their files live. The feed
    reports what they make there: readability is decided per node from the
    home's own grant, never from a grant somewhere above it. And the page is
    exactly the mutation: the home ensured before the cursor is not in it."""
    drive = await fx.drive()
    owner = await login(client, files_org.member.email, files_org.member_password)
    drive_id, home_id = await _drive_and_home(owner)
    assert drive_id == str(drive.id)

    await _settle(fx)
    cursor = await _cursor_past(owner, drive.id, home_id)
    node_id = await _made_under_home(owner, drive_id, home_id, idem())

    items, _ = await delta_until(owner, drive.id, carries=node_id, token=cursor)
    assert [item["id"] for item in items] == [node_id]
    assert items[0]["deleted"] is False
    assert items[0]["name"] == "under-home"
    assert items[0]["parentId"] == home_id


async def test_files_delta_shows_a_home_child_to_a_colleague_only_with_a_rung(
    files_on: None,
    client: AsyncClient,
    real_session: AsyncSession,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The same change read by a colleague: with no rung on the node the feed
    does not mention it at all, not even its id, and once the owner hands them
    Can view, the very same cursor answers with the row and its name."""
    drive = await fx.drive()
    colleague, password = await make_member(
        real_session, org_id=files_org.org.org_id, verified=True
    )
    assert password is not None

    owner = await login(client, files_org.member.email, files_org.member_password)
    drive_id, home_id = await _drive_and_home(owner)

    await _settle(fx)
    cursor = await _cursor_past(owner, drive.id, home_id)
    node_id = await _made_under_home(owner, drive_id, home_id, idem())

    # The colleague's first visit ensures their own home, an announced create
    # committed after the node: once the feed carries it, it has carried the
    # node's row too, so its absence is the feed's decision, not lag.
    as_colleague = await login(client, colleague.email, password)
    _, colleague_home = await _drive_and_home(as_colleague)
    withheld, _ = await delta_until(as_colleague, drive.id, carries=colleague_home, token=cursor)
    assert node_id not in [item["id"] for item in withheld]

    as_owner = await login(client, files_org.member.email, files_org.member_password)
    current = await as_owner.get(f"/api/v1/files/drives/{drive_id}/items/{node_id}")
    assert current.status_code == 200, current.text
    granted = await as_owner.post(
        f"/api/v1/files/drives/{drive_id}/items/{node_id}/permissions",
        json={"principal": {"kind": "user", "id": str(colleague.id)}, "role": "reader"},
        headers={**idem(), "If-Match": str(current.json()["etag"])},
    )
    assert granted.status_code == 201, granted.text

    as_colleague = await login(client, colleague.email, password)
    items, _ = await delta_until(as_colleague, drive.id, carries=node_id, token=cursor)
    by_id = {item["id"]: item for item in items}
    assert set(by_id) == {node_id, colleague_home}
    assert by_id[node_id]["deleted"] is False
    assert by_id[node_id]["name"] == "under-home"


async def test_files_delta_latest_returns_an_empty_page_and_a_link(
    files_on: None, files_client: AsyncClient, fx: FilesFixtures
) -> None:
    """``?token=latest`` means "start from now": nothing to apply, one link."""
    drive = await fx.drive()
    response = await files_client.get(f"/api/v1/files/drives/{drive.id}/delta?token=latest")
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["deltaLink"]
    assert "nextLink" not in body


async def test_files_delta_reports_a_changed_node_once(
    files_on: None, files_client: AsyncClient, fx: FilesFixtures, files_org: FilesOrgFixture
) -> None:
    """Two announcements for one node fold into ONE item — its latest state —
    which is what makes a rename a single item and not a delete plus a create."""
    drive = await fx.drive()
    node = await fx.node(b"notes.txt")

    await _settle(fx)
    cursor = await _cursor_past(files_client, drive.id, _skeleton(drive))
    await _announce(fx, files_org, node, version=1)
    await _announce(fx, files_org, node, version=2)

    items, _ = await delta_until(files_client, drive.id, carries=str(node.id), token=cursor)
    assert [item["id"] for item in items] == [str(node.id)]
    assert items[0]["deleted"] is False
    assert items[0]["name"] == "notes.txt"


async def test_files_delta_applying_a_page_twice_is_idempotent(
    files_on: None, files_client: AsyncClient, fx: FilesFixtures, files_org: FilesOrgFixture
) -> None:
    """A client's state after applying a page twice is the state after once."""
    drive = await fx.drive()
    node = await fx.node(b"notes.txt")

    await _settle(fx)
    cursor = await _cursor_past(files_client, drive.id, _skeleton(drive))
    await _announce(fx, files_org, node, version=1)

    page, _ = await delta_until(files_client, drive.id, carries=str(node.id), token=cursor)
    state: dict[str, Any] = {}
    for _ in range(2):
        for item in page:  # the page a client applies, applied a second time
            if item["deleted"]:
                state.pop(item["id"], None)
            else:
                state[item["id"]] = item
    assert list(state) == [str(node.id)]


async def test_files_delta_never_mentions_a_node_the_caller_cannot_read(
    files_on: None,
    client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
) -> None:
    """A member who is not on the node is not told it exists: no item, not
    even a nameless tombstone. A tombstone per private node handed every member
    the ids of everyone else's files and a signal each time one changed.

    The root, which the member may read, is announced after the secret node, so
    once the feed carries the root it has carried the secret node's row too."""
    drive = await fx.drive()
    node = await fx.node(b"secret-plans.txt")
    drive_id, root_id, secret = (
        drive.id,
        _skeleton(drive),
        SimpleNamespace(id=node.id, drive_id=node.drive_id),
    )
    root = SimpleNamespace(id=uuid.UUID(root_id), drive_id=drive_id)
    as_member = await login(client, files_org.member.email, files_org.member_password)

    await _settle(fx)
    cursor = await _cursor_past(as_member, drive_id, root_id)
    await _announce(fx, files_org, secret, version=1)
    await _announce(fx, files_org, root, version=2)

    items, _ = await delta_until(as_member, drive_id, carries=root_id, token=cursor)
    assert str(secret.id) not in [item["id"] for item in items]

    # The owner still hears about it: the omission is per caller, not a lost row.
    as_admin = await login(client, files_org.org.admin_email, files_org.org.admin_password)
    seen, _ = await delta_until(as_admin, drive_id, carries=str(secret.id), token=cursor)
    assert any(item["id"] == str(secret.id) and item["deleted"] is False for item in seen)


async def test_files_delta_an_expired_token_is_a_410_that_says_how_to_resync(
    files_on: None, files_client: AsyncClient, fx: FilesFixtures
) -> None:
    """The 410 carries all three things a client needs: what to do (``code``),
    where to start again (``Location``) and when (``Retry-After``)."""
    drive = await fx.drive()
    stale = DeltaToken(
        drive_id=DriveId(drive.id),
        outbox_id=0,
        issued_at=SystemClock().now() - DELTA_RETENTION - timedelta(days=1),
    ).encode(key=settings.effective_files_content_signing_key)

    response = await files_client.get(f"/api/v1/files/drives/{drive.id}/delta?token={stale}")
    assert response.status_code == 410
    assert response.json()["code"] == RESYNC_APPLY
    assert int(response.headers["Retry-After"]) > 0
    assert response.headers["Location"].endswith("token=latest")


async def test_files_delta_refuses_a_token_minted_for_another_drive(
    files_on: None, files_client: AsyncClient, fx: FilesFixtures
) -> None:
    """A token is signed over its drive, so pointing one at another drive is a
    422 rather than a read across."""
    drive = await fx.drive()
    foreign = DeltaToken(
        drive_id=DriveId(uuid.uuid4()), outbox_id=0, issued_at=SystemClock().now()
    ).encode(key=settings.effective_files_content_signing_key)

    response = await files_client.get(f"/api/v1/files/drives/{drive.id}/delta?token={foreign}")
    assert response.status_code == 422


async def test_files_delta_a_page_costs_the_same_number_of_statements_at_any_size(
    files_on: None, files_client: AsyncClient, fx: FilesFixtures, files_org: FilesOrgFixture
) -> None:
    """The delta budget's load-independent half: authorizing a page is a
    constant number of statements, not one decision per row.

    The route used to loop, calling ``authorized()`` per item — three queries
    and a platform decision row each, so a 10,000-row page was tens of
    thousands of statements and the budget row could not honestly be measured.
    It now hands the whole page to ``readable_ids``, which is what this counts:
    the same step the route runs, over real rows, at two page sizes a hundred
    times apart. A per-item loop fails on the small page already.

    Counted here rather than through the client because the suite's outer
    transaction pins ``pg_snapshot_xmin`` below every row the test writes, so
    an HTTP page comes back empty (see the skip note above); the authorization
    step is the part of the route whose cost this budget is about.

    The ids are resolved a batch at a time — a statement binds a bounded number
    of parameters, a page does not — so what stays constant is the cost of a
    batch: the large page pays it once per batch, not once per row.
    """
    drive = await fx.drive()
    ctx = ActingContext.for_user(
        user_id=files_org.member.id, org_id=fx.org_team_id, email=files_org.member.email
    )
    facts = AccessFacts(team_ids=frozenset({fx.org_team_id}))

    small = [NodeId((await fx.node(f"s{index}".encode())).id) for index in range(20)]
    large: list[NodeId] = []
    root = await fx._session.get(FileNode, drive.root_node_id)
    assert root is not None
    await fx._session.refresh(drive)
    ino = drive.next_ino
    rows = [
        FileNode(
            id=uuid.uuid4(),
            ino=ino + index,
            drive_id=drive.id,
            org_team_id=fx.org_team_id,
            parent_id=root.id,
            kind="file",
            name=f"b{index}".encode(),
            name_display=f"b{index}",
            name_key=f"b{index}",
            path_ids=f"{root.path_ids}.{ino_label(ino + index)}",
            depth=root.depth + 1,
        )
        for index in range(2_000)
    ]
    drive.next_ino = ino + len(rows)
    fx._session.add_all(rows)
    await fx._session.commit()
    large = [NodeId(row.id) for row in rows]

    counts: list[int] = []
    for page in (small, large):
        async with fx.repo.transaction() as scoped:
            with counting(engine.sync_engine) as seen:
                await readable_ids(scoped, ctx, page, facts=facts)
        counts.append(len([stmt for stmt in seen if _is_work(stmt)]))

    assert counts[0] <= 3, counts
    assert counts[1] <= counts[0] * math.ceil(len(large) / ID_BATCH), counts
