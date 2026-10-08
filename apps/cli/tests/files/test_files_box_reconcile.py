"""A box that takes a folder again recognises its files by node, not by name.

The story these pin: a box held a folder with ``notes.md`` (node N) in it and
went down; while it was down a person renamed N on the web, or trashed it; a
fresh process then took the folder again. Before the node map, that process met
the new name as a stranger to download and the old one as new work to push —
two files where the person renamed one, and a rename owed to the holder that
nothing could answer. Every assertion is read off the disk and a fake drive
that serves the lease, the tree and the bytes over a real ``httpx`` transport.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli.cloud.folder import ON_LAPSED_LEASE
from alkera_cli.files.live_sync import InboundEntry, LiveEntry, LiveSync
from alkera_cli.files.mount import MountRecord, load_record, mount, mount_record_path
from alkera_cli.files.nodemap import (
    KnownNode,
    NodeMap,
    NodeMapStore,
    known_for_pull,
    load_node_map,
    node_map_path,
    remember_pull,
    save_node_map,
)
from alkera_cli.files.pull import PullSummary
from alkera_cli.files.tree_watch import Change
from files._live_sync_fakes import FakeClock, FakeInboundApi, FakeWatcher
from files._live_sync_fakes import make_sync as _sync
from files._live_sync_fakes import write as _write

DRIVE = "11111111-1111-1111-1111-111111111111"
FOLDER = "folder-node"
FOLDER_PATH = "Shared/proj"
NOTE = "node-notes"
NOTE_BYTES = b"what the person wrote before the rename\n"
DOWNLOADABLE: dict[str, Any] = {"capabilities": {"can_read": True, "can_download": True}}


def _hash(payload: bytes) -> str:
    from blake3 import blake3

    return str(blake3(payload).hexdigest())


class _GoneError(RuntimeError):
    """What the SDK raises for an id the drive does not have."""

    status = 404


@dataclass
class _Node:
    name: str
    payload: bytes
    trashed: bool = False


@dataclass
class FakeDrive:
    """One leased folder of files: the Files namespace and the wire behind it."""

    nodes: dict[str, _Node] = field(default_factory=dict)
    fetched: list[str] = field(default_factory=list)

    # -- the namespace a mount and a pull read through --------------------

    def drive(self) -> dict[str, Any]:
        return {"id": DRIVE}

    def item(self, drive_id: str, item_id: str, **_: Any) -> dict[str, Any]:
        if item_id == FOLDER:
            return {"id": FOLDER, "kind": "folder", "etag": "7", "name": "proj", **DOWNLOADABLE}
        node = self.nodes.get(item_id)
        if node is None:
            raise _GoneError(item_id)
        return {"id": item_id, "kind": "file", "name": node.name, "trashed": node.trashed}

    def item_by_path(self, drive_id: str, item_path: str) -> dict[str, Any]:
        return self.item(drive_id, FOLDER)

    def children(self, drive_id: str, item_id: str, **_: Any) -> Iterator[dict[str, Any]]:
        if item_id != FOLDER:
            return
        for node_id, node in self.nodes.items():
            if node.trashed:
                continue
            yield {
                "id": node_id,
                "kind": "file",
                "name": node.name,
                "etag": "3",
                "file": {"size": len(node.payload), "content_hash": _hash(node.payload)},
                **DOWNLOADABLE,
            }

    # -- the wire: the lease routes and the bytes -------------------------

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/lease"):
            return httpx.Response(
                200,
                json={
                    "epoch": 4,
                    "expiresAt": "2099-01-01T00:00:00Z",
                    "heartbeatEvery": 15.0,
                    "syncInterval": 5.0,
                },
            )
        if path.endswith("/content"):
            node_id = path.rsplit("/", 2)[-2]
            self.fetched.append(node_id)
            return httpx.Response(302, headers={"location": f"https://content.test/c/{node_id}"})
        if path.startswith("/c/"):
            return httpx.Response(200, content=self.nodes[path.rsplit("/", 1)[-1]].payload)
        return httpx.Response(404, json={})


@pytest.fixture
def drive() -> FakeDrive:
    return FakeDrive(nodes={NOTE: _Node("notes.md", NOTE_BYTES)})


@pytest.fixture
def http(drive: FakeDrive) -> Iterator[httpx.Client]:
    with httpx.Client(transport=httpx.MockTransport(drive), base_url="https://api.test") as client:
        yield client


def _take(
    drive: FakeDrive, http: httpx.Client, root: Path, home: Path
) -> tuple[MountRecord, PullSummary]:
    """One take, as the box makes it: the map in, the pull, the map out."""
    remembered = load_node_map(root, home=home)
    record, pulled = mount(
        files=drive,
        http=http,
        source=FOLDER_PATH,
        node_id=FOLDER,
        root=root,
        machine="box",
        home=home,
        instance="box-instance",
        on_lapsed="overwrite",
        known=known_for_pull(remembered),
    )
    remember_pull(root, remembered, pulled, home=home)
    return record, pulled


def _fresh_sync(root: Path, api: FakeInboundApi, home: Path, pulled: PullSummary) -> LiveSync:
    """The live sync a restarted process starts on the folder it just took."""
    # One empty batch: the sweep of what is already on disk runs on the first.
    sync = _sync(root, api, FakeClock(), root_path=FOLDER_PATH, watcher=FakeWatcher([set()]))
    sync.node_map = NodeMapStore(root, home=home)
    sync.adopt({os.fsdecode(path): agreed for path, agreed in pulled.agreed.items()})
    sync.seed()
    return sync


def _sweep(sync: LiveSync) -> None:
    """The first thing a started sync does: offer the whole tree, then flush."""
    asyncio.run(sync.run())


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


# ---------------------------------------------------------------------------
# a rename made while the box was down
# ---------------------------------------------------------------------------


def test_a_rename_made_while_the_box_was_down_is_a_move_on_the_next_take(
    tmp_path: Path, drive: FakeDrive, http: httpx.Client
) -> None:
    root, home = tmp_path / "chat", tmp_path / "home"
    _take(drive, http, root, home)
    assert _files(root) == {"notes.md": NOTE_BYTES}
    drive.fetched.clear()

    # The person renames N on the web; the drive queues the rename for a holder
    # that is not running.
    drive.nodes[NOTE].name = "notes-renamed.md"

    _, pulled = _take(drive, http, root, home)

    assert _files(root) == {"notes-renamed.md": NOTE_BYTES}
    assert drive.fetched == [], "the bytes were already on the box; nothing is fetched"
    assert pulled.moved == {b"notes.md": b"notes-renamed.md"}

    # The restarted live sync pushes nothing new, and answers the rename.
    api = FakeInboundApi(root=root)
    api.queued = [InboundEntry(node_id=NOTE, state="inbound_rename", seq=5)]
    api.paths = {NOTE: f"{FOLDER_PATH}/notes-renamed.md"}
    sync = _fresh_sync(root, api, home, pulled)

    assert sync.pull_inbound() == [LiveEntry(node_id=NOTE, state="applied")]
    assert api.downloads == []
    _sweep(sync)
    assert api.uploads == [], "no second node is made for the old name"
    assert "notes.md" not in api.listed()
    assert _files(root) == {"notes-renamed.md": NOTE_BYTES}


def test_a_renamed_file_edited_on_the_box_is_not_moved(
    tmp_path: Path, drive: FakeDrive, http: httpx.Client
) -> None:
    """Bytes the drive has not seen are work: the move is only for a file that
    still holds what the two agreed on, and an edited one stays where it is."""
    root, home = tmp_path / "chat", tmp_path / "home"
    _take(drive, http, root, home)
    (root / "notes.md").write_bytes(b"an edit the drive never saw\n")
    drive.nodes[NOTE].name = "notes-renamed.md"

    _, pulled = _take(drive, http, root, home)

    assert pulled.moved == {}
    assert (root / "notes.md").read_bytes() == b"an edit the drive never saw\n"
    assert (root / "notes-renamed.md").read_bytes() == NOTE_BYTES


def test_a_rename_onto_a_name_the_box_already_uses_moves_nothing(
    tmp_path: Path, drive: FakeDrive, http: httpx.Client
) -> None:
    root, home = tmp_path / "chat", tmp_path / "home"
    _take(drive, http, root, home)
    (root / "notes-renamed.md").write_bytes(b"the box's own file under that name\n")
    drive.nodes[NOTE].name = "notes-renamed.md"

    _, pulled = _take(drive, http, root, home)

    assert pulled.moved == {}
    assert (root / "notes.md").read_bytes() == NOTE_BYTES


def test_a_rename_into_a_new_folder_moves_the_file_and_leaves_no_empty_folder(
    tmp_path: Path, drive: FakeDrive, http: httpx.Client
) -> None:
    """The file's node is what moved; the folder it left, emptied, would come
    back to the drive as a folder nobody has if it stayed on the box."""
    root, home = tmp_path / "chat", tmp_path / "home"
    drive.nodes[NOTE].name = "old.md"
    _take(drive, http, root, home)
    # The box moved it under a folder, and the map learned so.
    (root / "drafts").mkdir()
    os.replace(root / "old.md", root / "drafts" / "old.md")
    save_node_map(
        root,
        NodeMap(
            files={
                "drafts/old.md": KnownNode(
                    node_id=NOTE, size=len(NOTE_BYTES), content_hash=_hash(NOTE_BYTES)
                )
            }
        ),
        home=home,
    )
    drive.nodes[NOTE].name = "notes.md"

    _, pulled = _take(drive, http, root, home)

    assert pulled.moved == {b"drafts/old.md": b"notes.md"}
    assert _files(root) == {"notes.md": NOTE_BYTES}
    assert not (root / "drafts").exists()


# ---------------------------------------------------------------------------
# a delete made while the box was down
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "how",
    [
        pytest.param("trashed", id="the-drive-lists-it-trashed"),
        pytest.param("purged", id="the-drive-no-longer-has-the-node"),
    ],
)
def test_a_file_trashed_while_the_box_was_down_is_removed_not_pushed_back(
    tmp_path: Path, drive: FakeDrive, http: httpx.Client, how: str
) -> None:
    root, home = tmp_path / "chat", tmp_path / "home"
    _take(drive, http, root, home)
    if how == "trashed":
        drive.nodes[NOTE].trashed = True
    else:
        del drive.nodes[NOTE]

    _, pulled = _take(drive, http, root, home)

    assert pulled.removed == [b"notes.md"]
    assert _files(root) == {}
    api = FakeInboundApi(root=root)
    sync = _fresh_sync(root, api, home, pulled)
    _sweep(sync)
    assert api.uploads == []
    assert load_node_map(root, home=home) == NodeMap(files={})


def test_a_file_whose_node_merely_left_the_folder_stays(
    tmp_path: Path, drive: FakeDrive, http: httpx.Client
) -> None:
    """Absent from this folder but alive elsewhere on the drive is a move out,
    not a delete: the box does not remove what it cannot prove was deleted."""
    root, home = tmp_path / "chat", tmp_path / "home"
    _take(drive, http, root, home)
    moved_out = drive.nodes.pop(NOTE)

    class Elsewhere(FakeDrive):
        def item(self, drive_id: str, item_id: str, **kwargs: Any) -> dict[str, Any]:
            if item_id == NOTE:
                return {"id": NOTE, "kind": "file", "name": moved_out.name, "trashed": False}
            return super().item(drive_id, item_id, **kwargs)

    elsewhere = Elsewhere(nodes=drive.nodes)
    with httpx.Client(transport=httpx.MockTransport(elsewhere), base_url="https://api.test") as h:
        _, pulled = _take(elsewhere, h, root, home)

    assert pulled.removed == []
    assert (root / "notes.md").read_bytes() == NOTE_BYTES


def test_a_trashed_file_edited_on_the_box_is_kept(
    tmp_path: Path, drive: FakeDrive, http: httpx.Client
) -> None:
    root, home = tmp_path / "chat", tmp_path / "home"
    _take(drive, http, root, home)
    (root / "notes.md").write_bytes(b"work after the last push\n")
    drive.nodes[NOTE].trashed = True

    _, pulled = _take(drive, http, root, home)

    assert pulled.removed == []
    assert (root / "notes.md").read_bytes() == b"work after the last push\n"


# ---------------------------------------------------------------------------
# what the map does not know about
# ---------------------------------------------------------------------------


def test_a_file_no_holder_knew_a_node_for_is_still_pushed(
    tmp_path: Path, drive: FakeDrive, http: httpx.Client
) -> None:
    """The union rule survives: a file the box wrote and never pushed is kept
    on the take and goes up with the next push, whatever else the map did."""
    root, home = tmp_path / "chat", tmp_path / "home"
    _take(drive, http, root, home)
    _write(root, "unpushed.md", b"written just before the crash\n")
    drive.nodes[NOTE].name = "notes-renamed.md"

    _, pulled = _take(drive, http, root, home)

    assert (root / "unpushed.md").read_bytes() == b"written just before the crash\n"
    api = FakeInboundApi(root=root)
    sync = _fresh_sync(root, api, home, pulled)
    _sweep(sync)
    assert api.uploads == ["unpushed.md"]


def test_a_first_take_moves_and_removes_nothing(
    tmp_path: Path, drive: FakeDrive, http: httpx.Client
) -> None:
    """No map, today's behaviour: the drive's names come down and a local file
    it lacks stays for the union push."""
    root, home = tmp_path / "chat", tmp_path / "home"
    _write(root, "notes.md", NOTE_BYTES)
    drive.nodes[NOTE].name = "notes-renamed.md"

    _, pulled = _take(drive, http, root, home)

    assert pulled.moved == {} and pulled.removed == []
    assert _files(root) == {"notes.md": NOTE_BYTES, "notes-renamed.md": NOTE_BYTES}


# ---------------------------------------------------------------------------
# the live sync keeps the map, and starts from it
# ---------------------------------------------------------------------------


def test_the_live_sync_writes_down_the_node_of_a_file_it_pushed(tmp_path: Path) -> None:
    root, home = tmp_path / "chat", tmp_path / "home"
    api = FakeInboundApi(root=root)
    sync = _sync(root, api, FakeClock())
    sync.node_map = NodeMapStore(root, home=home)
    _write(root, "draft.md", b"the agent's draft\n")

    sync.classify(Change.added, str(root / "draft.md"))
    sync.flush()

    remembered = load_node_map(root, home=home)
    assert remembered is not None
    known = remembered.files["draft.md"]
    assert known.node_id == api.nodes["draft.md"]
    assert (known.content_hash, known.size) == (_hash(b"the agent's draft\n"), 18)


def test_the_map_is_written_no_more_often_than_the_debounce_and_the_last_word_lands(
    tmp_path: Path,
) -> None:
    root, home = tmp_path / "chat", tmp_path / "home"
    api = FakeInboundApi(root=root)
    clock = FakeClock()
    sync = _sync(root, api, clock)
    sync.node_map = NodeMapStore(root, home=home)
    for name in ("one.md", "two.md"):
        _write(root, name, name.encode())
        sync.classify(Change.added, str(root / name))
        sync.flush()

    within = load_node_map(root, home=home)
    assert within is not None and set(within.files) == {"one.md"}

    sync.drain(0.0)
    final = load_node_map(root, home=home)
    assert final is not None and set(final.files) == {"one.md", "two.md"}


def test_a_restarted_sync_applies_a_queued_rename_as_a_move(tmp_path: Path) -> None:
    """A process that pulled nothing still knows, from the map, which file the
    renamed node is: the rename is a move, never a download beside it."""
    root, home = tmp_path / "chat", tmp_path / "home"
    _write(root, "notes.md", NOTE_BYTES)
    save_node_map(
        root,
        NodeMap(
            files={
                "notes.md": KnownNode(
                    node_id=NOTE, size=len(NOTE_BYTES), content_hash=_hash(NOTE_BYTES)
                )
            }
        ),
        home=home,
    )
    api = FakeInboundApi(root=root)
    api.queued = [InboundEntry(node_id=NOTE, state="inbound_rename", seq=2)]
    api.paths = {NOTE: f"{FOLDER_PATH}/notes-renamed.md"}
    api.contents = {NOTE: NOTE_BYTES}
    sync = _sync(root, api, FakeClock(), root_path=FOLDER_PATH)
    sync.node_map = NodeMapStore(root, home=home)

    assert sync.seed() == 1
    assert sync.pull_inbound() == [LiveEntry(node_id=NOTE, state="applied")]

    assert api.downloads == []
    assert _files(root) == {"notes-renamed.md": NOTE_BYTES}
    remembered = load_node_map(root, home=home)
    assert remembered is not None and set(remembered.files) == {"notes-renamed.md"}


@pytest.mark.parametrize(
    "on_disk",
    [
        pytest.param(NOTE_BYTES + b"the agent's line\n", id="edited-while-down"),
        pytest.param(NOTE_BYTES, id="untouched"),
    ],
)
def test_a_restarted_sync_still_names_the_version_its_file_was_made_on(
    tmp_path: Path, on_disk: bytes
) -> None:
    """The box went down holding ``notes.md`` as the drive's etag 7, and the
    agent wrote to it while the sync was not running. The edit is made on
    etag 7, and the upload says so: fenced on the drive's head instead, it
    claims a version it never saw, and whatever people wrote since reads as
    removed by the agent (a live file's merge then drops all of it)."""
    root, home = tmp_path / "chat", tmp_path / "home"
    _write(root, "notes.md", on_disk)
    save_node_map(
        root,
        NodeMap(
            files={
                "notes.md": KnownNode(
                    node_id=NOTE,
                    size=len(NOTE_BYTES),
                    content_hash=_hash(NOTE_BYTES),
                    etag="7",
                )
            }
        ),
        home=home,
    )
    api = FakeInboundApi(root=root)
    sync = _sync(root, api, FakeClock(), root_path=FOLDER_PATH)
    sync.node_map = NodeMapStore(root, home=home)
    assert sync.seed() == 1

    sync.classify(Change.modified, str(root / "notes.md"))
    sync.flush()

    if on_disk == NOTE_BYTES:
        assert api.uploads == []
        return
    [(path, base)] = api.bases
    assert path == "notes.md"
    assert base is not None and (base.etag, base.content_hash) == ("7", _hash(NOTE_BYTES))


def test_the_store_keeps_what_lies_outside_the_watched_directory(tmp_path: Path) -> None:
    """A sync watching ``scratch`` inside the folder rewrites its own part of
    the map and nothing else; the pull's entries for the rest survive."""
    root, home = tmp_path / "chat", tmp_path / "home"
    outside = KnownNode(node_id="manifest-node", size=2, content_hash="h")
    save_node_map(root, NodeMap(files={"manifest.json": outside}), home=home)
    store = NodeMapStore(root, inside="scratch", home=home)

    store.save({"a.md": KnownNode(node_id="a-node")})

    assert store.load() == {"a.md": KnownNode(node_id="a-node")}
    remembered = load_node_map(root, home=home)
    assert remembered is not None
    assert remembered.files == {
        "manifest.json": outside,
        "scratch/a.md": KnownNode(node_id="a-node"),
    }


# ---------------------------------------------------------------------------
# the map on disk
# ---------------------------------------------------------------------------


def test_the_map_survives_a_restart_beside_the_record_and_is_not_read_as_one(
    tmp_path: Path, drive: FakeDrive, http: httpx.Client
) -> None:
    root, home = tmp_path / "chat", tmp_path / "home"
    _take(drive, http, root, home)

    remembered = load_node_map(root, home=home)
    assert remembered is not None
    assert remembered.files["notes.md"].node_id == NOTE
    assert load_record(root, home=home) is not None
    where = node_map_path(root, home=home)
    assert where.parent == mount_record_path(root, home=home).parent
    assert where.suffix != ".json"
    if os.name != "nt":
        assert where.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "contents",
    [
        pytest.param(None, id="no-map"),
        pytest.param("{not json", id="a-torn-map"),
        pytest.param("[1, 2]", id="not-an-object"),
    ],
)
def test_a_record_with_no_readable_map_takes_like_a_first_take(
    tmp_path: Path, contents: str | None
) -> None:
    root, home = tmp_path / "chat", tmp_path / "home"
    if contents is not None:
        where = node_map_path(root, home=home)
        where.parent.mkdir(parents=True)
        where.write_text(contents)

    assert load_node_map(root, home=home) is None
    assert known_for_pull(load_node_map(root, home=home)) is None


_FIXTURES = Path(__file__).parent / "fixtures" / "node_map"


@pytest.mark.parametrize(
    "fixture", sorted(_FIXTURES.glob("v*.json")), ids=lambda path: Path(path).stem
)
def test_every_map_a_past_writer_produced_still_loads(fixture: Path) -> None:
    node_map = NodeMap.model_validate(json.loads(fixture.read_text(encoding="utf-8")))

    assert node_map.files["notes.md"].node_id == NOTE
    assert node_map.schema_version == NodeMap.SCHEMA_VERSION


def test_the_map_corpus_covers_the_version_this_build_writes() -> None:
    versions = {path.stem for path in _FIXTURES.glob("v*.json")}
    assert f"v{NodeMap.SCHEMA_VERSION.replace('.', '_')}" in versions


# ---------------------------------------------------------------------------
# a lease that lapsed under the box (a restart cut its upload)
# ---------------------------------------------------------------------------


class _LapsingDrive(FakeDrive):
    """A drive whose lease comes back at a newer epoch on every take: the
    box's lease lapsed while it was down, as after a restart past the TTL."""

    epoch: int = 4

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/lease"):
            self.epoch += 1
            return httpx.Response(
                200,
                json={
                    "epoch": self.epoch,
                    "expiresAt": "2099-01-01T00:00:00Z",
                    "heartbeatEvery": 15.0,
                    "syncInterval": 5.0,
                },
            )
        return super().__call__(request)


def _take_as_the_box(
    drive: FakeDrive, http: httpx.Client, root: Path, home: Path
) -> tuple[MountRecord, PullSummary]:
    """A take with the policy the box uses for a lease that lapsed."""
    remembered = load_node_map(root, home=home)
    record, pulled = mount(
        files=drive,
        http=http,
        source=FOLDER_PATH,
        node_id=FOLDER,
        root=root,
        machine="box",
        home=home,
        instance="box-instance",
        on_lapsed=ON_LAPSED_LEASE,
        known=known_for_pull(remembered),
    )
    remember_pull(root, remembered, pulled, home=home)
    return record, pulled


BOX_EDIT = b"what the agent wrote before the restart\n"


def _staged(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*.alkera-conflict")
    }


@pytest.mark.parametrize(
    ("drive_edit", "on_disk", "set_aside"),
    [
        pytest.param(None, BOX_EDIT, None, id="only-the-box-moved"),
        pytest.param(
            b"what a person saved meanwhile\n",
            b"what a person saved meanwhile\n",
            BOX_EDIT,
            id="both-moved",
        ),
    ],
)
def test_a_box_retaking_its_folder_after_its_lease_lapsed_keeps_its_unsent_work(
    tmp_path: Path, drive_edit: bytes | None, on_disk: bytes, set_aside: bytes | None
) -> None:
    """The agent wrote ``notes.md``, the box restarted before the upload
    landed (the old process's upload was refused under the newer epoch), and
    the lease came back at a new epoch. The drive still holds what the two
    last agreed, so the bytes on the box are its own unsent work: they stay,
    and the union push sends them. Where the drive moved too, its bytes take
    the name and the box's are set aside beside it, never dropped (they used
    to be written over with no copy anywhere)."""
    drive = _LapsingDrive(nodes={NOTE: _Node("notes.md", NOTE_BYTES)})
    root, home = tmp_path / "chat", tmp_path / "home"
    with httpx.Client(transport=httpx.MockTransport(drive), base_url="https://api.test") as http:
        first, _ = _take_as_the_box(drive, http, root, home)
        (root / "notes.md").write_bytes(BOX_EDIT)
        if drive_edit is not None:
            drive.nodes[NOTE].payload = drive_edit

        second, pulled = _take_as_the_box(drive, http, root, home)

    assert second.epoch > first.epoch, "the lease lapsed and came back at a new epoch"
    assert (root / "notes.md").read_bytes() == on_disk
    staged = _staged(root)
    assert list(staged.values()) == ([] if set_aside is None else [set_aside])
    assert [os.fsdecode(path) for path in pulled.displaced_paths] == list(staged)


def test_the_box_s_set_aside_work_becomes_a_conflicted_copy_of_the_drive_s_file(
    tmp_path: Path,
) -> None:
    """What the take set aside reaches the drive: the sync that starts on the
    folder files it as a conflicted copy of the node, under the name the
    drive gives it, and the staging name is gone. Both texts are saved."""
    drive = _LapsingDrive(nodes={NOTE: _Node("notes.md", NOTE_BYTES)})
    root, home = tmp_path / "chat", tmp_path / "home"
    theirs = b"what a person saved meanwhile\n"
    with httpx.Client(transport=httpx.MockTransport(drive), base_url="https://api.test") as http:
        _take_as_the_box(drive, http, root, home)
        (root / "notes.md").write_bytes(BOX_EDIT)
        drive.nodes[NOTE].payload = theirs
        _, pulled = _take_as_the_box(drive, http, root, home)

    api = FakeInboundApi(root=root)
    api.nodes["notes.md"] = NOTE
    _sweep(_fresh_sync(root, api, home, pulled))

    assert [(of, data) for _copy, of, data in api.copies.values()] == [(NOTE, BOX_EDIT)]
    assert _staged(root) == {}
    assert (root / "notes.md").read_bytes() == theirs
    assert api.stored.get("notes.md") != BOX_EDIT, "the box's text went up over theirs"


def test_a_tree_kept_after_its_lease_was_let_go_is_taken_back_without_losing_work(
    tmp_path: Path,
) -> None:
    """A hand back that found no folder lets the lease go and keeps the tree
    on disk (with its node map) for the next take to push; the record goes.
    That next take used to read as a first take and write the drive's bytes
    over the box's unsent edit. It is taken back as a lapsed lease is: the
    drive's bytes take the name and the box's are set aside."""
    from alkera_cli.files.mount import remove_record

    drive = _LapsingDrive(nodes={NOTE: _Node("notes.md", NOTE_BYTES)})
    root, home = tmp_path / "chat", tmp_path / "home"
    theirs = b"what a person saved meanwhile\n"
    with httpx.Client(transport=httpx.MockTransport(drive), base_url="https://api.test") as http:
        _take_as_the_box(drive, http, root, home)
        (root / "notes.md").write_bytes(BOX_EDIT)
        remove_record(root, home=home)
        drive.nodes[NOTE].payload = theirs

        _, pulled = _take_as_the_box(drive, http, root, home)

    assert (root / "notes.md").read_bytes() == theirs
    assert list(_staged(root).values()) == [BOX_EDIT]
    assert len(pulled.displaced_paths) == 1


# ---------------------------------------------------------------------------
# a workspace's shared tree, taken again
# ---------------------------------------------------------------------------

SHARED = "files-node"
RECORDS = "chats-node"
WORKSPACE_KEY = "ws:8a7c1f2e-0000-4000-8000-000000000001"


@dataclass
class WorkspaceDrive(FakeDrive):
    """A workspace's folder: the shared ``files/`` tree holding the nodes, and
    the chats' ``.chats/`` records beside it, which a workspace take never
    walks."""

    def item(self, drive_id: str, item_id: str, **_: Any) -> dict[str, Any]:
        if item_id == SHARED:
            return {"id": SHARED, "kind": "folder", "name": "files", **DOWNLOADABLE}
        return super().item(drive_id, item_id)

    def children(self, drive_id: str, item_id: str, **_: Any) -> Iterator[dict[str, Any]]:
        if item_id == FOLDER:
            yield {"id": SHARED, "kind": "folder", "name": "files", **DOWNLOADABLE}
            yield {"id": RECORDS, "kind": "folder", "name": ".chats", **DOWNLOADABLE}
            return
        if item_id == SHARED:
            yield from super().children(drive_id, FOLDER)


def _take_workspace(
    drive: FakeDrive, http: httpx.Client, root: Path, home: Path
) -> tuple[MountRecord, PullSummary]:
    """One take of a workspace's folder, as the box's custody makes it: the
    workspace's own pull (the shared tree only) with the node map in."""
    from alkera_cli.cloud.custody_layout import PULL_LOCAL, CustodyLayout

    layout = CustodyLayout.beside(root.parent / "chats", root.parent)
    remembered = load_node_map(root, home=home)
    record, pulled = mount(
        files=drive,
        http=http,
        source=FOLDER_PATH,
        node_id=FOLDER,
        root=root,
        machine="box",
        home=home,
        instance="box-instance",
        purpose="workspace",
        on_lapsed="overwrite",
        pull_tree=layout.pull(WORKSPACE_KEY, PULL_LOCAL),
        known=known_for_pull(remembered),
    )
    remember_pull(root, remembered, pulled, home=home)
    return record, pulled


def test_a_shared_file_trashed_while_the_box_was_down_is_removed_not_pushed_back(
    tmp_path: Path,
) -> None:
    """Seen on a woken box: a file a person trashed from a workspace came back.
    The workspace take carries the node map like a chat's, so the shared file
    the drive trashed meanwhile is removed from the box rather than pushed up
    again as new work."""
    drive = WorkspaceDrive(nodes={NOTE: _Node("notes.md", NOTE_BYTES)})
    with httpx.Client(transport=httpx.MockTransport(drive), base_url="https://api.test") as http:
        root, home = tmp_path / "workspaces" / "ws-1", tmp_path / "home"
        _take_workspace(drive, http, root, home)
        assert _files(root) == {"files/notes.md": NOTE_BYTES}
        drive.nodes[NOTE].trashed = True

        _, pulled = _take_workspace(drive, http, root, home)

    assert pulled.removed == [b"files/notes.md"]
    assert _files(root) == {}
    assert load_node_map(root, home=home) == NodeMap(files={})


def _wake_workspace(
    drive: FakeDrive, http: httpx.Client, root: Path, home: Path
) -> tuple[MountRecord, PullSummary]:
    """A workspace take exactly as the box's custody makes it: the lease asks
    for the writes people make while the box holds it (``inbound``), with the
    box's policy for a lease that lapsed and the node map in."""
    from alkera_cli.cloud.custody_layout import PULL_LOCAL, CustodyLayout

    layout = CustodyLayout.beside(root.parent / "chats", root.parent)
    remembered = load_node_map(root, home=home)
    record, pulled = mount(
        files=drive,
        http=http,
        source=FOLDER_PATH,
        node_id=FOLDER,
        root=root,
        machine="box",
        home=home,
        instance="box-instance",
        purpose="workspace",
        inbound=True,
        live=True,
        on_lapsed=ON_LAPSED_LEASE,
        pull_tree=layout.pull(WORKSPACE_KEY, PULL_LOCAL),
        known=known_for_pull(remembered),
    )
    remember_pull(root, remembered, pulled, home=home)
    return record, pulled


RESTORED = b"the version a person restored while the box slept\n"
AGENT_EDIT = NOTE_BYTES + b"the agent's line\n"


@pytest.mark.parametrize(
    ("box_edit", "drive_edit", "on_disk"),
    [
        pytest.param(None, RESTORED, RESTORED, id="drive-moved-box-did-not"),
        pytest.param(AGENT_EDIT, None, AGENT_EDIT, id="box-moved-drive-did-not"),
        pytest.param(AGENT_EDIT, RESTORED, AGENT_EDIT, id="both-moved"),
        pytest.param(None, None, NOTE_BYTES, id="neither-moved"),
    ],
)
def test_a_woken_box_takes_what_the_drive_holds_unless_it_has_its_own_change(
    tmp_path: Path, box_edit: bytes | None, drive_edit: bytes | None, on_disk: bytes
) -> None:
    """A box took a workspace, every chat in it slept, and the lease never
    lapsed (the hand back did not land, or the box restarted inside the TTL):
    the next take comes back at the same epoch. The lease takes people's
    writes, so the box was never the only writer: a file still holding what
    the two last agreed is behind the drive and takes its bytes (it used to be
    kept, the agent read the replaced text and the push sent it back over the
    restore). A file the box changed is kept: where the drive moved too, the
    push files it on its agreed base and the drive keeps both."""
    drive = WorkspaceDrive(nodes={NOTE: _Node("notes.md", NOTE_BYTES)})
    with httpx.Client(transport=httpx.MockTransport(drive), base_url="https://api.test") as http:
        root, home = tmp_path / "workspaces" / "ws-1", tmp_path / "home"
        first, _ = _wake_workspace(drive, http, root, home)
        if box_edit is not None:
            (root / "files/notes.md").write_bytes(box_edit)
        if drive_edit is not None:
            drive.nodes[NOTE].payload = drive_edit

        second, pulled = _wake_workspace(drive, http, root, home)

    assert second.epoch == first.epoch, "the lease was handed straight back"
    assert _files(root) == {"files/notes.md": on_disk}
    assert pulled.kept_paths == ([b"files/notes.md"] if box_edit is not None else [])
    remembered = load_node_map(root, home=home)
    assert remembered is not None
    agreed = remembered.files["files/notes.md"].content_hash
    assert agreed == _hash(drive_edit if box_edit is None and drive_edit else NOTE_BYTES)


@pytest.mark.parametrize(
    ("on_disk", "copied"),
    [
        pytest.param(NOTE_BYTES, None, id="untouched-takes-the-restore"),
        pytest.param(AGENT_EDIT, AGENT_EDIT, id="edited-keeps-both"),
    ],
)
def test_a_restore_owed_to_a_restarted_sync_lands_and_a_genuine_edit_is_kept_beside_it(
    tmp_path: Path, on_disk: bytes, copied: bytes | None
) -> None:
    """The box held ``notes.md`` as the drive's etag 7 and went down; a person
    restored an older version, which the drive owes the box. The next process
    takes the restore: a file still holding the agreed bytes is replaced and
    nothing goes back up, no copy is made. Only bytes the box changed itself
    survive as a conflicted copy beside the restored file."""
    root, home = tmp_path / "chat", tmp_path / "home"
    _write(root, "notes.md", on_disk)
    save_node_map(
        root,
        NodeMap(
            files={
                "notes.md": KnownNode(
                    node_id=NOTE, size=len(NOTE_BYTES), content_hash=_hash(NOTE_BYTES), etag="7"
                )
            }
        ),
        home=home,
    )
    api = FakeInboundApi(root=root)
    api.nodes["notes.md"] = NOTE
    api.paths = {NOTE: f"{FOLDER_PATH}/notes.md"}
    api.contents = {NOTE: RESTORED}
    api.etags[NOTE] = 8
    api.queued = [InboundEntry(node_id=NOTE, state="inbound", seq=1)]
    sync = _sync(root, api, FakeClock(), root_path=FOLDER_PATH)
    sync.node_map = NodeMapStore(root, home=home)
    assert sync.seed() == 1

    [answered] = sync.pull_inbound()
    sync.classify(Change.modified, str(root / "notes.md"))
    sync.flush()

    assert (root / "notes.md").read_bytes() == RESTORED
    assert api.uploads == []
    assert answered.node_id == NOTE and answered.state == "applied"
    assert api.copy_bytes() == ({} if copied is None else {answered.displaced: copied})
