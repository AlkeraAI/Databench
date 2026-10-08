"""The chat folder a box holds while the chat is awake, and the sleep that
returns it.

The wire here is a real ``httpx`` client over a transport that answers as the
lease routes do, because everything worth proving happens on it: the purpose
the folder is taken under, the 409 a second box meets, whether the push went up
BEFORE the release, and whether the lease is gone afterwards. A stubbed mount
chain could show none of that — it would only show that this module calls the
functions it calls.

The push itself is handed a tree walker that reads the real local directory, so
"the bytes are on the server" is asserted as: the release the server received
carries the files that were actually on this box's disk.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from _mirror_service import Clock, build_service
from alkera_cli.cloud import CloudRestClient, CloudSocket
from alkera_cli.cloud.folder import (
    CHAT_FOLDER_PURPOSE,
    FOLDER_TRASHED,
    LEASE_ENDED,
    ChatFolders,
    FolderBusyError,
    ReleasedFolder,
    chat_folder_path,
)
from alkera_cli.cloud.mirror import ChatMirror
from alkera_cli.files.live_sync import RestLiveApi
from alkera_cli.files.mount import load_record
from alkera_cli.files.nodemap import KnownNode, NodeMap, save_node_map
from alkera_cli.files.push import AgreedBase, PushSummary
from alkera_cli.files.tree_watch import Change
from alkera_cli.harness import HarnessRuntime, HarnessUnavailableError
from alkera_cli.harness._fake import FakeAdapter
from alkera_core.project.directory import ProjectDirectory

DRIVE = "11111111-1111-1111-1111-111111111111"
NODE = "22222222-2222-2222-2222-222222222222"
CHAT = "chat-a"
#: Where a chat's ``.alkerachat`` folder really lives: the owner's home, under
#: the chat's title — never the ``Chats/<id>`` convention.
NODE_PATH = "home/ana/Chats/Kickoff.alkerachat"
MACHINE = "6a2c1b4d-3e1f-4b3c-8e6d-82b4d0a1c222"


# ---------------------------------------------------------------------------
# A Files backend, as far as the mount chain can tell one
# ---------------------------------------------------------------------------


class FakeFiles:
    """The two ``client.files`` calls the mount chain makes.

    ``missing`` is the backend that has no chat folders yet: every path lookup
    is a 404, which is how a box running ahead of the Files side meets it.
    """

    def __init__(self, *, missing: bool = False, node_path: str = NODE_PATH) -> None:
        self.missing = missing
        self.paths: list[str] = []
        self.ids: list[str] = []
        #: Where the chat's node is filed right now — a rename moves it.
        self.node_path = node_path

    def drive(self) -> dict[str, Any]:
        return {"id": DRIVE}

    def item(self, drive_id: str, item_id: str, *, select: str | None = None) -> dict[str, Any]:
        self.ids.append(item_id)
        return {
            "id": item_id,
            "etag": "7",
            "kind": "folder",
            "name": self.node_path.rpartition("/")[2],
            "pathBytes": "/" + self.node_path,
        }

    def item_by_path(self, drive_id: str, item_path: str) -> dict[str, Any]:
        self.paths.append(item_path)
        if self.missing:
            from alkera_sdk.client import AlkeraHTTPError

            raise AlkeraHTTPError(
                label="files/item-by-path",
                status=404,
                code="not_found",
                message="no such folder",
                trace_id=None,
            )
        return {"id": NODE, "etag": "7", "kind": "folder", "name": item_path}


def _in_an_hour() -> str:
    return (datetime.now(UTC) + timedelta(hours=1)).isoformat()


class LeaseServer:
    """The lease routes, as far as the mount chain can tell them apart.

    ``holder`` is the instance the server considers current; anyone else is
    answered 409 with the holder named, which is the fence as a second box meets
    it. ``released`` is the history a sleep leaves behind — it is what makes
    "the lease is gone" and "the final batch carried the bytes" both checkable.
    """

    def __init__(self, *, holder: str | None = None, epoch: int = 5) -> None:
        self.now = 1_000.0
        self.ttl = 60.0
        self.expires_at = 0.0
        self.holder = holder
        self.epoch = epoch
        self.acquires: list[dict[str, Any]] = []
        self.released: list[dict[str, Any]] = []
        self.snapshots: list[dict[str, Any]] = []
        self.beats = 0
        self.log: list[str] = []
        #: The agent assertion each lease call carried, in order.
        self.agents: list[str | None] = []
        #: The bytes the last push left on the server, by path relative to the
        #: chat folder — what a box that pulls the folder is handed.
        self.store: dict[str, bytes] = {}
        #: The destination path each push was aimed at, in order.
        self.dests: list[str] = []
        #: The streaming cadence this server serves on a grant, if any.
        self.live: dict[str, Any] = {}
        #: Every live batch a holder posted, in order.
        self.live_batches: list[dict[str, Any]] = []
        #: How many of the next heartbeats are refused as fenced even though
        #: this instance still holds the lease — what a lapse the server let
        #: run out, or a reap it has not yet re-granted, leaves behind.
        self.fence_beats = 0
        #: How many beats were refused as fenced, for any reason.
        self.fenced_beats = 0
        #: The folder is in the trash: every beat is fenced and every take is
        #: refused as `files.trashed`, as the drive answers for a deleted chat.
        self.trashed = False
        #: Set, every take is refused with this code: a refusal a box must NOT
        #: read as the folder being gone.
        self.refuse_takes: str | None = None
        #: The server ended the lease (the chat was put to sleep, deleted or
        #: moved): beats are fenced and a RE-take is refused as
        #: `files.lease_ended`, while a fresh take is granted as ever.
        self.ended = False

    @property
    def holder(self) -> str | None:
        return self._holder

    @holder.setter
    def holder(self, instance: str | None) -> None:
        """Whoever holds the folder holds it for a TTL: the server's clock and
        the moment the lease lapses are what hand a crashed box's folder over —
        the liveness rule. A beat pushes the moment out; nothing else does."""
        self._holder = instance
        self.expires_at = self.now + self.ttl if instance is not None else 0.0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        target = request.url.path
        body = json.loads(request.content) if request.content else {}
        instance = body.get("instanceId") or request.headers.get("X-Alkera-Lease-Instance")
        if target.endswith("/lease"):
            self.agents.append(_asserted_agent(request))
            refusal = FOLDER_TRASHED if self.trashed else self.refuse_takes
            if refusal is None and self.ended and body.get("retake"):
                refusal = LEASE_ENDED
            if refusal is not None:
                return httpx.Response(409, json={"code": refusal, "message": "refused"})
            if self.holder is not None and instance != self.holder and self.now < self.expires_at:
                return self._leased()
            self.holder = instance
            self.epoch += 1
            self.expires_at = self.now + self.ttl
            self.acquires.append(body)
            self.log.append("acquire")
            return httpx.Response(200, json=self._grant())
        if target.endswith("/lease/heartbeat"):
            if self.trashed or self.ended:
                self.fenced_beats += 1
                return self._fenced()
            if self.fence_beats > 0:
                self.fence_beats -= 1
                self.fenced_beats += 1
                return self._fenced()
            if instance != self.holder:
                # The heartbeat route has ONE refusal and it names nobody —
                # whether the lease merely lapsed, another box acquired it, the
                # reaper reaped it or a force-release completed. A fake that
                # named a holder here would let a box read the difference off
                # the beat, which against the real server it never can.
                self.fenced_beats += 1
                return self._fenced()
            self.beats += 1
            self.expires_at = self.now + self.ttl
            return httpx.Response(200, json=self._grant())
        if target.endswith("/snapshots"):
            if instance != self.holder:
                return self._leased()
            self.snapshots.append(body)
            self.log.append("snapshot")
            return httpx.Response(200, json={})
        if target.endswith("/lease/release"):
            if instance != self.holder:
                return self._leased()
            self.released.append(body)
            self.holder = None
            self.log.append("release")
            return httpx.Response(200, json={})
        if target.endswith("/leases"):
            return httpx.Response(200, json=[])
        if target.endswith("/lease/live"):
            if instance != self.holder:
                return httpx.Response(
                    409, json={"error": {"code": "files.lease_fenced", "message": "fenced"}}
                )
            self.live_batches.append(body)
            return httpx.Response(200, json={"liveSeq": len(self.live_batches), "pending": 0})
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

    def _fenced(self) -> httpx.Response:
        """The refusal a lapsed lease gets: an epoch the row no longer has, and
        no holder — because there is none."""
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "files.lease_fenced",
                    "message": "a newer epoch owns this subtree",
                }
            },
        )

    def _leased(self) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "files.leased",
                    "message": "this folder is in use",
                    "holder": "ana@alkera.dev",
                    "machine": "box-8",
                }
            },
        )


@pytest.fixture
def server() -> LeaseServer:
    return LeaseServer()


@pytest.fixture
def http(server: LeaseServer) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(server), base_url="http://files.test")


def _asserted_agent(request: httpx.Request) -> str | None:
    from alkera_core.authz import parse_agent_assertion

    assertion = parse_agent_assertion(dict(request.headers))
    return assertion.session_id if assertion is not None else None


def _walking_push(server: LeaseServer) -> Any:
    """A push that reads the real local tree and reports what it found.

    Nothing about the bytes is invented: it walks the directory the chat has
    been writing into and counts what is really on disk, so a release whose
    final batch reports nothing is a sleep that left the work behind. The
    bytes land in the server's store under the destination path, which is
    what a later pull is handed.
    """

    def push(*, root: Path, dest: str, **_kwargs: Any) -> PushSummary:
        server.log.append("push")
        server.dests.append(dest)
        summary = PushSummary()
        server.store = {}
        for path in sorted(Path(root).rglob("*")):
            if path.is_file():
                summary.uploaded += 1
                summary.bytes_uploaded += path.stat().st_size
                server.store[path.relative_to(root).as_posix()] = path.read_bytes()
        return summary

    return push


def _materializing_pull(server: LeaseServer) -> Any:
    """A pull that writes what the server holds into the local root."""

    def pull(*, root: Path, **_kwargs: Any) -> Any:
        from alkera_cli.files.pull import PullSummary

        server.log.append("pull")
        summary = PullSummary()
        for relative, data in server.store.items():
            target = Path(root) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            summary.files += 1
        return summary

    return pull


def _folders(
    tmp_path: Path,
    http: httpx.Client,
    *,
    missing: bool = False,
    box: str = "box-7",
    files: FakeFiles | None = None,
) -> ChatFolders:
    return ChatFolders(
        chats_root=tmp_path / box / "chats",
        files=files or FakeFiles(missing=missing),
        http=http,
        machine_id=box,
        home=tmp_path / box / "home",
    )


def _chat_with_node() -> dict[str, Any]:
    """The chat row as the wire serves it: the node the chat IS, no path."""
    return {"id": CHAT, "files_node_id": NODE}


def _pull_nothing(**_kwargs: Any) -> Any:
    from alkera_cli.files.pull import PullSummary

    return PullSummary()


# ---------------------------------------------------------------------------
# Where the folder is
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("chat", "expected"),
    [
        pytest.param({}, "Chats/chat-a", id="the-convention-when-the-record-says-nothing"),
        pytest.param({"folder_path": "/Chats/kickoff"}, "Chats/kickoff", id="snake-case"),
        pytest.param({"folderPath": "Chats/kickoff"}, "Chats/kickoff", id="camel-case"),
        pytest.param({"folder_path": "   "}, "Chats/chat-a", id="blank-is-not-a-path"),
        pytest.param({"folder_path": 7}, "Chats/chat-a", id="not-a-string-is-not-a-path"),
    ],
)
def test_the_folder_a_chat_names_wins_over_the_convention(
    chat: dict[str, Any], expected: str
) -> None:
    assert chat_folder_path(chat, CHAT) == expected


# ---------------------------------------------------------------------------
# Take, beat, hand back
# ---------------------------------------------------------------------------


def test_taking_a_chat_folder_leases_it_as_a_chat_on_this_box(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acquire says what it is for and who is holding it."""
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    folders = _folders(tmp_path, http)

    held = folders.take(CHAT, {}, instance="box-7:chat-a")

    assert held is not None
    assert held.org_path == "Chats/chat-a"
    assert held.root == tmp_path / "box-7" / "chats" / CHAT
    assert server.acquires[0]["purpose"] == CHAT_FOLDER_PURPOSE
    assert server.acquires[0]["machineId"] == "box-7"
    assert server.acquires[0]["instanceId"] == "box-7:chat-a"
    # The record on disk is what makes the resume the same holder.
    assert load_record(held.root, home=tmp_path / "box-7" / "home") is not None


def test_a_take_pulls_as_the_chat(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The take's pull is told whose folder it writes, so it repairs the folder
    for that chat's sandbox and writes every byte through the chat's tree."""
    from alkera_cli.cloud import folder as folder_module
    from alkera_cli.files import mount as mount_module
    from alkera_cli.files.pull import PullSummary

    seen: dict[str, Any] = {}

    def recording_pull(**kwargs: Any) -> PullSummary:
        seen.update(kwargs)
        return PullSummary()

    monkeypatch.setattr(folder_module, "_PULL_LOCAL", recording_pull)
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", mount_module.mount)
    folders = _folders(tmp_path, http)

    held = folders.take(CHAT, {}, instance="box-7:chat-a")

    assert held is not None
    assert seen["chat_id"] == CHAT
    assert seen["root"] == held.root


def test_a_folder_another_box_holds_refuses_rather_than_double_writing(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two boxes writing one chat folder is the outcome the lease rules out."""
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    server.holder = "box-8:chat-a"
    folders = _folders(tmp_path, http)

    with pytest.raises(FolderBusyError) as refused:
        folders.take(CHAT, {}, instance="box-7:chat-a")
    assert refused.value.holder == "ana@alkera.dev on box-8"
    assert folders.held(CHAT) is None


def test_a_backend_with_no_chat_folder_yet_keeps_no_custody_and_says_so_once(
    tmp_path: Path, http: httpx.Client, caplog: pytest.LogCaptureFixture
) -> None:
    """A chat served without durable scratch beats a chat not served at all."""
    folders = _folders(tmp_path, http, missing=True)

    with caplog.at_level("INFO", logger="alkera_cli.cloud.folder"):
        assert folders.take(CHAT, {}, instance="i") is None
        assert folders.take(CHAT, {}, instance="i") is None

    assert folders.held(CHAT) is None
    assert sum("is not served by this backend" in r.message for r in caplog.records) == 1


def test_sleep_pushes_the_folder_up_before_it_hands_the_lease_back(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The order is the whole promise, and the release carries the bytes.

    A release that ran first would give the folder to the next box before this
    one's work had left, so both facts are asserted: the push is logged before
    the release, and the final batch the server received names the file that was
    really on this box's disk.
    """
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    monkeypatch.setattr("alkera_cli.cloud.folder.unmount", _unmount_with(_walking_push(server)))
    folders = _folders(tmp_path, http)
    held = folders.take(CHAT, {}, instance="box-7:chat-a")
    assert held is not None
    (held.root / "sandbox").mkdir(parents=True, exist_ok=True)
    written = "the analysis so far"
    (held.root / "sandbox" / "plan.md").write_text(written, encoding="utf-8")

    released = folders.hand_back(CHAT)

    assert released is not None
    assert server.log == ["acquire", "push", "release"]
    final = server.released[0]["final"]
    assert final[0]["uploaded"] == 1
    assert final[0]["bytesUploaded"] == len(written)
    # The lease is gone: nobody holds the folder, and this box says so too.
    assert server.holder is None
    assert folders.held(CHAT) is None


@pytest.mark.parametrize(
    "refused", [pytest.param([], id="all-landed"), pytest.param(["sandbox/bad"], id="one-refused")]
)
def test_a_sleep_leaves_the_tree_on_the_box_only_when_the_drive_refused_part_of_it(
    tmp_path: Path,
    http: httpx.Client,
    server: LeaseServer,
    monkeypatch: pytest.MonkeyPatch,
    refused: list[str],
) -> None:
    """A sleep removes the chat's tree once the drive holds it. A file the
    drive refused for a reason of its own (a name, a size) is named in the
    release as left behind, and it is the only copy: the tree stays on this
    disk rather than being wiped with it, for the next take here to send."""
    push = _walking_push(server)

    def refusing(**kwargs: Any) -> PushSummary:
        summary = push(**kwargs)
        summary.failed.extend(refused)
        return summary

    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    monkeypatch.setattr("alkera_cli.cloud.folder.unmount", _unmount_with(refusing))
    folders = _folders(tmp_path, http)
    held = folders.take(CHAT, {}, instance="box-7:chat-a")
    assert held is not None
    (held.root / "sandbox").mkdir(parents=True, exist_ok=True)
    (held.root / "sandbox" / "bad").write_bytes(b"the agent's only copy")

    assert folders.hand_back(CHAT) is not None

    assert server.log == ["acquire", "push", "release"]
    kept = held.root / "sandbox" / "bad"
    assert kept.exists() is bool(refused)
    if refused:
        assert kept.read_bytes() == b"the agent's only copy"


def test_resume_takes_the_folder_again_at_a_higher_epoch(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slept chat is resumable: the same holder takes the folder back."""
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    monkeypatch.setattr("alkera_cli.cloud.folder.unmount", _unmount_with(_walking_push(server)))
    folders = _folders(tmp_path, http)
    first = folders.take(CHAT, {}, instance="box-7:chat-a")
    assert first is not None
    folders.hand_back(CHAT)

    again = folders.take(CHAT, {}, instance="box-7:chat-a")

    assert again is not None
    assert again.record.epoch > first.record.epoch
    assert server.holder == "box-7:chat-a"


def test_a_release_hands_the_lease_back_with_nothing_pushed(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verb for a chat that was taken and never ran: the lease goes back
    at once, no snapshot goes up with it (the server is told there is no
    final batch), and what is on this disk stays here rather than being
    uploaded as if it were this box's work. The folder is takeable again."""
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    folders = _folders(tmp_path, http)
    held = folders.take(CHAT, {}, instance="box-7:chat-a")
    assert held is not None
    (held.root / "sandbox").mkdir(parents=True, exist_ok=True)
    residue = held.root / "sandbox" / "stale.md"
    residue.write_text("an earlier life's half-pushed work", encoding="utf-8")

    assert folders.release(CHAT) is True

    assert server.log == ["acquire", "release"]
    assert "final" not in server.released[0]
    assert server.holder is None
    assert folders.held(CHAT) is None
    assert load_record(held.root, home=tmp_path / "box-7" / "home") is None
    assert residue.exists(), "released, not cleaned: nothing on this disk is touched"

    again = folders.take(CHAT, {}, instance="box-7:chat-a")
    assert again is not None and again.record.epoch > held.record.epoch


def test_a_release_of_a_folder_another_box_now_holds_is_not_an_error(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lease lapsed and someone else took it: there is nothing of ours to
    hand back, and the box stops believing it holds the folder."""
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    folders = _folders(tmp_path, http)
    assert folders.take(CHAT, {}, instance="box-7:chat-a") is not None
    server.holder = "box-8:chat-a"

    assert folders.release(CHAT) is False

    assert server.log == ["acquire"]
    assert server.holder == "box-8:chat-a"
    assert folders.held(CHAT) is None


def test_a_release_of_a_folder_not_held_is_a_no_op(
    tmp_path: Path, http: httpx.Client, server: LeaseServer
) -> None:
    folders = _folders(tmp_path, http)
    assert folders.release(CHAT) is False
    assert server.log == []


def test_a_lease_that_lapsed_with_nobody_on_it_keeps_the_folder_and_the_turn(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn may run for hours, and a minute of an unreachable API is not a
    reason to end it. Four beats refused as fenced with nobody named — the
    lease lapsed and is still unclaimed, so each re-take is granted — leave the
    folder this box's; the fifth lands and the box is beating for it as if
    nothing happened.

    False from ``beat`` is what the service stops the chat's mirror on, so
    "never False" here IS "the turn was never ended".
    """
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    folders = _folders(tmp_path, http)
    folders.take(CHAT, {}, instance="box-7:chat-a")
    assert folders.beat(CHAT) is True

    server.fence_beats = 4
    for _ in range(4):
        assert folders.beat(CHAT) is True, "a lapsed lease nobody took ended the chat"
        assert folders.held(CHAT) is not None
    assert server.fenced_beats == 4

    assert folders.beat(CHAT) is True
    assert folders.held(CHAT) is not None


def test_a_beat_that_is_fenced_gives_up_the_folder_rather_than_writing_on(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A box whose lease was taken must stop believing it holds the folder.

    The beat says only "fenced", with nobody named — so what proves the folder
    moved on is the re-take being refused, and the refusal is where the holder
    is finally named.
    """
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    folders = _folders(tmp_path, http)
    folders.take(CHAT, {}, instance="box-7:chat-a")
    assert folders.beat(CHAT) is True

    server.holder = "box-8:chat-a"  # the reaper handed it over
    acquires = len(server.acquires)

    assert folders.beat(CHAT) is False
    assert folders.held(CHAT) is None
    assert len(server.acquires) == acquires, "the re-take was refused, not granted"


def test_a_folder_in_the_trash_is_let_go_on_the_next_beat(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chat was deleted under a box that was still running it: the drive
    ended its lease with the trash, the beat is fenced, and the re-take is
    refused as ``files.trashed``. That is an answer, not a wire error, so the
    box stops holding the folder — ``False`` is what the service stops the
    chat's mirror on — instead of asking again on every beat for ever. Nothing
    is released or pushed: the folder is in the trash."""
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    folders = _folders(tmp_path, http)
    folders.take(CHAT, {}, instance="box-7:chat-a")
    assert folders.beat(CHAT) is True

    server.trashed = True

    assert folders.beat(CHAT) is False
    assert folders.held(CHAT) is None
    assert server.log == ["acquire"], "nothing was granted, released or pushed"
    # Let go for good: a later beat has no folder to ask about.
    assert folders.beat(CHAT) is False
    assert server.fenced_beats == 1


def test_a_lease_the_server_ended_is_let_go_and_not_taken_back(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server ended the chat under the box — put it to sleep, deleted it,
    moved it. The beat is fenced, and the re-take says it is a re-take, so the
    server can tell it from a lapse and refuse it: the box lets the chat go
    rather than take straight back what was just ended."""
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    folders = _folders(tmp_path, http)
    folders.take(CHAT, {}, instance="box-7:chat-a")

    server.ended = True

    assert folders.beat(CHAT) is False
    assert folders.held(CHAT) is None
    assert server.log == ["acquire"], "the ended lease was taken back, released or pushed"


def test_a_re_take_after_a_lapse_says_it_is_one_and_is_granted(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: a lapse nobody ended is re-granted, and the request that
    asked for it is marked as a re-take — the mark is the whole difference the
    server decides on."""
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    folders = _folders(tmp_path, http)
    folders.take(CHAT, {}, instance="box-7:chat-a")

    server.fence_beats = 1

    assert folders.beat(CHAT) is True
    assert [bool(body.get("retake")) for body in server.acquires] == [False, True]


@pytest.mark.parametrize("ending", ["idle", "evicted", "drained", None])
def test_a_sleep_releases_with_its_reason_so_the_release_is_the_chat_ending(
    tmp_path: Path,
    http: httpx.Client,
    server: LeaseServer,
    monkeypatch: pytest.MonkeyPatch,
    ending: str | None,
) -> None:
    """A box putting a chat to sleep hands its folder back through the server's
    chat-end transition, not beside it: the release carries why. A hand-back
    that is not a sleep carries nothing, and ends nothing."""
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    monkeypatch.setattr("alkera_cli.cloud.folder.unmount", _unmount_with(_walking_push(server)))
    folders = _folders(tmp_path, http)
    folders.take(CHAT, {}, instance="box-7:chat-a")

    folders.hand_back(CHAT, ending=ending)

    assert server.released[0].get("ending") == ending


def test_a_re_take_refused_for_another_reason_keeps_the_folder(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the trash lets a chat go. A re-take the drive refused for any other
    reason says nothing about who holds the folder, so the turn goes on and the
    next beat asks again."""
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    folders = _folders(tmp_path, http)
    folders.take(CHAT, {}, instance="box-7:chat-a")

    server.fence_beats = 1
    server.refuse_takes = "files.frozen"

    assert folders.beat(CHAT) is True
    assert folders.held(CHAT) is not None


def test_a_fenced_beat_the_re_take_is_granted_keeps_the_folder_at_the_new_epoch(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lease lapsed, or the reaper took it while nobody else wanted it. The
    beat is fenced with nobody named — the same shape a beat gets when the
    folder really has moved on — so the box asks for the folder again, and is
    granted it. The turn goes on, at the epoch the server just handed back."""
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    folders = _folders(tmp_path, http)
    folders.take(CHAT, {}, instance="box-7:chat-a")
    before = folders.held(CHAT)
    assert before is not None

    server.fence_beats = 1
    assert folders.beat(CHAT) is True
    held = folders.held(CHAT)
    assert held is not None
    assert held.record.epoch > before.record.epoch, "the box writes under the epoch it was given"
    assert server.acquires, "the folder was asked for again rather than assumed"

    # And it beats normally afterwards: the re-take left the server holding the
    # same instance, so the next beat lands.
    assert folders.beat(CHAT) is True
    assert folders.held(CHAT) is not None


# ---------------------------------------------------------------------------
# The service: eviction IS sleep
# ---------------------------------------------------------------------------


class RecordingFolders:
    """Custody as the SERVICE drives it.

    The mount chain is proved against the real wire above; what is in question
    here is the service's ordering — that a chat's folder is taken before its
    mirror starts, and handed back after its session is down.
    """

    def __init__(self, *, busy: set[str] | None = None) -> None:
        self.enabled = True
        self.busy = busy or set()
        self.taken: list[str] = []
        self.handed_back: list[str] = []
        #: Whether each hand-back was allowed to land the tree somewhere else
        #: when the folder turned out to be gone from the drive.
        self.recover_asked: list[bool] = []
        #: Folders given back with nothing pushed — a chat taken and never run.
        self.released: list[str] = []
        self.beats: list[str] = []
        self.pushes: list[str] = []
        self.order: list[str] = []
        self._held: dict[str, Any] = {}
        self.instances: dict[str, str] = {}
        #: The working directory each chat's folder is streaming, if any.
        self.streaming: dict[str, Path] = {}
        self.fenced: set[str] = set()
        self.machine: str | None = None
        self._dropped: set[str] = set()

    def bind_machine(self, machine_id: str) -> None:
        self.machine = machine_id

    def drop(self, chat_id: str) -> None:
        """What a push that found the lease gone leaves: the folder unheld
        and reported dropped."""
        self._held.pop(chat_id, None)
        self._dropped.add(chat_id)

    def dropped(self, chat_id: str) -> bool:
        return chat_id in self._dropped

    def settle_drop(self, chat_id: str) -> None:
        self._dropped.discard(chat_id)

    def take(self, chat_id: str, chat: Any, *, instance: str) -> Any:
        if chat_id in self.busy:
            raise FolderBusyError(chat_id, holder="box-8")
        self.taken.append(chat_id)
        self.order.append(f"take:{chat_id}")
        self.instances[chat_id] = instance
        held = SimpleNamespace(record=SimpleNamespace(node_id=f"node-{chat_id}"), live=None)
        self._held[chat_id] = held
        return held

    def held(self, chat_id: str) -> Any:
        return self._held.get(chat_id)

    def live(self, chat_id: str, working_dir: Path) -> Any:
        if chat_id not in self._held:
            return None
        self.streaming[chat_id] = working_dir
        return None

    def stop_live(self, chat_id: str, deadline: float = 5.0) -> None:
        self.streaming.pop(chat_id, None)

    def owed(self) -> list[str]:
        """Nothing: every hand-back here lands. A hand-back that does not, and
        the pass that picks it back up, are driven against the real custody in
        ``test_cloud_folder_handback.py``."""
        return []

    def push(self, chat_id: str) -> Any:
        if chat_id not in self._held:
            return None
        self.pushes.append(chat_id)
        self.order.append(f"push:{chat_id}")
        return PushSummary()

    def beat(self, chat_id: str) -> bool:
        self.beats.append(chat_id)
        if chat_id in self.fenced:
            self._held.pop(chat_id, None)
            return False
        return True

    def hand_back(
        self, chat_id: str, *, recover: bool = True, ending: str | None = None, gone: bool = False
    ) -> Any:
        self._held.pop(chat_id, None)
        self._dropped.discard(chat_id)
        self.handed_back.append(chat_id)
        self.recover_asked.append(recover)
        self.order.append(f"hand-back:{chat_id}")
        return ReleasedFolder(chat_id=chat_id, org_path=NODE_PATH, push=PushSummary(uploaded=1))

    def release(self, chat_id: str) -> bool:
        if self._held.pop(chat_id, None) is None:
            return False
        self.released.append(chat_id)
        self.order.append(f"release:{chat_id}")
        return True


async def _serve(service: Any, chat_id: str) -> None:
    await service._ensure_mirror(chat_id, {"id": chat_id})


async def test_an_idle_chat_is_slept_its_folder_pushed_and_released(tmp_path: Path) -> None:
    """The twenty-minute eviction IS the sleep: the folder goes back with it."""
    clock = Clock()
    folders = RecordingFolders()
    service, built = build_service(tmp_path, clock=clock, folders=folders, mirror_idle_minutes=20.0)
    await _serve(service, "chat-a")
    assert folders.taken == ["chat-a"]

    clock.advance(21 * 60)
    await service.sweep_idle_mirrors()

    assert built["chat-a"].stopped
    assert folders.handed_back == ["chat-a"]
    # The session is down BEFORE the push: a session still running could write
    # behind a lease this box has already given away.
    assert folders.order == ["take:chat-a", "hand-back:chat-a"]
    # The chat is still there, so a folder somebody threw away underneath it is
    # a folder whose work is still worth landing somewhere findable.
    assert folders.recover_asked == [True]


async def test_a_chat_with_a_background_job_is_never_slept(tmp_path: Path) -> None:
    """Silence is not quiescence.

    The published counter cannot see a background shell or an unanswered
    permission prompt; the mirror can, and the sweep asks it. Without that, a
    chat whose last publish was an hour ago loses a job that is still running.
    """
    clock = Clock()
    folders = RecordingFolders()
    service, built = build_service(tmp_path, clock=clock, folders=folders, mirror_idle_minutes=20.0)
    await _serve(service, "chat-a")
    built["chat-a"].quiescent = False  # a background job, or an ask nobody answered

    clock.advance(60 * 60)
    await service.sweep_idle_mirrors()

    assert not built["chat-a"].stopped
    assert folders.handed_back == []

    # …and once it owes nothing, the window runs from THERE and then sleeps it.
    built["chat-a"].quiescent = True
    await service.sweep_idle_mirrors()
    assert not built["chat-a"].stopped, "the window starts where the job ended"
    clock.advance(21 * 60)
    await service.sweep_idle_mirrors()
    assert built["chat-a"].stopped
    assert folders.handed_back == ["chat-a"]


async def test_a_chat_whose_folder_another_box_holds_is_not_served_here(tmp_path: Path) -> None:
    """No mirror, no session, no second writer."""
    clock = Clock()
    folders = RecordingFolders(busy={"chat-a"})
    service, built = build_service(tmp_path, clock=clock, folders=folders)

    await _serve(service, "chat-a")

    assert "chat-a" not in built
    assert service.mirrors == {}
    assert folders.taken == []


async def test_a_folder_taken_away_mid_life_closes_the_chat_it_belonged_to(
    tmp_path: Path,
) -> None:
    """A beat that comes back fenced ends the chat on this box.

    Left running, the session would keep writing a folder that is now another
    box's — which is the exact corruption the lease exists to prevent.
    """
    clock = Clock()
    folders = RecordingFolders()
    service, built = build_service(tmp_path, clock=clock, folders=folders)
    await _serve(service, "chat-a")

    folders.fenced.add("chat-a")
    await service._beat_folders()

    assert built["chat-a"].stopped
    assert service.mirrors == {}


async def test_a_folder_a_push_let_go_of_stops_its_chat_on_the_next_beat_pass(
    tmp_path: Path,
) -> None:
    """A push that found the lease gone drops the folder, and the beats skip
    an unheld folder. Before, the chat went on running there: its writes were
    saved nowhere, nobody beat the lease, and the box looked to everyone like
    it held the chat. The beat pass now stops it, so the next message takes
    the folder again (or is told who holds it)."""
    clock = Clock()
    folders = RecordingFolders()
    service, built = build_service(tmp_path, clock=clock, folders=folders)
    await _serve(service, "chat-a")
    await _serve(service, "chat-b")

    folders.drop("chat-a")
    await service._beat_folders()

    assert built["chat-a"].stopped
    assert not built["chat-b"].stopped
    assert set(service.mirrors) == {"chat-b"}
    assert not folders.dropped("chat-a"), "the drop is settled once it was acted on"


async def test_a_chat_served_without_a_folder_is_not_stopped_by_the_beat_pass(
    tmp_path: Path,
) -> None:
    """The asymmetric case: no folder held is not a folder dropped. A chat
    whose backend serves no chat folder runs as it always did."""
    clock = Clock()
    folders = RecordingFolders()
    service, built = build_service(tmp_path, clock=clock, folders=folders)
    await _serve(service, "chat-a")
    folders._held.pop("chat-a")

    await service._beat_folders()

    assert not built["chat-a"].stopped


async def test_a_box_that_holds_no_folders_serves_chats_exactly_as_before(
    tmp_path: Path,
) -> None:
    """Custody is additive: a backend without chat folders loses nothing."""
    clock = Clock()
    service, built = build_service(tmp_path, clock=clock, mirror_idle_minutes=20.0)

    await _serve(service, "chat-a")
    assert built["chat-a"].state == "running"

    clock.advance(21 * 60)
    await service.sweep_idle_mirrors()
    assert built["chat-a"].stopped


# ---------------------------------------------------------------------------
# Wiring the real chain's injectable halves
# ---------------------------------------------------------------------------


def _mount_with(pull_tree: Any) -> Any:
    """The real ``mount``, with only the tree walk replaced.

    The lease, the record, the fence headers and the purpose all still go over
    the wire; what is skipped is materializing an org tree there is no content
    service for in this suite.
    """
    from alkera_cli.files import mount as mount_module

    def call(**kwargs: Any) -> Any:
        # The production call names its own pull; this stands in for it.
        kwargs.pop("pull_tree", None)
        return mount_module.mount(pull_tree=pull_tree, **kwargs)

    return call


def _unmount_with(push_tree: Any) -> Any:
    """The real ``unmount`` — push, then release, in that order — with the tree
    walk replaced by one that reads the directory that is really there."""
    from alkera_cli.files import mount as mount_module

    def call(**kwargs: Any) -> Any:
        kwargs.pop("push_tree", None)
        return mount_module.unmount(push_tree=push_tree, **kwargs)

    return call


def test_a_mirror_with_no_session_owes_nothing_and_does_not_hold_its_slot(
    tmp_path: Path,
) -> None:
    """Only something a sleep would destroy makes a chat busy.

    The quiescence check is what stops the sweep taking a chat out from under a
    background job — but read as "anything that is not a healthy running session
    is busy" it would also pin the slot of a mirror that never started, and the
    box would run out of slots on chats it is not serving.
    """
    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"),
        adapter_factory=FakeAdapterFactory(FakeAdapter, available=True),
    )
    mirror = ChatMirror(
        chat_id="chat-a",
        runtime=runtime,
        socket=cast(CloudSocket, object()),
        rest=CloudRestClient(
            api_url="http://objects.test",
            token="t",
            agent_id="chat-a",
            transport=httpx.MockTransport(lambda _r: httpx.Response(404, json={})),
        ),
    )

    assert mirror.session is None
    assert mirror.quiescent is True


# ---------------------------------------------------------------------------
# The folder is the node the chat IS, filed where it really is
# ---------------------------------------------------------------------------


def test_a_chat_that_names_its_node_is_leased_by_the_node_at_its_real_path(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record carries the node id and no path. The lease is taken on THAT
    node, and the path recorded — the one the push and the release address —
    is where the node is filed, not the ``Chats/<id>`` convention: a push
    aimed at the convention would raise a stray folder at the drive root."""
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    monkeypatch.setattr("alkera_cli.cloud.folder.unmount", _unmount_with(_walking_push(server)))
    files = FakeFiles()
    folders = _folders(tmp_path, http, files=files)

    held = folders.take(CHAT, _chat_with_node(), instance="box-7:chat-a")

    assert held is not None
    assert held.org_path == NODE_PATH
    assert held.record.node_id == NODE
    assert files.paths == [], "a chat that names its node is never resolved by name"
    (held.root / "notes.md").write_text("kept", encoding="utf-8")
    folders.hand_back(CHAT)
    assert server.dests == [NODE_PATH]


def test_a_rename_while_the_box_holds_the_chat_moves_the_push_with_it(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    monkeypatch.setattr("alkera_cli.cloud.folder.unmount", _unmount_with(_walking_push(server)))
    files = FakeFiles()
    folders = _folders(tmp_path, http, files=files)
    held = folders.take(CHAT, _chat_with_node(), instance="box-7:chat-a")
    assert held is not None

    files.node_path = "home/ana/Chats/Renamed.alkerachat"
    folders.hand_back(CHAT)

    assert server.dests == ["home/ana/Chats/Renamed.alkerachat"]


def test_the_box_leases_as_the_machine_it_registered_as(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before registration the lease call carries no assertion; after
    ``bind_machine`` every call asserts the machine — the identity the routes
    admit a box under, and the badge a refused reader is shown."""
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    monkeypatch.setattr("alkera_cli.cloud.folder.unmount", _unmount_with(_walking_push(server)))
    folders = ChatFolders(
        chats_root=tmp_path / "chats", files=FakeFiles(), http=http, home=tmp_path / "home"
    )
    folders.take(CHAT, {}, instance="i")
    folders.hand_back(CHAT)

    folders.bind_machine(MACHINE)
    folders.take(CHAT, {}, instance="i")

    assert server.agents == [None, MACHINE]
    assert server.acquires[1]["machineId"] == MACHINE


# ---------------------------------------------------------------------------
# Bytes are the truth: a fresh box gets the folder back byte for byte
# ---------------------------------------------------------------------------


def test_a_resume_on_a_fresh_box_reproduces_the_folder_byte_for_byte(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Box 7 runs the chat, writes, sleeps. Box 8 — an empty chats root, an
    empty home, a different holder — takes the chat and finds every byte box 7
    wrote, because the sleep pushed them before it released and the resume
    pulled them after it acquired."""
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_materializing_pull(server)))
    monkeypatch.setattr("alkera_cli.cloud.folder.unmount", _unmount_with(_walking_push(server)))
    first = _folders(tmp_path, http, box="box-7")
    held = first.take(CHAT, _chat_with_node(), instance="box-7:chat-a")
    assert held is not None
    written = {
        "sandbox/plan.md": b"the analysis so far\n",
        "sandbox/data/rows.csv": b"a,b\n1,2\n",
        "notes.txt": bytes(range(256)),
    }
    for relative, data in written.items():
        target = held.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    assert first.hand_back(CHAT) is not None

    second = _folders(tmp_path, http, box="box-8")
    resumed = second.take(CHAT, _chat_with_node(), instance="box-8:chat-a")

    assert resumed is not None and resumed.root != held.root
    found = {
        path.relative_to(resumed.root).as_posix(): path.read_bytes()
        for path in sorted(resumed.root.rglob("*"))
        if path.is_file()
    }
    assert found == written
    assert server.log == [
        "acquire",
        "pull",
        "push",
        "release",
        "acquire",
        "pull",
    ]


def test_a_second_box_is_refused_by_name_until_the_first_hands_the_chat_back(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    monkeypatch.setattr("alkera_cli.cloud.folder.unmount", _unmount_with(_walking_push(server)))
    first = _folders(tmp_path, http, box="box-7")
    second = _folders(tmp_path, http, box="box-8")
    assert first.take(CHAT, {}, instance="box-7:chat-a") is not None

    with pytest.raises(FolderBusyError) as refused:
        second.take(CHAT, {}, instance="box-8:chat-a")
    assert refused.value.holder == "ana@alkera.dev on box-8"
    assert second.held(CHAT) is None

    first.hand_back(CHAT)
    assert second.take(CHAT, {}, instance="box-8:chat-a") is not None
    assert server.holder == "box-8:chat-a"


def test_a_box_that_crashed_holding_the_chat_is_reclaimed_once_its_lease_lapses(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No release ever comes from a killed box. The server's own expiry is
    the liveness rule: inside the TTL the chat stays its (a beat would keep
    it), past it the next box takes the folder under its own identity."""
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    first = _folders(tmp_path, http, box="box-7")
    assert first.take(CHAT, {}, instance="box-7:chat-a") is not None
    second = _folders(tmp_path, http, box="box-8")

    server.now += server.ttl - 1
    with pytest.raises(FolderBusyError):
        second.take(CHAT, {}, instance="box-8:chat-a")

    server.now += 2
    assert second.take(CHAT, {}, instance="box-8:chat-a") is not None
    assert server.holder == "box-8:chat-a"
    # ...and the dead box, were it to come back, is the one now refused.
    assert first.beat(CHAT) is False


def test_a_push_while_awake_saves_the_work_and_keeps_the_lease(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    monkeypatch.setattr("alkera_cli.cloud.folder.export", _export_with(_walking_push(server)))
    folders = _folders(tmp_path, http)
    held = folders.take(CHAT, _chat_with_node(), instance="box-7:chat-a")
    assert held is not None
    (held.root / "draft.md").write_text("half way", encoding="utf-8")

    summary = folders.push(CHAT)

    assert summary is not None and summary.uploaded == 1
    assert server.store == {"draft.md": b"half way"}
    assert server.log == ["acquire", "push", "snapshot"] and server.dests == [NODE_PATH]
    assert server.holder == "box-7:chat-a" and folders.held(CHAT) is not None


def _export_with(push_tree: Any) -> Any:
    from alkera_cli.files import mount as mount_module

    def call(**kwargs: Any) -> Any:
        kwargs.pop("push_tree", None)
        return mount_module.export(push_tree=push_tree, **kwargs)

    return call


def _push_refused(status: int, body: dict[str, Any]) -> Any:
    def push(**_kwargs: Any) -> PushSummary:
        raise httpx.HTTPStatusError(
            "refused",
            request=httpx.Request("POST", "http://files.test/tree"),
            response=httpx.Response(status, json=body),
        )

    return push


@pytest.mark.parametrize(
    ("status", "body", "kept", "said"),
    [
        pytest.param(
            409,
            {"code": "files.lease_fenced", "message": "a newer epoch owns this subtree"},
            False,
            "no longer holds its folder",
            id="fenced-is-a-change-of-hands",
        ),
        pytest.param(
            409,
            {"code": "files.exists", "message": "that name is taken in this folder"},
            True,
            "a folder push did not land",
            id="a-taken-name-is-not",
        ),
        pytest.param(
            409,
            {"code": "files.leased", "detail": {"holder": "me", "machine": "box-7"}},
            True,
            "a folder push did not land",
            id="our-own-lease-met-unfenced-is-not",
        ),
        pytest.param(
            412,
            {"code": "files.precondition", "message": "the etag moved"},
            True,
            "a folder push did not land",
            id="a-stale-version-is-not",
        ),
    ],
)
def test_a_refused_push_drops_custody_only_when_the_lease_changed_hands(
    tmp_path: Path,
    http: httpx.Client,
    server: LeaseServer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    status: int,
    body: dict[str, Any],
    kept: bool,
    said: str,
) -> None:
    """A 409 said "the folder was taken from us" whatever its code, so a box
    that met a taken name dropped the chat, stopped beating and never pushed
    the work again — while the lease was still its own. Only the lease's own
    refusals mean that; anything else is a push that did not land, kept for
    the next pass."""
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    monkeypatch.setattr("alkera_cli.cloud.folder.export", _export_with(_push_refused(status, body)))
    folders = _folders(tmp_path, http)
    assert folders.take(CHAT, _chat_with_node(), instance="box-7:chat-a") is not None

    with caplog.at_level("INFO", logger="alkera_cli.cloud.folder"):
        assert folders.push(CHAT) is None

    assert (folders.held(CHAT) is not None) is kept
    assert [r.message for r in caplog.records if said in r.message]
    assert not [r for r in caplog.records if "no longer holds" in r.message and kept]
    # The lease itself was never touched by the refusal: a kept folder still beats.
    assert folders.beat(CHAT) is kept


def _fenced_then(push_tree: Any, *, refusals: int = 1) -> Any:
    """A push refused on the fence ``refusals`` times, then ``push_tree``."""
    left = {"n": refusals}

    def push(**kwargs: Any) -> PushSummary:
        if left["n"] > 0:
            left["n"] -= 1
            raise httpx.HTTPStatusError(
                "refused",
                request=httpx.Request("POST", "http://files.test/tree"),
                response=httpx.Response(
                    409, json={"code": "files.lease_fenced", "message": "fenced"}
                ),
            )
        result: PushSummary = push_tree(**kwargs)
        return result

    return push


def test_a_push_refused_on_the_fence_while_the_lease_is_ours_is_pushed_again_and_kept(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The QA box read one fenced 409 on an upload as the folder being taken
    while its own beat renewed the lease a second later: it stopped pushing,
    stopped streaming, and the work never reached the drive. The beat is now
    asked first, and a lease that is still ours gets the push again."""
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    monkeypatch.setattr(
        "alkera_cli.cloud.folder.export", _export_with(_fenced_then(_walking_push(server)))
    )
    folders = _folders(tmp_path, http)
    held = folders.take(CHAT, _chat_with_node(), instance="box-7:chat-a")
    assert held is not None
    (held.root / "draft.md").write_text("the turn's work", encoding="utf-8")
    beats_before = server.beats

    summary = folders.push(CHAT)

    assert summary is not None and summary.uploaded == 1
    assert server.store == {"draft.md": b"the turn's work"}
    assert server.beats == beats_before + 1, "the lease was asked before the second push"
    assert folders.held(CHAT) is not None and not folders.dropped(CHAT)


def test_a_push_refused_on_the_fence_after_the_lease_moved_is_dropped_and_said(
    tmp_path: Path,
    http: httpx.Client,
    server: LeaseServer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    pushes: list[str] = []

    def counted(**kwargs: Any) -> PushSummary:
        pushes.append("push")
        return _fenced_then(_walking_push(server), refusals=99)(**kwargs)

    monkeypatch.setattr("alkera_cli.cloud.folder.export", _export_with(counted))
    folders = _folders(tmp_path, http)
    assert folders.take(CHAT, _chat_with_node(), instance="box-7:chat-a") is not None
    server.holder = "box-8:chat-a"  # another box took it

    with caplog.at_level("WARNING", logger="alkera_cli.cloud.folder"):
        assert folders.push(CHAT) is None

    assert folders.held(CHAT) is None and folders.dropped(CHAT)
    assert pushes == ["push"], "a lease that moved is not pushed into again"
    assert [r for r in caplog.records if "no longer holds" in r.message or "held by" in r.message]


def test_a_push_refused_on_the_fence_while_the_drive_is_unreachable_keeps_the_folder(
    tmp_path: Path, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is known when the beat that would say whose the lease is does
    not land: the folder is kept and the next pass asks again."""
    reachable = {"yes": True}

    def wire(request: httpx.Request) -> httpx.Response:
        if not reachable["yes"] and request.url.path.endswith("/lease/heartbeat"):
            raise httpx.ConnectError("unreachable", request=request)
        return server(request)

    http = httpx.Client(transport=httpx.MockTransport(wire), base_url="http://files.test")
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    monkeypatch.setattr(
        "alkera_cli.cloud.folder.export",
        _export_with(_fenced_then(_walking_push(server), refusals=99)),
    )
    folders = _folders(tmp_path, http)
    assert folders.take(CHAT, _chat_with_node(), instance="box-7:chat-a") is not None
    reachable["yes"] = False

    assert folders.push(CHAT) is None

    assert folders.held(CHAT) is not None and not folders.dropped(CHAT)


# ---------------------------------------------------------------------------
# The service: sleep is said after the folder is back, and an open wakes it
# ---------------------------------------------------------------------------


class RecordingRest(CloudRestClient):
    """The one route the sleep/wake cycle speaks over REST, recorded in the
    same order log the folders write — so "asleep was said AFTER the folder
    was handed back" is one list."""

    def __init__(self, order: list[str]) -> None:
        super().__init__(api_url="http://127.0.0.1:1", token="t", agent_id="machine:x")
        self._order = order
        self.reports: list[tuple[str, str]] = []
        #: The sentence each report carried, in the same order as ``reports``.
        self.reasons: list[str] = []

    def for_agent(self, agent_id: str) -> CloudRestClient:
        return self

    async def report_publisher_state(
        self,
        chat_id: str,
        *,
        state: str,
        reason: str = "",
        refusal_kind: str | None = None,
        ending: str | None = None,
    ) -> dict[str, Any]:
        self.reports.append((chat_id, state))
        self.reasons.append(reason)
        self._order.append(f"report:{chat_id}:{state}")
        return {}


async def _registered_service(tmp_path: Path, folders: RecordingFolders, **overrides: Any) -> Any:
    clock = Clock()
    rest = RecordingRest(folders.order)
    service, built = build_service(tmp_path, clock=clock, folders=folders, rest=rest, **overrides)
    await service._adopt_machine(MACHINE)
    return service, built, clock, rest


async def test_sleep_is_reported_after_the_folder_is_back_and_an_open_wakes_the_chat(
    tmp_path: Path,
) -> None:
    folders = RecordingFolders()
    service, built, clock, rest = await _registered_service(
        tmp_path, folders, mirror_idle_minutes=20.0
    )
    assert folders.machine == MACHINE, "the folders lease as the machine the box registered as"

    await service._ensure_mirror(CHAT, {"id": CHAT, "last_seq": 4})
    assert rest.reports == [(CHAT, "publishing")]

    clock.advance(21 * 60)
    await service.sweep_idle_mirrors()
    assert built[CHAT].stopped
    assert folders.order == [
        f"take:{CHAT}",
        f"report:{CHAT}:publishing",
        f"hand-back:{CHAT}",
        f"report:{CHAT}:asleep",
    ]

    # The same row again, nothing said and nobody at the door: it stays asleep.
    await service._ensure_mirror(CHAT, {"id": CHAT, "last_seq": 4})
    assert folders.taken == [CHAT] and rest.reports[-1] == (CHAT, "asleep")

    # A reader opened it: the row carries the wake, and the box takes it back.
    await service._ensure_mirror(
        CHAT, {"id": CHAT, "last_seq": 4, "wake_requested_at": "2026-09-15T10:00:00+00:00"}
    )
    assert folders.taken == [CHAT, CHAT]
    assert rest.reports[-1] == (CHAT, "publishing")
    assert not built[CHAT].stopped and CHAT in service.mirrors


async def test_a_box_going_away_puts_every_chat_it_serves_to_sleep(tmp_path: Path) -> None:
    """Which chat finishes first is not a contract — the releases are a
    `gather` on purpose, so that one slow folder costs its own chat and nothing
    else — but each chat's own hand-back before its own `asleep` is."""
    folders = RecordingFolders()
    service, built, _clock, rest = await _registered_service(tmp_path, folders)
    await service._ensure_mirror("chat-a", {"id": "chat-a"})
    await service._ensure_mirror("chat-b", {"id": "chat-b"})

    await service.stop()

    assert built["chat-a"].stopped and built["chat-b"].stopped
    assert sorted(folders.handed_back) == ["chat-a", "chat-b"]
    assert sorted(r for r in rest.reports if r[1] == "asleep") == [
        ("chat-a", "asleep"),
        ("chat-b", "asleep"),
    ]
    for chat_id in ("chat-a", "chat-b"):
        # A reader who sees `asleep` may open the chat at once, and the box that
        # answers has to find the lease free — per chat, in either order.
        assert folders.order.index(f"hand-back:{chat_id}") < folders.order.index(
            f"report:{chat_id}:asleep"
        )


async def test_a_turn_that_ended_pushes_the_folder_once_and_never_mid_turn(
    tmp_path: Path,
) -> None:
    folders = RecordingFolders()
    service, built, _clock, _rest = await _registered_service(tmp_path, folders)
    await service._ensure_mirror(CHAT, {"id": CHAT})

    await service._upkeep_folders()
    assert folders.pushes == [], "nothing has been said: nothing to save"

    built[CHAT].published_count = 3
    built[CHAT].turn_running = True
    await service._upkeep_folders()
    assert folders.pushes == [], "a half-written file is not a save"

    built[CHAT].turn_running = False
    await service._upkeep_folders()
    await service._upkeep_folders()
    assert folders.pushes == [CHAT], "once per turn, not once per tick"

    built[CHAT].published_count = 9
    await service._upkeep_folders()
    assert folders.pushes == [CHAT, CHAT]
    assert folders.handed_back == []
    # The upkeep pass pushes; keeping the lease is a different pass, and a
    # folder pushing for minutes must never be what stops the beats landing.
    assert folders.beats == []
    await service._beat_folders()
    assert folders.beats == [CHAT] and folders.pushes == [CHAT, CHAT]


# ---------------------------------------------------------------------------
# The service: a mirror that will not start gives the folder back and waits
# ---------------------------------------------------------------------------

POLL = 15.0
ROW = {"id": CHAT, "last_seq": 4}


def _wont_start(detail: str = "ses_f597") -> HarnessUnavailableError:
    return HarnessUnavailableError(
        f"agent session {detail!r} pinned in this chat's manifest no longer exists "
        "in the harness storage"
    )


async def _failing_service(
    tmp_path: Path, folders: RecordingFolders, failing: dict[str, BaseException], **overrides: Any
) -> Any:
    overrides.setdefault("poll_interval", POLL)
    return await _registered_service(tmp_path, folders, failing_starts=failing, **overrides)


async def test_a_mirror_that_will_not_start_gives_the_folder_back_unpushed(
    tmp_path: Path,
) -> None:
    """Nothing ran here, so nothing here may hold the chat — or push it.

    The take pulled the folder down; a mirror that never opened a session
    wrote nothing, and the disk may still carry an earlier life's residue, so
    the lease goes back WITHOUT a push. It goes back at once rather than on
    its own TTL: a chat held by a box that cannot serve it is a chat nobody
    can wake anywhere else.
    """
    folders = RecordingFolders()
    service, built, _clock, _rest = await _failing_service(tmp_path, folders, {CHAT: _wont_start()})

    await service._ensure_mirror(CHAT, dict(ROW))

    assert built[CHAT].stopped, "whatever the start spawned goes down with the mirror"
    assert service.mirrors == {}
    assert folders.held(CHAT) is None
    assert folders.released == [CHAT]
    assert folders.pushes == [] and folders.handed_back == []
    # The mirror is down before the release, and the release is done before
    # the reader is told: a wake elsewhere then finds the lease free.
    assert folders.order == [f"take:{CHAT}", f"release:{CHAT}", f"report:{CHAT}:refused"]


async def test_a_failed_start_is_not_retried_every_tick(tmp_path: Path) -> None:
    """The wait starts at twice the poll interval and doubles each time: a
    tick inside it does not take the folder, the first tick past it does."""
    folders = RecordingFolders()
    service, _built, clock, _rest = await _failing_service(tmp_path, folders, {CHAT: _wont_start()})
    await service._ensure_mirror(CHAT, dict(ROW))
    assert folders.taken == [CHAT]

    clock.advance(POLL)
    await service._ensure_mirror(CHAT, dict(ROW))
    assert folders.taken == [CHAT], "the next tick is inside the wait: the folder stays free"
    assert folders.released == [CHAT]

    clock.advance(POLL)
    await service._ensure_mirror(CHAT, dict(ROW))
    assert folders.taken == [CHAT, CHAT], "the tick past the wait tries again"
    assert folders.released == [CHAT, CHAT], "…and a second failure gives the folder back again"

    for _ in range(3):
        clock.advance(POLL)
        await service._ensure_mirror(CHAT, dict(ROW))
    assert folders.taken == [CHAT, CHAT], "the second wait is twice the first"
    clock.advance(POLL)
    await service._ensure_mirror(CHAT, dict(ROW))
    assert folders.taken == [CHAT, CHAT, CHAT]


async def test_the_wait_between_failed_starts_is_capped(tmp_path: Path) -> None:
    folders = RecordingFolders()
    service, _built, clock, _rest = await _failing_service(
        tmp_path, folders, {CHAT: _wont_start()}, poll_interval=100.0
    )
    await service._ensure_mirror(CHAT, dict(ROW))  # waits 200 s
    clock.advance(200.0)
    await service._ensure_mirror(CHAT, dict(ROW))  # 400 s, capped to 300 s
    assert folders.taken == [CHAT, CHAT]

    clock.advance(299.0)
    await service._ensure_mirror(CHAT, dict(ROW))
    assert folders.taken == [CHAT, CHAT]
    clock.advance(1.0)
    await service._ensure_mirror(CHAT, dict(ROW))
    assert folders.taken == [CHAT, CHAT, CHAT], "the cap, not the doubled wait"

    clock.advance(300.0)
    await service._ensure_mirror(CHAT, dict(ROW))
    assert folders.taken == [CHAT, CHAT, CHAT, CHAT], "and it stays at the cap"


@pytest.mark.parametrize(
    ("change", "ends_the_wait"),
    [
        pytest.param({"last_seq": 5}, True, id="a-reader-said-something"),
        pytest.param(
            {"wake_requested_at": "2026-09-15T10:00:00+00:00"}, True, id="a-reader-opened-it"
        ),
        pytest.param({"files_node_id": NODE}, True, id="the-chat-was-re-filed"),
        pytest.param({"title": "Renamed"}, False, id="a-rename-is-not-news"),
    ],
)
async def test_news_on_the_row_ends_the_wait_and_the_rest_does_not(
    tmp_path: Path, change: dict[str, Any], ends_the_wait: bool
) -> None:
    """A wait is for the SAME situation; something new on the row is tried at
    once — not every edit, or a box would retry on its own status reports."""
    folders = RecordingFolders()
    service, _built, clock, _rest = await _failing_service(tmp_path, folders, {CHAT: _wont_start()})
    await service._ensure_mirror(CHAT, dict(ROW))

    clock.advance(POLL)
    await service._ensure_mirror(CHAT, {**ROW, **change})

    assert folders.taken == ([CHAT, CHAT] if ends_the_wait else [CHAT])


async def test_the_reader_is_told_why_the_chat_could_not_be_resumed(tmp_path: Path) -> None:
    """Not a spinner: the chat reads as refused with the failure in words, and
    the service says it for whoever asks. Said once per reason — a retry that
    fails the same way does not re-announce; a different failure does."""
    folders = RecordingFolders()
    failing: dict[str, BaseException] = {CHAT: _wont_start()}
    service, _built, clock, rest = await _failing_service(tmp_path, folders, failing)

    await service._ensure_mirror(CHAT, dict(ROW))

    assert rest.reports == [(CHAT, "refused")]
    (reason,) = rest.reasons
    assert "could not resume" in reason and "ses_f597" in reason, reason
    assert service.start_failures == {CHAT: reason}
    assert folders.order.index(f"release:{CHAT}") < folders.order.index(f"report:{CHAT}:refused"), (
        "the folder is free by the time a reader is told; a wake elsewhere can take it"
    )

    clock.advance(2 * POLL)
    await service._ensure_mirror(CHAT, dict(ROW))
    assert rest.reports == [(CHAT, "refused")], "the same failure again is not news"

    # A different verdict. (An OSError would be a passing fault, said only
    # once it outlasts its tries.)
    failing[CHAT] = RuntimeError("the agent binary is gone")
    clock.advance(4 * POLL)
    await service._ensure_mirror(CHAT, dict(ROW))
    assert rest.reports == [(CHAT, "refused"), (CHAT, "refused")]
    assert "binary is gone" in rest.reasons[-1]


async def test_a_start_that_succeeds_on_retry_clears_the_wait_and_reports_publishing(
    tmp_path: Path,
) -> None:
    folders = RecordingFolders()
    failing: dict[str, BaseException] = {CHAT: _wont_start()}
    service, built, clock, rest = await _failing_service(
        tmp_path, folders, failing, mirror_idle_minutes=20.0
    )
    await service._ensure_mirror(CHAT, dict(ROW))
    assert rest.reports == [(CHAT, "refused")]

    failing.clear()
    clock.advance(2 * POLL)
    await service._ensure_mirror(CHAT, dict(ROW))

    assert CHAT in service.mirrors and built[CHAT].state == "running"
    assert rest.reports[-1] == (CHAT, "publishing")
    assert service.start_failures == {}
    assert folders.taken == [CHAT, CHAT]

    # The slate is clean: a later failure waits from the base again, not from
    # where the earlier run of failures left off.
    clock.advance(21 * 60)
    await service.sweep_idle_mirrors()
    assert folders.handed_back == [CHAT]
    failing[CHAT] = _wont_start()
    woken = {**ROW, "wake_requested_at": "2026-09-15T10:00:00+00:00"}
    await service._ensure_mirror(CHAT, woken)
    assert folders.taken == [CHAT, CHAT, CHAT] and folders.released == [CHAT, CHAT]
    clock.advance(POLL)
    await service._ensure_mirror(CHAT, woken)
    assert folders.taken == [CHAT, CHAT, CHAT]
    clock.advance(POLL)
    await service._ensure_mirror(CHAT, woken)
    assert folders.taken == [CHAT, CHAT, CHAT, CHAT], "twice the poll interval, as the first time"


# ---------------------------------------------------------------------------
# The take IS the pull: a fresh box holds every byte before its first turn
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _tree_of(root: Path) -> dict[str, str]:
    """Every regular file under ``root``, by sha256 — symlinks aside."""
    return {
        path.relative_to(root).as_posix(): _sha256(path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _seal(node_id: str) -> None:
    """The bridge's ``NO_DOWNLOAD`` on the folder, as a chat's is born with.

    Stamped over the sync DSN the way the live fixture opens the quota, so the
    folder the box leases is sealed exactly as a real chat's is: the item
    reads, nobody downloads, every child inherits the bit.
    """
    from alkera_core.config import settings
    from alkera_core.files.authz.decider import NO_DOWNLOAD_BIT
    from sqlalchemy import create_engine, text

    sync = create_engine(settings.database_url_sync)
    try:
        # The route's session is committed in the dependency's TEARDOWN, which
        # runs after the response has been written — so a 201 in hand does not
        # yet mean another connection can see the row. Wait for it rather than
        # racing it; a node that never appears still fails loudly.
        deadline = time.monotonic() + 5.0
        while True:
            with sync.begin() as connection:
                stamped = connection.execute(
                    text("UPDATE file_nodes SET flags = flags | :bit WHERE id = :id"),
                    {"bit": NO_DOWNLOAD_BIT, "id": node_id},
                )
            if stamped.rowcount == 1:
                return
            assert time.monotonic() < deadline, f"no node {node_id} to seal"
            time.sleep(0.05)
    finally:
        sync.dispose()


@pytest.fixture
def live_chat(tmp_path: Path) -> Any:
    """A real backend holding one sealed chat folder, and a box factory.

    The pull is a wire client: the capability it prunes on and the content
    route it fetches from are both decided by the server, so the only proof
    that a box can read its own chat folder back is the real routes.

    Every box the factory hands out is a PROVEN one — its own credential with a
    live workspace machine registered on it, which is the pair the drive checks
    an agent assertion against. The two headers on their own are a member
    spelling a public id: they buy no holdership, and a sealed folder stays
    sealed against them. A box that skipped the registration would not be
    refused at the seam this suite is about; it would be refused at the front
    door, and every claim below would be a claim about an impostor.
    """
    from alkera_cli.commands.files import FILES_TRANSFER_TIMEOUT
    from alkera_sdk import AlkeraClient
    from files._live_backend import live_backend

    with (
        live_backend(tmp_path / "server", box=True) as backend,
        AlkeraClient(
            base_url=backend.base_url, token=backend.token, timeout=FILES_TRANSFER_TIMEOUT
        ) as api,
        contextlib.ExitStack() as boxes,
    ):
        assert backend.box_token is not None and backend.box_machine is not None
        drive = api.files.drive()
        # Under the home: the drive root is a signpost that takes no direct write.
        node = api.files.create_folder(str(drive["id"]), str(drive["homeId"]), "Kickoff.alkerachat")
        _seal(str(node["id"]))
        sealed = api.files.item(str(drive["id"]), str(node["id"]))
        assert sealed["capabilities"]["can_download"] is False, f"not sealed: {sealed!r}"

        wires: dict[ChatFolders, AlkeraClient] = {}

        def box(name: str) -> ChatFolders:
            # The box's own credential, not the owner's: the registration that
            # proves a machine names one token, so the same headers sent on the
            # person's client are a person with a header.
            client = boxes.enter_context(
                AlkeraClient(
                    base_url=backend.base_url,
                    token=backend.box_token,
                    timeout=FILES_TRANSFER_TIMEOUT,
                )
            )
            folders = ChatFolders(
                chats_root=tmp_path / name / "chats",
                files=client.files,
                http=client.raw_client.get_httpx_client(),
                home=tmp_path / name / "home",
            )
            folders.bind_machine(backend.box_machine)
            wires[folders] = client
            return folders

        def wire(folders: ChatFolders) -> AlkeraClient:
            """The client a box speaks on, for a test driving the mount library
            under that box's lease directly. It has to be the holder's own: the
            lease is fenced on the identity the server derived, so the owner's
            client pushing a box's epoch is the forgery the fence refuses."""
            return wires[folders]

        yield box, {"files_node_id": str(node["id"])}, backend, api, wire


WRITTEN = {
    "qau.txt": b"one",
    "sandbox/data/rows.csv": b"a,b\n1,2\n",
    "blob.bin": bytes(range(256)),
}


def test_a_fresh_box_pulls_the_sealed_chat_folder_byte_for_byte_when_it_takes_it(
    live_chat: Any,
) -> None:
    """Box 7 runs the chat and sleeps; box 8, with an empty chats root and an
    empty home, takes it: every file the agent wrote is on box 8's disk, with
    the same sha256, the moment ``take`` returns — before any turn can run.

    The folder is ``NO_DOWNLOAD``. Read unfenced, the server tells the box
    there is nothing it may download and the pull prunes at the root without
    a single read; read as the holder, under the fence the lease just
    granted, the listing carries the hash and the content route serves.
    """
    box, chat, backend, _api, _wire = live_chat
    first = box("box-7")
    held = first.take(CHAT, chat, instance="box-7:chat-a")
    assert held is not None and held.pull is not None and held.pull.files == 0
    for relative, data in WRITTEN.items():
        target = held.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (held.root / "latest.txt").symlink_to("qau.txt")
    released = first.hand_back(CHAT)
    assert released is not None and released.push.uploaded == len(WRITTEN)

    backend.log.clear()
    second = box("box-8")
    resumed = second.take(CHAT, chat, instance="box-8:chat-a")

    assert resumed is not None and resumed.root != held.root
    assert _tree_of(resumed.root) == {path: _sha256(data) for path, data in WRITTEN.items()}
    assert resumed.pull is not None
    assert (resumed.pull.files, resumed.pull.unchanged, resumed.pull.symlinks) == (3, 0, 1)
    assert resumed.pull.bytes_downloaded == sum(len(data) for data in WRITTEN.values())
    assert resumed.pull.undownloadable == 0 and resumed.pull.warnings == []
    assert backend.log.count("/children") >= 1, "the folder was listed"
    assert backend.log.count("/content") == len(WRITTEN), "one content read per file"
    # The symlink round-trips: the same relative text, resolving to the same bytes.
    link = resumed.root / "latest.txt"
    assert link.is_symlink() and os.readlink(link) == "qau.txt"
    assert link.read_bytes() == b"one"


def test_a_machine_id_nobody_registered_takes_no_chat_folder(
    live_chat: Any, tmp_path: Path
) -> None:
    """The two assertion headers are a claim, not a credential.

    A box's machine id is on every chat it serves, so the id itself is public.
    Sending it from somewhere else — here the drive's own OWNER, who may read
    and write this folder and whose session the headers ride on — takes
    nothing: the folder is refused while it is free, and the box that actually
    registered the machine takes it afterwards untouched. This is the premise
    the rest of this suite's boxes rest on, which is why it is asserted rather
    than assumed.
    """
    from alkera_cli.commands.files import FILES_TRANSFER_TIMEOUT
    from alkera_sdk import AlkeraClient

    box, chat, backend, _api, _wire = live_chat
    with AlkeraClient(
        base_url=backend.base_url, token=backend.token, timeout=FILES_TRANSFER_TIMEOUT
    ) as owner:
        impostor = ChatFolders(
            chats_root=tmp_path / "impostor" / "chats",
            files=owner.files,
            http=owner.raw_client.get_httpx_client(),
            home=tmp_path / "impostor" / "home",
        )
        impostor.bind_machine(backend.box_machine)

        with pytest.raises(FolderBusyError):
            impostor.take(CHAT, chat, instance="box-7:chat-a")

    assert impostor.held(CHAT) is None
    real = box("box-7")
    held = real.take(CHAT, chat, instance="box-7:chat-a")
    assert held is not None, "the folder was free all along"


def test_an_identical_local_copy_is_not_downloaded_again(live_chat: Any) -> None:
    """The pull hashes what is on disk before it asks for anything: a take
    onto a directory already holding the server's bytes issues no content
    read and rewrites nothing. The shape that has such a directory is a box
    that died holding the chat (a clean sleep leaves nothing behind)."""
    box, chat, backend, _api, _wire = live_chat
    first = box("box-7")
    held = first.take(CHAT, chat, instance="box-7:chat-a")
    assert held is not None
    for relative, data in WRITTEN.items():
        target = held.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    assert first.push(CHAT) is not None
    before = _tree_of(held.root)
    stamps = {path: path.stat().st_mtime_ns for path in held.root.rglob("*") if path.is_file()}

    # The box came back: a new custody object over the same disk.
    backend.log.clear()
    again = box("box-7").take(CHAT, chat, instance="box-7:chat-a")

    assert again is not None and again.pull is not None
    assert (again.pull.files, again.pull.unchanged) == (0, len(WRITTEN))
    assert again.pull.bytes_downloaded == 0
    assert backend.log.count("/content") == 0, "nothing identical is fetched twice"
    assert _tree_of(again.root) == before
    assert {
        path: path.stat().st_mtime_ns for path in again.root.rglob("*") if path.is_file()
    } == stamps, "an unchanged file is not rewritten"


def test_a_local_file_never_pushed_survives_the_pull_and_a_local_edit_is_kept(
    live_chat: Any,
) -> None:
    """A box that already holds the folder and the lease keeps what it wrote.

    The box resumes its own mount (the same instance, the record still on
    disk — the crash-and-come-back shape): a file the cloud has never seen is
    still there, an edit the cloud has not seen is kept rather than pulled
    over, and the pull removes nothing. The next push carries both up.
    """
    box, chat, _backend, api, _wire = live_chat
    first = box("box-7")
    held = first.take(CHAT, chat, instance="box-7:chat-a")
    assert held is not None
    (held.root / "qau.txt").write_bytes(b"one")
    assert first.hand_back(CHAT) is not None

    running = box("box-8")
    resumed = running.take(CHAT, chat, instance="box-8:chat-a")
    assert resumed is not None and (resumed.root / "qau.txt").read_bytes() == b"one"
    (resumed.root / "never.txt").write_bytes(b"not yet pushed")
    (resumed.root / "qau.txt").write_bytes(b"one\ntwo\n")

    # Box 8 died and came back: a new custody object over the same disk.
    revived = box("box-8")
    assert revived.resumable(CHAT)
    back = revived.take(CHAT, chat, instance="box-8:chat-a")

    assert back is not None and back.pull is not None
    assert (back.root / "never.txt").read_bytes() == b"not yet pushed"
    assert (back.root / "qau.txt").read_bytes() == b"one\ntwo\n", "a local edit is kept"
    assert back.pull.kept == 1 and back.pull.files == 0
    drive_id = str(api.files.drive()["id"])
    listed = {row["name"] for row in api.files.children(drive_id, chat["files_node_id"])}
    assert "never.txt" not in listed, "the pull pushes nothing; the file is local-only still"

    released = revived.hand_back(CHAT)
    assert released is not None and released.push.uploaded == 2
    listed = {row["name"] for row in api.files.children(drive_id, chat["files_node_id"])}
    assert "never.txt" in listed


async def test_the_wake_pulls_the_folder_before_the_mirror_opens(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader opening a slept chat stamps the wake; the box that answers
    takes the lease and pulls the folder — and only then builds the mirror,
    so the first turn finds every file the last box pushed."""
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_materializing_pull(server)))
    monkeypatch.setattr("alkera_cli.cloud.folder.unmount", _unmount_with(_walking_push(server)))
    # What the last box left in Files.
    server.store = {"qau.txt": b"one"}
    folders = ChatFolders(
        chats_root=tmp_path / "chats", files=FakeFiles(), http=http, home=tmp_path / "home"
    )
    clock = Clock()
    service, built = build_service(
        tmp_path, clock=clock, folders=folders, rest=RecordingRest([]), mirror_idle_minutes=20.0
    )
    await service._adopt_machine(MACHINE)
    original = service._mirror_factory
    on_disk_at_open: list[bytes | None] = []

    def factory(chat_id: str, chat: dict[str, Any]) -> Any:
        target = folders.local_root(chat_id) / "qau.txt"
        on_disk_at_open.append(target.read_bytes() if target.exists() else None)
        return original(chat_id, chat)

    service._mirror_factory = factory

    await service._ensure_mirror(CHAT, {"id": CHAT, "last_seq": 4})
    assert on_disk_at_open == [b"one"], "the pull landed before the mirror was built"

    # The turn appended; the sleep pushes it up and hands the folder back.
    (folders.local_root(CHAT) / "qau.txt").write_bytes(b"one\ntwo\n")
    clock.advance(21 * 60)
    await service.sweep_idle_mirrors()
    assert built[CHAT].stopped and server.store == {"qau.txt": b"one\ntwo\n"}
    # The sleep left nothing of the chat on this box: the drive holds it now.
    assert not folders.local_root(CHAT).exists()

    # A reader opened it: the wake re-takes, and the pull runs again first.
    await service._ensure_mirror(
        CHAT, {"id": CHAT, "last_seq": 4, "wake_requested_at": "2026-09-15T10:00:00+00:00"}
    )

    assert on_disk_at_open == [b"one", b"one\ntwo\n"]
    assert server.log == ["acquire", "pull", "push", "release", "acquire", "pull"]
    assert CHAT in service.mirrors


# ---------------------------------------------------------------------------
# A chat that leaves a box leaves no trail on it, and its deletes travel
# ---------------------------------------------------------------------------


def _names(api: Any, chat: dict[str, Any]) -> set[str]:
    drive_id = str(api.files.drive()["id"])
    return {str(row["name"]) for row in api.files.children(drive_id, chat["files_node_id"])}


def _slept_on(box: Any, chat: dict[str, Any], name: str) -> Path:
    """Box ``name`` runs the chat, writes :data:`WRITTEN`, and sleeps it."""
    folders = box(name)
    held = folders.take(CHAT, chat, instance=f"{name}:chat-a")
    assert held is not None
    for relative, data in WRITTEN.items():
        target = held.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    assert folders.hand_back(CHAT) is not None
    return held.root


def test_a_slept_chat_leaves_nothing_on_the_box_and_the_next_box_finds_everything(
    live_chat: Any, tmp_path: Path
) -> None:
    """After the sleep the box that ran the chat holds none of its files, not
    even the node map naming them, and the box that wakes it holds all of them.
    This is what lets a dedicated box be handed to another org: nothing of the
    first org's chats is on its disk once they sleep."""
    from alkera_cli.files.nodemap import node_map_path

    box, chat, _backend, _api, _wire = live_chat
    old_root = _slept_on(box, chat, "box-7")

    assert not old_root.exists(), "the chat's tree stayed on the box it left"
    assert not node_map_path(old_root, home=tmp_path / "box-7" / "home").exists()
    assert not [p for p in (tmp_path / "box-7" / "chats").rglob("*") if p.is_file()]

    resumed = box("box-8").take(CHAT, chat, instance="box-8:chat-a")
    assert resumed is not None
    assert _tree_of(resumed.root) == {path: _sha256(data) for path, data in WRITTEN.items()}


def test_a_file_deleted_on_the_box_is_deleted_on_the_drive_when_the_chat_sleeps(
    live_chat: Any,
) -> None:
    """The push is a union — it never deletes — so without the prune a file the
    agent removed would come back on the next wake. The node map is the
    manifest of what the drive and the box agreed; a path it names that is
    gone from the disk is trashed on the drive before the lease goes back."""
    box, chat, _backend, api, _wire = live_chat
    _slept_on(box, chat, "box-7")
    second = box("box-8")
    held = second.take(CHAT, chat, instance="box-8:chat-a")
    assert held is not None
    (held.root / "qau.txt").unlink()
    (held.root / "sandbox" / "data" / "rows.csv").unlink()

    assert second.hand_back(CHAT) is not None

    assert "qau.txt" not in _names(api, chat)
    third = box("box-9").take(CHAT, chat, instance="box-9:chat-a")
    assert third is not None
    assert set(_tree_of(third.root)) == {"blob.bin"}


def test_the_checkpoint_push_prunes_too_and_keeps_the_lease(live_chat: Any) -> None:
    """Mid-life, the push that saves a turn carries its deletes as well, so a
    box that dies after it has not lost them."""
    box, chat, _backend, api, _wire = live_chat
    _slept_on(box, chat, "box-7")
    second = box("box-8")
    held = second.take(CHAT, chat, instance="box-8:chat-a")
    assert held is not None
    (held.root / "qau.txt").unlink()

    assert second.push(CHAT) is not None

    assert "qau.txt" not in _names(api, chat)
    assert "blob.bin" in _names(api, chat)
    assert second.held(CHAT) is not None, "a checkpoint keeps the folder"


def test_a_file_changed_on_the_drive_since_the_box_agreed_it_is_not_deleted(
    live_chat: Any, tmp_path: Path
) -> None:
    """The trash is conditional on the etag both sides agreed. Somebody's edit
    on the drive since (here: the agreement the box recorded no longer
    matches the node) is never thrown away by a delete on the box."""
    from alkera_cli.files.nodemap import KnownNode, NodeMap, load_node_map, save_node_map

    box, chat, _backend, api, _wire = live_chat
    _slept_on(box, chat, "box-7")
    home = tmp_path / "box-8" / "home"
    second = box("box-8")
    held = second.take(CHAT, chat, instance="box-8:chat-a")
    assert held is not None
    remembered = load_node_map(held.root, home=home)
    assert remembered is not None and "qau.txt" in remembered.files
    stale = remembered.files["qau.txt"].model_copy(update={"etag": '"not-the-head"'})
    save_node_map(
        held.root,
        NodeMap(files={**remembered.files, "qau.txt": KnownNode(**stale.model_dump())}),
        home=home,
    )
    (held.root / "qau.txt").unlink()

    assert second.push(CHAT) is not None

    assert "qau.txt" in _names(api, chat), "a precondition that failed still deleted"


def test_a_folder_missing_from_the_disk_altogether_prunes_nothing(
    live_chat: Any, tmp_path: Path
) -> None:
    """A root that is gone is a disk problem, not the agent deleting every
    file: nothing on the drive is trashed for it."""
    import shutil

    box, chat, _backend, api, _wire = live_chat
    _slept_on(box, chat, "box-7")
    second = box("box-8")
    held = second.take(CHAT, chat, instance="box-8:chat-a")
    assert held is not None
    shutil.rmtree(held.root)

    second.push(CHAT)

    assert {"qau.txt", "blob.bin", "sandbox"} <= _names(api, chat)


def test_the_box_keeps_its_hold_on_the_folder_to_itself(live_chat: Any) -> None:
    """A chat folder travels, so neither half of a transfer carries the state a
    box keeps about its own hold on it.

    One live folder, four facts, in the order a real chat meets them:

    1. The PUSH leaves the write lock and the forensic rotations behind. The
       lock names a pid on a host and the rotations are the payloads of locks a
       reclaim already judged dead; neither means anything on the box that
       takes the folder next, and the rotations accumulate one per take if they
       ride. Everything else goes up exactly as before — the harness's own
       ``.runtime/`` state included, which is the chat's and not the box's: the
       manifest pins the agent session that lives inside it.
    2. The TAKE prunes the rotations the last life left, and leaves the lock
       this box is actually holding alone.
    3. The PULL refuses to materialize one even when a folder pushed before
       this rule existed still holds it — a lock stamped with another host is
       treated as live forever, so nothing on this box could ever reclaim it.
    4. A SECOND take on a root that already has the server's bytes moves
       nothing, transcript and all: that, not excluding the transcript, is what
       keeps a resume cheap.
    """
    from alkera_cli.files.mount import export
    from alkera_cli.files.push import push as legacy_push

    box, chat, backend, api, wire = live_chat
    drive_id = str(api.files.drive()["id"])
    first = box("box-7")
    held = first.take(CHAT, chat, instance="box-7:chat-a")
    assert held is not None
    (held.root / ".lock").write_text('{"pid": 4242, "host": "box-7"}', encoding="utf-8")
    (held.root / ".lock.stale.1789500172.71e1df99").write_text("{}", encoding="utf-8")
    (held.root / ".lock.stale.1789500243.55f29dcc").write_text("{}", encoding="utf-8")
    (held.root / ".runtime" / "agent").mkdir(parents=True)
    (held.root / ".runtime" / "agent" / "agent.db").write_bytes(b"sqlite")
    (held.root / "chat.jsonl").write_bytes(b'{"event":"one"}\n' * 400)
    (held.root / "manifest.json").write_bytes(b'{"harness": {"agent_session_id": "ses_1"}}')
    (held.root / "qav.txt").write_bytes(b"1")

    # 1. The push.
    backend.log.clear()
    pushed = first.push(CHAT)

    assert pushed is not None
    rows = list(api.files.children(drive_id, chat["files_node_id"]))
    listed = {row["name"] for row in rows}
    assert ".lock" not in listed
    assert not [name for name in listed if name.startswith(".lock.stale.")]
    assert {".runtime", "chat.jsonl", "manifest.json", "qav.txt", "trace.digest.json"} <= listed
    runtime = next(row for row in rows if row["name"] == ".runtime")
    inner = {row["name"] for row in list(api.files.children(drive_id, str(runtime["id"])))}
    assert inner == {"agent"}, "the harness's own state is the chat's, and goes up whole"
    # Five files left the machine, not eight: the four on disk plus the digest
    # the box pins over its logs before the folder leaves it; the lock and its
    # two rotations are not merely unlisted afterwards, they were never
    # uploaded.
    assert pushed.uploaded == 5 and pushed.excluded == 3
    assert not [path for _method, path in backend.log.seen if ".lock" in path]

    # 2. The take prunes what the last life rotated aside, and only that.
    # The box died holding the chat and came back over the same disk (a clean
    # sleep leaves nothing on the box for a take to prune).
    (held.root / "keep.stale.txt").write_bytes(b"not a lock")
    assert first.push(CHAT) is not None
    backend.log.clear()
    first = box("box-7")
    again = first.take(CHAT, chat, instance="box-7:chat-a")

    assert again is not None
    left = sorted(path.name for path in again.root.iterdir())
    assert not [name for name in left if name.startswith(".lock.stale.")]
    assert ".lock" in left, "the live lock is not a forensic rotation"
    assert "keep.stale.txt" in left, "only the lock's own rotations are pruned"

    # 4. (proved on the same take) nothing moved: the transcript, the manifest
    # and the harness state were all already on disk with the server's bytes,
    # so the take is free however long the history is.
    assert again.pull is not None and again.pull.bytes_downloaded == 0
    assert (again.pull.files, again.pull.unchanged) == (0, 6)
    assert backend.log.count("/content") == 0

    # 3. A folder pushed the old way still holds the lock; a fresh box must
    # not write it. The push as it was before this rule, on the holder's own
    # wire — the lease fences on who the server says is asking, so the same
    # bytes sent from the owner's client are refused rather than written.
    holder = wire(first)
    export(
        files=holder.files,
        http=holder.raw_client.get_httpx_client(),
        record=again.record,
        push_tree=legacy_push,
    )
    stored = {row["name"] for row in list(api.files.children(drive_id, chat["files_node_id"]))}
    assert {".lock", "keep.stale.txt"} <= stored, "the legacy copy is there"
    assert first.hand_back(CHAT) is not None

    backend.log.clear()
    second = box("box-8")
    resumed = second.take(CHAT, chat, instance="box-8:chat-a")

    assert resumed is not None and resumed.root != held.root
    landed = sorted(path.name for path in resumed.root.iterdir())
    assert ".lock" not in landed
    assert not [name for name in landed if name.startswith(".lock.stale.")]
    assert landed == [
        ".runtime",
        "chat.jsonl",
        "keep.stale.txt",
        "manifest.json",
        "qav.txt",
        "trace.digest.json",
    ]
    assert resumed.pull is not None
    assert resumed.pull.undownloadable == 0 and resumed.pull.warnings == []
    assert (resumed.root / "manifest.json").read_bytes().startswith(b'{"harness"')


# ---------------------------------------------------------------------------
# One box, several chats: whose fence, and whose turn
# ---------------------------------------------------------------------------


class BoxLeases:
    """The lease routes for a box holding SEVERAL folders at once.

    :class:`LeaseServer` models one folder, which is the right shape for the
    take/sleep story; a box serving many chats needs a server that can hold
    more than one lease at a time, so this one keys everything by the instance
    that took it. Every request's fence is recorded in order, because whose
    epoch a write went out under is the whole question here.
    """

    def __init__(self) -> None:
        self.epoch = 4
        self.epochs: dict[str, int] = {}
        self.beats: dict[str, int] = {}
        self.fences: list[tuple[str | None, str | None]] = []
        self.log: list[tuple[str, str | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        target = request.url.path
        body = json.loads(request.content) if request.content else {}
        instance = body.get("instanceId") or request.headers.get("X-Alkera-Lease-Instance")
        self.fences.append(
            (
                request.headers.get("X-Alkera-Lease-Epoch"),
                request.headers.get("X-Alkera-Lease-Instance"),
            )
        )
        if target.endswith("/lease"):
            self.epoch += 1
            self.epochs[str(instance)] = self.epoch
            self.log.append(("acquire", instance))
            return httpx.Response(200, json=self._grant(str(instance)))
        if target.endswith("/lease/heartbeat"):
            if instance not in self.epochs:
                return self._fenced()
            self.beats[str(instance)] = self.beats.get(str(instance), 0) + 1
            self.log.append(("beat", instance))
            return httpx.Response(200, json=self._grant(str(instance)))
        if target.endswith("/snapshots"):
            if instance not in self.epochs:
                return self._fenced()
            self.log.append(("snapshot", instance))
            return httpx.Response(200, json={})
        if target.endswith("/lease/release"):
            if instance not in self.epochs:
                return self._fenced()
            self.epochs.pop(str(instance), None)
            self.log.append(("release", instance))
            return httpx.Response(200, json={})
        if target.endswith("/leases"):
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})

    def _grant(self, instance: str) -> dict[str, Any]:
        return {
            "epoch": self.epochs[instance],
            "expiresAt": _in_an_hour(),
            "heartbeatEvery": 15.0,
            "syncInterval": 5.0,
            "forced": False,
        }

    def _fenced(self) -> httpx.Response:
        return httpx.Response(
            409,
            json={"error": {"code": "files.lease_fenced", "message": "not yours"}},
        )


def _held_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ChatFolders, BoxLeases, httpx.Client]:
    """A box holding two chats' folders, each under its own lease."""
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    box = BoxLeases()
    http = httpx.Client(transport=httpx.MockTransport(box), base_url="http://files.test")
    folders = _folders(tmp_path, http, files=FakeFiles())
    assert folders.take("chat-a", {}, instance="box-7:chat-a") is not None
    assert folders.take("chat-b", {}, instance="box-7:chat-b") is not None
    return folders, box, http


def test_a_held_folder_is_found_by_the_node_its_lease_is_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drive asks for a file by the lease it is under, never by the chat.
    Each of two held folders answers to its own lease node, a lease this box
    does not hold answers nothing, and a folder handed back is not found."""
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    box = BoxLeases()
    http = httpx.Client(transport=httpx.MockTransport(box), base_url="http://files.test")
    folders = _folders(tmp_path, http, files=FakeFiles())
    node_a = "33333333-3333-3333-3333-333333333333"
    node_b = "44444444-4444-4444-4444-444444444444"
    held_a = folders.take("chat-a", {"files_node_id": node_a}, instance="box-7:chat-a")
    held_b = folders.take("chat-b", {"files_node_id": node_b}, instance="box-7:chat-b")
    assert held_a is not None and held_b is not None

    found_a = folders.held_by_lease_node(node_a)
    found_b = folders.held_by_lease_node(node_b)
    assert found_a is not None and found_a.chat_id == "chat-a"
    assert found_b is not None and found_b.chat_id == "chat-b"
    assert folders.held_by_lease_node(NODE) is None

    assert folders.release("chat-a") is True
    assert folders.held_by_lease_node(node_a) is None
    found_b = folders.held_by_lease_node(node_b)
    assert found_b is not None and found_b.chat_id == "chat-b"


def test_taking_a_chat_folder_asks_for_the_inbound_and_live_planes(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chat's folder is not a desktop mount: the user writes into it from the
    web while the box has it, and the box streams what the agent writes — so
    the acquire says so, and the cadence the server answers is kept."""
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    server.live = {"debounceMs": 300, "batchEveryMs": 500, "maxBatchEntries": 256}
    folders = _folders(tmp_path, http)

    held = folders.take(CHAT, {}, instance="box-7:chat-a")

    assert held is not None
    assert server.acquires[0]["inbound"] is True
    assert server.acquires[0]["live"] is True
    assert held.record.live == server.live


def test_each_held_folder_writes_under_its_own_lease_not_the_boxs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two chats, two leases, two clients.

    The fence used to be set on the client the whole box shares, so a push for
    one chat signed everything else in flight with that chat's epoch. Each
    folder now owns the client its writes leave on, and the shared one — the
    client the beat and every ordinary read use — carries no fence at all.
    """
    folders, box, http = _held_two(tmp_path, monkeypatch)
    a, b = folders.held("chat-a"), folders.held("chat-b")
    assert a is not None and b is not None
    assert a.client is not None and b.client is not None
    assert a.client is not b.client

    assert a.client.headers["X-Alkera-Lease-Instance"] == "box-7:chat-a"
    assert b.client.headers["X-Alkera-Lease-Instance"] == "box-7:chat-b"
    assert a.client.headers["X-Alkera-Lease-Epoch"] == str(box.epochs["box-7:chat-a"])
    assert b.client.headers["X-Alkera-Lease-Epoch"] == str(box.epochs["box-7:chat-b"])
    assert "X-Alkera-Lease-Instance" not in http.headers
    assert "X-Alkera-Lease-Epoch" not in http.headers


def test_a_push_signs_its_own_chats_writes_and_leaves_the_shared_client_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each call goes out under the lease it belongs to, and only that one.

    A chat's push is signed by that chat's folder client for its whole length;
    the beat that runs beside it carries its OWN lease (the heartbeat route
    names it per request) and leaves the client the whole box shares exactly as
    it found it — which is what stops the next call on it being signed as
    somebody's holder.
    """
    folders, box, http = _held_two(tmp_path, monkeypatch)

    def _push(**_kwargs: Any) -> PushSummary:
        return PushSummary()

    monkeypatch.setattr("alkera_cli.cloud.folder.export", _export_with(_push))
    mark = len(box.fences)

    assert folders.push("chat-a") is not None
    assert folders.beat("chat-b") is True

    after = box.fences[mark:]
    epochs = {"a": str(box.epochs["box-7:chat-a"]), "b": str(box.epochs["box-7:chat-b"])}
    assert after == [
        (epochs["a"], "box-7:chat-a"),
        (epochs["b"], "box-7:chat-b"),
    ]
    assert ("snapshot", "box-7:chat-a") in box.log
    assert box.beats == {"box-7:chat-b": 1}
    assert "X-Alkera-Lease-Instance" not in http.headers
    assert "X-Alkera-Lease-Epoch" not in http.headers


class RebindableFiles(FakeFiles):
    """A namespace that can say which client it speaks on, as the SDK's can.

    The real ``client.files`` is bound to one ``httpx.Client`` and answers
    :meth:`on_client` with the same namespace over another; this double does
    the same and remembers which one it was handed.
    """

    def __init__(self, http: httpx.Client | None = None) -> None:
        super().__init__()
        self.http = http

    def on_client(self, http: httpx.Client) -> RebindableFiles:
        return RebindableFiles(http)


def test_a_push_opens_its_upload_session_under_its_own_chats_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A push writes through two objects and both carry the same lease.

    The tree call and the content PUT go out on the folder's own client; the
    upload session is opened through the Files namespace. The fence is a header
    on a client, so a namespace still bound to the client the whole box shares
    opens every session with no lease on it and the server refuses the bytes as
    somebody else's write — a chat that silently never saves a turn.
    """
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    shared = RebindableFiles()
    http = httpx.Client(transport=httpx.MockTransport(BoxLeases()), base_url="http://files.test")
    folders = _folders(tmp_path, http, files=shared)
    assert folders.take("chat-a", {}, instance="box-7:chat-a") is not None
    seen: list[Any] = []

    def _push(*, files: Any, **_kwargs: Any) -> PushSummary:
        seen.append(files)
        return PushSummary()

    monkeypatch.setattr("alkera_cli.cloud.folder.export", _export_with(_push))

    assert folders.push("chat-a") is not None

    (used,) = seen
    assert used is not shared, "the push wrote through the client the whole box shares"
    assert used.http is not None
    assert used.http.headers["X-Alkera-Lease-Instance"] == "box-7:chat-a"
    # And the box's own namespace is left speaking on nobody's lease.
    assert shared.http is None


def test_a_stalled_push_on_one_chat_does_not_hold_up_another_chats_beat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lease every other chat is running on must not depend on this one.

    A box holds several chats at once. While one chat's push is on the wire —
    a big turn, a slow link, a server taking its time — every other chat's
    heartbeat still has to land, or their leases lapse and their folders are
    handed to somebody else while this box is still running them. The stall
    here is held open until the other chat's beat has come back, so a box that
    serialized the two would never finish rather than merely be slow.
    """
    folders, box, _http = _held_two(tmp_path, monkeypatch)
    push_started = threading.Event()
    let_the_push_finish = threading.Event()

    def _stalling_push(**_kwargs: Any) -> PushSummary:
        push_started.set()
        assert let_the_push_finish.wait(15.0), "the beat never came back"
        return PushSummary()

    monkeypatch.setattr("alkera_cli.cloud.folder.export", _export_with(_stalling_push))
    pusher = threading.Thread(target=folders.push, args=("chat-a",), daemon=True)
    pusher.start()
    assert push_started.wait(10.0)

    beaten: list[bool] = []
    beater = threading.Thread(target=lambda: beaten.append(folders.beat("chat-b")), daemon=True)
    beater.start()
    beater.join(10.0)

    assert not beater.is_alive(), "chat B's beat was queued behind chat A's push"
    assert beaten == [True]
    assert box.beats == {"box-7:chat-b": 1}
    let_the_push_finish.set()
    pusher.join(10.0)
    assert not pusher.is_alive()


def test_one_chat_still_takes_its_turn_against_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Separating the folders must not let ONE folder race itself: a sleep that
    overlapped its own push would hand the lease back while bytes were still
    going up under it."""
    folders, box, _http = _held_two(tmp_path, monkeypatch)
    in_push = threading.Event()
    let_go = threading.Event()
    slept: list[bool] = []

    def _stalling_push(**_kwargs: Any) -> PushSummary:
        in_push.set()
        assert let_go.wait(15.0)
        return PushSummary()

    monkeypatch.setattr("alkera_cli.cloud.folder.export", _export_with(_stalling_push))
    monkeypatch.setattr("alkera_cli.cloud.folder.unmount", _unmount_with(_walking_push_only()))
    pusher = threading.Thread(target=folders.push, args=("chat-a",), daemon=True)
    pusher.start()
    assert in_push.wait(10.0)

    sleeper = threading.Thread(
        target=lambda: slept.append(folders.hand_back("chat-a") is not None), daemon=True
    )
    sleeper.start()
    sleeper.join(1.0)
    assert sleeper.is_alive(), "the sleep ran while this chat's own push was still on the wire"

    let_go.set()
    pusher.join(10.0)
    sleeper.join(10.0)
    assert slept == [True]
    assert box.log[-1] == ("release", "box-7:chat-a")


def _walking_push_only() -> Any:
    def push(**_kwargs: Any) -> PushSummary:
        return PushSummary()

    return push


# -- the live sync a held folder runs ------------------------------------------


class ScriptedWatcher:
    """A watcher that stays open until the test lets it end.

    The real one puts an OS watch on a directory and never returns on its own,
    which is exactly the property the stop has to cope with: a sync that ends
    by itself would make "stopping flushes what is queued" pass for free.
    """

    def __init__(self) -> None:
        self.stop = threading.Event()
        self.started = threading.Event()
        self.ended = threading.Event()
        self.batches: list[set[tuple[Any, str]]] = []

    def arm(self, stop: threading.Event) -> ScriptedWatcher:
        """Take the stop handle custody hands the real watcher."""
        self.stop = stop
        return self

    async def changes(self) -> Any:
        async def stream() -> Any:
            self.started.set()
            for batch in self.batches:
                yield batch
            # A poll rather than an event wait: the stop handle custody hands
            # a watcher is a threading.Event (the real watcher is driven from a
            # thread), and awaiting one of those on this loop would block it.
            while not self.stop.is_set():  # noqa: ASYNC110
                await asyncio.sleep(0.01)
            self.ended.set()

        return stream()


def _live_folders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: LeaseServer,
    http: httpx.Client,
    **live: Any,
) -> tuple[ChatFolders, ScriptedWatcher]:
    """A box holding one chat's folder with a live sync on a scripted watcher."""
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    server.live = {"debounceMs": 10, "batchEveryMs": 10, "maxBatchEntries": 8, **live}
    watcher = ScriptedWatcher()
    folders = ChatFolders(
        chats_root=tmp_path / "box-7" / "chats",
        files=FakeFiles(),
        http=http,
        machine_id="box-7",
        home=tmp_path / "box-7" / "home",
        watcher_factory=lambda _root, _cadence, stop: watcher.arm(stop),
    )
    assert folders.take(CHAT, {}, instance="box-7:chat-a") is not None
    return folders, watcher


def test_a_folder_taken_without_the_live_plane_streams_nothing(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An older server that grants no live block leaves the chat exactly as it
    was: saved at the checkpoint push, with no watcher on its directory."""
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    server.live = {}
    folders = _folders(tmp_path, http)
    folders.take(CHAT, {}, instance="box-7:chat-a")
    working = folders.local_root(CHAT) / "scratch"
    working.mkdir(parents=True, exist_ok=True)

    assert folders.live(CHAT, working) is None
    held = folders.held(CHAT)
    assert held is not None and held.live is None


def test_the_live_sync_watches_the_working_directory_not_the_folder(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The records beside the working directory are the box's own, and the
    watched root is what keeps them out — not a filter that has to catch every
    rotation of them."""
    folders, _watcher = _live_folders(tmp_path, monkeypatch, server, http)
    working = folders.local_root(CHAT) / "scratch"
    working.mkdir(parents=True, exist_ok=True)

    sync = folders.live(CHAT, working)

    assert sync is not None
    assert sync.root == working
    assert working.parent == folders.local_root(CHAT)
    # A box holds many folders at once, so every line the sync logs has to name
    # which chat's it is.
    assert sync.chat_id == CHAT
    # Starting again is the same sync: two watchers on one directory would
    # each classify every write and upload it twice.
    assert folders.live(CHAT, working) is sync


def test_the_agent_s_tool_results_hear_of_conflicts_from_the_held_folder_s_sync(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool call in the working directory is told of a conflict by the sync
    the box holds for that folder — and by nothing once the sync has stopped."""
    from alkera_cli.plugins.plugin_base.delivery import conflict_notice_source

    folders, _watcher = _live_folders(tmp_path, monkeypatch, server, http)
    working = folders.local_root(CHAT) / "scratch"
    working.mkdir(parents=True, exist_ok=True)
    assert conflict_notice_source(working) is None

    sync = folders.live(CHAT, working)

    assert sync is not None
    assert conflict_notice_source(working) is sync
    folders.stop_live(CHAT)
    assert conflict_notice_source(working) is None


def test_the_sleep_stops_the_live_sync_before_it_pushes_and_releases(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The order is the promise: a batch still on the wire when the lease went
    back is refused by the fence, and those bytes are the only ones the push
    below has not already carried."""
    folders, watcher = _live_folders(tmp_path, monkeypatch, server, http)
    working = folders.local_root(CHAT) / "scratch"
    working.mkdir(parents=True, exist_ok=True)
    folders.live(CHAT, working)
    assert watcher.started.wait(5.0)
    order: list[str] = []

    def _push(**_kwargs: Any) -> PushSummary:
        order.append("push")
        assert watcher.ended.is_set(), "the push ran while the live sync was still streaming"
        return PushSummary()

    monkeypatch.setattr("alkera_cli.cloud.folder.unmount", _unmount_with(_push))

    released = folders.hand_back(CHAT)

    assert released is not None
    assert order == ["push"]
    assert watcher.ended.is_set()
    # And the release came after both: the folder is not handed to the next box
    # until this one's last bytes have left.
    assert server.log[-1] == "release"
    assert server.holder is None
    # The checkpoint push carried everything on the disk, so nothing is left
    # on the machine, and the release says so rather than saying nothing.
    assert server.released[-1]["unsyncedCount"] == 0


def test_a_release_drains_the_live_queue_and_names_what_did_not_land(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is pushed on a plain release, so a file the live plane could not
    land in its drain is still only on this machine — counted, and named by
    its path under the leased folder."""
    folders, watcher = _live_folders(tmp_path, monkeypatch, server, http, releaseDrainMs=200)
    working = folders.local_root(CHAT) / "scratch"
    working.mkdir(parents=True, exist_ok=True)
    (working / "report.md").write_bytes(b"q3")
    watcher.batches = [{(Change.added, str(working / "report.md"))}]

    def _refused(self: RestLiveApi, rel_path: str, node_id: str, size: int) -> None:
        raise httpx.ConnectError("the content store is unreachable")

    monkeypatch.setattr(RestLiveApi, "upload", _refused)
    assert folders.live(CHAT, working) is not None
    assert watcher.started.wait(5.0)

    assert folders.release(CHAT) is True

    assert server.released[-1]["unsyncedCount"] == 1
    assert server.released[-1]["unsyncedPaths"] == ["scratch/report.md"]


def test_a_path_the_live_sync_trashed_is_never_pushed_back_up(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The checkpoint push walks what is on disk. A name the trash has not
    caught up with would be uploaded again and undo the delete the box just
    made on the drive."""
    folders, _watcher = _live_folders(tmp_path, monkeypatch, server, http)
    working = folders.local_root(CHAT) / "scratch"
    working.mkdir(parents=True, exist_ok=True)
    sync = folders.live(CHAT, working)
    assert sync is not None
    sync.tombstones.update({"gone.txt", "old/"})
    skipped: list[Any] = []

    def _push(**kwargs: Any) -> PushSummary:
        skipped.append(kwargs.get("skip"))
        return PushSummary()

    monkeypatch.setattr("alkera_cli.cloud.folder._PUSH_LOCAL", _push)

    # Until its first sweep has offered what is on disk, the live plane is
    # about to send the whole working directory itself: one sender per file.
    assert folders.push(CHAT) is not None
    assert skipped == [("gone.txt", "old/", "scratch")]
    sync._swept = True
    assert folders.push(CHAT) is not None
    assert skipped[-1] == ("gone.txt", "old/")
    # A chat whose live sync trashed nothing names nothing: the push is
    # narrowed by what really went, never by a standing exclusion.
    sync.tombstones.clear()
    assert folders.push(CHAT) is not None
    assert skipped[-1] == ()


def test_a_checkpoint_push_is_fenced_on_what_the_box_last_agreed(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seen on a woken box: its push wrote a stale copy over a newer version a
    person saved, as a plain new version with no conflicted copy, because a
    push with no agreed base fences on the etag it just read. The checkpoint
    push now names, for every file the live sync watches, the bytes and etag
    this box last agreed with the drive, so the drive keeps the other version
    as a copy. The box's own records outside the watched directory carry no
    base: only a pull re-agrees them, and a stale one would copy the box's own
    work."""
    folders, _watcher = _live_folders(tmp_path, monkeypatch, server, http)
    root = folders.local_root(CHAT)
    working = root / "scratch"
    working.mkdir(parents=True, exist_ok=True)
    home = tmp_path / "box-7" / "home"
    for name, body in (("scratch/notes.txt", b"notes\n"), ("scratch/new.txt", b"n")):
        (root / name).write_bytes(body)
    (root / "chat.jsonl").write_bytes(b"{}\n" * 3)
    save_node_map(
        root,
        NodeMap(
            files={
                "scratch/notes.txt": KnownNode(
                    node_id="n-notes", size=6, content_hash="aa" * 32, etag="4"
                ),
                "scratch/new.txt": KnownNode(node_id="n-new", size=1),
                "chat.jsonl": KnownNode(node_id="n-chat", size=9, content_hash="bb" * 32, etag="7"),
            }
        ),
        home=home,
    )
    pushed: list[Any] = []

    def _push(**kwargs: Any) -> PushSummary:
        pushed.append(kwargs.get("bases"))
        return PushSummary()

    monkeypatch.setattr("alkera_cli.cloud.folder._PUSH_LOCAL", _push)

    # No live sync yet: its part of the map is not being kept current.
    assert folders.push(CHAT) is not None
    assert pushed[-1] == {}

    assert folders.live(CHAT, working) is not None
    (root / "scratch/notes.txt").write_bytes(b"notes, grown\n")
    assert folders.push(CHAT) is not None
    assert pushed[-1] == {
        "scratch/notes.txt": AgreedBase(etag="4", content_hash="aa" * 32),
    }


# -- the live plane is kept for the whole lease --------------------------------


def test_a_beat_on_the_wire_while_the_live_sync_starts_keeps_the_sync_on_the_handle(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The beat reads the handle, goes to the wire and writes the handle back;
    the live sync attaches itself to the handle in between. Whichever wrote
    last used to win, and when the beat did the sync was gone from the handle:
    its fence was never beaten again, it closed after two periods, and the box
    went deaf about the folder with nothing in the log."""
    from alkera_cli.files.mount import heartbeat as real_heartbeat

    folders, _watcher = _live_folders(tmp_path, monkeypatch, server, http)
    working = folders.local_root(CHAT) / "scratch"
    working.mkdir(parents=True, exist_ok=True)
    on_the_wire = threading.Event()
    let_go = threading.Event()

    def slow_heartbeat(**kwargs: Any) -> Any:
        on_the_wire.set()
        assert let_go.wait(10.0)
        return real_heartbeat(**kwargs)

    monkeypatch.setattr("alkera_cli.cloud.folder.heartbeat", slow_heartbeat)
    beaten: list[bool] = []
    beat = threading.Thread(target=lambda: beaten.append(folders.beat(CHAT)))
    beat.start()
    assert on_the_wire.wait(10.0)
    sync = folders.live(CHAT, working)
    assert sync is not None
    let_go.set()
    beat.join(10.0)

    assert beaten == [True]
    held = folders.held(CHAT)
    assert held is not None
    assert held.live is sync, "the beat's write dropped the live sync from the handle"
    assert held.record.epoch == server.epoch, "the sync's write dropped the beat's record"

    # And the next beat reaches the sync's fence: the silence it measures
    # starts over, which is the whole of what keeps the plane open.
    time.sleep(0.05)
    assert folders.beat(CHAT) is True
    assert sync.fence.silent_for < 0.05


def test_a_beat_that_lost_the_lease_stops_the_live_sync(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sync streaming under a lease that is gone has nothing left to send
    through — the fence refuses every batch — and left running it watches
    the directory for the life of the box, beside the sync the next take
    starts. The beat that learns the lease is gone ends it."""
    folders, watcher = _live_folders(tmp_path, monkeypatch, server, http)
    working = folders.local_root(CHAT) / "scratch"
    working.mkdir(parents=True, exist_ok=True)
    assert folders.live(CHAT, working) is not None
    assert watcher.started.wait(5.0)

    server.holder = "box-8:chat-a"  # the reaper handed it over

    assert folders.beat(CHAT) is False
    assert watcher.ended.wait(5.0), "the sync kept watching a folder this box had lost"


def test_a_fenced_beat_another_box_now_holds_stops_the_live_sync(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reap case as the real server serves it: the beat is fenced with
    nobody named and the re-take is refused, because another box acquired the
    folder in between. Nothing may be written under that lease again, so the
    streaming plane ends with the custody."""
    folders, watcher = _live_folders(tmp_path, monkeypatch, server, http)
    working = folders.local_root(CHAT) / "scratch"
    working.mkdir(parents=True, exist_ok=True)
    assert folders.live(CHAT, working) is not None
    assert watcher.started.wait(5.0)

    server.fence_beats = 1
    server.holder = "box-8:chat-a"

    assert folders.beat(CHAT) is False
    assert folders.held(CHAT) is None
    assert watcher.ended.wait(5.0), "the sync kept watching a folder this box had lost"


def test_a_landed_beat_tells_the_drive_the_plane_is_alive_when_nothing_else_has(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chat whose agent has written nothing for a while is not a holder that
    stopped syncing, and the beat is where it says so: an empty batch, which
    the drive stamps the lease as synced on."""
    monkeypatch.setattr("alkera_cli.files.live_sync.KEEPALIVE_EVERY", 0.0)
    folders, _watcher = _live_folders(tmp_path, monkeypatch, server, http)
    working = folders.local_root(CHAT) / "scratch"
    working.mkdir(parents=True, exist_ok=True)
    assert folders.live(CHAT, working) is not None
    assert server.live_batches == []

    assert folders.beat(CHAT) is True

    assert server.live_batches == [{"entries": []}]
    assert folders.held(CHAT) is not None


async def test_a_chat_the_boxs_sandbox_cannot_bound_is_refused_with_the_sentence(
    tmp_path: Path,
) -> None:
    """The box is set to gVisor but cannot run runsc: nothing runs, the folder
    goes back, and the row carries the sandbox sentence itself — not a
    "will try again" frame around it, since this box never will run it."""
    from alkera_cli.harness.adapter import HarnessSandboxRefusedError
    from alkera_cli.harness.sandbox import SandboxRefusedError, select_runtime

    try:
        select_runtime("gvisor", gvisor_ready=False)
        raise AssertionError("a gvisor box with no runsc must refuse")
    except SandboxRefusedError as exc:
        sentence = str(exc)
    assert "gVisor" in sentence
    folders = RecordingFolders()
    service, _built, _clock, rest = await _failing_service(
        tmp_path, folders, {CHAT: HarnessSandboxRefusedError(sentence)}
    )

    await service._ensure_mirror(CHAT, dict(ROW))

    assert service.mirrors == {}
    assert folders.held(CHAT) is None
    assert rest.reports == [(CHAT, "refused")]
    assert rest.reasons == [sentence]


def test_a_workspaces_checkpoint_push_is_fenced_on_what_the_box_last_agreed(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same as a chat's, for a workspace's shared tree: a woken box's push
    over a version a person saved since keeps that version as a conflicted
    copy. The push names the agreed base of every file under ``files/`` the
    live sync watches, and nothing under the chats' records."""
    workspace = "ws:8a7c1f2e-0000-4000-8000-000000000001"
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    server.live = {"debounceMs": 10, "batchEveryMs": 10, "maxBatchEntries": 8}
    watcher = ScriptedWatcher()
    home = tmp_path / "box-7" / "home"
    folders = ChatFolders(
        chats_root=tmp_path / "box-7" / "chats",
        files=FakeFiles(),
        http=http,
        machine_id="box-7",
        home=home,
        watcher_factory=lambda _root, _cadence, stop: watcher.arm(stop),
    )
    assert folders.take(workspace, {}, instance="box-7:ws") is not None
    root = folders.local_root(workspace)
    shared = root / "files"
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "plan.md").write_bytes(b"plan\n")
    save_node_map(
        root,
        NodeMap(
            files={
                "files/plan.md": KnownNode(
                    node_id="n-plan", size=5, content_hash="cc" * 32, etag="5"
                ),
            }
        ),
        home=home,
    )
    pushed: list[Any] = []

    def _push(**kwargs: Any) -> PushSummary:
        pushed.append((kwargs.get("bases"), kwargs.get("skip")))
        return PushSummary()

    monkeypatch.setattr("alkera_cli.cloud.custody_layout.PUSH_WORKSPACE", _push)

    assert folders.live(workspace, shared) is not None
    (shared / "plan.md").write_bytes(b"plan, the box's turn\n")
    assert folders.push(workspace) is not None

    bases, skip = pushed[-1]
    assert bases == {"files/plan.md": AgreedBase(etag="5", content_hash="cc" * 32)}
    assert ".chats" in skip
