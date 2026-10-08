"""The holder's tree report through the real app, driven by the holder's double.

A box holding a folder reports its tree ahead of the bytes; readers see the rows
at once, each saying whether the drive has its bytes. Everything below goes
through ``httpx`` against the mounted app with Postgres behind it -- the same
request the box sends, the same reads a browser makes -- so what is asserted is
the status contract, the rows a reader is shown and the frames the outbox
carries.
"""

from __future__ import annotations

import gzip
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from _files_kit import NOT_FOUND, refusal
from _live_holder import MockHolder
from alkera_core.authz.headers import agent_headers
from alkera_core.config import settings
from alkera_core.db.session import engine
from alkera_core.files.lease_tree import MAX_NAMED_FOLDERS
from blake3 import blake3
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests._suite_app import app as fastapi_app
from tests.conftest import app_client, login
from tests.files._boxes import registered_box as _registered_box

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

PREFIX = "/api/v1/files"


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------


async def _room(fx: Any) -> Any:
    drive = await fx.drive()
    drive.quota_bytes = settings.files_quota_default_bytes
    drive.quota_nodes = settings.files_quota_default_nodes
    await fx._session.commit()
    return drive


async def _held_project(
    files_client: AsyncClient, fx: Any, session: AsyncSession, idem: Any
) -> tuple[Any, Any, MockHolder]:
    """``/Shared/project`` taken live by the org admin's own mount."""
    drive = await _room(fx)
    project = await fx.node(b"project", kind="folder", parent=await fx.shared())
    holder = MockHolder(files_client, drive.id, project.id, machine="ana-mbp")
    taken = await holder.take(session, idem, purpose="mount", live=True)
    assert taken.status_code == 200, taken.text
    return drive, project, holder


def _file(
    path: str, *, size: int = 5, mtime: int = 1_758_625_000_123_456_789, **extra: Any
) -> dict[str, Any]:
    return {"op": "upsert", "path": path, "kind": "file", "size": size, "mtime_ns": mtime, **extra}


async def _rows(session: AsyncSession, drive_id: uuid.UUID) -> dict[bytes, Any]:
    rows = (
        await session.execute(
            text(
                "SELECT id, parent_id, name, kind, head_version_id, holder_size, holder_seq, "
                "trashed_at FROM file_nodes WHERE drive_id = :drive AND trashed_at IS NULL"
            ),
            {"drive": drive_id},
        )
    ).all()
    await session.commit()
    return {bytes(row.name): row for row in rows}


async def _outbox(session: AsyncSession, org_id: uuid.UUID, after: int = 0) -> list[Any]:
    rows = (
        await session.execute(
            text(
                "SELECT id, type, entity_id, payload FROM event_outbox WHERE org_id = :org "
                "AND id > :after AND type IN ('file_node.changed', 'file_lease.changed') "
                "ORDER BY id"
            ),
            {"org": org_id, "after": after},
        )
    ).all()
    await session.commit()
    return list(rows)


async def _last_outbox_id(session: AsyncSession) -> int:
    value = (
        await session.execute(text("SELECT coalesce(max(id), 0) FROM event_outbox"))
    ).scalar_one()
    await session.commit()
    return int(value)


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "compress", [pytest.param(False, id="plain"), pytest.param(True, id="gzip")]
)
async def test_a_report_mints_the_rows_it_names(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any, compress: bool
) -> None:
    drive, project, holder = await _held_project(files_client, fx, real_session, idem)
    answer = await holder.tree(
        [
            {"op": "upsert", "path": "src", "kind": "dir"},
            _file("src/a.py", size=23, mode=420, hash="b3:" + "ab" * 32),
            _file("papers/readme.md"),
        ],
        compress=compress,
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["applied"] == 3 and body["landing_count"] == 2 and body["live_seq"] >= 1
    rows = await _rows(real_session, drive.id)
    assert rows[b"a.py"].parent_id == rows[b"src"].id
    assert rows[b"src"].parent_id == project.id
    assert rows[b"papers"].kind == "folder"
    assert (rows[b"a.py"].head_version_id, rows[b"a.py"].holder_size) == (None, 23)


@pytest.mark.parametrize(
    ("route", "plain", "field", "reason"),
    [
        pytest.param(
            "tree",
            b'{"batch_id": "BATCH", "entries": [{"op": "upsert", "path": "a.txt", '
            b'"kind": "file\\u0000", "size": 5, "mtime_ns": 1}]}',
            "body.entries[0].kind",
            "null_character",
            id="tree-nul-in-a-field-files-does-not-answer-for",
        ),
        pytest.param(
            "tree",
            b'{"batch_id": "BATCH", "entries": [{"op": "upsert\\udc80", "path": "a.txt", '
            b'"kind": "file", "size": 5, "mtime_ns": 1}]}',
            "body.entries[0].op",
            "unpaired_surrogate",
            id="tree-lone-surrogate-in-a-field-files-does-not-answer-for",
        ),
        pytest.param(
            "tree/digests",
            b'{"paths": [""], "note": "a\\u0000b"}',
            "body.note",
            "null_character",
            id="digests-nul-in-a-field-files-does-not-answer-for",
        ),
    ],
)
async def test_a_gzipped_report_earns_the_refusal_its_plain_body_earns(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    route: str,
    plain: bytes,
    field: str,
    reason: str,
) -> None:
    """Both routes that take a gzipped body are scanned on what it decodes
    to: the same unstorable text refused plain is refused gzipped, with the
    same code and field, and nothing is written either way."""
    drive, _project, holder = await _held_project(files_client, fx, real_session, idem)
    before = await _rows(real_session, drive.id)
    body = plain.replace(b"BATCH", str(uuid.uuid4()).encode())
    sent = {**holder.fence, "Content-Type": "application/json"}
    url = f"{holder.item}/lease/{route}"
    refused_plain = await files_client.post(url, content=body, headers=sent)
    refused_gzip = await files_client.post(
        url, content=gzip.compress(body), headers={**sent, "Content-Encoding": "gzip"}
    )
    for refused in (refused_plain, refused_gzip):
        assert refused.status_code == 422, refused.text
        error = refused.json()["error"]
        assert error["code"] == "unstorable_text"
        assert error["details"] == {"field": field, "reason": reason}
    assert await _rows(real_session, drive.id) == before


@pytest.mark.parametrize(
    ("body", "encoding"),
    [
        pytest.param(b"not gzip at all", "gzip", id="garbage-under-gzip"),
        pytest.param(gzip.compress(b'{"batch_id": "x"}')[:-6], "gzip", id="a-truncated-member"),
        pytest.param(gzip.compress(b"{}") + b"trailing", "gzip", id="bytes-after-the-member"),
        pytest.param(b"{}", "br", id="an-encoding-the-route-does-not-speak"),
    ],
)
async def test_a_body_that_does_not_decode_is_a_400(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    body: bytes,
    encoding: str,
) -> None:
    drive, _project, holder = await _held_project(files_client, fx, real_session, idem)
    before = await _rows(real_session, drive.id)
    refused = await files_client.post(
        f"{holder.item}/lease/tree",
        content=body,
        headers={**holder.fence, "Content-Type": "application/json", "Content-Encoding": encoding},
    )
    assert refused.status_code == 400, refused.text
    assert refused.json()["code"] == "files.bad_encoding"
    assert await _rows(real_session, drive.id) == before


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param("too-many-entries", id="one-entry-past-the-ceiling"),
        pytest.param("gzip-bomb", id="a-small-body-that-decodes-past-the-cap"),
    ],
)
async def test_a_report_past_the_ceilings_is_a_413_the_holder_splits_on(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any, shape: str
) -> None:
    drive, _project, holder = await _held_project(files_client, fx, real_session, idem)
    before = await _rows(real_session, drive.id)
    if shape == "too-many-entries":
        refused = await holder.tree([_file(f"f{i}.txt") for i in range(2001)], compress=True)
    else:
        padded = json.dumps(
            {"batch_id": str(uuid.uuid4()), "entries": [_file("a.txt")], "pad": " " * (5 << 20)}
        ).encode()
        refused = await files_client.post(
            f"{holder.item}/lease/tree",
            content=gzip.compress(padded),
            headers={
                **holder.fence,
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
            },
        )
    assert refused.status_code == 413, refused.text
    assert refused.json()["code"] == "files.batch_too_large"
    assert await _rows(real_session, drive.id) == before


async def test_a_refused_path_is_named_back_to_the_holder(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    drive, _project, holder = await _held_project(files_client, fx, real_session, idem)
    before = await _rows(real_session, drive.id)
    refused = await holder.tree([_file("fine.txt"), _file("../escape.txt"), _file("x.alkerachat")])
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "files.lease_mismatch"
    assert refused.json()["detail"] == {
        "paths": ["../escape.txt", "x.alkerachat"],
        "indexes": [1, 2],
    }
    assert await _rows(real_session, drive.id) == before


async def test_a_replayed_batch_is_answered_again_and_a_reused_id_is_refused(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    drive, _project, holder = await _held_project(files_client, fx, real_session, idem)
    batch_id = uuid.uuid4()
    first = await holder.tree([_file("a.txt")], batch_id=batch_id)
    rows = await _rows(real_session, drive.id)
    again = await holder.tree([_file("a.txt")], batch_id=batch_id)
    assert (again.status_code, again.json()) == (200, first.json())
    assert await _rows(real_session, drive.id) == rows
    reused = await holder.tree([_file("b.txt")], batch_id=batch_id)
    assert reused.status_code == 422, reused.text
    assert reused.json()["code"] == "files.idempotency_mismatch"


# ---------------------------------------------------------------------------
# Only the holder
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def box(real_session: AsyncSession, files_org: Any) -> AsyncIterator[tuple[AsyncClient, str]]:
    """A provisioned box of this org speaking as its proven machine."""
    token, machine_id = await _registered_box(
        real_session,
        user_id=files_org.org.admin_id,
        email=files_org.org.admin_email,
        org_id=files_org.org.org_id,
    )
    async with AsyncClient(
        transport=ASGITransport(app=fastapi_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}", **agent_headers(machine_id)},
    ) as client:
        yield client, machine_id


@pytest.mark.parametrize(
    "fence",
    [
        pytest.param("the-boxs-own-pair", id="a-person-replaying-the-boxs-fence"),
        pytest.param("none", id="no-fence-at-all"),
    ],
)
async def test_a_caller_who_is_not_the_holder_is_refused_with_409(
    box: tuple[AsyncClient, str],
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    fence: str,
) -> None:
    """The box holds the folder; the org admin -- who may read and write all of
    it -- is still not its holder, and the pair the box writes under is not a
    password anybody may present."""
    client, machine_id = box
    drive = await _room(fx)
    project = await fx.node(b"project", kind="folder", parent=await fx.shared())
    holder = MockHolder(client, drive.id, project.id, machine=machine_id)
    assert (await holder.take(real_session, idem, purpose="mount", live=True)).status_code == 200
    assert (await holder.tree([_file("mine.txt")])).status_code == 200
    before = await _rows(real_session, drive.id)

    impostor = MockHolder(files_client, drive.id, project.id)
    refused = await impostor.tree(
        [_file("theirs.txt")], headers=holder.fence if fence != "none" else {}
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "files.lease_fenced"
    assert await _rows(real_session, drive.id) == before


# ---------------------------------------------------------------------------
# What readers see
# ---------------------------------------------------------------------------


async def test_a_minted_row_reads_as_unlanded_on_item_children_and_lookup(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any, files_org: Any
) -> None:
    drive, project, holder = await _held_project(files_client, fx, real_session, idem)
    assert (
        await holder.tree([_file("src/a.py", size=23, mtime=2_000_000_000_000_000_000)])
    ).status_code == 200
    rows = await _rows(real_session, drive.id)
    minted, src = rows[b"a.py"], rows[b"src"]

    async with app_client() as reader:
        await login(reader, files_org.member.email, files_org.member_password)
        item = await reader.get(f"{PREFIX}/drives/{drive.id}/items/{minted.id}")
        children = await reader.get(f"{PREFIX}/drives/{drive.id}/items/{src.id}/children")
        lookup = await reader.post(
            f"{PREFIX}/drives/{drive.id}/items/lookup", json={"ids": [str(minted.id)]}
        )
    assert item.status_code == children.status_code == lookup.status_code == 200, (
        item.text,
        children.text,
        lookup.text,
    )
    for label, row in (
        ("item", item.json()),
        ("children", children.json()["value"][0]),
        ("lookup", lookup.json()["value"][0]),
    ):
        assert row["id"] == str(minted.id), label
        assert row["live"]["content"] == "unlanded", label
        assert row["live"]["holder_size"] == 23, label
        assert row["live"]["holder_mtime"].startswith("2033-05-18T03:33:20"), label
        assert row["live"]["state"] is None, label
        assert row["lease"]["node_id"] == str(project.id), label
        assert row["lease"]["served"] == "live", label
        assert row["lease"]["landing_count"] == 1, label
        # The content tag moves with the report; the etag a writer races on does not.
        assert row["ctag"] != row["etag"], label
        assert row["ctag"].startswith(f"{row['etag']}.2000000000000000000."), label


async def test_a_holder_gone_quiet_reads_offline_and_a_lease_gone_reads_unsynced(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    drive, project, holder = await _held_project(files_client, fx, real_session, idem)
    assert (await holder.tree([_file("a.txt")])).status_code == 200
    minted = (await _rows(real_session, drive.id))[b"a.txt"]
    url = f"{PREFIX}/drives/{drive.id}/items/{minted.id}"

    await real_session.execute(
        text("UPDATE file_leases SET heartbeat_at = now() - interval '1 hour' WHERE node_id = :n"),
        {"n": project.id},
    )
    await real_session.commit()
    quiet = (await files_client.get(url)).json()
    assert quiet["lease"]["served"] == "offline"
    assert quiet["live"]["content"] == "unlanded"

    await real_session.execute(
        text("UPDATE file_leases SET released_at = now() WHERE node_id = :n"), {"n": project.id}
    )
    await real_session.commit()
    gone = (await files_client.get(url)).json()
    assert gone["lease"] is None
    assert gone["live"]["content"] == "unsynced"


async def test_a_minted_row_is_hidden_from_another_org(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    from backend.services.org import teams as team_service

    drive, _project, holder = await _held_project(files_client, fx, real_session, idem)
    assert (await holder.tree([_file("secret-plan.md")])).status_code == 200
    minted = (await _rows(real_session, drive.id))[b"secret-plan.md"]
    email = f"stranger-{uuid.uuid4().hex[:8]}@test.dev"
    await team_service.create_org_with_admin(
        real_session,
        org_name=f"stranger-{uuid.uuid4().hex[:6]}",
        admin_email=email,
        admin_first_name="Stranger",
        admin_last_name="Admin",
        admin_password="stranger-pass-12345",
    )
    await real_session.commit()
    async with app_client() as stranger:
        await login(stranger, email, "stranger-pass-12345")
        answer = await stranger.get(f"{PREFIX}/drives/{drive.id}/items/{minted.id}")
    assert (answer.status_code, refusal(answer)) == (404, NOT_FOUND)
    assert "secret-plan" not in answer.text


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------


async def test_a_report_announces_each_folder_it_touched_once(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any, files_org: Any
) -> None:
    drive, project, holder = await _held_project(files_client, fx, real_session, idem)
    mark = await _last_outbox_id(real_session)
    answer = await holder.tree([_file("a/one.txt"), _file("a/two.txt"), _file("b/three.txt")])
    assert answer.status_code == 200, answer.text
    rows = await _rows(real_session, drive.id)
    frames = await _outbox(real_session, files_org.org.org_id, mark)

    named = [row for row in frames if row.type == "file_node.changed"]
    assert sorted(row.entity_id for row in named) == sorted(
        str(folder) for folder in (project.id, rows[b"a"].id, rows[b"b"].id)
    )
    for row in named:
        assert row.payload["reason"] == "live_batch"
        assert row.payload["parent_id"] == row.payload["node_id"] == row.entity_id
        assert row.payload["drive_id"] == str(drive.id)
    (lease,) = [row for row in frames if row.type == "file_lease.changed"]
    assert lease.payload["landing_count"] == 3
    assert lease.payload["live_seq"] == answer.json()["live_seq"]
    assert "subtree" not in lease.payload


async def test_a_report_touching_more_folders_than_it_may_name_names_none(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any, files_org: Any
) -> None:
    _drive, _project, holder = await _held_project(files_client, fx, real_session, idem)
    mark = await _last_outbox_id(real_session)
    answer = await holder.tree([_file(f"d{i}/f.txt") for i in range(MAX_NAMED_FOLDERS)])
    assert answer.status_code == 200, answer.text
    frames = await _outbox(real_session, files_org.org.org_id, mark)
    assert [row.type for row in frames] == ["file_lease.changed"]
    assert frames[0].payload["subtree"] is True


# ---------------------------------------------------------------------------
# The bytes land
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("report", "content", "facet_kept"),
    [
        pytest.param("same-size", None, False, id="a-version-of-the-reported-size"),
        pytest.param("same-hash", None, False, id="a-version-with-the-reported-hash"),
        pytest.param("other-hash", "behind", True, id="the-disk-moved-on-while-it-uploaded"),
        pytest.param("other-size", "behind", True, id="a-version-of-another-size"),
    ],
)
async def test_a_landed_version_equal_to_the_report_settles_it(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    files_org: Any,
    report: str,
    content: str | None,
    facet_kept: bool,
) -> None:
    payload = b"hello, world"
    drive, _project, holder = await _held_project(files_client, fx, real_session, idem)
    extra: dict[str, Any] = {
        "same-size": {"size": len(payload)},
        "same-hash": {"size": len(payload), "hash": "b3:" + blake3(payload).hexdigest()},
        "other-hash": {"size": len(payload), "hash": "b3:" + "cd" * 32},
        "other-size": {"size": len(payload) + 1},
    }[report]
    assert (await holder.tree([_file("note.txt", **extra)])).status_code == 200
    minted = (await _rows(real_session, drive.id))[b"note.txt"]
    mark = await _last_outbox_id(real_session)

    landed = await holder.push(real_session, idem, minted.id, payload)
    assert landed.status_code in (200, 201), landed.text

    row = (await _rows(real_session, drive.id))[b"note.txt"]
    assert (row.holder_size is not None) is facet_kept
    item = (await files_client.get(f"{PREFIX}/drives/{drive.id}/items/{minted.id}")).json()
    assert (item["live"] or {}).get("content") == content
    frames = await _outbox(real_session, files_org.org.org_id, mark)
    saved = [
        row for row in frames if row.type == "file_node.changed" and row.entity_id == str(minted.id)
    ]
    assert [row.payload["reason"] for row in saved] == ["live_saved"]


async def test_a_folder_of_live_leased_folders_lists_in_the_same_statements_however_many(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """A ``Chats`` folder is a page of folders each held live by its box, and
    each row reads its lease's landing count. The count is part of the page's
    one live-plane statement, so two held rows and six cost the same — and each
    row still gets its own lease's count, not a neighbour's."""
    drive = await _room(fx)
    chats = await fx.node(b"chats", kind="folder", parent=await fx.shared())

    async def hold(index: int, reported: int) -> Any:
        folder = await fx.node(f"chat-{index}".encode(), kind="folder", parent=chats)
        holder = MockHolder(
            files_client, drive.id, folder.id, instance=f"box-{index}", machine=f"box-{index}"
        )
        taken = await holder.take(real_session, idem, live=True)
        assert taken.status_code == 200, taken.text
        report = [_file(f"f{n}.txt") for n in range(reported)]
        assert (await holder.tree(report)).status_code == 200
        return folder

    async def listing() -> tuple[int, dict[str, int]]:
        seen: list[str] = []

        def record(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
            seen.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", record)
        try:
            page = await files_client.get(f"{PREFIX}/drives/{drive.id}/items/{chats.id}/children")
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record)
        assert page.status_code == 200, page.text
        return len(seen), {
            row["name"]: row["lease"]["landing_count"] for row in page.json()["value"]
        }

    for index in range(2):
        await hold(index, reported=index + 1)
    await listing()  # warm: the first call pays for caches a page does not
    two, counts = await listing()
    assert counts == {"chat-0": 1, "chat-1": 2}

    for index in range(2, 6):
        await hold(index, reported=index + 1)
    six, counts = await listing()
    assert counts == {f"chat-{index}": index + 1 for index in range(6)}
    assert 0 < six == two
