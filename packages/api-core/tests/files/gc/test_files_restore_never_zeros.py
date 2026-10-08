"""Restore never returns zeros: parked objects come back before the head swap.

The reachability sweep does not erase an unreferenced object, it renames it to
``deleted/<key>`` and leaves it there for a week. A subtree whose trash window
has just closed therefore has version rows whose bytes are one rename away from
where the row says they are — and a restore that only cleared ``trashed_at``
would hand the user back a tree of files that read as nothing.

Every assertion here reads the real bytes off a real ``FilesystemStore`` after a
real sweep moved them; nothing is mocked, and the refusal case is proven by
comparing every ``file_nodes`` row before and after.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from _files_gc_factory import AdminOnlyFactory
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files import errors
from alkera_core.files.clock import FakeClock
from alkera_core.files.gc import Janitor
from alkera_core.files.ids import NodeId, TrashOpId
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.keys import deleted_key, object_key
from alkera_core.files.store.scoped import PrefixedDomainStore
from alkera_core.files.trash import TRASH_WINDOW, Trash
from alkera_core.models.files.history import FileContentGrant
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio

#: Ten files is the point: the recovery has to cover the whole subtree, and a
#: one-file folder would pass even if it only ever looked at the root.
FOLDER = "anchor.bin vault/ " + " ".join(f"vault/f{index}.bin" for index in range(10))

#: The sweep's breaker refuses a pass that would move more than 2% of a
#: domain's live bytes, so the drive holds one large live file the trashed
#: folder is a rounding error against — the breaker is another lane's test.
ANCHOR_BYTES = 1 << 30


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


def _payload(index: int) -> bytes:
    """Distinct, non-zero bytes: a zeroed file is visibly not this one."""
    return bytes([index + 1]) * (64 + index)


async def _seed_folder(
    session: AsyncSession,
    factory: FilesFactory,
    org: FilesOrg,
    domain: Any,
) -> tuple[dict[str, FileNode], dict[str, tuple[str, bytes]]]:
    """A ten-file folder whose every version has real bytes in the store."""
    drive = await factory.drive()
    tree = await factory.tree(FOLDER, drive=drive)
    content: dict[str, tuple[str, bytes]] = {}
    anchor_key = object_key(uuid.uuid4().bytes)
    domain.write(anchor_key, b"anchor")
    session.add(
        FileVersion(
            id=uuid.uuid4(),
            org_team_id=org.org_team_id,
            node_id=tree["anchor.bin"].id,
            seq=1,
            size_bytes=ANCHOR_BYTES,
            content_hash=uuid.uuid4().hex,
            store_key=anchor_key,
            source="upload",
        )
    )
    for index in range(10):
        path = f"vault/f{index}.bin"
        payload = _payload(index)
        key = object_key(uuid.uuid4().bytes)
        domain.write(key, payload)
        version = FileVersion(
            id=uuid.uuid4(),
            org_team_id=org.org_team_id,
            node_id=tree[path].id,
            seq=1,
            size_bytes=len(payload),
            content_hash=uuid.uuid4().hex,
            store_key=key,
            source="upload",
        )
        session.add(version)
        await session.flush()
        tree[path].head_version_id = version.id
        content[path] = (key, payload)
    await session.commit()
    return tree, content


async def _expire_window(session: AsyncSession, op_id: uuid.UUID) -> None:
    """Stand at the last minute of the window, as Postgres reads it.

    The deadline lives in the database, so moving the fake clock alone would
    prove nothing: the sweep asks ``purge_after > now()``. A window whose last
    minute has just elapsed is exactly the state in which the sweep parks the
    bytes while the trash op — and the user's right to restore — is still there.
    """
    await session.execute(
        text("UPDATE file_trash_ops SET purge_after = now() - interval '1 minute' WHERE id = :id"),
        {"id": op_id},
    )
    await session.commit()


async def _node_rows(session: AsyncSession, drive_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = await session.execute(
        text("SELECT * FROM file_nodes WHERE drive_id = :d ORDER BY id"), {"d": drive_id}
    )
    return [dict(row) for row in rows.mappings()]


async def _read(store: Any, key: str) -> bytes:
    return b"".join([chunk async for chunk in await store.get(key)])


async def _sweep(
    repo_for_org: Any, domain: Any, clock: FakeClock, org: FilesOrg, shard: int
) -> Any:
    janitor = Janitor(repo_for_org, AdminOnlyFactory(domain.store), clock, age_source=domain.ages)
    return await janitor.sweep(domain.id, org=org.scope, shard=shard, dry_run=False)


async def test_restore_at_the_last_minute_reads_every_byte_back(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    repo_for_org: Any,
    domain: Any,
    clock: FakeClock,
    shard: int,
) -> None:
    tree, content = await _seed_folder(files_session, files_factory, files_org, domain)
    store = PrefixedDomainStore(domain.store, domain.id)
    trash = Trash(repo, _ctx(files_org), clock, store)

    async with repo.transaction():
        op = await trash.trash(NodeId(tree["vault"].id), if_match=tree["vault"].etag)
    await _expire_window(files_session, op.id)
    clock.advance(TRASH_WINDOW - timedelta(minutes=1))

    result = await _sweep(repo_for_org, domain, clock, files_org, shard)
    assert set(result.moved) == {key for key, _ in content.values()}
    for key, _ in content.values():
        assert not domain.exists(key), f"{key} should be parked under deleted/"
        assert domain.exists(deleted_key(key))

    async with repo.transaction():
        await trash.restore(TrashOpId(op.id))

    for path, (key, payload) in content.items():
        assert domain.exists(key), f"{path} was left under deleted/"
        assert await _read(store, key) == payload, f"{path} did not read its own bytes back"
        assert not domain.exists(deleted_key(key))
    rows = await _node_rows(files_session, tree["vault"].drive_id)
    assert all(row["trashed_at"] is None for row in rows)


async def test_one_unrecoverable_object_refuses_the_whole_restore(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    repo_for_org: Any,
    domain: Any,
    clock: FakeClock,
    shard: int,
) -> None:
    tree, content = await _seed_folder(files_session, files_factory, files_org, domain)
    drive_id = tree["vault"].drive_id
    store = PrefixedDomainStore(domain.store, domain.id)
    trash = Trash(repo, _ctx(files_org), clock, store)

    async with repo.transaction():
        op = await trash.trash(NodeId(tree["vault"].id), if_match=tree["vault"].etag)
    await _expire_window(files_session, op.id)
    result = await _sweep(repo_for_org, domain, clock, files_org, shard)
    assert len(result.moved) == 10

    # One object is destroyed under `deleted/` — a bucket lifecycle rule, a bad
    # hand at 3am — while the other nine are perfectly recoverable.
    lost_key, _ = content["vault/f4.bin"]
    (domain.root / "domains" / str(domain.id) / deleted_key(lost_key)).unlink()

    before = await _node_rows(files_session, drive_id)
    with pytest.raises(errors.NotFound) as refusal:
        async with repo.transaction():
            await trash.restore(TrashOpId(op.id))
    assert lost_key in str(refusal.value)

    assert await _node_rows(files_session, drive_id) == before
    for path, (key, _) in content.items():
        if path == "vault/f4.bin":
            continue
        assert not domain.exists(key), f"{path} moved before the refusal"
        assert domain.exists(deleted_key(key))


async def test_a_grant_holder_keeps_reading_its_version_across_the_head_swap(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    repo_for_org: Any,
    domain: Any,
    clock: FakeClock,
    shard: int,
) -> None:
    """An in-flight grant is a sweep root, so its bytes never move at all."""
    tree, content = await _seed_folder(files_session, files_factory, files_org, domain)
    store = PrefixedDomainStore(domain.store, domain.id)
    trash = Trash(repo, _ctx(files_org), clock, store)
    held_key, held_payload = content["vault/f7.bin"]
    version_id = (
        await files_session.execute(
            text("SELECT id FROM file_versions WHERE store_key = :k"), {"k": held_key}
        )
    ).scalar_one()
    files_session.add(
        FileContentGrant(
            nonce=uuid.uuid4().hex,
            org_team_id=files_org.org_team_id,
            version_id=version_id,
            expires_at=clock.now() + timedelta(days=400),
        )
    )
    await files_session.commit()

    async with repo.transaction():
        op = await trash.trash(NodeId(tree["vault"].id), if_match=tree["vault"].etag)
    await _expire_window(files_session, op.id)
    await _sweep(repo_for_org, domain, clock, files_org, shard)
    assert await _read(store, held_key) == held_payload

    async with repo.transaction():
        await trash.restore(TrashOpId(op.id))
    assert await _read(store, held_key) == held_payload
    for key, payload in content.values():
        assert await _read(store, key) == payload
