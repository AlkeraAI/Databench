"""A hand-back that did not land, and a chat that woke before the retry.

The sleep pushes a folder and releases its lease. When the push meets an API
that is restarting, the folder stays held and the hand-back is owed: the next
upkeep pass tries again. A chat (or a workspace's member) that wakes before
that retry runs in the very tree the retry would push, release and wipe, so
waking it takes the debt back, and the retry leaves it alone.

Driven through the real service, the real folder custody, the real mount
chain and a real tree on disk, over a lease server that answers as the lease
routes do: what was released is read off what the server was sent, and what
survived is read off the disk.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from _mirror_service import Clock, build_service, unreachable
from alkera_cli.cloud.folder import ChatFolders
from alkera_cli.cloud.rest import CloudRestClient
from alkera_cli.cloud.workspace_seat import workspace_key
from alkera_cli.files import mount as mount_module
from alkera_cli.files.pull import PullSummary
from alkera_cli.files.push import PushSummary

DRIVE = "11111111-1111-4111-8111-111111111111"
WS = "8a7c1f2e-0000-4000-8000-000000000001"
WS_KEY = workspace_key(WS)
WS_NODE = "8a7c1f2e-0000-4000-8000-0000000000aa"
FILES_NODE = "8a7c1f2e-0000-4000-8000-0000000000ab"
SOLO_NODE = "8a7c1f2e-0000-4000-8000-0000000000cc"


class FakeFiles:
    """The ``client.files`` reads the mount chain makes: every node is where
    the lease says it is, and nothing is in the trash."""

    def drive(self) -> dict[str, Any]:
        return {"id": DRIVE, "rootId": "root-1", "homeId": "home-1"}

    def item(self, drive_id: str, item_id: str, *, select: str | None = None) -> dict[str, Any]:
        return {
            "id": item_id,
            "etag": "7",
            "kind": "folder",
            "trashed": False,
            "name": item_id,
            "pathBytes": f"/org/{item_id}",
        }


class LeaseServer:
    """The lease routes, recording which node each acquire and release named."""

    def __init__(self) -> None:
        self.epoch = 5
        self.log: list[tuple[str, str]] = []
        #: The streaming cadence a grant carries; empty serves no live plane.
        self.live: dict[str, Any] = {}
        self._lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        target = request.url.path
        node = target.split("/items/")[-1].split("/")[0] if "/items/" in target else ""
        with self._lock:
            if target.endswith("/lease"):
                self.epoch += 1
                self.log.append(("acquire", node))
                return httpx.Response(200, json=self._grant())
            if target.endswith("/lease/heartbeat"):
                return httpx.Response(200, json=self._grant())
            if target.endswith("/lease/release"):
                self.log.append(("release", node))
                return httpx.Response(200, json={})
        if target.endswith("/leases"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={})

    def released(self, node: str) -> int:
        return self.log.count(("release", node))

    def _grant(self) -> dict[str, Any]:
        return {
            "epoch": self.epoch,
            "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "heartbeatEvery": 15.0,
            "syncInterval": 5.0,
            "forced": False,
            **({"live": self.live} if self.live else {}),
        }


class Pushes:
    """The checkpoint push, reading the real tree it is pointed at."""

    def __init__(self) -> None:
        self.files: list[list[str]] = []

    def __call__(self, *, root: Path, dest: str, **_kwargs: Any) -> PushSummary:
        found = sorted(
            path.relative_to(root).as_posix() for path in Path(root).rglob("*") if path.is_file()
        )
        self.files.append(found)
        return PushSummary(uploaded=len(found))


class Flaky:
    """The unmount, refused by a restarting API for the first ``failing``
    hand-backs of the folder under ``root``."""

    def __init__(self, pushes: Pushes, root: Path, *, failing: int = 1) -> None:
        self.pushes = pushes
        self.root = root
        self.failing = failing

    def __call__(self, **kwargs: Any) -> Any:
        if Path(kwargs["root"]) == self.root and self.failing:
            self.failing -= 1
            raise httpx.ConnectError("[Errno 111] Connection refused")
        kwargs.pop("push_tree", None)
        return mount_module.unmount(push_tree=self.pushes, **kwargs)


class RecordingRest(CloudRestClient):
    def __init__(self) -> None:
        super().__init__(
            api_url="http://127.0.0.1:1",
            token="t",
            agent_id="machine:x",
            transport=httpx.MockTransport(unreachable),
        )

    def for_agent(self, agent_id: str) -> CloudRestClient:
        return self

    async def report_publisher_state(self, chat_id: str, **_: Any) -> dict[str, Any]:
        return {}


def _pull_nothing(**_kwargs: Any) -> PullSummary:
    return PullSummary()


def _mount(**kwargs: Any) -> Any:
    kwargs.pop("pull_tree", None)
    return mount_module.mount(pull_tree=_pull_nothing, **kwargs)


@pytest.fixture
def server() -> LeaseServer:
    return LeaseServer()


@pytest.fixture
def folders(tmp_path: Path, server: LeaseServer, monkeypatch: pytest.MonkeyPatch) -> ChatFolders:
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount)
    http = httpx.Client(transport=httpx.MockTransport(server), base_url="http://files.test")
    return ChatFolders(
        chats_root=tmp_path / "box" / ".alkera" / "chats",
        files=FakeFiles(),
        http=http,
        machine_id="box-7",
        home=tmp_path / "box" / "home",
    )


def _member(chat_id: str) -> dict[str, Any]:
    return {
        "id": chat_id,
        "workspace_id": WS,
        "workspace_layout": "native",
        "workspace_node_id": WS_NODE,
        "workspace_files_node_id": FILES_NODE,
        "files_node_id": str(uuid.uuid5(uuid.NAMESPACE_URL, chat_id)),
        "files_drive_id": DRIVE,
    }


def _flaky(monkeypatch: pytest.MonkeyPatch, folders: ChatFolders, key: str) -> tuple[Pushes, Flaky]:
    pushes = Pushes()
    flaky = Flaky(pushes, folders.local_root(key))
    monkeypatch.setattr("alkera_cli.cloud.folder.unmount", flaky)
    monkeypatch.setattr("alkera_cli.cloud.folder._PUSH_LOCAL", pushes)
    return pushes, flaky


# -- the folder custody ------------------------------------------------------------


def test_taking_an_owed_folder_again_takes_back_the_debt(
    folders: ChatFolders, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = {"id": "solo", "files_node_id": SOLO_NODE, "files_drive_id": DRIVE}
    held = folders.take("solo", row, instance="box-7:solo")
    assert held is not None
    _flaky(monkeypatch, folders, "solo")
    with pytest.raises(Exception, match="not handed back"):
        folders.hand_back("solo")
    assert folders.owed() == ["solo"]

    again = folders.take("solo", row, instance="box-7:solo")

    assert again is folders.held("solo") and again.record.epoch == held.record.epoch
    assert server.log.count(("acquire", SOLO_NODE)) == 1, "still held: nothing is taken anew"
    assert folders.owed() == []
    # A retry that raced the take finds nothing owed and leaves the folder alone.
    assert folders.hand_back("solo", if_owed=True) is None
    assert folders.held("solo") is again
    assert server.released(SOLO_NODE) == 0


class IdleWatcher:
    """A watcher on a directory nobody writes: no changes until it is stopped."""

    def __init__(self, stop: threading.Event) -> None:
        self._stop = stop

    async def changes(self) -> Any:
        async def stream() -> Any:
            await asyncio.to_thread(self._stop.wait)
            if False:  # pragma: no cover - makes this an async generator
                yield set()

        return stream()


def test_a_folder_taken_back_from_an_owed_hand_back_streams_again(
    tmp_path: Path, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failed hand-back stopped the folder's live sync before its push.
    The chat that takes the folder back writes into it again, so a watcher
    runs on its directory again; left stopped, every write waited for the
    next checkpoint push."""
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount)
    server.live = {"debounceMs": 10, "batchEveryMs": 10, "maxBatchEntries": 8}
    armed: list[Path] = []

    def watcher(root: Path, _cadence: Any, stop: threading.Event) -> IdleWatcher:
        armed.append(root)
        return IdleWatcher(stop)

    folders = ChatFolders(
        chats_root=tmp_path / "box" / ".alkera" / "chats",
        files=FakeFiles(),
        http=httpx.Client(transport=httpx.MockTransport(server), base_url="http://files.test"),
        machine_id="box-7",
        home=tmp_path / "box" / "home",
        watcher_factory=watcher,
    )
    row = {"id": "solo", "files_node_id": SOLO_NODE, "files_drive_id": DRIVE}
    assert folders.take("solo", row, instance="box-7:solo") is not None
    working = folders.local_root("solo") / "scratch"
    working.mkdir(parents=True)
    try:
        assert folders.live("solo", working) is not None
        _flaky(monkeypatch, folders, "solo")
        with pytest.raises(Exception, match="not handed back"):
            folders.hand_back("solo")

        folders.take("solo", row, instance="box-7:solo")
        assert folders.live("solo", working) is not None

        assert armed == [working, working], "the directory is watched again"
    finally:
        folders.stop_live("solo", deadline=0.0)


# -- the service, end to end -------------------------------------------------------


async def test_a_member_woken_before_the_retry_keeps_its_workspace_and_its_tree(
    tmp_path: Path,
    folders: ChatFolders,
    server: LeaseServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last member sleeps and the workspace's hand-back meets a restarting
    API. Another member wakes before the API is back. The upkeep pass that
    follows must not push, release and wipe the shared tree that member now
    runs in; the workspace goes back when that member sleeps, with both
    members' work."""
    pushes, flaky = _flaky(monkeypatch, folders, WS_KEY)
    service, built = build_service(
        tmp_path / "svc", clock=Clock(), folders=folders, rest=RecordingRest()
    )
    shared = folders.local_root(WS_KEY) / "files"

    await service._ensure_mirror("chat-a", _member("chat-a"))
    (shared / "before.md").write_text("chat a's turn", encoding="utf-8")
    await service._release_mirror("chat-a", ending="idle")
    assert flaky.failing == 0, "the workspace's first hand-back was refused"
    assert server.released(WS_NODE) == 0
    assert folders.owed() == [WS_KEY]

    await service._ensure_mirror("chat-b", _member("chat-b"))
    assert built["chat-b"].state == "running"
    (shared / "after.md").write_text("chat b's turn", encoding="utf-8")
    await service._upkeep_folders()

    assert server.released(WS_NODE) == 0, "the lease of a workspace in use was released"
    assert sorted(p.name for p in shared.iterdir()) == ["after.md", "before.md"]
    assert service._workspaces.members(WS_KEY) == {"chat-b"}
    assert WS_KEY in service._workspaces.keys(), "the workspace's lease is still beaten"
    assert folders.owed() == []

    await service._release_mirror("chat-b", ending="idle")

    assert server.released(WS_NODE) == 1
    assert ["files/after.md", "files/before.md"] in pushes.files
    assert not shared.exists(), "a workspace handed back leaves nothing on the box"


async def test_a_chat_woken_before_the_retry_keeps_its_folder_and_its_tree(
    tmp_path: Path,
    folders: ChatFolders,
    server: LeaseServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same for a chat on its own: its sleep's hand-back is owed, a
    message wakes it, and the retry must not take the folder from under it."""
    _pushes, flaky = _flaky(monkeypatch, folders, "solo")
    service, built = build_service(
        tmp_path / "svc", clock=Clock(), folders=folders, rest=RecordingRest()
    )
    row = {"id": "solo", "files_node_id": SOLO_NODE, "files_drive_id": DRIVE}
    root = folders.local_root("solo")

    await service._ensure_mirror("solo", row)
    (root / "before.md").write_text("the first turn", encoding="utf-8")
    await service._release_mirror("solo", ending="idle")
    assert flaky.failing == 0 and folders.owed() == ["solo"]

    await service._ensure_mirror("solo", {**row, "last_seq": 3})
    assert built["solo"].state == "running"
    (root / "after.md").write_text("the second turn", encoding="utf-8")
    await service._upkeep_folders()

    assert server.released(SOLO_NODE) == 0
    assert (root / "before.md").exists() and (root / "after.md").exists()
    assert folders.owed() == []
    assert folders.held("solo") is not None


async def test_an_owed_workspace_nobody_woke_is_handed_back_by_the_upkeep(
    tmp_path: Path,
    folders: ChatFolders,
    server: LeaseServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The debt is still paid when nobody wakes: the upkeep pass hands it
    back once the API answers, with what was written before the sleep."""
    pushes, _flaky_unmount = _flaky(monkeypatch, folders, WS_KEY)
    service, _built = build_service(
        tmp_path / "svc", clock=Clock(), folders=folders, rest=RecordingRest()
    )
    shared = folders.local_root(WS_KEY) / "files"
    await service._ensure_mirror("chat-a", _member("chat-a"))
    (shared / "work.md").write_text("the turn", encoding="utf-8")
    await service._release_mirror("chat-a", ending="idle")
    assert server.released(WS_NODE) == 0

    await service._upkeep_folders()

    assert server.released(WS_NODE) == 1
    assert ["files/work.md"] in pushes.files
    assert service._workspaces.keys() == []
    assert folders.owed() == []
    assert not shared.exists()
