"""The drive asks the machine for a file, and the machine answers.

The folder is a real directory, the live sync a real :class:`LiveSync` over the
fake drive the live-push suites use (``files._live_sync_fakes``), so an
``accepted`` answer is checked by what the drive then receives and in what
order — and every refusal by what the drive does NOT receive.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.cloud.folder import HeldFolder
from alkera_cli.cloud.machine_requests import MachineRequests, parse_request
from alkera_cli.cloud.transport import LaterAnswer
from alkera_cli.files.live_sync import LiveSync
from alkera_cli.files.push import PushSummary
from alkera_cli.files.tree_watch import Change
from files._live_sync_fakes import FakeClock, FakeLiveApi
from files._live_sync_fakes import make_sync as _sync
from files._live_sync_fakes import write as _write

LEASE = "chat-node"
MIB = 1_048_576


class Folders:
    """The folders a box holds, as the handler looks them up, and the drive a
    checkpoint push of one lands in."""

    def __init__(self, *held: HeldFolder, refused: tuple[str, ...] = ()) -> None:
        self.by_lease = {folder.record.node_id: folder for folder in held}
        self.by_chat = {folder.chat_id: folder for folder in held}
        self.drive: dict[str, bytes] = {}
        self.refused = refused
        self.unreachable = False

    def held_by_lease_node(self, lease_node_id: str) -> HeldFolder | None:
        return self.by_lease.get(lease_node_id)

    def push(self, chat_id: str) -> PushSummary | None:
        """What the box's checkpoint push of the folder lands: every file
        under it, less what the drive refuses."""
        if self.unreachable:
            return None
        root = self.by_chat[chat_id].root
        summary = PushSummary()
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            if not path.is_file():
                continue
            if relative in self.refused:
                summary.failed.append(relative)
                continue
            self.drive[relative] = path.read_bytes()
            summary.uploaded += 1
        return summary


class Box:
    """One held chat folder with its working directory streaming live."""

    def __init__(self, tmp_path: Path, *, epoch: int = 5) -> None:
        self.folder = tmp_path / "chats" / "chat-7"
        self.working = self.folder / "scratch"
        self.working.mkdir(parents=True)
        self.clock = FakeClock()
        self.api = FakeLiveApi(root=self.working)
        self.sync: LiveSync = _sync(self.working, self.api, self.clock)
        self.sync.record = self.sync.record.model_copy(update={"epoch": epoch})
        self.held = HeldFolder(
            chat_id="chat-7",
            org_path="home/ana/Chats/Kickoff.alkerachat",
            root=self.folder,
            record=self.sync.record,
            live=self.sync,
        )

    def write(self, relative: str, data: bytes) -> Path:
        return _write(self.working, relative, data)

    def queue(self, relative: str, data: bytes) -> Path:
        path = self.write(relative, data)
        self.sync.classify(Change.added, str(path))
        return path


def _frame(
    path: str,
    *,
    on_disk: Path | None = None,
    lease: str = LEASE,
    size: int | None = None,
    mtime_ns: int | None = None,
    request_id: str = "req-1",
    **extra: Any,
) -> dict[str, Any]:
    """A promote frame naming ``path`` from the lease root, expecting what is
    on disk at ``on_disk`` unless told otherwise."""
    if on_disk is not None:
        info = on_disk.stat()
        size = info.st_size if size is None else size
        mtime_ns = info.st_mtime_ns if mtime_ns is None else mtime_ns
    return {
        "t": "machine.request",
        "request_id": request_id,
        "kind": "promote",
        "lease_node_id": lease,
        "node_id": "node-of-the-file",
        "path": path,
        "expected": {"size": size or 0, "mtime_ns": mtime_ns or 0},
        "deadline_ms": 8000,
        **extra,
    }


def _outcome(handler: MachineRequests, frame: dict[str, Any]) -> str:
    ack = handler.handle(frame)
    assert ack is not None
    assert ack["t"] == "machine.ack"
    assert ack["request_id"] == frame["request_id"]
    return str(ack["outcome"])


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


# -- accepted ---------------------------------------------------------------


def test_a_promote_puts_the_file_ahead_of_everything_queued_before_it(box: Box) -> None:
    """The note and the export were queued first and would go first. The
    reader is waiting on the dataset, so it lands first."""
    box.queue("note.md", b"a short note")
    box.queue("export.csv", b"e" * (2 * MIB))
    dataset = box.queue("data/big.csv", b"d" * (3 * MIB))
    handler = MachineRequests(Folders(box.held))

    assert _outcome(handler, _frame("scratch/data/big.csv", on_disk=dataset)) == "accepted"
    box.sync.flush()

    assert box.api.uploads == ["data/big.csv", "note.md", "export.csv"]
    assert box.api.stored["data/big.csv"] == b"d" * (3 * MIB)


def test_the_ack_is_the_contracts_shape(box: Box) -> None:
    report = box.write("report.md", b"q3")
    handler = MachineRequests(Folders(box.held))

    ack = handler.handle(_frame("scratch/report.md", on_disk=report, request_id="r-42"))

    assert ack == {"t": "machine.ack", "request_id": "r-42", "outcome": "accepted"}


def test_a_folder_whose_live_root_is_the_lease_root_takes_the_path_as_it_is(
    tmp_path: Path,
) -> None:
    box = Box(tmp_path)
    flat = HeldFolder(
        chat_id="chat-7", org_path="x", root=box.working, record=box.sync.record, live=box.sync
    )
    report = box.write("report.md", b"q3")

    outcome = _outcome(MachineRequests(Folders(flat)), _frame("report.md", on_disk=report))
    box.sync.flush()

    assert outcome == "accepted"
    assert box.api.uploads == ["report.md"]


# -- not the holder -----------------------------------------------------------


def test_a_lease_this_box_does_not_hold_is_not_holder(box: Box) -> None:
    report = box.write("report.md", b"q3")
    handler = MachineRequests(Folders(box.held))

    outcome = _outcome(handler, _frame("scratch/report.md", on_disk=report, lease="other-lease"))
    box.sync.flush()

    assert outcome == "not_holder"
    assert box.api.uploads == []


def test_a_closed_fence_is_not_holder(box: Box) -> None:
    """Thirty seconds without a beat: this holder cannot prove the folder is
    its own, so it promises nothing."""
    report = box.write("report.md", b"q3")
    handler = MachineRequests(Folders(box.held))
    box.clock.advance(31)

    assert _outcome(handler, _frame("scratch/report.md", on_disk=report)) == "not_holder"
    assert box.sync.promotions == frozenset()


def test_a_request_naming_a_newer_epoch_is_not_holder(box: Box) -> None:
    """The lease was handed over: the drive holds it at an epoch this box
    never had."""
    report = box.write("report.md", b"q3")
    handler = MachineRequests(Folders(box.held))

    outcome = _outcome(handler, _frame("scratch/report.md", on_disk=report, epoch=6))

    assert outcome == "not_holder"
    assert box.sync.promotions == frozenset()


def test_a_request_naming_the_held_epoch_is_answered(box: Box) -> None:
    report = box.write("report.md", b"q3")
    handler = MachineRequests(Folders(box.held))

    assert _outcome(handler, _frame("scratch/report.md", on_disk=report, epoch=5)) == "accepted"


def test_a_sync_still_writing_under_an_epoch_the_folder_moved_past_is_not_holder(
    box: Box,
) -> None:
    report = box.write("report.md", b"q3")
    moved = HeldFolder(
        chat_id="chat-7",
        org_path=box.held.org_path,
        root=box.folder,
        record=box.sync.record.model_copy(update={"epoch": 6}),
        live=box.sync,
    )

    outcome = _outcome(MachineRequests(Folders(moved)), _frame("scratch/report.md", on_disk=report))

    assert outcome == "not_holder"


def test_a_folder_held_without_the_live_plane_is_not_holder(box: Box) -> None:
    report = box.write("report.md", b"q3")
    dark = HeldFolder(
        chat_id="chat-7", org_path="x", root=box.folder, record=box.sync.record, live=None
    )

    outcome = _outcome(MachineRequests(Folders(dark)), _frame("scratch/report.md", on_disk=report))

    assert outcome == "not_holder"


# -- missing ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("scratch/nothing-here.md", id="no-such-file"),
        pytest.param("scratch/data", id="a-directory"),
        pytest.param("manifest.json", id="beside-the-live-root"),
        pytest.param("scratch/../manifest.json", id="dot-dot-out"),
        pytest.param("scratch//report.md", id="an-empty-segment"),
        pytest.param("scratch/", id="the-live-root-itself"),
        pytest.param("/etc/passwd", id="absolute"),
        pytest.param("scratch/re\x00port.md", id="a-nul"),
    ],
)
def test_a_path_that_names_no_file_in_the_live_root_is_missing(box: Box, path: str) -> None:
    box.write("data/rows.csv", b"1\n")
    (box.folder / "manifest.json").write_bytes(b"{}")
    # A file of the same name inside the live root, so a path read without
    # its step down from the lease would find one.
    box.write("manifest.json", b"{}")
    handler = MachineRequests(Folders(box.held))

    assert _outcome(handler, _frame(path, size=2, mtime_ns=1)) == "missing"
    box.sync.flush()
    assert box.api.uploads == []


@pytest.mark.parametrize(
    "planted",
    [
        pytest.param("report.md", id="a-link-as-the-file"),
        pytest.param("reports/q3.md", id="a-link-as-a-folder-on-the-way"),
    ],
)
def test_a_planted_link_out_of_the_root_is_missing_and_never_read(
    box: Box, tmp_path: Path, planted: str
) -> None:
    """The file the link points at matches what the drive expects byte for
    byte and stamp for stamp — the answer is still ``missing``, and the drive
    never receives it."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    secret = outside / "q3.md"
    secret.write_bytes(b"host credentials")
    if planted == "report.md":
        (box.working / "report.md").symlink_to(secret)
    else:
        (box.working / "reports").symlink_to(outside)
    handler = MachineRequests(Folders(box.held))

    outcome = _outcome(handler, _frame(f"scratch/{planted}", on_disk=secret))
    box.sync.flush()

    assert outcome == "missing"
    assert box.sync.promotions == frozenset()
    assert box.api.uploads == []


# -- changed ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("size_delta", "mtime_delta"),
    [
        pytest.param(1, 0, id="another-size"),
        pytest.param(0, 1_000, id="another-mtime"),
    ],
)
def test_a_file_that_is_not_the_one_the_drive_listed_is_changed_with_what_is_seen(
    box: Box, size_delta: int, mtime_delta: int
) -> None:
    report = box.write("report.md", b"revised")
    info = report.stat()
    handler = MachineRequests(Folders(box.held))

    ack = handler.handle(
        _frame(
            "scratch/report.md",
            size=info.st_size + size_delta,
            mtime_ns=info.st_mtime_ns + mtime_delta,
        )
    )

    assert ack is not None
    assert ack["outcome"] == "changed"
    assert ack["observed"] == {"size": info.st_size, "mtime_ns": info.st_mtime_ns}
    assert box.sync.promotions == frozenset()


def test_a_changed_answer_about_a_file_stamped_before_1970_is_one_the_gateway_reads(
    box: Box,
) -> None:
    """A file's modified time can be before the epoch (an archive extracted
    as it was stamped, ``touch -t 1969…``), and the box reports what it sees.
    The gateway closes a machine's socket on any ack it cannot parse — so an
    honest answer it refused would cut the box's whole socket, its chats'
    streams with it, every time a reader asked for that file."""
    import json

    from alkera_core.schemas.realtime.machine import parse_machine_frame

    report = box.write("report.md", b"revised")
    os.utime(report, ns=(0, -1_000_000_000))
    info = report.stat()
    handler = MachineRequests(Folders(box.held))

    ack = handler.handle(
        _frame(
            "scratch/report.md",
            size=info.st_size + 1,
            mtime_ns=1,
            request_id="0b6f3c1e-8a4d-4c55-9e2a-7d1f0c3b2a11",
        )
    )

    assert ack is not None
    assert ack["observed"] == {"size": info.st_size, "mtime_ns": info.st_mtime_ns}
    read = parse_machine_frame(json.dumps(ack))
    assert read.outcome == "changed"
    assert read.observed is not None and read.observed.mtime_ns == info.st_mtime_ns


# -- busy -------------------------------------------------------------------------


def test_past_the_in_flight_cap_the_machine_is_busy_until_one_lands(box: Box) -> None:
    files = {name: box.write(name, name.encode()) for name in ("a.md", "b.md", "c.md", "d.md")}
    handler = MachineRequests(Folders(box.held), inflight=2)

    first = _outcome(handler, _frame("scratch/a.md", on_disk=files["a.md"]))
    second = _outcome(handler, _frame("scratch/b.md", on_disk=files["b.md"]))
    third = _outcome(handler, _frame("scratch/c.md", on_disk=files["c.md"]))
    # Asking again for a file already on its way is the same promotion, not
    # a second one — even with the allowance spent.
    again = _outcome(handler, _frame("scratch/a.md", on_disk=files["a.md"]))

    assert (first, second, third, again) == ("accepted", "accepted", "busy", "accepted")
    box.sync.flush()
    assert sorted(box.api.uploads) == ["a.md", "b.md"]

    assert _outcome(handler, _frame("scratch/d.md", on_disk=files["d.md"])) == "accepted"


def test_the_in_flight_cap_is_the_machines_across_every_folder_it_holds(
    tmp_path: Path,
) -> None:
    one = Box(tmp_path / "one")
    two = Box(tmp_path / "two")
    two_held = HeldFolder(
        chat_id="chat-8",
        org_path="y",
        root=two.folder,
        record=two.sync.record.model_copy(update={"node_id": "other-lease"}),
        live=two.sync,
    )
    two.sync.record = two_held.record
    a = one.write("a.md", b"a")
    b = two.write("b.md", b"b")
    handler = MachineRequests(Folders(one.held, two_held), inflight=1)

    assert _outcome(handler, _frame("scratch/a.md", on_disk=a)) == "accepted"
    assert _outcome(handler, _frame("scratch/b.md", on_disk=b, lease="other-lease")) == "busy"


def test_a_folder_the_box_let_go_of_frees_the_promotions_it_was_still_carrying(
    tmp_path: Path,
) -> None:
    """A folder's sync stops (the chat ended, the lease went to another box)
    while promotions it accepted are still queued: nothing will ever upload
    them. They must not go on counting against the machine's in-flight
    allowance, or a box that let go of a folder at the wrong moment answers
    ``busy`` to every reader of every folder for as long as it runs."""
    one = Box(tmp_path / "one")
    two = Box(tmp_path / "two")
    two_held = HeldFolder(
        chat_id="chat-8",
        org_path="y",
        root=two.folder,
        record=two.sync.record.model_copy(update={"node_id": "other-lease"}),
        live=two.sync,
    )
    two.sync.record = two_held.record
    a = one.write("a.md", b"a")
    b = two.write("b.md", b"b")
    folders = Folders(one.held, two_held)
    handler = MachineRequests(folders, inflight=1)

    assert _outcome(handler, _frame("scratch/a.md", on_disk=a)) == "accepted"
    # The first folder is let go of before its promotion uploaded.
    del folders.by_lease[LEASE]

    assert _outcome(handler, _frame("scratch/b.md", on_disk=b, lease="other-lease")) == "accepted"


def test_a_fenced_folders_promotions_stop_counting_against_the_machine(tmp_path: Path) -> None:
    """The same for a folder whose own fence closed: it writes nothing, so what
    it had promoted is not in flight any more."""
    one = Box(tmp_path / "one")
    two = Box(tmp_path / "two")
    two_held = HeldFolder(
        chat_id="chat-8",
        org_path="y",
        root=two.folder,
        record=two.sync.record.model_copy(update={"node_id": "other-lease"}),
        live=two.sync,
    )
    two.sync.record = two_held.record
    a = one.write("a.md", b"a")
    b = two.write("b.md", b"b")
    handler = MachineRequests(Folders(one.held, two_held), inflight=1)

    assert _outcome(handler, _frame("scratch/a.md", on_disk=a)) == "accepted"
    one.clock.advance(31)
    assert one.sync.fenced

    assert _outcome(handler, _frame("scratch/b.md", on_disk=b, lease="other-lease")) == "accepted"


def test_past_the_per_minute_cap_a_lease_is_busy_until_the_minute_turns(box: Box) -> None:
    now = [0.0]
    files = {name: box.write(name, name.encode()) for name in ("a.md", "b.md", "c.md")}
    handler = MachineRequests(Folders(box.held), inflight=10, per_minute=2, clock=lambda: now[0])

    assert _outcome(handler, _frame("scratch/a.md", on_disk=files["a.md"])) == "accepted"
    assert _outcome(handler, _frame("scratch/b.md", on_disk=files["b.md"])) == "accepted"
    box.sync.flush()
    # Both landed, so nothing is in flight: it is the minute that refuses.
    assert _outcome(handler, _frame("scratch/c.md", on_disk=files["c.md"])) == "busy"

    now[0] = 60.0
    assert _outcome(handler, _frame("scratch/c.md", on_disk=files["c.md"])) == "accepted"


# -- what is not answered -----------------------------------------------------------


@pytest.mark.parametrize(
    "frame",
    [
        pytest.param(
            {**_frame("scratch/a.md", size=1, mtime_ns=1), "kind": "evict"}, id="a-new-kind"
        ),
        pytest.param(
            {
                k: v
                for k, v in _frame("scratch/a.md", size=1, mtime_ns=1).items()
                if k != "request_id"
            },
            id="no-request-id",
        ),
        pytest.param(
            {**_frame("scratch/a.md", size=1, mtime_ns=1), "expected": {}}, id="no-expectation"
        ),
        pytest.param(
            {**_frame("scratch/a.md", size=1, mtime_ns=1), "expected": {"size": -1, "mtime_ns": 1}},
            id="a-negative-size",
        ),
        pytest.param({**_frame("scratch/a.md", size=1, mtime_ns=1), "path": ""}, id="no-path"),
        pytest.param(
            {**_frame("scratch/a.md", size=1, mtime_ns=1), "t": "machine.ack"}, id="not-a-request"
        ),
    ],
)
def test_a_request_this_box_cannot_answer_is_not_answered(box: Box, frame: dict[str, Any]) -> None:
    """The drive's deadline covers a request nobody answers; a guessed answer
    would be a claim about a verb this box never implemented."""
    box.write("a.md", b"a")
    handler = MachineRequests(Folders(box.held))

    assert handler.handle(frame) is None
    assert box.sync.promotions == frozenset()


def test_an_epoch_that_is_not_a_number_is_read_as_no_epoch() -> None:
    parsed = parse_request({**_frame("a.md", size=1, mtime_ns=1), "epoch": True})
    assert parsed is not None and parsed.epoch is None


def test_the_handler_reads_no_bytes_on_the_socket_thread(box: Box) -> None:
    """Accepting a promotion stats the file and nothing more: the bytes are
    read by the live sync's own round. A file the handler cannot open for
    reading is still accepted."""
    report = box.write("report.md", b"q3")
    os.chmod(report, 0o000)
    try:
        handler = MachineRequests(Folders(box.held))
        assert _outcome(handler, _frame("scratch/report.md", on_disk=report)) == "accepted"
    finally:
        os.chmod(report, 0o644)


# -- a live document moved ------------------------------------------------------


class _Notices:
    """The folder's text peer, as far as the handler reaches it: what it was told."""

    def __init__(self) -> None:
        self.told: list[tuple[str, str]] = []

    def notify(self, node_id: str, token: str) -> None:
        self.told.append((node_id, token))


def _live_text(*, lease: str = LEASE, epoch: int = 5, **extra: Any) -> dict[str, Any]:
    return {
        "t": "machine.request",
        "request_id": "req-9",
        "kind": "live_text",
        "lease_node_id": lease,
        "epoch": epoch,
        "node_id": "node-of-the-file",
        "token": "1.AAEC",
        **extra,
    }


# -- a flush before a trash ----------------------------------------------------


def _flush(*, lease: str = LEASE, request_id: str = "flush-1", **extra: Any) -> dict[str, Any]:
    return {
        "t": "machine.request",
        "request_id": request_id,
        "kind": "flush",
        "lease_node_id": lease,
        "deadline_ms": 30000,
        **extra,
    }


@pytest.mark.parametrize(
    ("frame", "told"),
    [
        pytest.param(_live_text(), [("node-of-the-file", "1.AAEC")], id="its-folder"),
        pytest.param(_live_text(epoch=4), [], id="an-epoch-it-no-longer-holds"),
        pytest.param(_live_text(lease="someone-elses"), [], id="a-lease-it-does-not-hold"),
        pytest.param(_live_text(token=""), [], id="no-token"),
    ],
)
def test_a_live_text_notice_reaches_the_folder_s_peer_and_is_never_answered(
    box: Box, frame: dict[str, Any], told: list[tuple[str, str]]
) -> None:
    """The drive says a file's live document moved: the folder holding it at
    the epoch named hands it to its text peer, and nothing is sent back (the
    drive waits for no answer to a notice)."""
    peer = _Notices()
    box.sync.peer = peer
    handler = MachineRequests(Folders(box.held))

    assert handler.handle(frame) is None
    assert peer.told == told


def _settled(answer: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
    assert isinstance(answer, LaterAnswer), answer
    return dict(answer.now), asyncio.run(_await(answer.then))


async def _await(then: Any) -> Any:
    return await then


def test_a_flush_is_accepted_at_once_and_settled_once_the_folder_is_pushed(box: Box) -> None:
    """A person is trashing the folder: the box says at once that it is on it,
    pushes everything it holds of the folder, unsent edits included, and says
    so when the push has ended."""
    box.write("notes.md", b"the agent's unsent edit")
    folders = Folders(box.held)

    now, later = _settled(MachineRequests(folders).handle(_flush(epoch=5)))

    assert now == {"t": "machine.ack", "request_id": "flush-1", "outcome": "accepted"}
    assert later == {"t": "machine.ack", "request_id": "flush-1", "outcome": "flushed"}
    assert folders.drive == {"scratch/notes.md": b"the agent's unsent edit"}


@pytest.mark.parametrize(
    "setup",
    [
        pytest.param("refused", id="the-drive-refused-a-file"),
        pytest.param("unreachable", id="the-push-did-not-land"),
    ],
)
def test_a_flush_whose_push_did_not_land_whole_is_busy(box: Box, setup: str) -> None:
    box.write("notes.md", b"edit")
    folders = (
        Folders(box.held, refused=("scratch/notes.md",))
        if setup == "refused"
        else Folders(box.held)
    )
    folders.unreachable = setup == "unreachable"

    _now, later = _settled(MachineRequests(folders).handle(_flush()))

    assert later == {"t": "machine.ack", "request_id": "flush-1", "outcome": "busy"}


@pytest.mark.parametrize(
    ("lease", "extra", "fenced"),
    [
        pytest.param("someone-elses-lease", {}, False, id="a-lease-this-box-does-not-hold"),
        pytest.param(LEASE, {"epoch": 6}, False, id="an-epoch-this-box-does-not-hold"),
        pytest.param(LEASE, {}, True, id="a-closed-fence"),
    ],
)
def test_a_flush_this_box_cannot_serve_is_not_holder_and_pushes_nothing(
    box: Box, lease: str, extra: dict[str, Any], fenced: bool
) -> None:
    box.write("notes.md", b"edit")
    folders = Folders(box.held)
    if fenced:
        box.clock.advance(31)

    answer = MachineRequests(folders).handle(_flush(lease=lease, **extra))

    assert answer == {"t": "machine.ack", "request_id": "flush-1", "outcome": "not_holder"}
    assert folders.drive == {}
