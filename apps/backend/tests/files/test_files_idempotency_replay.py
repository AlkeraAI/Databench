"""Every mutating Files route spends its ``Idempotency-Key``: the same key with
the same body performs ONE effect and answers the first call's stored status and
bytes, whichever family the route belongs to.

One pin, parametrized over the families, rather than a copy per handler. Each
scenario builds the tree it needs, names the mutation and a count of the
effect it must leave exactly one of; the test sends the mutation twice under
one key and asserts the replay is byte-identical and the count is one.

RED at HEAD before the wrapper reached every family: a copy queued two
operations, a batch created two folders, a rename answered a 412 on the retry
because the etag had moved, a cancel answered ``409 files.operation_settled``,
a version restore appended two versions.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from _files_kit import FilesFixtures, FilesOrgFixture, node_etag
from alkera_core.authz.principal import ActingContext
from alkera_core.files.clock import SystemClock
from alkera_core.files.ops import Operations
from alkera_core.files.trash import Trash
from alkera_core.models.files.history import FileConflict
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/files"

Count = Callable[[], Awaitable[int]]


@dataclass(frozen=True, slots=True)
class Mutation:
    """One request, and the count of its effect after any number of sends."""

    method: str
    url: str
    effect: Count | None
    json: Any = None
    content: bytes | None = None
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)


async def _shared_id(client: AsyncClient, drive: dict[str, Any]) -> str:
    """``/Shared`` — the top-of-drive folder a caller may actually write into."""
    listed = await client.get(f"{BASE}/drives/{drive['id']}/items/{drive['rootId']}/children")
    return str(next(row for row in listed.json()["value"] if row["name"] == "Shared")["id"])


@dataclass(frozen=True, slots=True)
class Lab:
    client: AsyncClient
    fx: FilesFixtures
    session: AsyncSession
    org: FilesOrgFixture
    drive_id: str
    #: The folder every scenario builds in — `/Shared`, not the drive root: a
    #: traversal-only signpost refuses every direct write, so a create aimed
    #: there is a 422 long before idempotency is reached.
    root_id: str

    @property
    def ctx(self) -> ActingContext:
        return ActingContext.for_user(
            user_id=self.org.org.admin_id,
            org_id=self.org.org.org_id,
            email=self.org.org.admin_email,
        )

    def item(self, node_id: uuid.UUID | str) -> str:
        return f"{BASE}/drives/{self.drive_id}/items/{node_id}"

    async def etag(self, node_id: uuid.UUID | str) -> dict[str, str]:
        return {"If-Match": await node_etag(self.session, uuid.UUID(str(node_id)))}

    def count(self, sql: str, **params: Any) -> Count:
        async def run() -> int:
            result = await self.session.execute(text(sql), params)
            return int(result.scalar_one())

        return run

    def ops_of(self, kind: str) -> Count:
        return self.count(
            "SELECT count(*) FROM file_ops WHERE org_team_id = :org AND kind = :kind",
            org=self.fx.org_team_id,
            kind=kind,
        )

    def children_named(self, parent_id: uuid.UUID | str, name: str) -> Count:
        return self.count(
            "SELECT count(*) FROM file_nodes WHERE parent_id = :parent "
            "AND name_display = :name AND trashed_at IS NULL",
            parent=uuid.UUID(str(parent_id)),
            name=name,
        )

    def versions_of(self, node_id: uuid.UUID) -> Count:
        return self.count("SELECT count(*) FROM file_versions WHERE node_id = :node", node=node_id)


Scenario = Callable[[Lab], Awaitable[Mutation]]


# ---------------------------------------------------------------------------
# the scenarios: one per mutating handler family
# ---------------------------------------------------------------------------


async def copy(lab: Lab) -> Mutation:
    source = await lab.fx.node(b"copied.txt")
    target = await lab.fx.node(b"into", kind="folder")
    return Mutation(
        "POST", f"{lab.item(source.id)}/copy", lab.ops_of("copy"), json={"parentId": str(target.id)}
    )


async def bulk(lab: Lab) -> Mutation:
    root_etag = int((await lab.etag(lab.root_id))["If-Match"].strip('"'))
    items = [
        {
            "id": "a",
            "op": "createFolder",
            "parentId": lab.root_id,
            "name": "batched",
            "ifMatch": root_etag,
        }
    ]
    return Mutation(
        "POST",
        f"{BASE}/drives/{lab.drive_id}/bulk",
        lab.children_named(lab.root_id, "batched"),
        json={"items": items},
    )


async def tree(lab: Lab) -> Mutation:
    return Mutation(
        "POST",
        f"{lab.item(lab.root_id)}/tree",
        lab.children_named(lab.root_id, "skel"),
        json={"paths": ["skel/a"]},
        headers=await lab.etag(lab.root_id),
    )


async def rename(lab: Lab) -> Mutation:
    node = await lab.fx.node(b"before.txt")
    before = node.etag
    return Mutation(
        "PATCH",
        lab.item(node.id),
        lab.count(
            "SELECT etag - :before FROM file_nodes WHERE id = :id", before=before, id=node.id
        ),
        json={"name": "after.txt"},
        headers=await lab.etag(node.id),
    )


async def trash(lab: Lab) -> Mutation:
    node = await lab.fx.node(b"doomed.txt")
    return Mutation(
        "DELETE", lab.item(node.id), lab.ops_of("trash"), headers=await lab.etag(node.id)
    )


async def star(lab: Lab) -> Mutation:
    node = await lab.fx.node(b"starred.txt")
    return Mutation(
        "PUT",
        f"{lab.item(node.id)}/star",
        lab.count(
            "SELECT count(*) FROM file_stars WHERE node_id = :node AND user_id = :me",
            node=node.id,
            me=lab.org.org.admin_id,
        ),
        headers=await lab.etag(node.id),
    )


async def put_content(lab: Lab) -> Mutation:
    node = await lab.fx.node(b"bytes.txt")
    payload = b"hello, twice\n"
    return Mutation(
        "PUT",
        f"{lab.item(node.id)}/content",
        lab.versions_of(node.id),
        content=payload,
        headers={
            **await lab.etag(node.id),
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(payload)),
        },
    )


async def grant(lab: Lab) -> Mutation:
    node = await lab.fx.node(b"shared", kind="folder")
    return Mutation(
        "POST",
        f"{lab.item(node.id)}/permissions",
        lab.count(
            "SELECT count(*) FROM file_shares WHERE node_id = :node AND revoked_at IS NULL",
            node=node.id,
        ),
        json={"principal": {"kind": "user", "id": str(lab.org.member.id)}, "role": "reader"},
        headers=await lab.etag(node.id),
    )


async def revoke(lab: Lab) -> Mutation:
    node = await lab.fx.node(b"unshared", kind="folder")
    granted = await lab.client.post(
        f"{lab.item(node.id)}/permissions",
        json={"principal": {"kind": "user", "id": str(lab.org.member.id)}, "role": "reader"},
        headers={"Idempotency-Key": uuid.uuid4().hex, **await lab.etag(node.id)},
    )
    assert granted.status_code == 201, granted.text
    return Mutation(
        "DELETE",
        f"{lab.item(node.id)}/permissions/{granted.json()['id']}",
        lab.count(
            "SELECT count(*) FROM file_shares WHERE node_id = :node AND revoked_at IS NOT NULL",
            node=node.id,
        ),
        headers=await lab.etag(node.id),
    )


async def restore_version(lab: Lab) -> Mutation:
    node = await lab.fx.node(b"notes.txt")
    first = await lab.fx.version(node, seq=1, content_hash="11" * 32, size_bytes=11)
    await lab.fx.version(node, seq=2, content_hash="22" * 32, size_bytes=22)
    return Mutation(
        "POST",
        f"{lab.item(node.id)}/versions/{first.id}/restore",
        lab.count("SELECT count(*) - 2 FROM file_versions WHERE node_id = :node", node=node.id),
        json={},
        headers=await lab.etag(node.id),
    )


async def resolve_conflict(lab: Lab) -> Mutation:
    node = await lab.fx.node(b"doc.txt")
    theirs = await lab.fx.version(node, seq=1, content_hash="aa" * 32)
    mine = await lab.fx.version(node, seq=2, content_hash="bb" * 32)
    row = FileConflict(
        id=uuid.uuid4(),
        org_team_id=lab.fx.org_team_id,
        node_id=node.id,
        base_version_id=None,
        theirs_version_id=theirs.id,
        mine_version_id=mine.id,
        actor=lab.fx.actor_id,
        state="open",
    )
    lab.session.add(row)
    await lab.session.commit()
    return Mutation(
        "POST",
        f"{BASE}/drives/{lab.drive_id}/conflicts/{row.id}/resolve",
        lab.count(
            "SELECT count(*) FROM file_conflicts WHERE node_id = :node AND state <> 'open'",
            node=node.id,
        ),
        json={"keep": "theirs"},
        headers=await lab.etag(node.id),
    )


async def _trashed(lab: Lab, node_id: uuid.UUID, etag: int) -> uuid.UUID:
    handle = Trash(lab.fx.repo, lab.ctx, SystemClock())
    async with lab.fx.repo.transaction():
        op = await handle.trash(node_id, if_match=etag)
    await lab.session.commit()
    return uuid.UUID(str(op.id))


async def restore_from_trash(lab: Lab) -> Mutation:
    folder = await lab.fx.node(b"papers", kind="folder")
    op_id = await _trashed(lab, folder.id, folder.etag)
    return Mutation(
        "POST",
        f"{BASE}/drives/{lab.drive_id}/trash/{op_id}/restore",
        lab.count(
            "SELECT count(*) FROM file_nodes WHERE id = :id AND trashed_at IS NULL", id=folder.id
        ),
        json={},
        headers={"If-Match": "0"},
    )


async def acquire_lease(lab: Lab) -> Mutation:
    folder = await lab.fx.node(b"mount", kind="folder")
    return Mutation(
        "POST",
        f"{lab.item(folder.id)}/lease",
        lab.count(
            "SELECT count(*) FROM file_leases WHERE node_id = :node AND released_at IS NULL",
            node=folder.id,
        ),
        json={"instanceId": "instance-a", "machineId": "machine-a", "purpose": "mount"},
        headers=await lab.etag(folder.id),
    )


async def force_release(lab: Lab) -> Mutation:
    folder = await lab.fx.node(b"held", kind="folder")
    taken = await lab.client.post(
        f"{lab.item(folder.id)}/lease",
        json={"instanceId": "instance-a", "machineId": "machine-a", "purpose": "mount"},
        headers={"Idempotency-Key": uuid.uuid4().hex, **await lab.etag(folder.id)},
    )
    assert taken.status_code == 200, taken.text
    # The grant the manager is handed is the effect: a replay must answer the
    # same epoch rather than take the folder back a second time.
    return Mutation(
        "POST",
        f"{lab.item(folder.id)}/lease/force-release",
        None,
        headers=await lab.etag(folder.id),
    )


async def cancel_operation(lab: Lab) -> Mutation:
    operations = Operations(lab.fx.repo, lab.ctx, SystemClock())
    drive = await lab.fx.drive()
    state = await operations.start("move", drive_id=drive.id, total=3)
    await lab.session.commit()
    return Mutation(
        "POST",
        f"{BASE}/drives/{lab.drive_id}/operations/{state.id}/cancel",
        lab.count(
            "SELECT count(*) FROM file_ops WHERE id = :id AND state = 'cancelled'", id=state.id
        ),
        headers=await lab.etag(lab.root_id),
    )


async def undo_operation(lab: Lab) -> Mutation:
    node = await lab.fx.node(b"undone.txt")
    deleted = await lab.client.delete(
        lab.item(node.id), headers={"Idempotency-Key": uuid.uuid4().hex, **await lab.etag(node.id)}
    )
    assert deleted.status_code == 200, deleted.text
    return Mutation(
        "POST",
        f"{BASE}/drives/{lab.drive_id}/operations/{deleted.json()['id']}/undo",
        lab.ops_of("undo"),
        headers=await lab.etag(lab.root_id),
    )


async def download(lab: Lab) -> Mutation:
    folder = await lab.fx.node(b"export", kind="folder")
    return Mutation(
        "POST",
        f"{lab.item(folder.id)}/download",
        lab.ops_of("download"),
        headers=await lab.etag(folder.id),
    )


SCENARIOS: list[Any] = [
    pytest.param(copy, id="items-copy"),
    pytest.param(bulk, id="bulk"),
    pytest.param(tree, id="items-tree"),
    pytest.param(rename, id="items-patch"),
    pytest.param(trash, id="items-delete"),
    pytest.param(star, id="items-star"),
    pytest.param(put_content, id="content-put"),
    pytest.param(grant, id="sharing-grant"),
    pytest.param(revoke, id="sharing-revoke"),
    pytest.param(restore_version, id="versions-restore"),
    pytest.param(resolve_conflict, id="conflicts-resolve"),
    pytest.param(restore_from_trash, id="trash-restore"),
    pytest.param(acquire_lease, id="leases-acquire"),
    pytest.param(force_release, id="leases-force-release"),
    pytest.param(cancel_operation, id="operations-cancel"),
    pytest.param(undo_operation, id="operations-undo"),
    pytest.param(download, id="operations-download"),
]


async def _send(client: AsyncClient, mutation: Mutation, key: str) -> Any:
    return await client.request(
        mutation.method,
        mutation.url,
        json=mutation.json,
        content=mutation.content,
        params=mutation.params,
        headers={"Idempotency-Key": key, **mutation.headers},
    )


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_the_same_key_and_body_replays_the_first_answer_with_one_effect(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    scenario: Scenario,
) -> None:
    """Two sends, one key, one effect, identical bytes — for every family."""
    drive = (await files_client.get(f"{BASE}/drives")).json()
    lab = Lab(
        files_client,
        fx,
        real_session,
        files_org,
        str(drive["id"]),
        await _shared_id(files_client, drive),
    )
    mutation = await scenario(lab)
    key = uuid.uuid4().hex

    first = await _send(files_client, mutation, key)
    assert first.status_code < 400, f"{mutation.method} {mutation.url}: {first.text}"

    second = await _send(files_client, mutation, key)
    assert second.status_code == first.status_code, second.text
    assert second.content == first.content
    assert second.headers.get("location") == first.headers.get("location")
    if mutation.effect is not None:
        assert await mutation.effect() == 1


async def test_a_different_body_under_a_spent_key_is_refused_without_a_second_effect(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    """The stored answer belongs to the request that earned it: a copy retried
    under its key but into a different folder is the documented 422, and the
    second folder receives nothing."""
    drive = (await files_client.get(f"{BASE}/drives")).json()
    lab = Lab(
        files_client,
        fx,
        real_session,
        files_org,
        str(drive["id"]),
        await _shared_id(files_client, drive),
    )
    source = await lab.fx.node(b"copied.txt")
    first_target = await lab.fx.node(b"first", kind="folder")
    second_target = await lab.fx.node(b"second", kind="folder")
    key = uuid.uuid4().hex

    first = await files_client.post(
        f"{lab.item(source.id)}/copy",
        json={"parentId": str(first_target.id)},
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 202, first.text
    reused = await files_client.post(
        f"{lab.item(source.id)}/copy",
        json={"parentId": str(second_target.id)},
        headers={"Idempotency-Key": key},
    )
    assert reused.status_code == 422, reused.text
    assert reused.json()["code"] == "files.idempotency_mismatch"
    assert await lab.ops_of("copy")() == 1
