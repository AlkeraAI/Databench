"""The conflicts routes: what the list carries, and what each resolution leaves behind.

Every resolve case asserts the *node set* afterwards — which head the node
points at, and whether a sibling now exists — because that is what the user
sees. Asserting only the 200 would pass even if the route promoted nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from _files_kit import NOT_FOUND, drive_root_etag, node_etag, refusal
from _live_holder import MockHolder
from _oracle import probe, quiesce_auth, work_report
from alkera_core.config import settings
from alkera_core.db.session import engine
from alkera_core.files.conflicts import conflicted_copy_name
from alkera_core.models.files.history import FileConflict, FileHistory
from alkera_core.models.files.tree import FileNode
from backend.services.org import teams as team_service
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files.conftest import FilesFixtures

pytestmark = pytest.mark.asyncio

API = "/api/v1/files"


def _named_in(name: str, who: str, started: datetime, finished: datetime) -> bool:
    """The copy's name carries the minute the server stamped it, somewhere between
    when the test started the action and when it answered: accept either end, so a
    run that crosses a minute boundary is not a failure."""
    return name in {
        conflicted_copy_name(b"report.md", who, started).decode(),
        conflicted_copy_name(b"report.md", who, finished).decode(),
    }


async def _conflicted(
    fx: FilesFixtures, session: AsyncSession, *, name: bytes = b"doc.txt"
) -> tuple[FileNode, FileConflict, uuid.UUID, uuid.UUID]:
    """A node with two divergent versions and the open conflict between them.

    Under `/Shared`, not the drive root: resolving the conflict puts the loser
    beside it under a new name, and the root takes no direct write.
    """
    node = await fx.node(name, parent=await fx.shared())
    theirs = await fx.version(node, seq=1, content_hash="aa" * 32)
    mine = await fx.version(node, seq=2, content_hash="bb" * 32)
    row = FileConflict(
        id=uuid.uuid4(),
        org_team_id=fx.org_team_id,
        node_id=node.id,
        base_version_id=None,
        theirs_version_id=theirs.id,
        mine_version_id=mine.id,
        actor=fx.actor_id,
        state="open",
    )
    session.add(row)
    await session.commit()
    return node, row, theirs.id, mine.id


async def _resolving(
    session: AsyncSession, node_id: uuid.UUID, idem: Callable[[], dict[str, str]]
) -> dict[str, str]:
    """The headers a resolve carries: a fresh key and the etag of the node the
    caller is promoting a version onto — the same precondition the promote
    itself runs under, so a node that moved meanwhile is refused rather than
    silently overwritten."""
    return {**idem(), "If-Match": await node_etag(session, node_id)}


async def test_list_shows_the_drives_open_conflicts(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
) -> None:
    node, row, theirs, mine = await _conflicted(fx, real_session)
    drive = await fx.drive()

    listed = await files_client.get(f"{API}/drives/{drive.id}/conflicts")
    assert listed.status_code == 200, listed.text
    assert listed.json()["value"] == [
        {
            "id": str(row.id),
            "nodeId": str(node.id),
            "baseVersionId": None,
            "theirsVersionId": str(theirs),
            "mineVersionId": str(mine),
            "state": "open",
            # A sync-recorded conflict: nothing the drive settled, so no copy
            # and no writers named.
            "copyNodeId": None,
            "arrivedFrom": None,
            "who": None,
            "displacedBy": None,
            "resolvedBy": None,
        }
    ]


async def test_a_resolve_names_who_resolved_it(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The resolver is written on the row and travels as ``resolvedBy``. A
    resolved row leaves the list, so it is put back in view to read it there:
    the wire carries the column, not a constant."""
    node, row, _theirs, _mine = await _conflicted(fx, real_session)
    drive = await fx.drive()
    listed = await files_client.get(f"{API}/drives/{drive.id}/conflicts")
    assert [entry["resolvedBy"] for entry in listed.json()["value"]] == [None]

    done = await files_client.post(
        f"{API}/drives/{drive.id}/conflicts/{row.id}/resolve",
        json={"keep": "mine"},
        headers=await _resolving(real_session, node.id, idem),
    )
    assert done.status_code == 200, done.text
    await real_session.execute(
        text("UPDATE file_conflicts SET state = 'open' WHERE id = :id"), {"id": row.id}
    )
    await real_session.commit()

    listed = await files_client.get(f"{API}/drives/{drive.id}/conflicts")
    assert [entry["resolvedBy"] for entry in listed.json()["value"]] == [str(fx.actor_id)]


async def test_a_resolved_conflict_leaves_the_list(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    node, row, _theirs, _mine = await _conflicted(fx, real_session)
    drive = await fx.drive()
    done = await files_client.post(
        f"{API}/drives/{drive.id}/conflicts/{row.id}/resolve",
        json={"keep": "mine"},
        headers=await _resolving(real_session, node.id, idem),
    )
    assert done.status_code == 200, done.text
    assert (await files_client.get(f"{API}/drives/{drive.id}/conflicts")).json()["value"] == []


@pytest.mark.parametrize(
    ("keep", "expect_copy"),
    [
        pytest.param("mine", False, id="keep-mine-promotes-my-version"),
        pytest.param("theirs", False, id="keep-theirs-promotes-their-version"),
        pytest.param("both", True, id="keep-both-adds-a-sibling-for-my-side"),
    ],
)
async def test_each_resolution_leaves_the_documented_node_set(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
    keep: str,
    expect_copy: bool,
) -> None:
    node, row, theirs, mine = await _conflicted(fx, real_session)
    drive = await fx.drive()
    parent_id = node.parent_id
    node_id = node.id
    node_name = node.name
    before = await _sibling_ids(real_session, parent_id)

    answer = await files_client.post(
        f"{API}/drives/{drive.id}/conflicts/{row.id}/resolve",
        json={"keep": keep},
        headers=await _resolving(real_session, node_id, idem),
    )
    assert answer.status_code == 200, answer.text
    expected_head = mine if keep == "mine" else theirs
    assert answer.json()["headVersionId"] == str(expected_head)

    real_session.expire_all()
    reloaded = await real_session.get(FileNode, node_id)
    assert reloaded is not None
    assert reloaded.head_version_id == expected_head

    after = await _sibling_ids(real_session, parent_id)
    added = after - before
    assert (len(added) == 1) is expect_copy
    if expect_copy:
        copy_id = uuid.UUID(answer.json()["copyNodeId"])
        assert added == {copy_id}
        copy = await real_session.get(FileNode, copy_id)
        assert copy is not None
        assert copy.head_version_id == mine
        assert copy.name != node_name


async def test_resolving_twice_is_a_409(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    node, row, _theirs, _mine = await _conflicted(fx, real_session)
    drive = await fx.drive()
    url = f"{API}/drives/{drive.id}/conflicts/{row.id}/resolve"
    first = await _resolving(real_session, node.id, idem)
    assert (await files_client.post(url, json={"keep": "theirs"}, headers=first)).status_code == 200
    again = await files_client.post(
        url,
        json={"keep": "theirs"},
        headers=await _resolving(real_session, node.id, idem),
    )
    assert again.status_code == 409, again.text
    assert again.json()["code"] == "files.conflict_resolved"


async def test_an_unknown_conflict_is_the_opaque_404(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    drive = await fx.drive()
    answer = await files_client.post(
        f"{API}/drives/{drive.id}/conflicts/{uuid.uuid4()}/resolve",
        json={"keep": "mine"},
        headers={**idem(), "If-Match": await drive_root_etag(real_session, drive)},
    )
    assert answer.status_code == 404
    assert refusal(answer) == NOT_FOUND


async def test_an_unknown_keep_is_422(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    node, row, _theirs, _mine = await _conflicted(fx, real_session)
    drive = await fx.drive()
    answer = await files_client.post(
        f"{API}/drives/{drive.id}/conflicts/{row.id}/resolve",
        json={"keep": "neither"},
        headers=await _resolving(real_session, node.id, idem),
    )
    assert answer.status_code == 422, answer.text


async def test_resolve_without_an_if_match_is_428(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """A resolve promotes a version onto a node, so it must say which state of
    that node it is promoting onto. Without the header nothing is promoted and
    the conflict is still open."""
    _node, row, _theirs, _mine = await _conflicted(fx, real_session)
    drive = await fx.drive()
    refused = await files_client.post(
        f"{API}/drives/{drive.id}/conflicts/{row.id}/resolve",
        json={"keep": "mine"},
        headers=idem(),
    )
    assert refused.status_code == 428, refused.text
    assert refused.json()["code"] == "files.if_match_required"
    listed = await files_client.get(f"{API}/drives/{drive.id}/conflicts")
    assert [entry["id"] for entry in listed.json()["value"]] == [str(row.id)]


async def test_resolve_without_an_idempotency_key_is_428(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
) -> None:
    _node, row, _theirs, _mine = await _conflicted(fx, real_session)
    drive = await fx.drive()
    answer = await files_client.post(
        f"{API}/drives/{drive.id}/conflicts/{row.id}/resolve", json={"keep": "mine"}
    )
    assert answer.status_code == 428, answer.text


async def _sibling_ids(session: AsyncSession, parent_id: uuid.UUID | None) -> set[uuid.UUID]:
    rows = (
        (await session.execute(select(FileNode.id).where(FileNode.parent_id == parent_id)))
        .scalars()
        .all()
    )
    return set(rows)


async def _outbox_node_rows(session: AsyncSession, node_id: uuid.UUID) -> list[str]:
    """The `file_node.changed` announcements standing for this node."""
    rows = await session.execute(
        text(
            "SELECT type FROM event_outbox"
            " WHERE entity = 'file_node' AND entity_id = :node"
            " AND type = 'file_node.changed'"
        ),
        {"node": str(node_id)},
    )
    return [row[0] for row in rows]


async def test_a_resolve_is_on_record_and_announced(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """A resolved conflict reaches the delta consumers, not just the node row.

    The feed itself cannot be read through the in-process client (the suite's
    open transaction pins the cluster xmin below every row it just wrote — see
    `test_files_delta_routes`), so the proof is the two rows the feed is built
    from: the history row and the `file_node.changed` outbox row. Without them a
    second device never learns the conflict was resolved.
    """
    node, row, _theirs, mine = await _conflicted(fx, real_session)
    drive = await fx.drive()

    answer = await files_client.post(
        f"{API}/drives/{drive.id}/conflicts/{row.id}/resolve",
        json={"keep": "mine"},
        headers=await _resolving(real_session, node.id, idem),
    )
    assert answer.status_code == 200, answer.text

    history = (
        (
            await real_session.execute(
                select(FileHistory).where(FileHistory.node_id == node.id).order_by(FileHistory.seq)
            )
        )
        .scalars()
        .all()
    )
    assert len(history) == 1, "a resolve must leave exactly one history row"
    assert history[0].after is not None
    assert history[0].after["conflict_resolution"] == "mine"
    assert history[0].after["head_version_id"] == str(mine)
    assert await _outbox_node_rows(real_session, node.id) == ["file_node.changed"]


# ---------------------------------------------------------------------------
# Conflicts the drive settles itself: a chat folder a machine holds
# ---------------------------------------------------------------------------


async def _held_chat(
    fx: FilesFixtures,
    session: AsyncSession,
    client: AsyncClient,
    idem: Callable[[], dict[str, str]],
) -> tuple[Any, uuid.UUID, uuid.UUID, MockHolder]:
    """An awake chat folder with room, its working directory holding
    ``report.md`` the box already landed, and the box holding the chat."""
    drive = await fx.drive()
    drive.quota_bytes = settings.files_quota_default_bytes
    drive.quota_nodes = settings.files_quota_default_nodes
    chat = await fx.node(b"Kickoff.alkerachat", kind="folder", parent=await fx.home())
    scratch = await fx.node(b"scratch", kind="folder", parent=chat)
    report = await fx.node(b"report.md", parent=scratch)
    await session.execute(
        text("UPDATE file_nodes SET subtype = 'chat', target_object_id = :object WHERE id = :node"),
        {"object": uuid.uuid4(), "node": chat.id},
    )
    await session.commit()
    holder = MockHolder(client, drive.id, chat.id)
    assert (await holder.take(session, idem, purpose="chat", live=True)).status_code == 200
    landed = await holder.push(session, idem, report.id, b"first from the box\n")
    assert landed.status_code == 201, landed.text
    return drive, scratch.id, report.id, holder


async def _web_save(
    client: AsyncClient,
    session: AsyncSession,
    drive_id: uuid.UUID,
    node_id: uuid.UUID,
    payload: bytes,
    idem: Callable[[], dict[str, str]],
) -> None:
    """A person saving the file in the browser: the node's current etag, no fence."""
    saved = await client.put(
        f"{API}/drives/{drive_id}/items/{node_id}/content",
        content=payload,
        headers={
            **idem(),
            "If-Match": await node_etag(session, node_id),
            "Content-Type": "application/octet-stream",
        },
    )
    assert saved.status_code == 201, saved.text


async def _display_name(session: AsyncSession, user_id: uuid.UUID) -> str:
    row = (
        await session.execute(
            text("SELECT first_name, last_name, email FROM users WHERE id = :id"), {"id": user_id}
        )
    ).one()
    return f"{row.first_name} {row.last_name}".strip() or str(row.email)


async def _auto_conflict(
    fx: FilesFixtures,
    session: AsyncSession,
    client: AsyncClient,
    idem: Callable[[], dict[str, str]],
) -> tuple[Any, uuid.UUID, uuid.UUID, dict[str, Any]]:
    """The box lands its bytes on the base it agreed before a person saved."""
    drive, _scratch, report, holder = await _held_chat(fx, session, client, idem)
    agreed = int(await node_etag(session, report))
    await _web_save(client, session, drive.id, report, b"the person's edit\n", idem)
    pushed = await holder.push(session, idem, report, b"the box's edit\n", base=agreed)
    assert pushed.status_code == 201, pushed.text
    listed = await client.get(f"{API}/drives/{drive.id}/conflicts")
    assert listed.status_code == 200, listed.text
    [entry] = listed.json()["value"]
    return drive, report, uuid.UUID(entry["copyNodeId"]), entry


async def test_a_holder_push_over_a_web_save_is_settled_not_412(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    started = datetime.now(UTC)
    drive, report, copy_id, entry = await _auto_conflict(fx, real_session, files_client, idem)
    finished = datetime.now(UTC)

    assert entry["state"] == "auto"
    assert entry["nodeId"] == str(report)
    assert entry["arrivedFrom"] == "holder"
    assert entry["who"] == "box-7", "who names the writer whose bytes kept the name"
    assert entry["displacedBy"] == await _display_name(real_session, fx.actor_id)
    copy = await files_client.get(f"{API}/drives/{drive.id}/items/{copy_id}")
    assert copy.status_code == 200, copy.text
    assert copy.json()["conflictOf"] == str(report)
    assert _named_in(copy.json()["name"], entry["displacedBy"], started, finished), copy.json()[
        "name"
    ]
    original = await files_client.get(f"{API}/drives/{drive.id}/items/{report}")
    assert original.json()["conflictOf"] is None


@pytest.mark.parametrize(
    "who",
    [
        pytest.param("web", id="a-person-with-no-fence"),
        pytest.param("stale", id="a-superseded-epoch"),
    ],
)
async def test_conflict_of_from_anyone_but_the_holder_is_refused(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
    who: str,
) -> None:
    _drive, scratch, report, holder = await _held_chat(fx, real_session, files_client, idem)
    headers = {} if who == "web" else holder.stale_fence(epoch=(holder.epoch or 1) - 1)

    refused = await holder.submit_conflict(
        idem, scratch, "report.md", b"displaced\n", conflict_of=report, headers=headers
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "files.lease_mismatch"
    sessions = (
        await real_session.execute(
            text("SELECT count(*) FROM file_upload_sessions WHERE conflict_of = :node"),
            {"node": report},
        )
    ).scalar_one()
    assert sessions == 0


async def test_a_completed_submission_answers_the_copys_id_and_name(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    drive, scratch, report, holder = await _held_chat(fx, real_session, files_client, idem)
    await _web_save(files_client, real_session, drive.id, report, b"the person's\n", idem)
    before = await node_etag(real_session, report)

    started = datetime.now(UTC)
    completed = await holder.submit_conflict(
        idem, scratch, "report.md", b"what the box had\n", conflict_of=report
    )
    finished = datetime.now(UTC)

    assert completed.status_code == 202, completed.text
    answer = completed.json()
    assert _named_in(answer["name"], "box-7", started, finished), answer["name"]
    copy = await files_client.get(f"{API}/drives/{drive.id}/items/{answer['nodeId']}")
    assert copy.status_code == 200, copy.text
    assert copy.json()["name"] == answer["name"]
    assert copy.json()["file"]["size"] == len(b"what the box had\n")
    assert await node_etag(real_session, report) == before, "the original is untouched"
    listed = (await files_client.get(f"{API}/drives/{drive.id}/conflicts")).json()["value"]
    assert [(row["id"], row["arrivedFrom"], row["copyNodeId"]) for row in listed] == [
        (answer["conflictId"], "web", answer["nodeId"])
    ]


@pytest.mark.parametrize(
    ("keep", "head", "copy_trashed"),
    [
        pytest.param("mine", b"the box's edit\n", True, id="keep-this"),
        pytest.param("theirs", b"the person's edit\n", True, id="keep-the-other"),
        pytest.param("both", b"the box's edit\n", False, id="keep-both"),
    ],
)
async def test_each_keep_on_an_auto_conflict_does_what_it_says_and_undoes(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
    keep: str,
    head: bytes,
    copy_trashed: bool,
) -> None:
    drive, report, copy_id, entry = await _auto_conflict(fx, real_session, files_client, idem)
    drive_id = drive.id

    resolved = await files_client.post(
        f"{API}/drives/{drive_id}/conflicts/{entry['id']}/resolve",
        json={"keep": keep},
        headers=await _resolving(real_session, report, idem),
    )

    assert resolved.status_code == 200, resolved.text
    head_size = (await files_client.get(f"{API}/drives/{drive_id}/items/{report}")).json()
    assert head_size["file"]["size"] == len(head)
    real_session.expire_all()
    copy = await real_session.get(FileNode, copy_id)
    assert copy is not None
    assert (copy.trashed_at is not None) is copy_trashed
    assert (await files_client.get(f"{API}/drives/{drive_id}/conflicts")).json()["value"] == []
    if copy_trashed:
        restored = await files_client.post(
            f"{API}/drives/{drive_id}/trash/{copy.trash_op_id}/restore",
            json={},
            headers={**idem(), "If-Match": "0"},
        )
        assert restored.status_code == 200, restored.text
        real_session.expire_all()
        back = await real_session.get(FileNode, copy_id)
        assert back is not None and back.trashed_at is None


async def test_an_unknown_and_a_foreign_conflict_cost_the_same(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """Another org's auto conflict answers what an invented id answers, for the
    same number of statements: the resolve authorizes a node either way."""
    other_org, other_admin = await team_service.create_org_with_admin(
        real_session,
        org_name=f"other-{uuid.uuid4().hex[:8]}",
        admin_email=f"other-{uuid.uuid4().hex[:8]}@test.dev",
        admin_first_name="Other",
        admin_last_name="Admin",
        admin_password="other-pass-12345",
    )
    await real_session.commit()
    theirs = type(fx)(real_session, other_org.id, other_admin.id)
    node = await theirs.node(b"theirs.md", parent=await theirs.shared())
    version = await theirs.version(node, seq=1, content_hash="cc" * 32)
    foreign = FileConflict(
        id=uuid.uuid4(),
        org_team_id=other_org.id,
        node_id=node.id,
        base_version_id=version.id,
        theirs_version_id=version.id,
        mine_version_id=version.id,
        actor=other_admin.id,
        state="auto",
        arrived_from="holder",
        who="their-box",
        displaced_by="Other Admin",
    )
    real_session.add(foreign)
    await real_session.commit()
    drive = await fx.drive()
    headers = {"If-Match": await drive_root_etag(real_session, drive)}
    urls = [
        f"{API}/drives/{drive.id}/conflicts/{conflict}/resolve"
        for conflict in (uuid.uuid4(), foreign.id)
    ]
    await files_client.post(urls[0], json={"keep": "mine"}, headers={**idem(), **headers})

    probes = []
    for url in urls:
        quiesce_auth()
        probes.append(
            await probe(
                files_client,
                engine.sync_engine,
                "POST",
                url,
                json={"keep": "mine"},
                headers={**idem(), **headers},
            )
        )

    assert [observed.status for observed in probes] == [404, 404]
    assert probes[0].body == probes[1].body
    assert probes[0].statements == probes[1].statements, work_report(
        probes, ("invented", "foreign")
    )
