"""The holder's tree report, applied against real Postgres.

Every case here takes a real lease through ``LeaseService.acquire`` and applies
a real report through :class:`LeaseTreeService`, then reads back what a reader
of the table would see. The refusals are held to the invariant the fence exists
for: a refused batch leaves the tree, the lease's sequence, the outbox and the
idempotency store exactly as they were.
"""

from __future__ import annotations

import unicodedata
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.config import settings
from alkera_core.files.clock import FakeClock
from alkera_core.files.errors import InvalidRequest, TooLarge
from alkera_core.files.freshness import HeadFacts, HolderFacet, current, current_sql
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.lease_tree import (
    MAX_NAMED_FOLDERS,
    LeaseTreeService,
    TreeAnswer,
    TreeChange,
)
from alkera_core.files.leases import LeaseConflict, LeaseContext, LeaseService, holder_identity
from alkera_core.files.namespace import Namespace
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.versions import FileVersion
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio

INSTANCE = "box-1"
MACHINE = "box-7"
_H1 = bytes.fromhex("11" * 32)
_H2 = bytes.fromhex("22" * 32)


def _ctx(org: FilesOrg, principal_id: uuid.UUID | None = None) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(principal_id or org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


@contextmanager
def _counted(engine: AsyncEngine) -> Iterator[list[str]]:
    seen: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


@dataclass(frozen=True)
class Held:
    """``team/`` leased live by the org admin, with a small tree around it."""

    root: uuid.UUID
    drive_id: uuid.UUID
    epoch: int
    ids: dict[str, uuid.UUID]
    drive: Any = None


async def _held(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    *,
    live: bool = True,
    drive: Any = None,
    name: str = "team",
) -> Held:
    if drive is not None:
        # A refused batch rolled the session back; the drive's counter moved.
        await repo.session.refresh(drive)
    drive = drive or await files_factory.drive()
    nodes = {
        path.replace(name, "team", 1): node
        for path, node in (
            await files_factory.tree(
                f"{name}/ {name}/papers/ {name}/papers/spec.md {name}/notes.md {name}-outside.md",
                drive=drive,
            )
        ).items()
    }
    async with repo.transaction():
        grant = await LeaseService(repo, _ctx(files_org), clock).acquire(
            NodeId(nodes["team"].id), instance_id=INSTANCE, machine_id=MACHINE
        )
    async with repo.transaction():
        await repo.session.execute(
            text("UPDATE file_leases SET live_cadence = CAST(:c AS jsonb) WHERE node_id = :n"),
            {"c": '{"metadataEveryMs": 300}' if live else "{}", "n": nodes["team"].id},
        )
    return Held(
        root=nodes["team"].id,
        drive_id=drive.id,
        epoch=grant.epoch,
        ids={path: node.id for path, node in nodes.items()},
        drive=drive,
    )


def _file(
    path: str,
    *,
    size: int = 10,
    mtime: int = 1_000,
    mode: int | None = None,
    digest: bytes | None = None,
) -> TreeChange:
    return TreeChange(
        op="upsert",
        path=path.encode(),
        kind="file",
        size=size,
        mtime_ns=mtime,
        mode=mode,
        hash=digest,
    )


def _dir(path: str) -> TreeChange:
    return TreeChange(op="upsert", path=path.encode(), kind="dir")


def _delete(path: str) -> TreeChange:
    return TreeChange(op="delete", path=path.encode())


def _rename(source: str, path: str, *, kind: str = "file", size: int = 10) -> TreeChange:
    return TreeChange(
        op="rename",
        path=path.encode(),
        from_path=source.encode(),
        kind="dir" if kind == "dir" else "file",
        size=size if kind == "file" else None,
        mtime_ns=2_000 if kind == "file" else None,
    )


async def _apply(
    repo: FilesRepo,
    files_org: FilesOrg,
    clock: FakeClock,
    held: Held,
    changes: list[TreeChange],
    *,
    batch_id: uuid.UUID | None = None,
    epoch: int | None = None,
    instance: str = INSTANCE,
    principal: uuid.UUID | None = None,
    service: LeaseTreeService | None = None,
) -> TreeAnswer:
    service = service or LeaseTreeService(repo, _ctx(files_org, principal), clock)
    async with repo.transaction():
        return await service.apply(
            NodeId(held.root),
            drive_id=DriveId(held.drive_id),
            epoch=held.epoch if epoch is None else epoch,
            instance_id=instance,
            batch_id=batch_id or uuid.uuid4(),
            changes=changes,
        )


async def _tree(repo: FilesRepo, held: Held) -> dict[str, Any]:
    """Every node under the lease root by its relative path, trashed ones
    included, read as the table owner."""
    rows = (
        await repo.session.execute(
            text(
                "SELECT id, parent_id, name, kind, trashed_at, head_version_id, mode, mtime_ns, "
                "depth, holder_size, holder_mtime_ns, holder_hash, holder_seq, acl_id, flags "
                "FROM file_nodes WHERE drive_id = :drive"
            ),
            {"drive": held.drive_id},
        )
    ).all()
    by_id = {row.id: row for row in rows}
    out: dict[str, Any] = {}
    for row in rows:
        parts: list[str] = []
        cursor: Any = row
        while cursor is not None and cursor.id != held.root:
            parts.append(bytes(cursor.name).decode())
            cursor = by_id.get(cursor.parent_id)
        if cursor is not None and parts:
            out["/".join(reversed(parts))] = row
    await repo.session.commit()
    return out


async def _snapshot(repo: FilesRepo, files_org: FilesOrg, held: Held) -> tuple[Any, ...]:
    """Everything a refused batch must leave exactly as it was."""
    tree = await _tree(repo, held)
    seq = (
        await repo.session.execute(
            text("SELECT live_seq, last_sync_at FROM file_leases WHERE node_id = :n"),
            {"n": held.root},
        )
    ).one()
    outbox = (
        await repo.session.execute(
            text("SELECT count(*) FROM event_outbox WHERE org_id = :org"),
            {"org": files_org.org_team_id},
        )
    ).scalar_one()
    keys = (
        await repo.session.execute(
            text("SELECT count(*) FROM file_idempotency_keys WHERE org_team_id = :org"),
            {"org": files_org.org_team_id},
        )
    ).scalar_one()
    await repo.session.commit()
    return (
        sorted((path, row.id, row.trashed_at, row.holder_size) for path, row in tree.items()),
        tuple(seq),
        outbox,
        keys,
    )


async def _land(session: AsyncSession, node_id: uuid.UUID, *, size: int, digest: bytes) -> None:
    """Give a node a head version, the way a landed upload leaves it."""
    node_row = (
        await session.execute(
            text("SELECT org_team_id FROM file_nodes WHERE id = :id"), {"id": node_id}
        )
    ).one()
    version = FileVersion(
        id=uuid.uuid4(),
        org_team_id=node_row.org_team_id,
        node_id=node_id,
        seq=1,
        size_bytes=size,
        content_hash=digest.hex(),
        source="upload",
        store_key=f"objects/{digest.hex()}",
    )
    session.add(version)
    await session.flush()
    await session.execute(
        text(
            "UPDATE file_nodes SET head_version_id = :v, size = :s, mtime_ns = 5000 WHERE id = :id"
        ),
        {"v": version.id, "s": size, "id": node_id},
    )
    await session.commit()


# ---------------------------------------------------------------------------
# Rows appear
# ---------------------------------------------------------------------------


async def test_a_report_mints_every_row_with_the_holders_facet(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    held = await _held(repo, files_factory, files_org, clock)
    answer = await _apply(
        repo,
        files_org,
        clock,
        held,
        [
            _dir("src"),
            _file("src/a.py", size=5, mtime=7, mode=0o600, digest=_H1),
            _file("deep/x/y.txt", size=3, mtime=9),
        ],
    )
    tree = await _tree(repo, held)

    a = tree["src/a.py"]
    assert (a.kind, a.head_version_id, a.mode, a.mtime_ns) == ("file", None, 0o600, 7)
    assert (a.holder_size, a.holder_mtime_ns, bytes(a.holder_hash), a.holder_seq) == (
        5,
        7,
        _H1,
        answer.live_seq,
    )
    assert tree["src/a.py"].parent_id == tree["src"].id
    # The folders on the way to a reported file exist although nobody named them.
    assert tree["deep"].kind == tree["deep/x"].kind == "folder"
    assert tree["deep/x/y.txt"].parent_id == tree["deep/x"].id
    assert tree["deep/x/y.txt"].mode == 0o644
    assert tree["deep/x/y.txt"].depth == tree["deep/x"].depth + 1
    # Folders carry no report.
    assert tree["src"].holder_size is None and tree["deep"].holder_seq is None
    assert (answer.applied, answer.landing_count) == (3, 2)


async def test_a_second_report_of_the_same_paths_mints_nothing_new(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    held = await _held(repo, files_factory, files_org, clock)
    await _apply(repo, files_org, clock, held, [_file("a/b.txt", size=1, mtime=1)])
    first = await _tree(repo, held)
    answer = await _apply(repo, files_org, clock, held, [_file("a/b.txt", size=2, mtime=3)])
    second = await _tree(repo, held)
    assert {path: row.id for path, row in first.items()} == {
        path: row.id for path, row in second.items()
    }
    row = second["a/b.txt"]
    assert (row.holder_size, row.holder_mtime_ns, row.holder_seq) == (2, 3, answer.live_seq)
    # A file with no bytes follows the disk's modified time.
    assert row.mtime_ns == 3


# ---------------------------------------------------------------------------
# The fence comes first
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "who",
    [
        pytest.param("stale-epoch", id="a-superseded-epoch"),
        pytest.param("other-instance", id="another-instance"),
        pytest.param("not-the-holder", id="a-colleague-replaying-the-pair"),
    ],
)
async def test_a_batch_from_anyone_but_the_holder_leaves_nothing(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    who: str,
) -> None:
    held = await _held(repo, files_factory, files_org, clock)
    before = await _snapshot(repo, files_org, held)
    kwargs: dict[str, Any] = {
        "stale-epoch": {"epoch": held.epoch - 1},
        "other-instance": {"instance": "box-2"},
        "not-the-holder": {"principal": files_org.member_id},
    }[who]
    with pytest.raises(LeaseConflict) as refused:
        await _apply(
            repo, files_org, clock, held, [_file("new.txt"), _delete("notes.md")], **kwargs
        )
    assert refused.value.code == "files.lease_fenced"
    assert await _snapshot(repo, files_org, held) == before


async def test_a_lease_that_runs_no_live_plane_takes_no_report(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    held = await _held(repo, files_factory, files_org, clock, live=False)
    before = await _snapshot(repo, files_org, held)
    with pytest.raises(LeaseConflict) as refused:
        await _apply(repo, files_org, clock, held, [_file("new.txt")])
    assert refused.value.code == "files.lease_mismatch"
    assert await _snapshot(repo, files_org, held) == before


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(b"../outside.md", id="parent-segment"),
        pytest.param(b"papers/../../outside.md", id="parent-segment-deeper"),
        pytest.param(b"/team/notes.md", id="leading-slash"),
        pytest.param(b"papers//x", id="empty-segment"),
        pytest.param(b"./x", id="dot-segment"),
        pytest.param(b"papers/", id="trailing-slash"),
        pytest.param(b"Kickoff.alkerachat", id="a-pointer-name"),
        pytest.param(b"papers/a\x00b", id="a-name-the-namespace-refuses"),
        pytest.param(b"state/.lock", id="state-the-machine-keeps"),
    ],
)
async def test_one_path_outside_the_root_refuses_the_whole_batch(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    bad: bytes,
) -> None:
    held = await _held(repo, files_factory, files_org, clock)
    before = await _snapshot(repo, files_org, held)
    service = LeaseTreeService(
        repo, _ctx(files_org), clock, local_state=lambda path: path.endswith(b"/.lock")
    )
    with pytest.raises(LeaseConflict) as refused:
        await _apply(
            repo,
            files_org,
            clock,
            held,
            [_file("fine.txt"), TreeChange(op="upsert", path=bad, kind="file", size=1, mtime_ns=1)],
            service=service,
        )
    assert refused.value.code == "files.lease_mismatch"
    assert refused.value.detail == {"paths": [bad.decode("utf-8", "replace")], "indexes": [1]}
    assert await _snapshot(repo, files_org, held) == before


async def test_a_rename_source_outside_the_root_refuses_the_batch(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    held = await _held(repo, files_factory, files_org, clock)
    before = await _snapshot(repo, files_org, held)
    with pytest.raises(LeaseConflict) as refused:
        await _apply(repo, files_org, clock, held, [_rename("../outside.md", "stolen.md")])
    assert refused.value.code == "files.lease_mismatch"
    assert refused.value.detail is not None and refused.value.detail["indexes"] == [0]
    assert await _snapshot(repo, files_org, held) == before


async def test_a_batch_past_the_entry_ceiling_is_refused_before_anything_is_read(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    files_engine: AsyncEngine,
) -> None:
    held = await _held(repo, files_factory, files_org, clock)
    before = await _snapshot(repo, files_org, held)
    changes = [_file(f"f{i}.txt") for i in range(settings.files_live_metadata_max_entries + 1)]
    service = LeaseTreeService(repo, _ctx(files_org), clock)
    async with repo.transaction():
        with _counted(files_engine) as seen, pytest.raises(TooLarge) as refused:
            await service.apply(
                NodeId(held.root),
                drive_id=DriveId(held.drive_id),
                epoch=held.epoch,
                instance_id=INSTANCE,
                batch_id=uuid.uuid4(),
                changes=changes,
            )
    assert refused.value.code == "files.batch_too_large"
    assert seen == []
    assert await _snapshot(repo, files_org, held) == before


# ---------------------------------------------------------------------------
# Deletes, renames, replays
# ---------------------------------------------------------------------------


async def test_deletes_run_deepest_first_so_a_file_without_bytes_leaves_no_row(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    files_session: AsyncSession,
) -> None:
    """Named parent first, the batch still removes the child first: a file whose
    bytes never landed is removed outright, where a parent swept into the trash
    first would have taken it along as a trashed row."""
    held = await _held(repo, files_factory, files_org, clock)
    await _apply(repo, files_org, clock, held, [_file("old/a.txt"), _file("old/b.txt")])
    minted = await _tree(repo, held)
    await _land(files_session, minted["old/b.txt"].id, size=10, digest=_H1)

    await _apply(
        repo,
        files_org,
        clock,
        held,
        [_delete("old"), _delete("old/a.txt"), _delete("old/b.txt"), _delete("ghost")],
    )
    after = await _tree(repo, held)
    assert "old/a.txt" not in after, "a file with no bytes goes outright, not to the trash"
    assert after["old/b.txt"].trashed_at is not None, "a file with bytes keeps its history"
    assert after["old"].trashed_at is not None
    history = (
        await repo.session.execute(
            text("SELECT count(*) FROM file_history WHERE node_id = :n"),
            {"n": minted["old/a.txt"].id},
        )
    ).scalar_one()
    assert history == 0


async def test_a_rename_whose_source_the_drive_does_not_know_is_a_report_of_its_destination(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    held = await _held(repo, files_factory, files_org, clock)
    answer = await _apply(repo, files_org, clock, held, [_rename("never/was.tmp", "out/a.csv")])
    tree = await _tree(repo, held)
    assert "never" not in tree and "never/was.tmp" not in tree
    assert tree["out/a.csv"].holder_size == 10
    assert tree["out/a.csv"].holder_seq == answer.live_seq


async def test_a_rename_moves_the_node_it_names_and_replaces_what_stood_there(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    held = await _held(repo, files_factory, files_org, clock)
    await _apply(repo, files_org, clock, held, [_file("papers/final.md", size=4)])
    before = await _tree(repo, held)

    await _apply(repo, files_org, clock, held, [_rename("notes.md", "papers/final.md", size=12)])
    after = await _tree(repo, held)
    moved = after["papers/final.md"]
    assert moved.id == held.ids["team/notes.md"]
    assert moved.parent_id == held.ids["team/papers"]
    assert "notes.md" not in after
    # What stood at the destination had no bytes, so it is gone outright.
    assert before["papers/final.md"].id not in {row.id for row in after.values()}
    assert moved.holder_size == 12
    kinds = (
        await repo.session.execute(
            text("SELECT kind FROM file_history WHERE node_id = :n ORDER BY seq"),
            {"n": moved.id},
        )
    ).scalars()
    assert list(kinds)[-2:] == ["move", "rename"]


async def test_a_renamed_folders_new_children_land_inside_it(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    held = await _held(repo, files_factory, files_org, clock)
    await _apply(
        repo,
        files_org,
        clock,
        held,
        [_rename("papers", "manual", kind="dir"), _file("manual/intro.md")],
    )
    tree = await _tree(repo, held)
    assert tree["manual"].id == held.ids["team/papers"]
    assert tree["manual/intro.md"].parent_id == held.ids["team/papers"]
    assert tree["manual/spec.md"].id == held.ids["team/papers/spec.md"]


async def test_a_replayed_batch_answers_the_first_answer_and_writes_nothing(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    held = await _held(repo, files_factory, files_org, clock)
    batch_id = uuid.uuid4()
    changes = [_file("once.txt"), _file("twice/b.txt")]
    first = await _apply(repo, files_org, clock, held, changes, batch_id=batch_id)
    before = await _snapshot(repo, files_org, held)

    again = await _apply(repo, files_org, clock, held, changes, batch_id=batch_id)
    assert again == first
    assert await _snapshot(repo, files_org, held) == before

    with pytest.raises(InvalidRequest) as refused:
        await _apply(repo, files_org, clock, held, [_file("other.txt")], batch_id=batch_id)
    assert refused.value.code == "files.idempotency_mismatch"
    # A batch id is the lease's: the same id under another lease is a new batch.
    other = await _held(repo, files_factory, files_org, clock, drive=held.drive, name="other")
    fresh = await _apply(repo, files_org, clock, other, changes, batch_id=batch_id)
    assert fresh.live_seq == 1


async def test_a_path_that_changes_kind_on_disk_replaces_the_row(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    held = await _held(repo, files_factory, files_org, clock)
    await _apply(repo, files_org, clock, held, [_file("thing")])
    was = (await _tree(repo, held))["thing"].id
    await _apply(repo, files_org, clock, held, [_file("thing/inside.txt")])
    tree = await _tree(repo, held)
    assert tree["thing"].kind == "folder" and tree["thing"].id != was
    assert tree["thing/inside.txt"].parent_id == tree["thing"].id


# ---------------------------------------------------------------------------
# Freshness on the rows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("report", "cleared", "landing"),
    [
        pytest.param(_file("notes.md", size=10, mtime=5000), True, 0, id="same-size-and-mtime"),
        pytest.param(
            _file("notes.md", size=10, mtime=9999, digest=_H1), True, 0, id="same-hash-new-mtime"
        ),
        pytest.param(
            _file("notes.md", size=10, mtime=5000, digest=_H2), False, 1, id="same-pair-other-hash"
        ),
        pytest.param(_file("notes.md", size=11, mtime=5000), False, 1, id="the-size-moved"),
    ],
)
async def test_a_report_of_bytes_the_drive_already_has_leaves_no_facet(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    files_session: AsyncSession,
    report: TreeChange,
    cleared: bool,
    landing: int,
) -> None:
    held = await _held(repo, files_factory, files_org, clock)
    await _land(files_session, held.ids["team/notes.md"], size=10, digest=_H1)
    answer = await _apply(repo, files_org, clock, held, [report])
    row = (await _tree(repo, held))["notes.md"]
    assert (row.holder_size is None) is cleared
    assert answer.landing_count == landing
    # A landed row keeps the landed version's time unless the report proved the
    # bytes current, when it takes the disk's.
    assert row.mtime_ns == (report.mtime_ns if cleared else 5000)


@pytest.mark.parametrize(
    ("head", "facet"),
    [
        pytest.param((10, 5000, _H1), (10, 5000, None), id="pair-equal"),
        pytest.param((10, 5000, _H1), (11, 5000, None), id="size-differs"),
        pytest.param((10, 5000, _H1), (10, 5001, None), id="mtime-differs"),
        pytest.param((10, 5000, _H1), (10, 5000, _H2), id="hash-differs-pair-equal"),
        pytest.param((10, 5000, _H1), (99, 1, _H1), id="hash-equal-pair-differs"),
        pytest.param(None, (10, 5000, None), id="no-head"),
    ],
)
async def test_the_sql_predicate_answers_what_the_python_one_does(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_session: AsyncSession,
    head: tuple[int, int, bytes] | None,
    facet: tuple[int, int, bytes | None],
) -> None:
    drive = await files_factory.drive()
    node = (await files_factory.tree("f.bin", drive=drive))["f.bin"]
    if head is not None:
        await _land(files_session, node.id, size=head[0], digest=head[2])
        await files_session.execute(
            text("UPDATE file_nodes SET mtime_ns = :m WHERE id = :id"),
            {"m": head[1], "id": node.id},
        )
    await files_session.execute(
        text(
            "UPDATE file_nodes SET holder_size = :s, holder_mtime_ns = :m, holder_hash = :h "
            "WHERE id = :id"
        ),
        {"s": facet[0], "m": facet[1], "h": facet[2], "id": node.id},
    )
    await files_session.commit()
    in_sql = (
        await files_session.execute(
            text(
                f"SELECT {current_sql('n', 'hv')} FROM file_nodes n "
                "LEFT JOIN file_versions hv ON hv.id = n.head_version_id WHERE n.id = :id"
            ),
            {"id": node.id},
        )
    ).scalar_one()
    in_python = current(HeadFacts(*head) if head is not None else None, HolderFacet(*facet))
    assert bool(in_sql) is in_python


# ---------------------------------------------------------------------------
# Ceilings and cost
# ---------------------------------------------------------------------------


async def test_the_ceiling_on_reported_files_refuses_the_batch_that_passes_it(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "files_holder_max_nodes", 3)
    held = await _held(repo, files_factory, files_org, clock)
    await _apply(repo, files_org, clock, held, [_file("a"), _file("b"), _file("c")])
    before = await _snapshot(repo, files_org, held)
    with pytest.raises(LeaseConflict) as refused:
        await _apply(repo, files_org, clock, held, [_file("d")])
    assert refused.value.code == "files.live_too_many"
    assert await _snapshot(repo, files_org, held) == before
    # Re-reporting what is already counted is not growth.
    await _apply(repo, files_org, clock, held, [_file("a", size=99)])


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param("fresh", id="two-thousand-new-files"),
        pytest.param("again", id="two-thousand-reports-of-known-files"),
        pytest.param("clone", id="five-hundred-new-files-in-twenty-new-folders"),
    ],
)
async def test_two_thousand_entries_cost_a_bounded_handful_of_statements(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    files_engine: AsyncEngine,
    shape: str,
) -> None:
    held = await _held(repo, files_factory, files_org, clock)
    changes = [_file(f"pkg{i % 10}/mod{i}.py", size=i, mtime=i + 1) for i in range(2000)]
    if shape == "clone":
        changes = [_file(f"dir{i % 20}/f{i}.txt", size=i, mtime=i + 1) for i in range(500)]
    if shape == "again":
        await _apply(repo, files_org, clock, held, changes)
        changes = [_file(f"pkg{i % 10}/mod{i}.py", size=i + 1, mtime=i + 2) for i in range(2000)]
    service = LeaseTreeService(repo, _ctx(files_org), clock)
    async with repo.transaction():
        with _counted(files_engine) as seen:
            answer = await service.apply(
                NodeId(held.root),
                drive_id=DriveId(held.drive_id),
                epoch=held.epoch,
                instance_id=INSTANCE,
                batch_id=uuid.uuid4(),
                changes=changes,
            )
    assert answer.landing_count == len(changes)
    assert len(seen) <= 24, "\n".join(seen)


async def test_a_batch_touching_many_folders_names_none_of_them(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    held = await _held(repo, files_factory, files_org, clock)
    await _apply(
        repo, files_org, clock, held, [_file(f"d{i}/f.txt") for i in range(MAX_NAMED_FOLDERS)]
    )
    frames = (
        await repo.session.execute(
            text(
                "SELECT type, payload FROM event_outbox WHERE org_id = :org "
                "AND type IN ('file_node.changed', 'file_lease.changed') ORDER BY id"
            ),
            {"org": files_org.org_team_id},
        )
    ).all()
    await repo.session.commit()
    # 32 new folders under the root plus the root itself: 33 folders touched.
    assert [row.type for row in frames] == ["file_lease.changed"]
    assert frames[0].payload["subtree"] is True
    assert frames[0].payload["landing_count"] == MAX_NAMED_FOLDERS


# ---------------------------------------------------------------------------
# The one headless file create
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(TreeChange(op="upsert", path=b"scratch/.cache", kind="folder"), id="cache"),
        pytest.param(
            TreeChange(
                op="upsert", path=b"scratch/.local/bin/tool", kind="file", size=1, mtime_ns=1
            ),
            id="local-file",
        ),
        pytest.param(TreeChange(op="delete", path=b"scratch/.cache"), id="delete-cache"),
        pytest.param(TreeChange(op="delete", path=b"scratch/.local"), id="delete-local"),
    ],
)
async def test_the_agents_home_directories_are_refused_and_trash_nothing(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    change: TreeChange,
) -> None:
    """A box runs a chat's commands with ``HOME`` at the working directory, so
    ``.cache/`` and ``.local/`` grow there; the drive files neither, and a
    delete of either moves nothing to the Trash."""
    from alkera_core.project.local_state import is_local_state

    held = await _held(repo, files_factory, files_org, clock)
    before = await _snapshot(repo, files_org, held)
    service = LeaseTreeService(repo, _ctx(files_org), clock, local_state=is_local_state)
    with pytest.raises(LeaseConflict) as refused:
        await _apply(repo, files_org, clock, held, [_file("fine.txt"), change], service=service)
    assert refused.value.code == "files.lease_mismatch"
    assert refused.value.detail["indexes"] == [1]
    assert await _snapshot(repo, files_org, held) == before


# ---------------------------------------------------------------------------
# Inos are reserved apart from the batch
# ---------------------------------------------------------------------------


async def _next_ino(session: AsyncSession, drive_id: uuid.UUID) -> int:
    value = (
        await session.execute(
            text("SELECT next_ino FROM file_drives WHERE id = :d"), {"d": drive_id}
        )
    ).scalar_one()
    await session.commit()
    return int(value)


async def test_the_batch_never_writes_the_drive_row_in_its_own_transaction(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """The ino block is taken on another connection and committed before the
    batch starts, so the batch's own connection never updates the drive row --
    the row every other writer in the org queues on."""
    held = await _held(repo, files_factory, files_org, clock)
    seen: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        seen.append(statement)

    bind = repo.session.bind
    assert bind is not None
    service = LeaseTreeService(repo, _ctx(files_org), clock)
    async with repo.transaction():
        sync = (await repo.session.connection()).sync_connection
        assert sync is not None
        event.listen(sync, "before_cursor_execute", record)
        try:
            answer = await service.apply(
                NodeId(held.root),
                drive_id=DriveId(held.drive_id),
                epoch=held.epoch,
                instance_id=INSTANCE,
                batch_id=uuid.uuid4(),
                changes=[_file(f"src/m{i}.py") for i in range(50)],
            )
        finally:
            event.remove(sync, "before_cursor_execute", record)
    assert answer.applied == 50
    writes = [s for s in seen if s.lstrip().upper().startswith("UPDATE FILE_DRIVES")]
    assert writes == []


async def test_a_refused_batch_wastes_its_block_and_the_next_batch_reuses_no_ino(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """A block committed apart outlives the batch that reserved it: a batch
    refused after the reservation leaves ``next_ino`` moved on, and the rows a
    later batch mints take numbers no earlier row has."""
    held = await _held(repo, files_factory, files_org, clock)
    before = await _next_ino(repo.session, held.drive_id)
    with pytest.raises(LeaseConflict):
        await _apply(repo, files_org, clock, held, [_file("a/one.txt")], epoch=held.epoch + 1)
    await repo.session.rollback()
    after_refusal = await _next_ino(repo.session, held.drive_id)
    assert after_refusal > before
    await _apply(repo, files_org, clock, held, [_file(f"b/f{i}.txt") for i in range(5)])
    inos = (
        (
            await repo.session.execute(
                text("SELECT ino FROM file_nodes WHERE drive_id = :d"), {"d": held.drive_id}
            )
        )
        .scalars()
        .all()
    )
    await repo.session.commit()
    assert len(inos) == len(set(inos))
    minted = (
        await repo.session.execute(
            text("SELECT min(ino) FROM file_nodes WHERE drive_id = :d AND holder_size IS NOT NULL"),
            {"d": held.drive_id},
        )
    ).scalar_one()
    await repo.session.commit()
    assert minted >= after_refusal


async def test_the_holder_may_keep_both_normalization_forms_its_disk_holds(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """A Linux disk holds ``café`` in both forms as two files. The holder
    reports what is there, so the drive keeps both and flags them, rather than
    leaving one on the machine and nowhere else."""
    held = await _held(repo, files_factory, files_org, clock)
    namespace = Namespace(repo, _ctx(files_org), clock)
    fence = LeaseContext(
        epoch=held.epoch, instance_id=INSTANCE, holder=holder_identity(_ctx(files_org))
    )
    made = []
    async with repo.transaction():
        for form in ("NFC", "NFD"):
            made.append(
                await namespace.create(
                    DriveId(held.drive_id),
                    NodeId(held.root),
                    "folder",
                    unicodedata.normalize(form, "café").encode(),
                    lease=fence,
                )
            )
    assert len({node.name for node in made}) == 2
    for node in made:
        await repo.session.refresh(node)
        assert node.flags_names["macos_safe"] is False
