"""A file's version history over HTTP: list, fetch, put one back.

The restore cases are the interesting ones. Restoring must be a forward write —
a NEW head version carrying the old bytes — because that is what keeps it
undoable and keeps the object referenced the whole time; a route that rewound by
deleting versions would pass "the head is the old content" and fail every
assertion here about the chain that is left behind.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from _files_kit import NOT_FOUND, node_etag, refusal
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files.conftest import FilesFixtures

pytestmark = pytest.mark.asyncio


async def test_files_versions_lists_the_chain_and_names_the_head(
    files_on: None, files_client: AsyncClient, fx: FilesFixtures
) -> None:
    """Every version is listed, and exactly one is flagged as the head."""
    drive = await fx.drive()
    node = await fx.node(b"notes.txt")
    await fx.version(node, seq=1, content_hash="11" * 32)
    head = await fx.version(node, seq=2, content_hash="22" * 32)

    response = await files_client.get(f"/api/v1/files/drives/{drive.id}/items/{node.id}/versions")
    assert response.status_code == 200
    versions = response.json()["versions"]
    assert [row["seq"] for row in versions] == [1, 2]
    assert [row["id"] for row in versions if row["isHead"]] == [str(head.id)]


async def test_files_versions_say_who_wrote_each_by_name_and_which_machine(
    files_on: None, files_client: AsyncClient, fx: FilesFixtures, real_session: AsyncSession
) -> None:
    """A version reads as the member who wrote it, by name, and as the machine
    whose agent sent it. Somebody who is not a member of the drive's org is not
    named, and nor is anyone by their address."""
    from alkera_core.models import User
    from sqlalchemy import update

    await real_session.execute(
        update(User).where(User.id == fx.actor_id).values(first_name="Dana", last_name="Ruiz")
    )
    await real_session.commit()
    drive = await fx.drive()
    node = await fx.node(b"plan.md")
    await fx.version(node, seq=1, content_hash="11" * 32, created_by=fx.actor_id)
    await fx.version(
        node,
        seq=2,
        content_hash="22" * 32,
        created_by=fx.actor_id,
        version_metadata={"machine_id": "box-7"},
    )
    await fx.version(node, seq=3, content_hash="33" * 32, created_by=uuid.uuid4())

    response = await files_client.get(f"/api/v1/files/drives/{drive.id}/items/{node.id}/versions")
    assert response.status_code == 200
    rows = response.json()["versions"]
    assert [(row["author"], row["machine"]) for row in rows] == [
        ("Dana Ruiz", None),
        ("Dana Ruiz", "box-7"),
        (None, None),
    ]


async def test_files_version_content_redirects_to_a_signed_url(
    files_on: None, files_client: AsyncClient, fx: FilesFixtures
) -> None:
    """Bytes are never served from the API origin: the answer is a 302 to a
    single-use path on the content domain."""
    drive = await fx.drive()
    node = await fx.node(b"notes.txt")
    version = await fx.version(node)

    response = await files_client.get(
        f"/api/v1/files/drives/{drive.id}/items/{node.id}/versions/{version.id}/content",
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"].startswith("/c/")


async def test_files_version_content_hides_a_version_from_another_node(
    files_on: None, files_client: AsyncClient, fx: FilesFixtures
) -> None:
    """A version id that belongs to a different node is a 404 with the opaque
    body — the caller was not entitled to learn it exists."""
    drive = await fx.drive()
    mine = await fx.node(b"mine.txt")
    await fx.version(mine)
    theirs = await fx.node(b"theirs.txt")
    other = await fx.version(theirs)

    response = await files_client.get(
        f"/api/v1/files/drives/{drive.id}/items/{mine.id}/versions/{other.id}/content",
        follow_redirects=False,
    )
    assert response.status_code == 404
    assert refusal(response) == NOT_FOUND


async def test_files_version_restore_appends_a_new_head_over_the_old_bytes(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """The old content becomes current WITHOUT losing the version it replaced:
    the chain grows by one and the newest row carries the restored hash."""
    drive = await fx.drive()
    node = await fx.node(b"notes.txt")
    first = await fx.version(node, seq=1, content_hash="11" * 32, size_bytes=11)
    await fx.version(node, seq=2, content_hash="22" * 32, size_bytes=22)

    restored = await files_client.post(
        f"/api/v1/files/drives/{drive.id}/items/{node.id}/versions/{first.id}/restore",
        json={},
        headers={**idem(), "If-Match": await node_etag(real_session, node.id)},
    )
    assert restored.status_code == 200, restored.text

    listed = (
        await files_client.get(f"/api/v1/files/drives/{drive.id}/items/{node.id}/versions")
    ).json()["versions"]
    assert [row["seq"] for row in listed] == [1, 2, 3]
    assert [row["contentHash"] for row in listed] == ["11" * 32, "22" * 32, "11" * 32]
    assert [row["seq"] for row in listed if row["isHead"]] == [3]


@pytest.mark.parametrize(
    ("headers", "with_key", "expected", "code"),
    [
        pytest.param({}, False, 428, "files.idempotency_key_required", id="no-idempotency-key"),
        pytest.param({}, True, 428, "files.if_match_required", id="no-if-match"),
        pytest.param({"If-Match": '"9999"'}, True, 412, None, id="stale-if-match"),
    ],
)
async def test_files_version_restore_refuses_a_malformed_request(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    idem: Any,
    headers: dict[str, str],
    with_key: bool,
    expected: int,
    code: str | None,
) -> None:
    """Each precondition has its own status: a missing key and a missing
    ``If-Match`` are each a 428 naming which one is absent, and an etag that no
    longer matches is a 412. In every case the head is left where it was."""
    drive = await fx.drive()
    node = await fx.node(b"notes.txt")
    version = await fx.version(node)
    sent = dict(headers)
    if with_key:
        sent.update(idem())

    response = await files_client.post(
        f"/api/v1/files/drives/{drive.id}/items/{node.id}/versions/{version.id}/restore",
        json={},
        headers=sent,
    )
    assert response.status_code == expected
    if code is not None:
        assert response.json()["code"] == code
    listed = (
        await files_client.get(f"/api/v1/files/drives/{drive.id}/items/{node.id}/versions")
    ).json()["versions"]
    assert [row["seq"] for row in listed] == [1]


async def test_files_versions_hides_a_node_that_is_not_yours(
    files_on: None, files_client: AsyncClient, fx: FilesFixtures
) -> None:
    """A node id nobody owns answers the same opaque 404 as one in another org."""
    drive = await fx.drive()
    response = await files_client.get(
        f"/api/v1/files/drives/{drive.id}/items/{uuid.uuid4()}/versions"
    )
    assert response.status_code == 404
    assert refusal(response) == NOT_FOUND


async def test_files_version_restore_answers_with_the_mount_it_sits_under(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """The node a restore hands back names the holder of its mount.

    Version history is where somebody goes when a mounted folder looks wrong,
    so an answer that said the file was under no lease hid exactly the fact
    that explains what they are looking at.

    The restore rides under the holder's own epoch, because a restore appends a
    new head and a leased folder has one writer: the facet this pins is what the
    holder reads back off its own write.
    """
    drive = await fx.drive()
    folder = await fx.node(b"mounted", kind="folder")
    node = await fx.node(b"notes.txt", parent=folder)
    first = await fx.version(node, seq=1, content_hash="11" * 32, size_bytes=11)
    await fx.version(node, seq=2, content_hash="22" * 32, size_bytes=22)
    granted = await files_client.post(
        f"/api/v1/files/drives/{drive.id}/items/{folder.id}/lease",
        json={"instanceId": "instance-a", "machineId": "machine-a", "purpose": "mount"},
        headers={**idem(), "If-Match": await node_etag(real_session, folder.id)},
    )
    assert granted.status_code == 200, granted.text

    restored = await files_client.post(
        f"/api/v1/files/drives/{drive.id}/items/{node.id}/versions/{first.id}/restore",
        json={},
        headers={
            **idem(),
            "If-Match": await node_etag(real_session, node.id),
            "X-Alkera-Lease-Epoch": str(granted.json()["epoch"]),
            "X-Alkera-Lease-Instance": "instance-a",
        },
    )

    assert restored.status_code == 200, restored.text
    lease = restored.json()["lease"]
    assert lease is not None, "a restored version under a live mount rendered no lease"
    assert lease["machine"] == "machine-a"
