"""The holder's own client against the real tree route.

The lanes each proved their half against a double of the other: the CLI drove
:class:`RestLiveApi` against a ``MockTransport`` playing the route, and the
backend drove the route with a test holder speaking JSON by hand. Here the two
real halves meet. :class:`LiveSync` and :class:`RestLiveApi` run exactly as a
box runs them -- the metadata queue, the split on 413, the search on 409, the
wait on 429, the stop on a fence -- and every request they make is carried, as
bytes, into the mounted FastAPI app with Postgres behind it.

The holder is synchronous and the app is not, so the holder runs on a worker
thread and its transport hands each request to the test's own event loop
(:class:`_Bridge`). Nothing is stubbed between the two but the socket.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from _live_holder import MockHolder
from alkera_cli.files.live_sync import (
    THROTTLE_MAX_WAIT,
    LiveCadence,
    LiveSync,
    RestLiveApi,
    TreeEntry,
)
from alkera_cli.files.mount import LeaseSupersededError, MountRecord, SelfFence
from alkera_cli.files.tree_watch import Change
from alkera_core.config import settings
from backend.api import rate_limit
from blake3 import blake3
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

PREFIX = "/api/v1/files"


# ---------------------------------------------------------------------------
# The wire between a synchronous holder and the in-process app
# ---------------------------------------------------------------------------


@dataclass
class _Seen:
    method: str
    path: str
    status: int
    encoding: str | None
    headers: dict[str, str]
    body: Any


@dataclass
class _Bridge(httpx.BaseTransport):
    """A synchronous transport that carries each request to the app.

    The request's bytes and headers go through untouched; the answer's status,
    headers and body come back untouched. ``mangle`` lets a test play a proxy
    that corrupts a compressed body on the way.
    """

    client: AsyncClient
    loop: asyncio.AbstractEventLoop
    seen: list[_Seen] = field(default_factory=list)
    mangle: Callable[[bytes], bytes] | None = None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        if self.mangle is not None and request.headers.get("content-encoding") == "gzip":
            body = self.mangle(body)
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in ("host", "content-length")
        }
        future = asyncio.run_coroutine_threadsafe(
            self.client.request(
                request.method, request.url.raw_path.decode(), content=body, headers=headers
            ),
            self.loop,
        )
        answer = future.result(timeout=120)
        if request.url.path.endswith("/lease/tree"):
            self.seen.append(
                _Seen(
                    method=request.method,
                    path=request.url.path,
                    status=answer.status_code,
                    encoding=request.headers.get("content-encoding"),
                    headers=dict(answer.headers),
                    body=answer.json() if answer.content else None,
                )
            )
        kept = {
            key: value
            for key, value in answer.headers.items()
            if key.lower() not in ("content-encoding", "content-length", "transfer-encoding")
        }
        return httpx.Response(
            answer.status_code, headers=kept, content=answer.content, request=request
        )

    @property
    def trees(self) -> list[_Seen]:
        return list(self.seen)


@dataclass
class _BridgedFiles:
    """The one slice of the SDK a live flush reads: the node at a path."""

    http: httpx.Client

    def item_by_path(self, drive_id: str, item_path: str) -> dict[str, Any]:
        answer = self.http.get(
            f"{PREFIX}/drives/{drive_id}/root:/{quote(item_path.lstrip('/'), safe='/')}"
        )
        if answer.status_code == 404:
            raise RuntimeError(f"GET {item_path} returned 404 — {{}}")
        answer.raise_for_status()
        found: dict[str, Any] = answer.json()
        return found

    def item(self, drive_id: str, item_id: str, *, select: str | None = None) -> dict[str, Any]:
        answer = self.http.get(f"{PREFIX}/drives/{drive_id}/items/{item_id}")
        answer.raise_for_status()
        found: dict[str, Any] = answer.json()
        return found

    def item_under(self, drive_id: str, item_id: str, item_path: str) -> dict[str, Any]:
        """The step down from a folder the holder has: how a box on a chat's
        lease, which reads nothing above that folder, addresses a file."""
        answer = self.http.get(
            f"{PREFIX}/drives/{drive_id}/items/{item_id}:/{quote(item_path.strip('/'), safe='/')}"
        )
        if answer.status_code == 404:
            raise RuntimeError(f"GET {item_id}:/{item_path} returned 404 — {{}}")
        answer.raise_for_status()
        found: dict[str, Any] = answer.json()
        return found


class _Clock:
    def __init__(self) -> None:
        self.now = 10_000.0

    def monotonic(self) -> float:
        return self.now


class _NoWatcher:
    async def changes(self) -> Any:  # pragma: no cover - the tests flush by hand
        raise AssertionError("these tests classify and flush by hand")


@dataclass
class _Box:
    """A real holder over a real folder on disk, wired to the app."""

    sync: LiveSync
    api: RestLiveApi
    bridge: _Bridge
    clock: _Clock
    root: Path

    def write(self, relative: str, data: bytes = b"x") -> Path:
        where = self.root / relative
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_bytes(data)
        self.sync.classify(Change.added, str(where))
        return where

    async def flush(self) -> None:
        await asyncio.to_thread(self.sync.flush)


async def _held(
    files_client: AsyncClient, fx: Any, session: AsyncSession, idem: Any
) -> tuple[Any, Any, MockHolder]:
    drive = await fx.drive()
    drive.quota_bytes = settings.files_quota_default_bytes
    drive.quota_nodes = settings.files_quota_default_nodes
    await fx._session.commit()
    project = await fx.node(b"project", kind="folder", parent=await fx.shared())
    holder = MockHolder(files_client, drive.id, project.id, machine="ana-mbp")
    taken = await holder.take(session, idem, purpose="mount", live=True)
    assert taken.status_code == 200, taken.text
    return drive, project, holder


async def _box(
    files_client: AsyncClient,
    fx: Any,
    drive: Any,
    project: Any,
    holder: MockHolder,
    tmp_path: Path,
    *,
    fence: dict[str, str] | None = None,
    cadence: LiveCadence | None = None,
) -> _Box:
    bridge = _Bridge(files_client, asyncio.get_running_loop())
    http = httpx.Client(
        transport=bridge,
        base_url="http://test",
        headers=holder.fence if fence is None else fence,
    )
    shared = await fx.shared()
    dest = f"{bytes(shared.name).decode()}/{bytes(project.name).decode()}"
    root = tmp_path / "project"
    root.mkdir()
    clock = _Clock()
    api = RestLiveApi(
        files=_BridgedFiles(http),
        http=http,
        root=root,
        drive_id=str(drive.id),
        lease_node_id=str(project.id),
        dest=dest,
        push=lambda **_kwargs: None,
    )
    sync = LiveSync(
        root=root,
        record=MountRecord(node_id=str(project.id), heartbeat_every=15.0),
        # The settle window is about a file still being written; these cases write
        # once and expect the hash on the next batch.
        cadence=cadence or LiveCadence(metadata_gzip_bytes=0, settle_ms=0),
        api=api,
        watcher=_NoWatcher(),
        fence=SelfFence(grace=86_400.0, monotonic=clock.monotonic),
        clock=clock,
        sleep=lambda _seconds: None,
    )
    return _Box(sync=sync, api=api, bridge=bridge, clock=clock, root=root)


async def _listed(session: AsyncSession, drive_id: uuid.UUID) -> dict[bytes, Any]:
    """Every live row under the drive, by name."""
    rows = (
        await session.execute(
            text(
                "SELECT id, parent_id, name, kind, holder_size, holder_hash FROM file_nodes "
                "WHERE drive_id = :drive AND trashed_at IS NULL"
            ),
            {"drive": drive_id},
        )
    ).all()
    await session.commit()
    return {bytes(row.name): row for row in rows}


async def _live_seq(session: AsyncSession, node_id: uuid.UUID) -> int:
    value = (
        await session.execute(
            text("SELECT live_seq FROM file_leases WHERE node_id = :n"), {"n": node_id}
        )
    ).scalar_one()
    await session.commit()
    return int(value)


# ---------------------------------------------------------------------------
# The shape on the wire
# ---------------------------------------------------------------------------


async def test_the_holders_batch_lands_gzipped_with_its_hash_and_a_rename_keeps_the_node(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any, tmp_path: Path
) -> None:
    """snake_case keys, the ``from`` alias and the ``b3:`` hash as the holder
    spells them are what the route reads: the batch lands compressed, the
    holder's digest is stored, and a rename the content round detects moves
    the node the drive already had instead of minting a second one."""
    drive, project, holder = await _held(files_client, fx, real_session, idem)
    box = await _box(files_client, fx, drive, project, holder, tmp_path)

    box.write("data/a.tmp", b"twelve bytes")
    await box.flush()
    await box.flush()  # the digest the upload proved rides the next batch
    before = await _listed(real_session, drive.id)
    assert before[b"a.tmp"].parent_id == before[b"data"].id
    assert bytes(before[b"a.tmp"].holder_hash) == blake3(b"twelve bytes").digest()

    os.rename(box.root / "data/a.tmp", box.root / "data/a.csv")
    box.sync.classify(Change.deleted, str(box.root / "data/a.tmp"))
    box.sync.classify(Change.added, str(box.root / "data/a.csv"))
    await box.flush()

    after = await _listed(real_session, drive.id)
    assert b"a.tmp" not in after
    assert after[b"a.csv"].id == before[b"a.tmp"].id
    sent = box.bridge.trees
    assert sent and all(call.status == 200 for call in sent), [c.body for c in sent]
    assert all(call.encoding == "gzip" for call in sent)


async def test_a_body_a_proxy_mangles_is_a_400_and_the_holder_keeps_its_rows(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any, tmp_path: Path
) -> None:
    """A compressed body that does not decode is the route's 400
    ``files.bad_encoding``. To the holder it is a refusal like any other that
    is not the fence: the rows stay queued, and the next offer lands them."""
    drive, project, holder = await _held(files_client, fx, real_session, idem)
    box = await _box(files_client, fx, drive, project, holder, tmp_path)
    box.bridge.mangle = lambda body: body[: len(body) // 2]

    box.write("keep.txt")
    await box.flush()
    (refused,) = box.bridge.trees
    assert (refused.status, refused.body["code"]) == (400, "files.bad_encoding")
    assert b"keep.txt" not in await _listed(real_session, drive.id)
    assert "keep.txt" in box.sync.metadata

    box.bridge.mangle = None
    box.clock.now += RETRY_WAIT_CEILING
    box.write("keep.txt")
    await box.flush()
    assert b"keep.txt" in await _listed(real_session, drive.id)


#: Longer than any wait the holder puts between two offers of the same rows.
RETRY_WAIT_CEILING = THROTTLE_MAX_WAIT + 1.0


async def test_a_batch_past_the_routes_ceiling_is_split_and_every_row_lands(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any, tmp_path: Path
) -> None:
    """A grant that let the holder batch more than the route takes (an older
    grant, a misconfigured one) meets 413 ``files.batch_too_large``; the
    holder halves and sends both halves, and nothing is dropped."""
    drive, project, holder = await _held(files_client, fx, real_session, idem)
    box = await _box(
        files_client,
        fx,
        drive,
        project,
        holder,
        tmp_path,
        cadence=LiveCadence(metadata_gzip_bytes=0, metadata_max_entries=2001, settle_ms=0),
    )
    for index in range(2001):
        box.write(f"pkg{index % 7}/f{index}.txt")
    assert len(box.sync.metadata) == 2001

    await asyncio.to_thread(box.sync._flush_metadata, hold=False)

    first = box.bridge.trees[0]
    assert (first.status, first.body["code"]) == (413, "files.batch_too_large")
    assert all(call.status == 200 for call in box.bridge.trees[1:])
    listed = await _listed(real_session, drive.id)
    assert {f"f{index}.txt".encode() for index in range(2001)} <= set(listed)


# ---------------------------------------------------------------------------
# Refusals the holder acts on
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="NTFS cannot hold a name with a trailing space")
async def test_a_path_the_route_refuses_is_dropped_and_only_that_path(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A folder whose name the drive will not file (a trailing space is legal
    on disk and refused by the naming contract) is found by the route, which
    names the entry back in ``detail.indexes``/``detail.paths``. The holder
    drops exactly the entries named and lands the rest -- including a file at
    the root that happens to share the refused file's leaf name.

    Today's box refuses such a name before it sends it; the box here is one
    whose naming rule is older than the server's, which is the case the
    route's own answer exists for."""
    from alkera_cli.files import live_paths, name_rules

    monkeypatch.setattr(live_paths, "path_refusal", lambda _relative: None)
    monkeypatch.setattr(name_rules, "path_refusal", lambda _relative: None)
    drive, project, holder = await _held(files_client, fx, real_session, idem)
    box = await _box(files_client, fx, drive, project, holder, tmp_path)
    box.write("notes /readme.md")
    box.write("readme.md")
    box.write("ok.txt")

    await asyncio.to_thread(box.sync._flush_metadata, hold=False)

    refused = [call for call in box.bridge.trees if call.status == 409]
    assert refused and refused[0].body["code"] == "files.lease_mismatch"
    assert refused[0].body["detail"]["paths"] == ["notes /readme.md"]
    assert isinstance(refused[0].body["detail"]["indexes"], list)
    listed = await _listed(real_session, drive.id)
    assert b"ok.txt" in listed
    assert b"readme.md" in listed, "a root file sharing the refused leaf name was dropped too"
    assert listed[b"readme.md"].parent_id == project.id
    assert b"notes " not in listed
    assert box.sync.metadata == {}


async def test_a_stale_epoch_is_fenced_and_the_holder_stops_having_written_nothing(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any, tmp_path: Path
) -> None:
    drive, project, holder = await _held(files_client, fx, real_session, idem)
    assert holder.epoch is not None
    box = await _box(
        files_client,
        fx,
        drive,
        project,
        holder,
        tmp_path,
        fence=holder.stale_fence(holder.epoch - 1),
    )
    seq = await _live_seq(real_session, project.id)
    before = await _listed(real_session, drive.id)

    box.write("late.txt")
    with pytest.raises(LeaseSupersededError):
        await box.flush()

    (fenced,) = box.bridge.trees
    assert (fenced.status, fenced.body["code"]) == (409, "files.lease_fenced")
    assert await _listed(real_session, drive.id) == before
    assert await _live_seq(real_session, project.id) == seq


async def test_a_throttled_batch_waits_the_servers_retry_after_and_keeps_its_delete(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route's own 429 names a ``Retry-After``. The holder offers nothing
    until that wait (up to its ceiling) has passed on its clock, keeps
    coalescing meanwhile, and the delete it was holding is not lost: once the
    wait is over the file is gone from the drive."""
    drive, project, holder = await _held(files_client, fx, real_session, idem)
    box = await _box(files_client, fx, drive, project, holder, tmp_path)
    box.write("doomed.txt")
    await asyncio.to_thread(box.sync._flush_metadata, hold=False)
    assert b"doomed.txt" in await _listed(real_session, drive.id)

    limiter = rate_limit.REGISTRY.limiter("lease_tree")
    monkeypatch.setattr(limiter, "_per_minute", lambda: 1)
    monkeypatch.setattr(limiter, "_burst", lambda: 1)
    limiter.reset()
    assert (await holder.tree([{"op": "upsert", "path": "spend", "kind": "dir"}])).status_code == (
        200
    )

    (box.root / "doomed.txt").unlink()
    box.sync.classify(Change.deleted, str(box.root / "doomed.txt"))
    await asyncio.to_thread(box.sync._flush_metadata, hold=False)
    throttled = box.bridge.trees[-1]
    assert throttled.status == 429, throttled.body
    asked = float(throttled.headers["retry-after"])
    wait = min(asked, THROTTLE_MAX_WAIT)
    offered = len(box.bridge.trees)
    assert box.sync.metadata["doomed.txt"].op == "delete"

    box.clock.now += wait - 0.5
    box.write("later.txt")
    await asyncio.to_thread(box.sync._flush_metadata, hold=False)
    assert len(box.bridge.trees) == offered, "offered again before the server's wait was over"

    limiter.reset()
    box.clock.now += 1.0
    await asyncio.to_thread(box.sync._flush_metadata, hold=False)
    assert box.bridge.trees[-1].status == 200
    listed = await _listed(real_session, drive.id)
    assert b"doomed.txt" not in listed
    assert b"later.txt" in listed


def _entry(path: str) -> TreeEntry:
    return TreeEntry(op="upsert", path=path, kind="file", size=1, mtime_ns=1, mode=0o644)


async def test_a_body_under_the_gzip_threshold_travels_plain_and_one_over_it_compressed(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any, tmp_path: Path
) -> None:
    drive, project, holder = await _held(files_client, fx, real_session, idem)
    box = await _box(files_client, fx, drive, project, holder, tmp_path)
    small = await asyncio.to_thread(
        box.api.tree, str(uuid.uuid4()), [_entry("small.txt")], gzip_above=10_000
    )
    large = await asyncio.to_thread(
        box.api.tree,
        str(uuid.uuid4()),
        [_entry(f"big/{index}.txt") for index in range(200)],
        gzip_above=10_000,
    )
    assert [call.encoding for call in box.bridge.trees] == [None, "gzip"]
    assert (small.applied, large.applied) == (1, 200)
    assert large.live_seq == small.live_seq + 1
    assert large.landing_count == 201


# ---------------------------------------------------------------------------
# A holder-driven listing, end to end
# ---------------------------------------------------------------------------


async def _outbox_after(session: AsyncSession, org_id: uuid.UUID, after: int) -> list[Any]:
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


async def _outbox_mark(session: AsyncSession) -> int:
    value = (
        await session.execute(text("SELECT coalesce(max(id), 0) FROM event_outbox"))
    ).scalar_one()
    await session.commit()
    return int(value)


async def _children(client: AsyncClient, drive_id: uuid.UUID, folder: uuid.UUID) -> list[Any]:
    answer = await client.get(
        f"{PREFIX}/drives/{drive_id}/items/{folder}/children", params={"limit": 200}
    )
    assert answer.status_code == 200, answer.text
    rows: list[Any] = answer.json()["value"]
    return rows


async def test_a_clone_is_listed_before_its_bytes_and_each_landing_settles_one_row(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    files_org: Any,
) -> None:
    """Five hundred files under twenty folders, reported by the holder, are
    listed at once as ``unlanded`` with the disk's size and time; the tags a
    preview keys on hold still between two reads; landing ten of them clears
    exactly those ten and moves the folder's count by ten; and the batch is
    announced once per folder it touched."""
    drive, project, holder = await _held(files_client, fx, real_session, idem)
    mark = await _outbox_mark(real_session)
    entries = [
        {
            "op": "upsert",
            "path": f"dir{index % 20}/file{index}.txt",
            "kind": "file",
            "size": 12,
            "mtime_ns": 1_758_625_000_000_000_000 + index,
        }
        for index in range(500)
    ]
    answer = await holder.tree(entries, compress=True)
    assert answer.status_code == 200, answer.text
    assert answer.json()["landing_count"] == 500

    frames = await _outbox_after(real_session, files_org.org.org_id, mark)
    listed = await _listed(real_session, drive.id)
    folders = {listed[f"dir{index}".encode()].id for index in range(20)}
    named = [row for row in frames if row.type == "file_node.changed"]
    assert sorted(row.entity_id for row in named) == sorted(
        str(folder) for folder in {*folders, project.id}
    )
    assert {row.payload["reason"] for row in named} == {"live_batch"}
    assert all(row.payload["parent_id"] == row.entity_id for row in named)
    (lease_frame,) = [row for row in frames if row.type == "file_lease.changed"]
    assert "subtree" not in lease_frame.payload

    first: dict[str, Any] = {}
    for folder in folders:
        for row in await _children(files_client, drive.id, folder):
            first[row["id"]] = row
    assert len(first) == 500
    for row in first.values():
        assert row["live"]["content"] == "unlanded"
        assert row["live"]["holder_size"] == 12
        assert row["live"]["holder_mtime"] is not None
        assert row["lease"]["served"] == "live"
        assert row["lease"]["landing_count"] == 500
    again = {
        row["id"]: row
        for folder in folders
        for row in await _children(files_client, drive.id, folder)
    }
    assert {key: (row["etag"], row["ctag"]) for key, row in again.items()} == {
        key: (row["etag"], row["ctag"]) for key, row in first.items()
    }

    landed = sorted(first)[:10]
    for node_id in landed:
        pushed = await holder.push(real_session, idem, uuid.UUID(node_id), b"twelve bytes")
        assert pushed.status_code in (200, 201), pushed.text

    after = {
        row["id"]: row
        for folder in folders
        for row in await _children(files_client, drive.id, folder)
    }
    assert set(after) == set(first), "a landing listed a second node beside the minted one"
    for node_id, row in after.items():
        if node_id in landed:
            assert row["live"] is None, row["live"]
        else:
            assert row["live"]["content"] == "unlanded"
        assert row["lease"]["served"] == "live"
        assert row["lease"]["landing_count"] == 490


async def test_a_clone_touching_more_folders_than_it_names_marks_the_lease_frame(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    files_org: Any,
) -> None:
    _drive, _project, holder = await _held(files_client, fx, real_session, idem)
    mark = await _outbox_mark(real_session)
    answer = await holder.tree(
        [
            {"op": "upsert", "path": f"pkg{index}/mod.py", "kind": "file", "size": 1}
            for index in range(40)
        ]
    )
    assert answer.status_code == 200, answer.text
    frames = await _outbox_after(real_session, files_org.org.org_id, mark)
    assert [row.type for row in frames] == ["file_lease.changed"]
    assert frames[0].payload["subtree"] is True


# ---------------------------------------------------------------------------
# Shapes at the edge of the contract
# ---------------------------------------------------------------------------


async def test_a_rename_onto_its_own_path_is_a_report_of_that_path(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """``from`` equal to ``path`` moves nothing: the node stays the node and
    the report's size is what the row now says."""
    drive, _project, holder = await _held(files_client, fx, real_session, idem)
    first = await holder.tree([{"op": "upsert", "path": "a.txt", "kind": "file", "size": 1}])
    assert first.status_code == 200, first.text
    before = (await _listed(real_session, drive.id))[b"a.txt"]

    again = await holder.tree(
        [{"op": "rename", "from": "a.txt", "path": "a.txt", "kind": "file", "size": 7}]
    )
    assert again.status_code == 200, again.text
    after = (await _listed(real_session, drive.id))[b"a.txt"]
    assert (after.id, after.holder_size) == (before.id, 7)


async def test_a_plain_body_past_four_mebibytes_is_the_routes_413_not_the_edges(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """Two thousand entries of deep paths make a body past the decoded cap
    without a byte of compression. The refusal is the tree route's own
    ``files.batch_too_large`` -- the code the holder splits on -- and not an
    earlier layer's, and nothing is written."""
    drive, _project, holder = await _held(files_client, fx, real_session, idem)
    deep = "/".join(["d" * 240] * 9)
    entries = [
        {"op": "upsert", "path": f"{deep}/f{index}.txt", "kind": "file", "size": 1}
        for index in range(2000)
    ]
    before = await _listed(real_session, drive.id)
    refused = await holder.tree(entries)
    assert refused.status_code == 413, refused.text
    assert refused.json()["code"] == "files.batch_too_large"
    assert await _listed(real_session, drive.id) == before
