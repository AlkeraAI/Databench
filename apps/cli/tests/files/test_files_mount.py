"""The mount chain: the lease, the fence, the record, and the way back.

The wire here is a real ``httpx`` client over a transport that answers as the
lease routes do, because everything this module is about happens *in the
headers*: the instance and machine a mount acquires under, the epoch every
write it makes carries, and the 409 a superseded holder is answered with. A
fake client object could not show any of it.

The two-actor proof against the live backend (A edits, B is refused with A
named, A unmounts, B reads every change) is `test_files_mount_live.py`'s job
and is not yet written; what is pinned here is every decision the client makes
around that exchange, including the ones that only happen when the server
says no.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from _help_text import HELP_ENV
from _profiles import ORG_A, store
from alkera_cli.commands import files as files_cli
from alkera_cli.files.mount import (
    AlreadyMountedError,
    LeaseSupersededError,
    MountRecord,
    NotMountedError,
    fenced,
    fenced_client,
    heartbeat,
    hold,
    load_record,
    mount,
    mount_record_path,
    mounts,
    save_record,
    unmount,
    watch,
)
from alkera_cli.files.pull import LocalChangesError, PullSummary
from alkera_cli.files.push import PushSummary
from alkera_cli.files.target import ContainmentError, MaterializationTarget
from alkera_core.files.links import LinkKind
from alkera_sdk.client import AlkeraHTTPError
from typer.testing import CliRunner

DRIVE = "11111111-1111-1111-1111-111111111111"
NODE = "22222222-2222-2222-2222-222222222222"


# ---------------------------------------------------------------------------
# A server that answers the lease family
# ---------------------------------------------------------------------------


class FakeFiles:
    """The ``client.files`` calls a mount makes, and nothing else."""

    def __init__(self, etag: str = "7") -> None:
        self.etag = etag
        #: Every id the mount asked about, in order. A mount reads the node it
        #: holds by id — the path form is only how a fresh mount finds one.
        self.ids: list[str] = []

    def drive(self) -> dict[str, Any]:
        return {"id": DRIVE}

    def item(self, drive_id: str, item_id: str, *, select: str | None = None) -> dict[str, Any]:
        self.ids.append(item_id)
        return {"id": item_id, "etag": self.etag, "kind": "folder", "pathBytes": "/Shared/proj"}

    def item_by_path(self, drive_id: str, item_path: str) -> dict[str, Any]:
        return {"id": NODE, "etag": self.etag, "kind": "folder", "name": item_path}


def _in_an_hour() -> str:
    """A lease that is still alive whenever this suite is run.

    The expiry a served lease carries is now read — a lease past its own
    deadline is no longer this machine's folder — so a fixed instant in a
    fixture is a date the suite eventually walks past.
    """
    return (datetime.now(UTC) + timedelta(hours=1)).isoformat()


class LeaseServer:
    """The lease routes, as far as a mount can tell them apart.

    ``holder`` is the instance the server considers current: a call from any
    other instance is answered 409 with the holder named, which is the fence
    exactly as a second machine meets it.
    """

    def __init__(self, *, holder: str | None = None, epoch: int = 5) -> None:
        self.holder = holder
        self.epoch = epoch
        self.seen: list[httpx.Request] = []
        self.released: list[dict[str, Any]] = []
        #: The streaming cadence this server serves on a grant. Empty is the
        #: server that has no live plane, which is what a holder built against
        #: a newer client still has to cope with.
        self.live: dict[str, Any] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        target = request.url.path
        body = json.loads(request.content) if request.content else {}
        instance = body.get("instanceId") or request.headers.get("X-Alkera-Lease-Instance")
        if target.endswith("/lease"):
            if self.holder is not None and instance != self.holder:
                return self._leased()
            self.holder = instance
            return httpx.Response(200, json=self._grant())
        if target.endswith("/lease/heartbeat"):
            if instance != self.holder:
                return self._leased()
            return httpx.Response(200, json=self._grant())
        if target.endswith("/lease/release"):
            if instance != self.holder:
                return self._leased()
            self.released.append(body)
            self.holder = None
            return httpx.Response(200, json={})
        if target.endswith("/leases"):
            rows = (
                []
                if self.holder is None
                else [
                    {
                        "nodeId": NODE,
                        "epoch": self.epoch,
                        "machine": "laptop",
                        "purpose": "mount",
                        "since": "2026-09-09T00:00:00Z",
                        "expiresAt": _in_an_hour(),
                        "lastSyncAt": None,
                    }
                ]
            )
            return httpx.Response(200, json=rows)
        return httpx.Response(404, json={})

    def _grant(self) -> dict[str, Any]:
        grant: dict[str, Any] = {
            "epoch": self.epoch,
            "expiresAt": _in_an_hour(),
            "heartbeatEvery": 15.0,
            "syncInterval": 5.0,
            "forced": False,
        }
        if self.live:
            grant["live"] = self.live
        return grant

    def _leased(self) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "files.leased",
                    "message": "this folder is in use",
                    "holder": "ana@alkera.dev",
                    "machine": "MacBook Pro",
                }
            },
        )

    def bodies(self, suffix: str) -> list[dict[str, Any]]:
        return [
            json.loads(request.content)
            for request in self.seen
            if request.url.path.endswith(suffix) and request.content
        ]


@pytest.fixture
def server() -> LeaseServer:
    return LeaseServer()


@pytest.fixture
def http(server: LeaseServer) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(server), base_url="http://files.test")


def _pull_nothing(**_kwargs: Any) -> PullSummary:
    return PullSummary()


# ---------------------------------------------------------------------------
# mount
# ---------------------------------------------------------------------------


def test_mount_acquires_with_an_instance_and_a_machine_then_records_it(
    tmp_path: Path, http: httpx.Client, server: LeaseServer
) -> None:
    """The acquire carries what the route needs, and the mount is on disk."""
    root = tmp_path / "proj"
    record, _ = mount(
        files=FakeFiles(),
        http=http,
        source="/Shared/proj",
        root=root,
        machine="laptop",
        home=tmp_path / "home",
        pull_tree=_pull_nothing,
    )

    acquire = server.bodies("/lease")[0]
    assert acquire["machineId"] == "laptop"
    assert acquire["purpose"] == "mount"
    assert acquire["instanceId"] == record.instance_id
    assert record.epoch == 5
    assert record.node_id == NODE
    assert Path(record.local_root) == root.resolve()

    on_disk = load_record(root, home=tmp_path / "home")
    assert on_disk is not None
    assert on_disk.instance_id == record.instance_id
    assert on_disk.epoch == 5


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Windows has no POSIX permission bits: os.chmod there honours only the "
        "read-only flag, so a 0600 record still stats as 0666. Owner-only access "
        "is an ACL on that platform, which this record does not yet set."
    ),
)
def test_the_mount_record_is_only_readable_by_its_owner(tmp_path: Path, http: httpx.Client) -> None:
    """0600: the record names the folder this user holds and on which machine."""
    root = tmp_path / "proj"
    mount(
        files=FakeFiles(),
        http=http,
        source="/Shared/proj",
        root=root,
        home=tmp_path / "home",
        pull_tree=_pull_nothing,
    )
    mode = mount_record_path(root, home=tmp_path / "home").stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_mount_pulls_the_subtree_into_the_local_root(tmp_path: Path, http: httpx.Client) -> None:
    """The pull is driven with the directory and the org path the user named."""
    seen: dict[str, Any] = {}

    def _pull(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return _pull_nothing()

    root = tmp_path / "proj"
    mount(
        files=FakeFiles(),
        http=http,
        source="/Shared/proj",
        root=root,
        home=tmp_path / "home",
        pull_tree=_pull,
    )
    assert seen["root"] == root
    assert seen["source"] == "/Shared/proj"


def test_a_second_mount_of_the_same_directory_resumes_as_the_same_holder(
    tmp_path: Path, http: httpx.Client, server: LeaseServer
) -> None:
    """The SIGKILL recovery: the record survives, so the resume is not a second
    holder. The re-acquire sends the *same* instance id, which is the only
    reason the server hands the folder back rather than answering 409."""
    root = tmp_path / "proj"
    home = tmp_path / "home"
    first, _ = mount(
        files=FakeFiles(),
        http=http,
        source="/Shared/proj",
        root=root,
        home=home,
        pull_tree=_pull_nothing,
    )
    # The holder's process is gone; the lease is still there and still theirs.
    again, _ = mount(
        files=FakeFiles(),
        http=http,
        source="/Shared/proj",
        root=root,
        home=home,
        pull_tree=_pull_nothing,
    )
    assert again.instance_id == first.instance_id
    assert [body["instanceId"] for body in server.bodies("/lease")] == [
        first.instance_id,
        first.instance_id,
    ]


def test_a_mount_into_a_folder_someone_else_holds_is_refused_with_the_holder(
    tmp_path: Path,
) -> None:
    """A second machine — a second instance id — cannot take the folder, and
    the refusal names who has it."""
    server = LeaseServer(holder="the-other-laptop")
    other = httpx.Client(transport=httpx.MockTransport(server), base_url="http://files.test")
    with pytest.raises(LeaseSupersededError) as refusal:
        mount(
            files=FakeFiles(),
            http=other,
            source="/Shared/proj",
            root=tmp_path / "proj",
            home=tmp_path / "home",
            pull_tree=_pull_nothing,
        )
    assert refusal.value.holder == "ana@alkera.dev on MacBook Pro"
    assert load_record(tmp_path / "proj", home=tmp_path / "home") is None


# ---------------------------------------------------------------------------
# the fence on every write
# ---------------------------------------------------------------------------


def test_a_fenced_block_carries_the_epoch_and_the_instance_and_nothing_else(
    tmp_path: Path, http: httpx.Client, server: LeaseServer
) -> None:
    """A write from a mount says which lease it is under, and says nothing
    about the drive's ceilings.

    A holder running several folders keeps one client per folder for the
    client's whole life, and the live plane writes on it every few hundred
    milliseconds. A marker claiming "this one is my last" would ride every one
    of those — the drive cannot tell which push really is the last — so nothing
    here sets one, and the last push before a release is bounded like the rest.
    """
    root = tmp_path / "proj"
    home = tmp_path / "home"
    record, _ = mount(
        files=FakeFiles(),
        http=http,
        source="/Shared/proj",
        root=root,
        home=home,
        pull_tree=_pull_nothing,
    )

    with fenced(http, record) as writing:
        assert writing.headers["X-Alkera-Lease-Epoch"] == str(record.epoch)
        assert writing.headers["X-Alkera-Lease-Instance"] == record.instance_id
        # This build's If-Match is the version its bytes were made on, and it
        # says so: a co-edited document trusts it as a base.
        assert writing.headers["X-Alkera-Lease-Base"] == "agreed"
        assert "X-Alkera-Lease-Final" not in writing.headers
    assert "X-Alkera-Lease-Epoch" not in http.headers
    assert "X-Alkera-Lease-Base" not in http.headers


def test_unmount_pushes_under_the_epoch_then_releases_in_one_call(
    tmp_path: Path, http: httpx.Client, server: LeaseServer
) -> None:
    """Every write the push makes carries the fence, and the release carries the
    final snapshot in the same call — not a snapshot then a release."""
    root = tmp_path / "proj"
    home = tmp_path / "home"
    record, _ = mount(
        files=FakeFiles(),
        http=http,
        source="/Shared/proj",
        root=root,
        home=home,
        pull_tree=_pull_nothing,
    )

    fenced: dict[str, str | None] = {}

    def _push(**kwargs: Any) -> PushSummary:
        client: httpx.Client = kwargs["http"]
        fenced["epoch"] = client.headers.get("X-Alkera-Lease-Epoch")
        fenced["instance"] = client.headers.get("X-Alkera-Lease-Instance")
        fenced["final"] = client.headers.get("X-Alkera-Lease-Final")
        return PushSummary(uploaded=1)

    summary = unmount(files=FakeFiles(), http=http, root=root, home=home, push_tree=_push)

    # ...and it says nothing about the drive's ceilings: which push is the last
    # is the holder's own word, so the bytes go up bounded like every other
    # write and the release carries only what the server applies itself.
    assert fenced == {"epoch": str(record.epoch), "instance": record.instance_id, "final": None}
    assert len(server.released) == 1
    # The release carries the batch its own push just moved, not an empty one:
    # a reader who sees the lease gone sees the last changes it made.
    assert server.released[0]["final"] == [
        {
            "uploaded": 1,
            "unchanged": 0,
            "folders": 0,
            "symlinks": 0,
            "specials": 0,
            "bytesUploaded": 0,
        }
    ]
    assert server.released[0]["epoch"] == record.epoch
    assert summary.push.uploaded == 1
    assert load_record(root, home=home) is None


def test_the_fence_comes_off_the_client_once_the_push_is_done(
    tmp_path: Path, http: httpx.Client
) -> None:
    """The headers are scoped to the push, so an unrelated later call on the
    same session is not silently claiming a lease it no longer holds."""
    root = tmp_path / "proj"
    home = tmp_path / "home"
    mount(
        files=FakeFiles(),
        http=http,
        source="/Shared/proj",
        root=root,
        home=home,
        pull_tree=_pull_nothing,
    )
    unmount(
        files=FakeFiles(),
        http=http,
        root=root,
        home=home,
        push_tree=lambda **_kwargs: PushSummary(),
    )
    assert "X-Alkera-Lease-Epoch" not in http.headers


def test_a_superseded_push_stops_the_unmount_without_releasing(
    tmp_path: Path, http: httpx.Client, server: LeaseServer
) -> None:
    """A holder the server superseded has nothing to hand back: the release is
    never sent and the record stays, so a human can see what happened."""
    root = tmp_path / "proj"
    home = tmp_path / "home"
    mount(
        files=FakeFiles(),
        http=http,
        source="/Shared/proj",
        root=root,
        home=home,
        pull_tree=_pull_nothing,
    )

    def _push(**_kwargs: Any) -> PushSummary:
        raise httpx.HTTPStatusError(
            "conflict",
            request=httpx.Request("PUT", "http://files.test/x"),
            response=httpx.Response(
                409,
                json={
                    "error": {
                        "code": "files.lease_fenced",
                        "message": "superseded",
                        "holder": "ana@alkera.dev",
                        "machine": "MacBook Pro",
                    }
                },
            ),
        )

    with pytest.raises(LeaseSupersededError) as refusal:
        unmount(files=FakeFiles(), http=http, root=root, home=home, push_tree=_push)

    assert refusal.value.holder == "ana@alkera.dev on MacBook Pro"
    assert server.released == []
    assert load_record(root, home=home) is not None


def _refusal(status: int, body: dict[str, Any]) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        "refused",
        request=httpx.Request("POST", "http://files.test/x"),
        response=httpx.Response(status, json=body),
    )


@pytest.mark.parametrize(
    ("status", "body", "superseded"),
    [
        pytest.param(
            409, {"code": "files.lease_fenced", "message": "newer epoch"}, True, id="fenced"
        ),
        pytest.param(
            409,
            {"code": "files.leased", "detail": {"holder": "ana", "machine": "box-8"}},
            True,
            id="another-machine-holds-it",
        ),
        pytest.param(
            409,
            {"code": "files.leased", "detail": {"holder": "me", "machine": "box-7"}},
            False,
            id="our-own-lease-met-unfenced-is-not-a-change-of-hands",
        ),
        pytest.param(409, {"code": "files.exists", "message": "taken"}, False, id="a-taken-name"),
        pytest.param(409, {"code": "files.moving"}, False, id="a-subtree-mid-move"),
        pytest.param(409, {"error": {"code": "files.trashed"}}, False, id="a-trashed-parent"),
        pytest.param(409, {}, False, id="a-409-that-names-no-code"),
        pytest.param(412, {"code": "files.precondition"}, False, id="a-stale-etag"),
    ],
)
def test_only_the_leases_own_refusals_are_a_supersession(
    status: int, body: dict[str, Any], superseded: bool
) -> None:
    """A 409 is a fact about the write, not about the lease, unless the code
    says otherwise: reading every 409 as "the folder was taken from us" had a
    box abandon its chat's work over a taken name."""
    from alkera_cli.files.mount import superseded_from

    record = MountRecord(machine="box-7")
    assert (superseded_from(_refusal(status, body), record=record) is not None) is superseded


def test_a_push_refused_for_its_write_neither_releases_nor_forgets_the_mount(
    tmp_path: Path, http: httpx.Client, server: LeaseServer
) -> None:
    """The name-taken refusal reaches the caller as what it is; the lease is
    still ours, so nothing is released and the record stays for the retry."""
    root = tmp_path / "proj"
    home = tmp_path / "home"
    mount(
        files=FakeFiles(),
        http=http,
        source="/Shared/proj",
        root=root,
        home=home,
        pull_tree=_pull_nothing,
    )

    def _push(**_kwargs: Any) -> PushSummary:
        raise _refusal(409, {"code": "files.exists", "message": "that name is taken"})

    with pytest.raises(httpx.HTTPStatusError):
        unmount(files=FakeFiles(), http=http, root=root, home=home, push_tree=_push)

    assert server.released == []
    assert load_record(root, home=home) is not None


def test_unmounting_a_directory_that_is_not_mounted_says_so(
    tmp_path: Path, http: httpx.Client
) -> None:
    with pytest.raises(NotMountedError):
        unmount(files=FakeFiles(), http=http, root=tmp_path / "nowhere", home=tmp_path / "home")


# ---------------------------------------------------------------------------
# heartbeat + mounts
# ---------------------------------------------------------------------------


def test_a_heartbeat_that_is_fenced_out_raises_rather_than_pretending(
    tmp_path: Path,
) -> None:
    server = LeaseServer(holder="somebody-else")
    client = httpx.Client(transport=httpx.MockTransport(server), base_url="http://files.test")
    record = MountRecord(
        instance_id="mine", drive_id=DRIVE, node_id=NODE, local_root=str(tmp_path), epoch=4
    )
    with pytest.raises(LeaseSupersededError):
        heartbeat(http=client, record=record, home=tmp_path / "home")


def test_hold_beats_until_it_is_told_to_stop(tmp_path: Path) -> None:
    """The loop is driven by injected time, so the beat count is the assertion
    and no test waits on a clock."""
    server = LeaseServer(holder="mine")
    client = httpx.Client(transport=httpx.MockTransport(server), base_url="http://files.test")
    record = MountRecord(
        instance_id="mine",
        drive_id=DRIVE,
        node_id=NODE,
        local_root=str(tmp_path),
        epoch=5,
        heartbeat_every=0.0,
    )
    save_record(record, home=tmp_path / "home")
    beats = {"n": 0}

    def _stop() -> bool:
        beats["n"] += 1
        return beats["n"] > 6

    hold(http=client, record=record, stop=_stop, home=tmp_path / "home", sleep=lambda _s: None)
    assert beats["n"] == 7
    assert len(server.bodies("/lease/heartbeat")) == 3


class SnapshottingLeaseServer(LeaseServer):
    """The lease routes plus the snapshot a finished batch announces itself
    with, which is the one other call ``watch`` makes."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/snapshots"):
            self.seen.append(request)
            return httpx.Response(204)
        return super().__call__(request)


def test_a_long_export_does_not_hold_up_the_beats_that_keep_the_lease(tmp_path: Path) -> None:
    """A watched mount beats all the way through a batch, not after it.

    A batch is an item lookup and an upload per file over a re-walked tree, so
    a 10k-file folder cannot finish inside the lease's TTL. Run in the beat's
    own turn it meant no beat left the machine until the push was done — by
    which time the lease was gone, and the snapshot the push ends with is
    refused at the fence it lost, or the holder is fenced part-way through the
    batch.

    The claim is an ordering one and nothing here measures an interval: the
    push is held open until the beats it must not be blocking have landed, and
    the loop's only real exit is that push finishing — so a runner that
    schedules the batch thread late makes the loop beat for longer, never for
    fewer turns. A loaded machine reading a beat count short was this test
    counting wall-clock, not the mount losing its lease. The one clock left is
    the guard that lets go of a push whose beats can never come, so the
    sequenced-behind regression fails rather than hangs.
    """
    server = SnapshottingLeaseServer(holder="mine")
    #: Set for as long as the batch is inside the push, so only beats that had
    #: to cross it are counted.
    pushing = threading.Event()
    pushed = threading.Event()
    landed = threading.Condition()
    beats = {"n": 0}

    def _beat_watching_transport(request: httpx.Request) -> httpx.Response:
        response = server(request)
        if request.url.path.endswith("/lease/heartbeat") and pushing.is_set():
            with landed:
                beats["n"] += 1
                landed.notify_all()
        return response

    client = httpx.Client(
        transport=httpx.MockTransport(_beat_watching_transport), base_url="http://files.test"
    )
    record = MountRecord(
        instance_id="mine",
        drive_id=DRIVE,
        node_id=NODE,
        local_root=str(tmp_path),
        epoch=5,
        heartbeat_every=0.0,
        sync_interval=0.0,
    )
    save_record(record, home=tmp_path / "home")
    during: dict[str, int] = {}

    def _slow_push(**_kwargs: Any) -> PushSummary:
        pushing.set()
        try:
            with landed:
                landed.wait_for(lambda: beats["n"] >= 2, timeout=5.0)
                during["beats"] = beats["n"]
        finally:
            pushed.set()
        return PushSummary()

    turns = {"n": 0}

    def _stop() -> bool:
        # While a batch is out there, that this loop keeps beating is the whole
        # subject — so nothing may stop it but the push being done. The turn
        # cap is a hang guard for a batch that never reaches the push at all,
        # never the window the beats are counted in.
        turns["n"] += 1
        return pushed.is_set() or turns["n"] > 20_000

    watch(
        files=cast(Any, FakeFiles()),
        http=client,
        record=record,
        stop=_stop,
        home=tmp_path / "home",
        sleep=lambda _s: time.sleep(0.001),
        push_tree=_slow_push,
    )

    assert during.get("beats", 0) >= 2, (
        f"only {during.get('beats', 0)} beats landed while the batch was pushing; the lease was "
        "being kept by nothing but the export finishing"
    )


def test_mounts_keeps_a_folder_checked_out_when_no_process_is_beating_for_it(
    tmp_path: Path,
) -> None:
    """A record whose process is gone still holds the folder, and says so.

    The lease is what decides it. ``mount --no-hold`` takes a folder and exits
    by design, and a SIGKILLed holder is the same shape: the server still lists
    the lease at this epoch, so the folder IS still checked out to this machine
    and nobody else can write it. Calling that ``stale`` told the customer the
    opposite of the truth seconds after the mount succeeded. Whether anything
    is beating is reported separately — see ``running``.
    """
    server = LeaseServer(holder="a")
    http = httpx.Client(transport=httpx.MockTransport(server), base_url="http://files.test")
    home = tmp_path / "home"
    live = MountRecord(
        instance_id="a",
        drive_id=DRIVE,
        node_id=NODE,
        org_path="Shared/proj",
        local_root=str(tmp_path / "live"),
        epoch=5,
        pid=os.getpid(),
    )
    save_record(live, home=home)
    assert [row.state for row in mounts(files=FakeFiles(), http=http, home=home)] == ["live"]

    killed = live.model_copy(update={"local_root": str(tmp_path / "live"), "pid": 0x7FFFFFF})
    save_record(killed, home=home)
    rows = mounts(files=FakeFiles(), http=http, home=home)
    assert [row.state for row in rows] == ["live"]
    assert [row.running for row in rows] == [False]


def test_mounts_calls_a_lease_the_server_no_longer_lists_stale(
    tmp_path: Path,
) -> None:
    server = LeaseServer(holder=None)
    client = httpx.Client(transport=httpx.MockTransport(server), base_url="http://files.test")
    home = tmp_path / "home"
    save_record(
        MountRecord(
            instance_id="a",
            drive_id=DRIVE,
            node_id=NODE,
            local_root=str(tmp_path / "gone"),
            epoch=5,
            pid=os.getpid(),
        ),
        home=home,
    )
    [row] = mounts(files=FakeFiles(), http=client, home=home)
    assert row.running is True
    assert row.held is False
    assert row.state == "stale"


# ---------------------------------------------------------------------------
# F-310: a link whose stored target changed can be rewritten
# ---------------------------------------------------------------------------


def test_a_symlink_already_on_disk_can_be_written_with_a_new_target(
    tmp_path: Path,
) -> None:
    """A re-pull of a tree whose link moved must land the new target.

    Before the leaf allowance, ``flush_links`` resolved the link's own path and
    the containment walk refused it as "traverses the symlink" — the link the
    pull wrote last time made itself unwritable.
    """
    target = MaterializationTarget(tmp_path / "root")
    (tmp_path / "root" / "sub").mkdir(parents=True)
    target.write_symlink(b"sub/link", LinkKind.RELATIVE, b"../first")
    target.flush_links()

    target.write_symlink(b"sub/link", LinkKind.RELATIVE, b"../second")
    target.flush_links()

    assert os.readlink(tmp_path / "root" / "sub" / "link") == "../second"


def test_the_containment_check_still_stands_on_the_parent(tmp_path: Path) -> None:
    """The allowance is the leaf only: a link *inside* a symlinked directory is
    still refused, so a link cannot be used as a door out of the root."""
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside").mkdir()
    os.symlink(tmp_path / "outside", root / "door")
    target = MaterializationTarget(root)
    with pytest.raises(ContainmentError):
        target.write_symlink(b"door/link", LinkKind.RELATIVE, b"../x")


# ---------------------------------------------------------------------------
# the three verbs, through the real typer parse
# ---------------------------------------------------------------------------


@pytest.fixture
def signed_in(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """A signed-in session whose client is inert, so a verb's own work is what
    the assertion sees."""
    calls: list[Any] = []

    class _Auth:
        token = "t"
        api_url = "http://files.test"

    class _Raw:
        def get_httpx_client(self) -> Any:
            return httpx.Client(base_url="http://files.test")

    class _Client:
        files = FakeFiles()
        raw_client = _Raw()

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *_exc: Any) -> bool:
            return False

    store(ORG_A, current=True, api_url=_Auth.api_url)
    monkeypatch.setattr(files_cli, "AlkeraClient", lambda **_kwargs: _Client())
    return calls


def test_the_mount_verb_reaches_the_library_and_does_not_hold_when_told_not_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signed_in: list[Any]
) -> None:
    record = MountRecord(instance_id="a", epoch=5, local_root=str(tmp_path))

    def _mount(**kwargs: Any) -> Any:
        signed_in.append(kwargs)
        return record, _pull_nothing()

    monkeypatch.setattr(files_cli, "mount", _mount)
    monkeypatch.setattr(
        files_cli, "watch", lambda **_kwargs: pytest.fail("--no-hold must not heartbeat")
    )
    result = CliRunner().invoke(
        files_cli.files_app, ["mount", "/Shared/proj", str(tmp_path / "here"), "--no-hold"]
    )
    assert result.exit_code == 0, result.output
    assert signed_in[0]["source"] == "/Shared/proj"
    assert signed_in[0]["root"] == tmp_path / "here"


def test_the_unmount_verb_reports_the_release_and_the_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signed_in: list[Any]
) -> None:
    from alkera_cli.files.mount import UnmountSummary

    summary = UnmountSummary(
        record=MountRecord(org_path="Shared/proj", epoch=5, local_root=str(tmp_path)),
        push=PushSummary(uploaded=2),
    )
    monkeypatch.setattr(files_cli, "unmount", lambda **kwargs: summary)
    result = CliRunner().invoke(files_cli.files_app, ["unmount", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "checked Shared/proj back in" in result.output


def test_the_unmount_verb_exits_one_and_names_the_holder_when_fenced_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signed_in: list[Any]
) -> None:
    def _refuse(**_kwargs: Any) -> Any:
        raise LeaseSupersededError("superseded", holder="ana on MacBook Pro")

    monkeypatch.setattr(files_cli, "unmount", _refuse)
    result = CliRunner().invoke(files_cli.files_app, ["unmount", str(tmp_path)])
    assert result.exit_code == 1
    assert "ana on MacBook Pro" in result.output


def test_the_mounts_verb_prints_each_record_with_its_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signed_in: list[Any]
) -> None:
    from alkera_cli.files.mount import MountStatus

    rows = [
        MountStatus(
            record=MountRecord(
                org_path="Shared/proj", local_root=str(tmp_path), epoch=5, machine="laptop"
            ),
            held=True,
            running=False,
        )
    ]
    monkeypatch.setattr(files_cli, "mounts", lambda **_kwargs: rows)
    result = CliRunner().invoke(files_cli.files_app, ["mounts"], env=HELP_ENV)
    assert result.exit_code == 0, result.output
    # rich hard-wraps this line to the console width, and both the width and the
    # length of ``tmp_path`` belong to the machine running the suite — a long
    # temp root folds the sentence mid-phrase ("(nothing\nis syncing it right
    # now)") on a narrow terminal while a wide CI runner never sees it. So read
    # the words, not the picture: the wide console keeps the usual case on one
    # line, and collapsing the whitespace means even a temp root longer than
    # that width still reads back as the sentence the command printed.
    said = " ".join(result.output.split())
    # The lease is listed and unexpired, so the folder is still this machine's;
    # what the dead pid changes is only that nothing is syncing it.
    assert "checked out to you" in said
    assert "nothing is syncing it right now" in said
    assert "Shared/proj" in said


class OtherFolder(FakeFiles):
    """The same drive, a different node — a second org folder."""

    OTHER = "33333333-3333-3333-3333-333333333333"

    def item_by_path(self, drive_id: str, item_path: str) -> dict[str, Any]:
        return {"id": self.OTHER, "etag": self.etag, "kind": "folder", "name": item_path}


def test_mounting_a_second_org_folder_onto_a_live_mount_is_refused(
    tmp_path: Path, http: httpx.Client, server: LeaseServer
) -> None:
    """The first lease has to stay releasable.

    One directory carries one record, so overwriting it would lose the epoch
    and the instance id the first lease can only be released under — leaving
    that folder leased to a holder nobody on this machine can address. The
    refusal names the folder already there and the way out.
    """
    root = tmp_path / "proj"
    home = tmp_path / "home"
    first, _ = mount(
        files=FakeFiles(),
        http=http,
        source="/Shared/proj",
        root=root,
        home=home,
        pull_tree=_pull_nothing,
    )

    with pytest.raises(AlreadyMountedError) as refused:
        mount(
            files=OtherFolder(),
            http=http,
            source="/Shared/other",
            root=root,
            home=home,
            pull_tree=_pull_nothing,
        )

    assert refused.value.org_path == "Shared/proj"
    assert "Shared/proj" in str(refused.value)
    assert "unmount" in str(refused.value)
    still = load_record(root, home=home)
    assert still is not None
    assert (still.node_id, still.instance_id) == (first.node_id, first.instance_id)
    assert [body["instanceId"] for body in server.bodies("/lease")] == [first.instance_id]
    assert server.holder == first.instance_id


def test_the_mount_command_reports_a_second_folder_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signed_in: list[Any]
) -> None:
    """The operator sees the refusal and the way out, not a stack trace."""

    def _refuse(**_kwargs: Any) -> Any:
        raise AlreadyMountedError(
            "already holds a mount of 'Shared/proj'; run `alkera files unmount .` first",
            org_path="Shared/proj",
        )

    monkeypatch.setattr(files_cli, "mount", _refuse)
    result = CliRunner().invoke(
        files_cli.files_app, ["mount", "/Shared/other", str(tmp_path / "proj")]
    )
    assert result.exit_code == 2
    assert "Shared/proj" in result.output
    assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# the record's own shape over time
# ---------------------------------------------------------------------------

#: Where the record corpus lives: one file per version this writer has ever
#: produced, loaded by the walker below with today's reader.
_RECORD_FIXTURES = Path(__file__).parent / "fixtures" / "mount_record"


def _record_fixtures() -> list[Path]:
    return sorted(_RECORD_FIXTURES.glob("v*.json"))


def test_the_record_corpus_covers_the_version_this_build_writes() -> None:
    """A walker over an empty directory passes without reading anything.

    The corpus IS the regression net for the record's evolution, so a version
    whose fixture was never added (or was deleted) has to fail here rather than
    quietly leave the walk with nothing to walk.
    """
    versions = {path.stem for path in _record_fixtures()}
    assert "v1_0_0" in versions
    assert f"v{MountRecord.SCHEMA_VERSION.replace('.', '_')}" in versions


@pytest.mark.parametrize("fixture", _record_fixtures(), ids=lambda path: cast(Path, path).stem)
def test_every_record_a_past_writer_produced_still_loads(fixture: Path) -> None:
    """Today's reader resumes a mount an older build took.

    The record is what makes a resume the *same* holder; a build that could not
    read the one on disk would take a second lease on a folder this machine
    already holds. The fix for a failure here is a migration, never an edit to
    the fixture.
    """
    record = MountRecord.model_validate(json.loads(fixture.read_text(encoding="utf-8")))

    assert record.instance_id == "inst-7"
    assert record.node_id == NODE
    assert record.epoch == 5
    assert record.heartbeat_every == 15.0
    # Whatever it was written as, it is read forward to today's version.
    assert record.schema_version == MountRecord.SCHEMA_VERSION


def test_a_record_written_before_the_live_plane_reads_as_having_none() -> None:
    """The additive field's default is the honest answer for an old record."""
    old = json.loads((_RECORD_FIXTURES / "v1_0_0.json").read_text(encoding="utf-8"))
    assert "live" not in old

    assert MountRecord.model_validate(old).live == {}


def test_the_cadence_a_grant_served_survives_a_round_trip(tmp_path: Path) -> None:
    """The record carries the block back, unread — including a key this build
    has never heard of, which is the whole reason it is kept verbatim."""
    served = {"debounceMs": 300, "maxBatchEntries": 256, "somethingNewer": ["a"]}
    record = MountRecord(instance_id="inst-7", node_id=NODE, local_root=str(tmp_path), live=served)

    again = MountRecord.model_validate(json.loads(record.model_dump_json()))

    assert again.live == served
    assert again.schema_version == "1.1.0"


# ---------------------------------------------------------------------------
# asking for the live plane
# ---------------------------------------------------------------------------


def test_a_plain_mount_asks_for_neither_inbound_nor_live(
    tmp_path: Path, http: httpx.Client, server: LeaseServer
) -> None:
    """The default is the single writer it always was, on the same wire bytes."""
    record, _ = mount(
        files=FakeFiles(),
        http=http,
        source="/Shared/proj",
        root=tmp_path / "proj",
        home=tmp_path / "home",
        pull_tree=_pull_nothing,
    )

    acquire = server.bodies("/lease")[0]
    assert "inbound" not in acquire
    assert "live" not in acquire
    assert record.live == {}


def test_a_live_mount_asks_for_both_and_keeps_the_cadence_it_was_served(
    tmp_path: Path, http: httpx.Client, server: LeaseServer
) -> None:
    """What the holder streams under is the server's answer, not a local guess."""
    server.live = {"debounceMs": 300, "batchEveryMs": 500, "maxBatchEntries": 256}

    record, _ = mount(
        files=FakeFiles(),
        http=http,
        source="/Shared/proj",
        root=tmp_path / "proj",
        home=tmp_path / "home",
        inbound=True,
        live=True,
        pull_tree=_pull_nothing,
    )

    acquire = server.bodies("/lease")[0]
    assert acquire["inbound"] is True
    assert acquire["live"] is True
    assert record.live == server.live
    on_disk = load_record(tmp_path / "proj", home=tmp_path / "home")
    assert on_disk is not None
    assert on_disk.live == server.live


def test_a_server_that_serves_no_cadence_leaves_the_record_saying_so(
    tmp_path: Path, http: httpx.Client
) -> None:
    """A grant with no live block is a lease with no live plane — not a record
    carrying half a cadence the holder would then stream under."""
    record, _ = mount(
        files=FakeFiles(),
        http=http,
        source="/Shared/proj",
        root=tmp_path / "proj",
        home=tmp_path / "home",
        inbound=True,
        live=True,
        pull_tree=_pull_nothing,
    )

    assert record.live == {}


# ---------------------------------------------------------------------------
# a client of one holder's own
# ---------------------------------------------------------------------------


def _record_for(epoch: int = 5, instance: str = "inst-7") -> MountRecord:
    return MountRecord(instance_id=instance, drive_id=DRIVE, node_id=NODE, epoch=epoch)


def test_a_fenced_client_carries_the_pair_on_every_request_it_ever_makes(
    http: httpx.Client, server: LeaseServer
) -> None:
    """The fence is the client's, not one block's.

    ``fenced`` puts the headers on for the length of a ``with``; a holder that
    owns its client writes under its epoch for as long as it holds the folder,
    so the second request carries them exactly as the first did.
    """
    client = fenced_client(lambda: http, _record_for())

    client.get("/api/v1/files/drives/x/leases")
    client.get("/api/v1/files/drives/x/leases")

    pairs = [
        (
            request.headers.get("X-Alkera-Lease-Epoch"),
            request.headers.get("X-Alkera-Lease-Instance"),
        )
        for request in server.seen
    ]
    assert pairs == [("5", "inst-7"), ("5", "inst-7")]


def test_a_fenced_client_signs_with_the_bearer_the_box_holds_now(
    http: httpx.Client, server: LeaseServer
) -> None:
    """A lease outlives the credential it was taken under: an org worker's is
    replaced every few minutes, on the shared client. The folder's own client
    signs each request with the bearer the shared one holds at that moment,
    never the one it was made with (which has expired by then)."""
    http.headers["Authorization"] = "Bearer first"
    client = fenced_client(lambda: http, _record_for())
    client.get("/api/v1/files/drives/x/leases")
    http.headers["Authorization"] = "Bearer rotated"
    client.get("/api/v1/files/drives/x/leases")

    seen = [request.headers.get("Authorization") for request in server.seen]
    assert seen == ["Bearer first", "Bearer rotated"]
    assert server.seen[-1].headers.get("X-Alkera-Lease-Epoch") == "5"


def test_two_holders_clients_never_sign_each_others_writes(
    http: httpx.Client, server: LeaseServer
) -> None:
    """The reason the fence moved off the shared client: a box holds several
    folders, and one folder's epoch must never ride on another's request."""
    first = fenced_client(lambda: http, _record_for(epoch=5, instance="inst-a"))
    second = fenced_client(lambda: http, _record_for(epoch=9, instance="inst-b"))

    first.get("/api/v1/files/drives/x/leases")
    second.get("/api/v1/files/drives/x/leases")
    first.get("/api/v1/files/drives/x/leases")

    assert [request.headers.get("X-Alkera-Lease-Instance") for request in server.seen] == [
        "inst-a",
        "inst-b",
        "inst-a",
    ]
    assert [request.headers.get("X-Alkera-Lease-Epoch") for request in server.seen] == [
        "5",
        "9",
        "5",
    ]


def test_the_client_a_holder_was_built_from_is_left_unfenced(
    http: httpx.Client, server: LeaseServer
) -> None:
    """Whatever else the process does on the shared client is not signed as
    this holder: an unfenced read is what the server tells a stranger from a
    holder by, and a beat carries its epoch in the body."""
    fenced_client(lambda: http, _record_for())

    assert "X-Alkera-Lease-Epoch" not in http.headers
    assert "X-Alkera-Lease-Instance" not in http.headers

    http.get("/api/v1/files/drives/x/leases")
    assert server.seen[-1].headers.get("X-Alkera-Lease-Epoch") is None


def test_a_fenced_client_keeps_the_bases_wire_and_its_transport(
    http: httpx.Client, server: LeaseServer
) -> None:
    """One connection pool per box, not one per chat — and the same base url,
    or a relative request would go nowhere."""
    http.headers["X-Alkera-Agent-Session"] = "sess-1"

    client = fenced_client(lambda: http, _record_for())

    assert client.base_url == http.base_url
    # The transport is shared, which is also why nothing closes a folder's
    # client: closing it would close the pool every other folder is using.
    assert getattr(client, "_transport", None) is getattr(http, "_transport", None)
    client.get("/api/v1/files/drives/x/leases")
    assert server.seen[-1].headers.get("X-Alkera-Agent-Session") == "sess-1"


# ---------------------------------------------------------------------------
# the hand-back over a full drive
# ---------------------------------------------------------------------------


def test_a_hand_back_a_full_drive_refuses_keeps_the_mount_and_says_why(
    tmp_path: Path, http: httpx.Client, server: LeaseServer
) -> None:
    """A push refused for want of room is a ceiling, not a crash.

    The bytes are still on this machine and the lease is still ours, so nothing
    is released and the record stays — and the refusal is handed on in the
    shape every Files verb already turns into one sentence, instead of the raw
    transport error that reached a person as a traceback.
    """
    root = tmp_path / "proj"
    home = tmp_path / "home"
    mount(
        files=FakeFiles(),
        http=http,
        source="/Shared/proj",
        root=root,
        home=home,
        pull_tree=_pull_nothing,
    )

    def _push(**_kwargs: Any) -> PushSummary:
        raise _refusal(507, {"code": "files.quota_bytes", "message": "no room on this drive"})

    with pytest.raises(AlkeraHTTPError) as refusal:
        unmount(files=FakeFiles(), http=http, root=root, home=home, push_tree=_push)

    assert refusal.value.status == 507
    assert refusal.value.code == "files.quota_bytes"
    assert server.released == []
    assert load_record(root, home=home) is not None


def test_the_unmount_verb_over_a_full_drive_is_one_sentence_and_keeps_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signed_in: list[Any]
) -> None:
    """Through the command a person actually runs: the drive says there is no
    room, they read what to do about it, and the mount they still hold is still
    on disk afterwards."""
    from alkera_cli.host import paths

    home = tmp_path / "home"
    root = tmp_path / "proj"
    root.mkdir()
    # The verb resolves its default home through `paths` when it runs, so the
    # seam to move is the one it reads, not a copy the mount module once held.
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    save_record(
        MountRecord(
            instance_id="mine",
            drive_id=DRIVE,
            node_id=NODE,
            org_path="Shared/proj",
            local_root=str(root),
            epoch=3,
        ),
        home=home,
    )

    def _push(**_kwargs: Any) -> PushSummary:
        raise _refusal(507, {"code": "files.quota_bytes", "message": "no room on this drive"})

    monkeypatch.setattr(files_cli, "push", _push)

    result = CliRunner().invoke(files_cli.files_app, ["unmount", str(root)])

    assert result.exit_code == 1
    assert "out of storage" in result.output
    assert "Traceback" not in result.output
    assert load_record(root, home=home) is not None


# ---------------------------------------------------------------------------
# A take whose pull failed after the grant
# ---------------------------------------------------------------------------


def _pull_refused_by(exc: BaseException) -> Any:
    def pull_tree(**_kwargs: Any) -> PullSummary:
        raise exc

    return pull_tree


_LISTING_500 = httpx.HTTPStatusError(
    "500 on the first listing",
    request=httpx.Request("GET", "http://files.test/children"),
    response=httpx.Response(500),
)


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(_LISTING_500, id="a-500-on-the-first-listing"),
        pytest.param(
            AlkeraHTTPError(
                label="files/children", status=500, code="internal", message="", trace_id=None
            ),
            id="the-sdk-says-500",
        ),
        pytest.param(httpx.ReadTimeout("the drive stopped answering"), id="a-timeout"),
    ],
)
def test_a_pull_that_fails_after_the_grant_gives_the_lease_back(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, exc: BaseException
) -> None:
    """Nothing holds the folder through a take that failed, so the lease goes
    back now: left to its TTL it refused the folder to every other holder for
    that long. Nothing is pushed, and no record claims a mount."""
    root = tmp_path / "proj"
    with pytest.raises(type(exc)):
        mount(
            files=FakeFiles(),
            http=http,
            source="/Shared/proj",
            node_id=NODE,
            root=root,
            home=tmp_path / "home",
            pull_tree=_pull_refused_by(exc),
        )

    assert len(server.released) == 1
    assert server.released[0].get("final") is None, "a failed take pushes nothing"
    assert server.holder is None
    assert load_record(root, home=tmp_path / "home") is None


def test_a_failed_resume_puts_back_the_record_it_found(
    tmp_path: Path, http: httpx.Client, server: LeaseServer
) -> None:
    root = tmp_path / "proj"
    home = tmp_path / "home"
    first, _ = mount(
        files=FakeFiles(),
        http=http,
        source="/Shared/proj",
        root=root,
        home=home,
        pull_tree=_pull_nothing,
    )
    server.epoch = 6

    with pytest.raises(httpx.HTTPStatusError):
        mount(
            files=FakeFiles(),
            http=http,
            source="/Shared/proj",
            root=root,
            home=home,
            pull_tree=_pull_refused_by(_LISTING_500),
        )

    assert server.holder is None
    assert load_record(root, home=home) == first


def test_a_pull_refused_for_local_changes_keeps_the_lease_for_the_unmount(
    tmp_path: Path, http: httpx.Client, server: LeaseServer
) -> None:
    """A refusal is not a failure: the way out is an unmount that pushes the
    named files under this very grant, so the lease and its record stay."""
    root = tmp_path / "proj"
    refused = LocalChangesError("differs", paths=[b"notes.md"])
    with pytest.raises(LocalChangesError):
        mount(
            files=FakeFiles(),
            http=http,
            source="/Shared/proj",
            root=root,
            home=tmp_path / "home",
            pull_tree=_pull_refused_by(refused),
        )

    assert server.released == []
    record = load_record(root, home=tmp_path / "home")
    assert record is not None and record.instance_id == server.holder
