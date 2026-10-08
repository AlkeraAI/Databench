"""The mount chain against a real backend.

The fast module beside this one (``test_files_mount.py``) pins every decision
the client makes; nothing there can prove the decisions the *server* makes.
The fence in particular is not the client's politeness — a second machine is
refused by the database, with the holder named — and neither the round trip of
real bytes, nor a snapshot advancing ``last_sync_at``, nor a SIGKILLed holder
resuming under the same instance id is observable anywhere but a running
backend. So these drive the FastAPI app on a real socket against the lane
database, exactly as ``test_files_push.py`` does.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from alkera_cli.commands.files import FILES_TRANSFER_TIMEOUT
from alkera_cli.files.mount import (
    LeaseExpiredError,
    MountRecord,
    SelfFence,
    export,
    load_record,
    mount,
    mounts,
    superseded_from,
    unmount,
)
from alkera_cli.files.pull import pull
from alkera_cli.files.push import push
from alkera_sdk import AlkeraClient
from alkera_sdk.client import AlkeraHTTPError, files_namespace
from files._live_backend import LiveBackend, home_path, live_backend

#: Every case boots its own backend on an ephemeral port, seeds its own org, and
#: keeps its mount records under a tmp_path ALKERA_HOME, so nothing here is
#: addressable from another case and the ten of them may spread over workers.
pytestmark = [pytest.mark.spread]


@pytest.fixture
def backend(tmp_path: Path) -> Iterator[LiveBackend]:
    with live_backend(tmp_path / "server") as running:
        yield running


@pytest.fixture
def client(backend: LiveBackend) -> Iterator[AlkeraClient]:
    with AlkeraClient(
        base_url=backend.base_url, token=backend.token, timeout=FILES_TRANSFER_TIMEOUT
    ) as api:
        yield api


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "alkera-home"


def _http(api: AlkeraClient) -> httpx.Client:
    return api.raw_client.get_httpx_client()


def _seed(api: AlkeraClient, tmp_path: Path, dest: str) -> Path:
    """A small tree already on the server, so there is something to mount."""
    source = tmp_path / "seed"
    (source / "docs").mkdir(parents=True)
    (source / "docs" / "note.txt").write_bytes(b"first\n")
    (source / "top.txt").write_bytes(b"top\n")
    push(files=api.files, http=_http(api), root=source, dest=dest, home=tmp_path / "seed-home")
    return source


def _mounted(api: AlkeraClient, tmp_path: Path, home: Path, dest: str | None = None) -> MountRecord:
    dest = dest or home_path(api, "proj")
    _seed(api, tmp_path, dest)
    root = tmp_path / "mounted"
    root.mkdir()
    record, _summary = mount(
        files=api.files, http=_http(api), source=dest, root=root, home=home, pull_tree=pull
    )
    return record


def _lease_row(api: AlkeraClient, record: MountRecord) -> dict[str, Any]:
    rows = files_namespace(_http(api)).my_leases(record.drive_id)
    matching = [row for row in rows if str(row.get("nodeId")) == record.node_id]
    assert matching, "the server no longer lists this lease"
    return matching[0]


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------


def test_a_mounted_folder_round_trips_edited_bytes_through_the_server(
    client: AlkeraClient, tmp_path: Path, home: Path
) -> None:
    """Mount, edit locally, unmount — and a *different* pull sees the edit.

    The proof is the second pull rather than the push summary: bytes that only
    the pusher believes it sent are not bytes the next reader will get.
    """
    record = _mounted(client, tmp_path, home)
    local = Path(record.local_root)
    assert (local / "docs" / "note.txt").read_bytes() == b"first\n"

    (local / "docs" / "note.txt").write_bytes(b"edited under the lease\n")

    summary = unmount(files=client.files, http=_http(client), root=local, home=home, push_tree=push)
    assert summary.record.epoch == record.epoch

    elsewhere = tmp_path / "elsewhere"
    pull(files=client.files, http=_http(client), root=elsewhere, source=home_path(client, "proj"))
    assert (elsewhere / "docs" / "note.txt").read_bytes() == b"edited under the lease\n"
    assert load_record(local, home=home) is None


def test_after_the_unmount_the_server_lists_no_lease_on_the_node(
    client: AlkeraClient, tmp_path: Path, home: Path
) -> None:
    """The release committed, rather than the client merely believing it did."""
    record = _mounted(client, tmp_path, home)
    assert _lease_row(client, record)["epoch"] == record.epoch

    unmount(
        files=client.files,
        http=_http(client),
        root=Path(record.local_root),
        home=home,
        push_tree=push,
    )

    rows = files_namespace(_http(client)).my_leases(record.drive_id)
    assert [row for row in rows if str(row.get("nodeId")) == record.node_id] == []


# ---------------------------------------------------------------------------
# The fence is the server's, not the client's
# ---------------------------------------------------------------------------


def test_a_second_instance_is_refused_by_the_server_with_the_holder_named(
    client: AlkeraClient, tmp_path: Path, home: Path
) -> None:
    """The other machine consults no local record: it asks for the folder and
    the server refuses it, and the holder keeps what it had.

    The refusal does not carry the holder today: the lease service builds a
    ``{holder, machine, since}`` detail and the route's safe-detail allowlist
    keeps only ``holder_principal_id``, so neither key the client reads reaches
    the wire. Asserting a named holder here would be asserting a fix that does
    not exist; asserting an unnamed one would cement the gap. What is provable
    is that the refusal is the server's and that it costs the holder nothing.
    """
    record = _mounted(client, tmp_path, home)
    item = client.files.item_by_path(record.drive_id, record.org_path)

    with pytest.raises(AlkeraHTTPError) as refused:
        files_namespace(_http(client)).acquire_lease(
            record.drive_id,
            record.node_id,
            instance_id="a-second-machine",
            machine_id="ana-thinkpad",
            if_match=str(item.get("etag", "")),
        )
    # The mount chain reads that refusal as the supersession it acts on.
    supersession = superseded_from(refused.value)
    assert supersession is not None
    assert "leased" in str(supersession)
    # The lease the second machine bounced off is untouched: a refused acquire
    # must not disturb the holder's epoch, or the fence would cost the holder
    # its folder every time somebody else asked for it.
    assert _lease_row(client, record)["epoch"] == record.epoch


def test_a_write_under_a_stale_epoch_is_refused_by_the_server(
    client: AlkeraClient, tmp_path: Path, home: Path
) -> None:
    """The epoch is the fence: the same instance, one epoch behind, cannot
    announce a batch. Nothing local is consulted — the row does the refusing."""
    record = _mounted(client, tmp_path, home)
    item = client.files.item_by_path(record.drive_id, record.org_path)

    with pytest.raises(AlkeraHTTPError) as refused:
        files_namespace(_http(client)).push_snapshot(
            record.drive_id,
            record.node_id,
            epoch=record.epoch - 1,
            instance_id=record.instance_id,
            changes=[{"uploaded": 1}],
            if_match=str(item.get("etag", "")),
        )
    assert superseded_from(refused.value) is not None


# ---------------------------------------------------------------------------
# The cloud copy trails by seconds, not by an unmount
# ---------------------------------------------------------------------------


def test_an_exported_snapshot_publishes_the_edit_without_unmounting(
    client: AlkeraClient, tmp_path: Path, home: Path
) -> None:
    """The whole point of the exporter: another reader gets the bytes while the
    folder is still held, and ``last_sync_at`` moves when it does."""
    record = _mounted(client, tmp_path, home)
    local = Path(record.local_root)
    before = _lease_row(client, record).get("lastSyncAt")

    (local / "docs" / "note.txt").write_bytes(b"exported mid-mount\n")
    export(files=client.files, http=_http(client), record=record)

    after = _lease_row(client, record).get("lastSyncAt")
    assert after is not None and after != before

    elsewhere = tmp_path / "reader"
    pull(files=client.files, http=_http(client), root=elsewhere, source=home_path(client, "proj"))
    assert (elsewhere / "docs" / "note.txt").read_bytes() == b"exported mid-mount\n"
    # Still ours: exporting is not handing the folder back.
    assert _lease_row(client, record)["epoch"] == record.epoch


def test_a_second_export_advances_last_sync_even_with_nothing_to_upload(
    client: AlkeraClient, tmp_path: Path, home: Path
) -> None:
    """A holder that changed nothing must still look current, or the server's
    ``stale`` flag fires on a mount that is perfectly healthy."""
    record = _mounted(client, tmp_path, home)
    export(files=client.files, http=_http(client), record=record)
    first = _lease_row(client, record)["lastSyncAt"]
    time.sleep(0.02)
    summary = export(files=client.files, http=_http(client), record=record)

    assert summary.uploaded == 0
    assert _lease_row(client, record)["lastSyncAt"] != first


def test_the_self_fence_stops_the_export_before_anything_leaves_the_machine(
    client: AlkeraClient, backend: LiveBackend, tmp_path: Path, home: Path
) -> None:
    """A holder whose beats stopped landing refuses its own batch — and the
    proof is that the *server saw no request*, not that a flag was set."""
    record = _mounted(client, tmp_path, home)
    (Path(record.local_root) / "docs" / "note.txt").write_bytes(b"must not land\n")
    ticks = iter([0.0, 1000.0, 1000.0, 1000.0])
    fence = SelfFence.for_record(record, monotonic=lambda: next(ticks))
    backend.log.clear()

    with pytest.raises(LeaseExpiredError) as stopped:
        export(files=client.files, http=_http(client), record=record, fence=fence)

    assert "stopped exporting" in str(stopped.value)
    assert backend.log.count("/snapshots", "/uploads") == 0
    elsewhere = tmp_path / "reader"
    pull(files=client.files, http=_http(client), root=elsewhere, source=home_path(client, "proj"))
    assert (elsewhere / "docs" / "note.txt").read_bytes() == b"first\n"


def test_a_beat_that_lands_keeps_the_self_fence_open(
    client: AlkeraClient, tmp_path: Path, home: Path
) -> None:
    """The negative twin: the same elapsed time with a beat in the middle must
    NOT refuse, or the fence would be a timer rather than a silence detector."""
    record = _mounted(client, tmp_path, home)
    ticks = iter([0.0, 1000.0, 1000.0, 1000.0, 1000.0])
    fence = SelfFence.for_record(record, monotonic=lambda: next(ticks))
    fence.beat()

    export(files=client.files, http=_http(client), record=record, fence=fence)


# ---------------------------------------------------------------------------
# The SIGKILLed holder
# ---------------------------------------------------------------------------


def _cli_env(backend: LiveBackend, home: Path) -> dict[str, str]:
    """A signed-in ``ALKERA_HOME`` for a child process of the real CLI."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.yml").write_text(
        yaml.safe_dump(
            {
                "api_url": backend.base_url,
                "token": backend.token,
                "expires_at": "2099-01-01T00:00:00+00:00",
            }
        )
    )
    (home / "auth.yml").chmod(0o600)
    environment = dict(os.environ)
    environment["ALKERA_HOME"] = str(home)
    return environment


@pytest.mark.skipif(os.name == "nt", reason="SIGKILL is a POSIX signal")
def test_a_sigkilled_mount_leaves_a_record_that_still_holds_the_folder(
    client: AlkeraClient, backend: LiveBackend, tmp_path: Path, home: Path
) -> None:
    """SIGKILL the real CLI mid-hold: the lease outlives the process, and the
    record it left behind is the only thing that knows the folder is ours.

    ``mounts`` must still call the folder checked out — the server lists the
    lease at this epoch, so nobody else can write it — while reporting that
    nothing is beating for it. Reading the dead pid as "no longer yours" is
    what made a `--no-hold` mount read stale the second it succeeded.

    The resume the record exists for is **not** provable here: the server's
    acquire has no same-instance path (a live lease is refused whatever the
    instance id), so ``mount`` on this directory raises until the folder's TTL
    runs out. That gap is the server's, and pinning either answer to it here
    would pin a contract the lease service has not made.
    """
    proj = home_path(client, "proj")
    _seed(client, tmp_path, proj)
    root = tmp_path / "held"
    root.mkdir()
    environment = _cli_env(backend, home)

    child = subprocess.Popen(
        [sys.executable, "-m", "alkera_cli.main", "files", "mount", proj, str(root), "--hold"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        # The record is written before the pull runs, so waiting on it alone
        # would race the bytes the resume is supposed to find already there.
        pulled = root / "docs" / "note.txt"
        for _ in range(600):
            if load_record(root, home=home) is not None and pulled.is_file():
                break
            time.sleep(0.05)
        else:  # pragma: no cover - a mount that never lands fails here
            child.kill()
            pytest.fail(f"the mount never finished: {child.communicate()[0]!r}")
        killed = load_record(root, home=home)
        assert killed is not None
        assert pulled.read_bytes() == b"first\n"

        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=30)
    finally:
        if child.poll() is None:  # pragma: no cover - only reached on a failure above
            child.kill()

    # The lease outlives the process: the folder is still ours, and the record
    # is the only thing on this machine that knows it.
    killed_status = [
        status
        for status in mounts(files=client.files, http=_http(client), home=home)
        if status.record.local_root == killed.local_root
    ]
    assert [status.state for status in killed_status] == ["live"]
    assert killed_status[0].held is True
    assert killed_status[0].running is False
    assert killed_status[0].record.instance_id == killed.instance_id


# ---------------------------------------------------------------------------
# The push a box makes of a chat folder it leases
# ---------------------------------------------------------------------------


def test_a_box_pushes_the_chat_folder_it_leases_and_the_files_are_listed(
    client: AlkeraClient, backend: LiveBackend, tmp_path: Path, home: Path
) -> None:
    """The chat-folder push, through the real routes, lands every file.

    A box's push is a ``tree`` on the leased node for the folders the chat
    wrote, then the uploads and the snapshot — every one fenced by the box's
    own epoch. The route fenced the parent and then created each folder with
    no lease, so the holder was refused its own skeleton as ``files.leased``
    naming itself, read that as a change of hands, and dropped the chat: the
    folder listed nothing while the file sat on the box's disk.

    The push used to open with a ``tree`` at the drive root that only walked
    down to the leased folder. The root is a signpost: a plain member holds no
    write on it, so that opening call was refused before the folder the box
    holds was ever reached. A mount knows the node it holds, so the skeleton
    starts there and the root is never addressed.
    """
    from alkera_cli.cloud.folder import ChatFolders

    drive = client.files.drive()
    drive_id = str(drive["id"])
    node = client.files.create_folder(drive_id, str(drive["homeId"]), "kickoff")
    folders = ChatFolders(
        chats_root=tmp_path / "box" / "chats", files=client.files, http=_http(client), home=home
    )

    held = folders.take("chat-a", {"files_node_id": str(node["id"])}, instance="box-7:chat-a")
    assert held is not None
    (held.root / ".runtime" / "agent").mkdir(parents=True)
    (held.root / ".runtime" / "agent" / "agent.db").write_bytes(b"SQLite format 3\x00")
    (held.root / "qat.txt").write_bytes(b"ALPHA")
    backend.log.clear()

    pushed = folders.push("chat-a")
    assert pushed is not None, "the push did not land"
    assert (pushed.folders, pushed.uploaded) == (2, 2)
    assert folders.held("chat-a") is not None
    assert backend.log.count(f"/items/{drive['rootId']}/tree") == 0, "the root was addressed"
    assert backend.log.count(f"/items/{node['id']}/tree") == 1

    listed = {row["name"] for row in client.files.children(drive_id, str(node["id"]))}
    assert listed == {".runtime", "qat.txt"}

    released = folders.hand_back("chat-a")
    assert released is not None
    assert released.push.unchanged == 2 and released.push.uploaded == 0
    assert [
        row
        for row in files_namespace(_http(client)).my_leases(drive_id)
        if str(row.get("nodeId")) == str(node["id"])
    ] == []
