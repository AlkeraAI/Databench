"""A box keeps every chat folder it holds in one call.

A box holds one lease per chat, and one beat per lease every fifteen seconds
was most of what an idle box asked the API — growing with every chat it ran.
The batched beat keeps them all at once, and every verdict it answers must be
acted on exactly as the per-lease beat's answer was: a renewal moves nothing, a
superseded lease is asked for again (and kept when the re-take is granted,
given up when it is refused), and a server that predates the batch is beaten
one lease at a time as before.

The wire is a real ``httpx`` client over a transport that serves the lease
routes per folder, and the mount chain that takes each folder is the real one.
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli.cloud.folder import ChatFolders

DRIVE = "11111111-1111-1111-1111-111111111111"
CHATS = {
    "chat-a": "22222222-2222-2222-2222-00000000000a",
    "chat-b": "22222222-2222-2222-2222-00000000000b",
    "chat-c": "22222222-2222-2222-2222-00000000000c",
}


class Files:
    def __init__(self) -> None:
        self.drive_reads = 0
        self.item_reads: list[tuple[str, str]] = []

    def drive(self) -> dict[str, Any]:
        self.drive_reads += 1
        return {"id": DRIVE}

    def item(self, drive_id: str, item_id: str, *, select: str | None = None) -> dict[str, Any]:
        self.item_reads.append((drive_id, item_id))
        return {
            "id": item_id,
            "etag": "7",
            "kind": "folder",
            "name": f"{item_id}.alkerachat",
            "pathBytes": f"/home/ana/Chats/{item_id}.alkerachat",
        }


class Leases:
    """The lease routes, one lease per folder.

    ``holders`` is who the server considers current for each folder; a beat
    from anyone else is fenced with nobody named, as the real route answers.
    ``batch_served`` False is a server older than the batched beat.
    """

    def __init__(self, *, batch_served: bool = True) -> None:
        self.batch_served = batch_served
        #: The live cadence a grant serves; empty grants no live plane.
        self.live: dict[str, Any] = {}
        self.holders: dict[str, str] = {}
        self.epochs: dict[str, int] = {}
        #: Folders another box has taken: an acquire from us is refused naming it.
        self.taken_by: dict[str, str] = {}
        self.requests: list[str] = []
        self.batch_calls: list[list[dict[str, Any]]] = []

    def _grant(self, node: str) -> dict[str, Any]:
        return {
            "epoch": self.epochs[node],
            "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "heartbeatEvery": 15.0,
            "syncInterval": 5.0,
            "forced": False,
            **({"live": self.live} if self.live else {}),
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(f"{request.method} {path}")
        body = json.loads(request.content) if request.content else {}
        if path.endswith("/leases/heartbeat"):
            if not self.batch_served:
                return httpx.Response(404, json={"detail": "Not Found"})
            self.batch_calls.append(body["leases"])
            verdicts = []
            for entry in body["leases"]:
                node = entry["nodeId"]
                if node not in self.epochs:
                    verdicts.append({"nodeId": node, "verdict": "gone", "grant": None})
                elif self.holders.get(node) != entry["instanceId"]:
                    verdicts.append({"nodeId": node, "verdict": "superseded", "grant": None})
                else:
                    verdicts.append(
                        {"nodeId": node, "verdict": "renewed", "grant": self._grant(node)}
                    )
            return httpx.Response(200, json={"leases": verdicts})
        node = path.split("/items/")[1].split("/")[0] if "/items/" in path else ""
        instance = body.get("instanceId")
        if path.endswith("/lease"):
            if node in self.taken_by:
                return httpx.Response(
                    409,
                    json={
                        "error": {
                            "code": "files.leased",
                            "message": "this folder is in use",
                            "holder": self.taken_by[node],
                        }
                    },
                )
            self.holders[node] = instance
            self.epochs[node] = self.epochs.get(node, 4) + 1
            return httpx.Response(200, json=self._grant(node))
        if path.endswith("/lease/heartbeat"):
            if self.holders.get(node) != instance:
                return httpx.Response(
                    409,
                    json={"error": {"code": "files.lease_fenced", "message": "fenced"}},
                )
            return httpx.Response(200, json=self._grant(node))
        if path.endswith("/lease/live") and request.method == "POST":
            return httpx.Response(200, json={"liveSeq": 0, "pending": 0})
        return httpx.Response(404, json={})

    def beats(self) -> list[str]:
        return [r for r in self.requests if r.endswith("heartbeat")]


def _pull_nothing(**_kwargs: Any) -> Any:
    from alkera_cli.files.pull import PullSummary

    return PullSummary()


@pytest.fixture(autouse=True)
def _real_mount_no_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real mount chain — lease, record, fence — with the tree walk skipped."""
    from alkera_cli.files import mount as mount_module

    def call(**kwargs: Any) -> Any:
        kwargs.pop("pull_tree", None)
        return mount_module.mount(pull_tree=_pull_nothing, **kwargs)

    monkeypatch.setattr("alkera_cli.cloud.folder.mount", call)


def _holding_three(tmp_path: Path, server: Leases) -> ChatFolders:
    folders = ChatFolders(
        chats_root=tmp_path / "chats",
        files=Files(),
        http=httpx.Client(transport=httpx.MockTransport(server), base_url="http://files.test"),
        machine_id="box-7",
        home=tmp_path / "home",
    )
    for chat, node in CHATS.items():
        assert folders.take(chat, {"id": chat, "files_node_id": node}, instance=f"box-7:{chat}")
    server.requests.clear()
    return folders


def test_three_held_folders_are_kept_in_one_call(tmp_path: Path) -> None:
    server = Leases()
    folders = _holding_three(tmp_path, server)

    kept = folders.beat_all(list(CHATS))

    assert kept == dict.fromkeys(CHATS, True)
    assert server.beats() == [f"POST /api/v1/files/drives/{DRIVE}/leases/heartbeat"]
    assert [e["nodeId"] for e in server.batch_calls[0]] == list(CHATS.values())
    assert all(folders.held(chat) is not None for chat in CHATS)


def test_a_superseded_lease_nobody_else_holds_is_taken_again_at_its_new_epoch(
    tmp_path: Path,
) -> None:
    """The per-lease beat's fence refusal names nobody, so the folder is asked
    for again; granted, the box keeps it and writes under the new epoch."""
    server = Leases()
    folders = _holding_three(tmp_path, server)
    before = folders.held("chat-b")
    assert before is not None
    server.holders[CHATS["chat-b"]] = "a-lapse-left-nobody"

    kept = folders.beat_all(list(CHATS))

    assert kept == dict.fromkeys(CHATS, True)
    after = folders.held("chat-b")
    assert after is not None and after.record.epoch > before.record.epoch
    assert f"POST /api/v1/files/drives/{DRIVE}/items/{CHATS['chat-b']}/lease" in server.requests


def test_a_superseded_lease_another_box_took_is_given_up_and_only_that_one(
    tmp_path: Path,
) -> None:
    server = Leases()
    folders = _holding_three(tmp_path, server)
    server.holders[CHATS["chat-b"]] = "box-8:chat-b"
    server.taken_by[CHATS["chat-b"]] = "ana@alkera.dev"

    kept = folders.beat_all(list(CHATS))

    assert kept == {"chat-a": True, "chat-b": False, "chat-c": True}
    assert folders.held("chat-b") is None
    assert folders.held("chat-a") is not None and folders.held("chat-c") is not None


def test_a_gone_verdict_is_read_as_the_per_lease_404_was_not_as_a_lost_lease(
    tmp_path: Path,
) -> None:
    """The per-lease beat's 404 never ended the chat: a beat that did not land
    is not a folder that moved on. The batched ``gone`` keeps that reading."""
    server = Leases()
    folders = _holding_three(tmp_path, server)
    del server.epochs[CHATS["chat-c"]]

    kept = folders.beat_all(list(CHATS))

    assert kept == dict.fromkeys(CHATS, True)
    assert folders.held("chat-c") is not None


def test_a_server_without_the_batch_is_asked_once_then_beaten_per_lease(tmp_path: Path) -> None:
    server = Leases(batch_served=False)
    folders = _holding_three(tmp_path, server)

    assert folders.beat_all(list(CHATS)) is None
    assert folders.beat_all(list(CHATS)) is None
    assert server.beats() == [f"POST /api/v1/files/drives/{DRIVE}/leases/heartbeat"], (
        "a server that refused the batch once is not asked again every pass"
    )
    assert all(folders.beat(chat) for chat in CHATS)


def test_a_batch_that_did_not_land_keeps_every_folder(tmp_path: Path) -> None:
    """A wire failure is a beat that did not land for each folder in the call —
    the TTL holds for several more, so nothing is given up on it."""
    server = Leases()
    folders = _holding_three(tmp_path, server)

    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable")

    folders._http = httpx.Client(transport=httpx.MockTransport(down), base_url="http://files.test")

    assert folders.beat_all(list(CHATS)) == dict.fromkeys(CHATS, True)
    assert all(folders.held(chat) is not None for chat in CHATS)


# ---------------------------------------------------------------------------
# The service's beat pass, over the same wire
# ---------------------------------------------------------------------------


def _service_holding_three(tmp_path: Path, server: Leases) -> tuple[Any, dict[str, Any]]:
    from _mirror_service import Clock, build_service

    folders = ChatFolders(
        chats_root=tmp_path / ".alkera" / "chats",
        files=Files(),
        http=httpx.Client(transport=httpx.MockTransport(server), base_url="http://files.test"),
        machine_id="box-7",
        home=tmp_path / "home",
    )
    return build_service(tmp_path, clock=Clock(), folders=folders)


async def test_the_beat_pass_keeps_three_chats_folders_in_one_request(tmp_path: Path) -> None:
    server = Leases()
    service, built = _service_holding_three(tmp_path, server)
    for chat, node in CHATS.items():
        await service._ensure_mirror(chat, {"id": chat, "files_node_id": node})
    server.requests.clear()

    await service._beat_folders()
    await service._beat_folders()

    assert server.beats() == [f"POST /api/v1/files/drives/{DRIVE}/leases/heartbeat"] * 2
    assert all(built[chat].state == "running" for chat in CHATS)


async def test_the_beat_pass_closes_only_the_chat_whose_folder_another_box_took(
    tmp_path: Path,
) -> None:
    server = Leases()
    service, built = _service_holding_three(tmp_path, server)
    for chat, node in CHATS.items():
        await service._ensure_mirror(chat, {"id": chat, "files_node_id": node})
    server.holders[CHATS["chat-b"]] = "box-8:chat-b"
    server.taken_by[CHATS["chat-b"]] = "ana@alkera.dev"

    await service._beat_folders()

    assert built["chat-b"].stopped
    assert set(service.mirrors) == {"chat-a", "chat-c"}
    assert not built["chat-a"].stopped and not built["chat-c"].stopped


async def test_the_beat_pass_beats_each_folder_against_a_server_without_the_batch(
    tmp_path: Path,
) -> None:
    server = Leases(batch_served=False)
    service, built = _service_holding_three(tmp_path, server)
    for chat, node in CHATS.items():
        await service._ensure_mirror(chat, {"id": chat, "files_node_id": node})
    server.requests.clear()

    await service._beat_folders()

    per_lease = sorted(r for r in server.beats() if "/items/" in r)
    assert per_lease == sorted(
        f"POST /api/v1/files/drives/{DRIVE}/items/{node}/lease/heartbeat" for node in CHATS.values()
    )
    assert all(built[chat].state == "running" for chat in CHATS)


def test_a_push_reads_the_folder_by_the_drive_its_lease_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where the folder is filed now is one item read, by the drive the lease
    is on — not a drive listing first to learn an id the lease already holds."""
    from alkera_cli.files.push import PushSummary

    server = Leases()
    files = Files()
    folders = ChatFolders(
        chats_root=tmp_path / "chats",
        files=files,
        http=httpx.Client(transport=httpx.MockTransport(server), base_url="http://files.test"),
        machine_id="box-7",
        home=tmp_path / "home",
    )
    assert folders.take("chat-a", {"id": "chat-a", "files_node_id": CHATS["chat-a"]}, instance="i")
    monkeypatch.setattr("alkera_cli.cloud.folder.export", lambda **_kwargs: PushSummary())
    files.drive_reads = 0
    files.item_reads.clear()

    assert folders.push("chat-a") is not None

    assert files.drive_reads == 0
    assert files.item_reads == [(DRIVE, CHATS["chat-a"])]


class IdleWatcher:
    """A watcher on a directory nobody writes: no changes until it is stopped."""

    def __init__(self, stop: threading.Event) -> None:
        self._stop = stop

    async def changes(self) -> Any:
        async def stream() -> Any:
            while not self._stop.is_set():  # noqa: ASYNC110
                await asyncio.sleep(0.01)
            if False:  # pragma: no cover - makes this an async generator
                yield set()

        return stream()


def test_the_batch_says_the_plane_runs_instead_of_a_keepalive_per_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A streaming folder's plane used to send an empty live batch every half
    minute just to be stamped synced. The batched beat carries that word for
    the folders whose plane runs, and no folder sends a keepalive of its own —
    even with the keepalive due on every beat."""
    monkeypatch.setattr("alkera_cli.files.live_sync.KEEPALIVE_EVERY", 0.0)
    server = Leases()
    server.live = {"debounceMs": 10, "batchEveryMs": 10, "maxBatchEntries": 8}
    folders = ChatFolders(
        chats_root=tmp_path / "chats",
        files=Files(),
        http=httpx.Client(transport=httpx.MockTransport(server), base_url="http://files.test"),
        machine_id="box-7",
        home=tmp_path / "home",
        watcher_factory=lambda _root, _cadence, stop: IdleWatcher(stop),
    )
    for chat, node in CHATS.items():
        assert folders.take(chat, {"id": chat, "files_node_id": node}, instance=f"box-7:{chat}")
    working = folders.local_root("chat-a") / "scratch"
    working.mkdir(parents=True, exist_ok=True)
    assert folders.live("chat-a", working) is not None
    try:
        server.requests.clear()

        assert folders.beat_all(list(CHATS)) == dict.fromkeys(CHATS, True)

        said = {entry["nodeId"]: entry["synced"] for entry in server.batch_calls[-1]}
        assert said == {
            CHATS["chat-a"]: True,
            CHATS["chat-b"]: False,
            CHATS["chat-c"]: False,
        }
        assert [r for r in server.requests if r.endswith("/lease/live")] == [], (
            "a folder sent a keepalive of its own beside the batched beat"
        )
    finally:
        folders.stop_live("chat-a", deadline=0.0)
