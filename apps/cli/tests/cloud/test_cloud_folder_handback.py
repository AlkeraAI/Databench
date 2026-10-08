"""Handing a chat's folder back when the drive has moved it, lost it, or says no.

The box takes a chat folder under a lease and gives it back when the chat
sleeps. Everything about *which* folder that is has to survive whatever a
person did to it while the box was holding it: a rename, a move to another
part of the drive, a title carrying a `?`, a trip to the trash. The lease is on
a node id, so the id — never the name — is what the hand-back addresses, and a
node the drive no longer has is a recovery, not a drop.

The wire is a real ``httpx`` client over a transport that answers as the lease
routes do, so "the push went up before the release" and "the lease is gone
afterwards" are both read off what the server was actually sent.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from _mirror_service import Clock, build_service, unreachable
from alkera_cli.cloud.folder import (
    ChatFolders,
    FolderHandBackError,
    ReleasedFolder,
    recovery_path,
    shown_in_home,
)
from alkera_cli.cloud.rest import CloudApiError, CloudRestClient
from alkera_cli.files.mount import load_record
from alkera_cli.files.push import PushSummary
from alkera_core.files.providers.registry import object_type_of
from alkera_core.models.files.tree import FileNode
from alkera_sdk.client import AlkeraHTTPError

DRIVE = "11111111-1111-1111-1111-111111111111"
NODE = "22222222-2222-2222-2222-222222222222"
HOME = "33333333-3333-3333-3333-333333333333"
CHAT = "chat-a"
HOME_PATH = "home/ana"
NODE_PATH = f"{HOME_PATH}/Chats/Kickoff.alkerachat"


# ---------------------------------------------------------------------------
# A Files backend, as far as the mount chain can tell one
# ---------------------------------------------------------------------------


class FakeFiles:
    """The ``client.files`` calls the mount chain and the recovery make."""

    def __init__(self, *, node_path: str = NODE_PATH) -> None:
        #: Where the chat's node is filed right now — a rename or a move
        #: changes it under the box's feet.
        self.node_path = node_path
        #: Whether the chat's node is in the trash.
        self.trashed = False
        #: Node ids the drive no longer has at all.
        self.purged: set[str] = set()
        #: Drive paths this backend really holds, without a leading slash. A
        #: lookup of anything else 404s, the way the server answers for a name
        #: nobody has taken yet.
        self.existing: set[str] = set()
        self.ids: list[str] = []
        self.paths: list[str] = []

    def drive(self) -> dict[str, Any]:
        return {"id": DRIVE, "rootId": "root-1", "homeId": HOME}

    def item(self, drive_id: str, item_id: str, *, select: str | None = None) -> dict[str, Any]:
        self.ids.append(item_id)
        if item_id in self.purged:
            raise AlkeraHTTPError(
                label="files/item",
                status=404,
                code="not_found",
                message="no such node",
                trace_id=None,
            )
        if item_id == HOME:
            return {"id": HOME, "etag": "1", "kind": "folder", "pathBytes": "/" + HOME_PATH}
        return {
            "id": item_id,
            "etag": "7",
            "kind": "folder",
            "trashed": self.trashed,
            "name": self.node_path.rpartition("/")[2],
            "pathBytes": "/" + self.node_path,
        }

    def item_by_path(self, drive_id: str, item_path: str) -> dict[str, Any]:
        self.paths.append(item_path)
        cleaned = item_path.strip("/")
        if cleaned not in self.existing:
            raise AlkeraHTTPError(
                label="files/item-by-path",
                status=404,
                code="not_found",
                message="no such node",
                trace_id=None,
            )
        return {"id": NODE, "etag": "7", "kind": "folder", "name": cleaned.rpartition("/")[2]}


class LeaseServer:
    """The lease routes, as far as the mount chain can tell them apart."""

    def __init__(self) -> None:
        self.holder: str | None = None
        self.epoch = 5
        self.log: list[str] = []
        self.released: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        target = request.url.path
        body = json.loads(request.content) if request.content else {}
        instance = body.get("instanceId") or request.headers.get("X-Alkera-Lease-Instance")
        if target.endswith("/lease"):
            self.holder = instance
            self.epoch += 1
            self.log.append("acquire")
            return httpx.Response(200, json=self._grant())
        if target.endswith("/lease/heartbeat"):
            return httpx.Response(200, json=self._grant())
        if target.endswith("/snapshots"):
            self.log.append("snapshot")
            return httpx.Response(200, json={})
        if target.endswith("/lease/release"):
            self.released.append(body)
            self.holder = None
            self.log.append("release")
            return httpx.Response(200, json={})
        if target.endswith("/leases"):
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})

    def _grant(self) -> dict[str, Any]:
        return {
            "epoch": self.epoch,
            "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "heartbeatEvery": 15.0,
            "syncInterval": 5.0,
            "forced": False,
        }


@pytest.fixture
def server() -> LeaseServer:
    return LeaseServer()


@pytest.fixture
def http(server: LeaseServer) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(server), base_url="http://files.test")


def _pull_nothing(**_kwargs: Any) -> Any:
    from alkera_cli.files.pull import PullSummary

    return PullSummary()


def _mount_with(pull_tree: Any) -> Any:
    from alkera_cli.files import mount as mount_module

    def call(**kwargs: Any) -> Any:
        kwargs.pop("pull_tree", None)
        return mount_module.mount(pull_tree=pull_tree, **kwargs)

    return call


def _unmount_with(push_tree: Any) -> Any:
    from alkera_cli.files import mount as mount_module

    def call(**kwargs: Any) -> Any:
        kwargs.pop("push_tree", None)
        return mount_module.unmount(push_tree=push_tree, **kwargs)

    return call


class Pushes:
    """A push that reads the real local tree and records where it was aimed."""

    def __init__(self) -> None:
        self.dests: list[str] = []
        self.files: list[list[str]] = []

    def __call__(self, *, root: Path, dest: str, **_kwargs: Any) -> PushSummary:
        self.dests.append(dest)
        summary = PushSummary()
        found: list[str] = []
        for path in sorted(Path(root).rglob("*")):
            if path.is_file():
                summary.uploaded += 1
                summary.bytes_uploaded += path.stat().st_size
                found.append(path.relative_to(root).as_posix())
        self.files.append(found)
        return summary


def _wired(monkeypatch: pytest.MonkeyPatch, pushes: Pushes | None = None) -> Pushes:
    """The real mount chain with only the tree walk replaced."""
    pushes = pushes or Pushes()
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    monkeypatch.setattr("alkera_cli.cloud.folder.unmount", _unmount_with(pushes))
    monkeypatch.setattr("alkera_cli.cloud.folder._PUSH_LOCAL", pushes)
    return pushes


def _folders(tmp_path: Path, http: httpx.Client, files: FakeFiles) -> ChatFolders:
    return ChatFolders(
        chats_root=tmp_path / "box" / "chats",
        files=files,
        http=http,
        machine_id="box-7",
        home=tmp_path / "box" / "home",
    )


def _taken(tmp_path: Path, http: httpx.Client, files: FakeFiles) -> tuple[ChatFolders, Path]:
    folders = _folders(tmp_path, http, files)
    held = folders.take(CHAT, {"id": CHAT, "files_node_id": NODE}, instance="box-7:chat-a")
    assert held is not None
    (held.root / "notes.md").write_text("the turn's work", encoding="utf-8")
    return folders, held.root


# ---------------------------------------------------------------------------
# The name moved; the id did not
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("moved_to", "what"),
    [
        pytest.param(f"{HOME_PATH}/Chats/Renamed.alkerachat", "renamed", id="renamed"),
        pytest.param(f"{HOME_PATH}/Archive/2026/Kickoff.alkerachat", "moved", id="moved-away"),
        pytest.param(
            f"{HOME_PATH}/Chats/What model are you? ✨.alkerachat",
            "renamed to a name a URL path mangles",
            id="question-mark-and-unicode",
        ),
        pytest.param(
            f"{HOME_PATH}/Chats/100%25 sure/Kickoff.alkerachat",
            "moved under a percent",
            id="percent-in-an-ancestor",
        ),
    ],
)
def test_a_folder_that_moved_while_the_box_held_it_is_handed_back_where_it_is_now(
    tmp_path: Path,
    http: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
    moved_to: str,
    what: str,
) -> None:
    """The push follows the node, not the name it had when the lease was taken.

    Every one of these is a chat somebody touched while a box was running it.
    A push aimed at the recorded name would raise a stray folder at the old
    place — or, for the `?`, address a path that ends before the name does.
    """
    files = FakeFiles()
    pushes = _wired(monkeypatch)
    folders, _root = _taken(tmp_path, http, files)

    files.node_path = moved_to
    released = folders.hand_back(CHAT)

    assert released is not None, f"a chat {what} must still hand its folder back"
    assert released.recovered is False
    assert pushes.dests == [moved_to]
    assert pushes.files == [["notes.md"]]
    assert released.org_path == moved_to


def test_the_release_precondition_is_read_off_the_node_not_off_the_name(
    tmp_path: Path, http: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The etag the release is fenced with comes from the id form.

    Read by path it would be the etag of whatever is filed under that name at
    that instant — after a rename, some other folder's, or a 404.
    """
    files = FakeFiles()
    _wired(monkeypatch)
    folders, _root = _taken(tmp_path, http, files)
    files.paths.clear()

    folders.hand_back(CHAT)

    assert files.paths == [], "no part of the hand-back may address the folder by name"
    assert NODE in files.ids


# ---------------------------------------------------------------------------
# The folder is gone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "gone",
    [pytest.param("trashed", id="trashed"), pytest.param("purged", id="purged")],
)
def test_a_folder_that_is_gone_lands_the_work_in_the_owners_recovery_folder(
    tmp_path: Path,
    http: httpx.Client,
    server: LeaseServer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    gone: str,
) -> None:
    """A folder the drive no longer holds is never a reason to drop the work.

    Neither is it a reason to retry forever: the tree goes somewhere a person
    can find it, the lease goes back, and the box says where it put it.
    """
    files = FakeFiles()
    pushes = _wired(monkeypatch)
    folders, root = _taken(tmp_path, http, files)
    if gone == "trashed":
        files.trashed = True
    else:
        files.purged.add(NODE)

    with caplog.at_level(logging.WARNING, logger="alkera_cli.cloud.folder"):
        released = folders.hand_back(CHAT)

    assert isinstance(released, ReleasedFolder)
    assert released.recovered is True
    expected = recovery_path(HOME_PATH, "Kickoff.alkerachat")
    assert pushes.dests == [expected]
    assert pushes.files == [["notes.md"]], "the whole tree goes, not a token file"
    assert released.org_path == expected
    # What the owner is told names their home, never the id it is stored under.
    assert released.shown_path == "Home" + expected[len(HOME_PATH) :]
    assert HOME_PATH not in released.shown_path
    assert any(expected in record.getMessage() for record in caplog.records)
    # The lease does not stay held on a folder nobody can write to again, and
    # the record goes with it so the next take is a fresh mount.
    assert folders.held(CHAT) is None
    assert load_record(root, home=tmp_path / "box" / "home") is None


def test_a_trashed_folder_gives_its_lease_back_rather_than_waiting_for_the_ttl(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = FakeFiles()
    _wired(monkeypatch)
    folders, _root = _taken(tmp_path, http, files)
    files.trashed = True

    folders.hand_back(CHAT)

    assert server.log[-1] == "release"
    assert server.holder is None


# ---------------------------------------------------------------------------
# The folder is gone because the CHAT is gone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "gone",
    [pytest.param("trashed", id="trashed"), pytest.param("purged", id="purged")],
)
def test_a_folder_gone_with_its_chat_is_not_landed_anywhere(
    tmp_path: Path,
    http: httpx.Client,
    server: LeaseServer,
    monkeypatch: pytest.MonkeyPatch,
    gone: str,
) -> None:
    """Deleting a chat is what trashed its folder, so a copy is not a rescue.

    Landing the working tree in the recovery folder would put the files of a
    chat somebody has just deleted straight back into the drive, under a name
    the delete never touched — a twin of the folder that is meant to be in the
    trash. Nothing goes up; only the lease comes back.

    What the two cases do with the tree on this disk differs, and the
    difference is what the drive said. The trash is the delete's own signature,
    so a trashed folder's copy is wiped with the lease. A folder the drive
    answers NOTHING for is not confirmed gone: that is the same 404 a box gets
    for a chat that has since been bound to another machine and whose lease
    has lapsed — a live chat whose last turn exists on this disk alone — so
    the lease record goes and the tree stays.
    """
    files = FakeFiles()
    pushes = _wired(monkeypatch)
    folders, root = _taken(tmp_path, http, files)
    if gone == "trashed":
        files.trashed = True
    else:
        files.purged.add(NODE)

    released = folders.hand_back(CHAT, recover=False)

    assert isinstance(released, ReleasedFolder)
    assert released.recovered is False
    assert pushes.dests == [], "a deleted chat's work is never uploaded again"
    assert folders.held(CHAT) is None
    assert load_record(root, home=tmp_path / "box" / "home") is None
    if gone == "trashed":
        assert released.discarded is True and released.kept is False
        # A trashed node's lease goes back now rather than in however long
        # the TTL is, and nothing of the deleted chat stays on the box.
        assert server.log[-1] == "release"
        assert server.holder is None
        assert not root.exists(), "a confirmed delete leaves nothing of the chat on the box"
    else:
        assert released.kept is True and released.discarded is False
        assert (root / "notes.md").read_text(encoding="utf-8") == "the turn's work", (
            "a not-found never costs the only copy of the last turn's writes"
        )


def test_a_chat_that_is_gone_still_pushes_to_a_folder_that_is_not(
    tmp_path: Path, http: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the gone-folder branch changes.

    A chat this box may no longer read can still have a live folder — it was
    unbound, or moved out of the box's audience — and that folder is where the
    last turn's work belongs. The work is left behind only when there is
    nowhere live to put it AND no chat to open it with.
    """
    files = FakeFiles()
    pushes = _wired(monkeypatch)
    folders, _root = _taken(tmp_path, http, files)

    released = folders.hand_back(CHAT, recover=False)

    assert released is not None and released.discarded is False
    assert pushes.dests == [NODE_PATH]
    assert pushes.files == [["notes.md"]]


class MovedChatFiles(FakeFiles):
    """The drive as it answers a box whose chat has been bound to another
    machine: the folder is still there, and the box may see it only under the
    lease it still holds — which the server reads off the fence headers on the
    client the request goes out on. Unfenced, the box is a stranger to the
    folder and is told it does not exist, exactly as a purged node is.

    ``moved`` is flipped once the box holds the folder: the take that came
    before it was the bound box's, and was admitted without a fence.
    """

    def __init__(self, *, node_path: str = NODE_PATH) -> None:
        super().__init__(node_path=node_path)
        self.moved = False
        #: Whether each look at the folder came fenced, in order.
        self.reads: list[bool] = []

    def on_client(self, http: httpx.Client) -> Any:
        return _OnClient(self, fenced="X-Alkera-Lease-Epoch" in http.headers)

    def item(self, drive_id: str, item_id: str, *, select: str | None = None) -> dict[str, Any]:
        return self.read(drive_id, item_id, fenced=False, select=select)

    def read(
        self, drive_id: str, item_id: str, *, fenced: bool, select: str | None = None
    ) -> dict[str, Any]:
        self.reads.append(fenced)
        if self.moved and not fenced:
            raise AlkeraHTTPError(
                label="files/item",
                status=404,
                code="not_found",
                message="no such node",
                trace_id=None,
            )
        return super().item(drive_id, item_id, select=select)


class _OnClient:
    """``MovedChatFiles`` re-bound to one client: reads carry that client's fence."""

    def __init__(self, files: MovedChatFiles, *, fenced: bool) -> None:
        self._files = files
        self._fenced = fenced

    def __getattr__(self, name: str) -> Any:
        return getattr(self._files, name)

    def item(self, drive_id: str, item_id: str, *, select: str | None = None) -> dict[str, Any]:
        return self._files.read(drive_id, item_id, fenced=self._fenced, select=select)


def test_a_chat_that_moved_off_this_box_is_handed_back_under_its_fence(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Placement moved the chat to another box while this one held its folder.

    The folder is where it was; only the binding changed. Once the binding is
    gone the lease is the one thing that still admits this box, and the server
    reads it off the fence — so the hand-back's look at the folder must go out
    fenced like its writes do. Unfenced, the answer was a 404 the box read as a
    purged folder, and the last turn's work was never pushed.
    """
    files = MovedChatFiles()
    pushes = _wired(monkeypatch)
    folders, root = _taken(tmp_path, http, files)
    files.moved = True

    released = folders.hand_back(CHAT, recover=False)

    assert released is not None
    assert released.discarded is False and released.kept is False and released.recovered is False
    assert pushes.dests == [NODE_PATH], "the last turn's work landed in the chat's own folder"
    assert pushes.files == [["notes.md"]]
    assert server.log[-1] == "release" and server.holder is None
    assert not root.exists(), "pushed and released, nothing of the chat stays on the box"
    assert files.reads[-1] is True, "the look at the folder went out under the fence"


class DeletedChatRest(CloudRestClient):
    """A backend that answers for a chat which has been deleted.

    A tombstone is hidden from every read, so the box's re-read of the chat the
    doorbell named comes back 404 — the same answer a chat in another org
    gives, and the same answer a box gets for a chat that has since been bound
    to another machine.
    """

    def __init__(self) -> None:
        super().__init__(api_url="http://127.0.0.1:1", token="t", agent_id="machine:x")
        self.reports: list[tuple[str, str, str]] = []

    def for_agent(self, agent_id: str) -> CloudRestClient:
        return self

    async def get_chat(self, chat_id: str) -> dict[str, Any]:
        raise CloudApiError(
            404, {"detail": "Not found"}, method="GET", path=f"/api/v1/chats/{chat_id}"
        )

    async def report_publisher_state(
        self,
        chat_id: str,
        *,
        state: str,
        reason: str = "",
        refusal_kind: str | None = None,
        ending: str | None = None,
    ) -> dict[str, Any]:
        self.reports.append((chat_id, state, reason))
        return {}


@pytest.mark.asyncio
async def test_deleting_a_chat_leaves_no_second_copy_of_its_folder(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole path, from the doorbell a delete rings to the drive.

    A deleted chat reads back as a 404 and its folder is trashed in the same
    transaction. The box that was running it must take that as "this chat is
    over", not as "somebody threw my folder away": the one thing it may not do
    is re-upload the tree beside the folder the delete just trashed.
    """
    files = FakeFiles()
    pushes = _wired(monkeypatch)
    folders, _root = _taken(tmp_path, http, files)
    files.trashed = True
    rest = DeletedChatRest()
    service, _built = build_service(tmp_path / "svc", clock=Clock(), folders=folders, rest=rest)

    await service.reconcile_chat(CHAT)

    assert pushes.dests == [], "the delete must not raise a twin of the trashed folder"
    assert server.log[-1] == "release"
    assert folders.held(CHAT) is None


@pytest.mark.asyncio
async def test_a_chat_moved_off_this_box_lands_its_last_turn_in_its_folder(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole path, from the doorbell a move rings to the drive.

    The box hears the chat changed, re-reads it and is told 404 — the same word
    a deletion gets. What separates the two is not the chat route but the
    folder: a deleted chat's folder is in the trash, a moved chat's is where it
    was. So the box hands back as it always does, under the lease it still
    holds — every file it wrote lands in the chat's folder for the box that has
    the chat now, and the lease goes back so that box can take it — instead of
    discarding the tree as a purged chat's.
    """
    files = MovedChatFiles()
    pushes = _wired(monkeypatch)
    folders, root = _taken(tmp_path, http, files)
    files.moved = True
    rest = DeletedChatRest()
    service, _built = build_service(tmp_path / "svc", clock=Clock(), folders=folders, rest=rest)

    await service.reconcile_chat(CHAT)

    assert pushes.dests == [NODE_PATH], "the move must not cost the chat its last turn's files"
    assert pushes.files == [["notes.md"]]
    assert server.log[-1] == "release" and server.holder is None
    assert folders.held(CHAT) is None
    assert not root.exists()


# ---------------------------------------------------------------------------
# What the recovered folder is called
# ---------------------------------------------------------------------------


def _opens_as(name: str) -> str | None:
    """What the drive makes of an object folder by this name.

    An object node's type is read off the extension its name ENDS in — the
    portal labels, filters and opens by that answer — so a recovered folder
    that has lost the extension can never be read back as a chat, whatever is
    bound to it afterwards. This is that rule, asked the way the portal asks it.
    """
    return object_type_of(
        FileNode(id=uuid.uuid4(), name=name.encode("utf-8"), target_object_id=uuid.uuid4())
    )


class LandingPushes(Pushes):
    """A push that leaves the folder it landed behind in the drive.

    Which is what makes a second recovery meet the first one's name instead of
    a drive that has never heard of it.
    """

    def __init__(self, files: FakeFiles) -> None:
        super().__init__()
        self._files = files

    def __call__(self, *, root: Path, dest: str, **kwargs: Any) -> PushSummary:
        summary = super().__call__(root=root, dest=dest, **kwargs)
        self._files.existing.add(dest.strip("/"))
        return summary


@pytest.mark.parametrize(
    ("held_as", "stem"),
    [
        pytest.param("Kickoff.alkerachat", "Kickoff", id="a-chat-folder"),
        pytest.param(
            "What model are you? ✨.alkerachat",
            "What model are you? ✨",
            id="a-title-a-url-mangles",
        ),
        pytest.param(CHAT, CHAT, id="the-path-convention-carries-no-suffix"),
    ],
)
def test_a_recovered_folder_still_opens_as_a_chat(
    tmp_path: Path,
    http: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
    held_as: str,
    stem: str,
) -> None:
    """The day stamp goes inside the name, never after the extension.

    `Kickoff.alkerachat 2026-09-18` is a folder the drive no longer reads as a
    chat at all — the extension is not the last thing in the name any more — so
    the work would land where a person can see it and nowhere they can open it.
    A folder the convention named, which carries no extension to begin with,
    gains one for the same reason.
    """
    files = FakeFiles(node_path=f"{HOME_PATH}/Chats/{held_as}")
    pushes = _wired(monkeypatch)
    folders, _root = _taken(tmp_path, http, files)
    files.trashed = True

    released = folders.hand_back(CHAT)

    assert released is not None and released.recovered is True
    assert pushes.dests == [released.org_path]
    name = released.org_path.rpartition("/")[2]
    assert _opens_as(name) == "chat", f"the drive reads {name!r} as {_opens_as(name)!r}"
    assert name.count(".alkerachat") == 1, "the extension is never spelled twice"
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert name == f"{stem} (recovered {today}).alkerachat"
    assert released.org_path.startswith(f"{HOME_PATH}/Chats/Recovered/")


def test_a_second_recovery_on_the_same_day_gets_a_name_of_its_own(
    tmp_path: Path, http: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two recoveries of one chat are two folders, both of them chats.

    The day alone does not separate them, and landing the second tree on top of
    the first would overwrite a file the first recovery saved — so the counter
    goes inside the name too, in front of the extension.
    """
    files = FakeFiles()
    pushes = _wired(monkeypatch, LandingPushes(files))
    folders, _root = _taken(tmp_path, http, files)
    files.trashed = True
    first = folders.hand_back(CHAT)

    folders, _root = _taken(tmp_path, http, files)
    second = folders.hand_back(CHAT)

    assert first is not None and second is not None
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert first.org_path.rpartition("/")[2] == f"Kickoff (recovered {today}).alkerachat"
    assert second.org_path.rpartition("/")[2] == f"Kickoff (recovered {today}) 2.alkerachat"
    assert _opens_as(second.org_path.rpartition("/")[2]) == "chat"
    assert pushes.dests == [first.org_path, second.org_path], "neither landed on the other"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param("Kickoff.alkerachat", "Kickoff (recovered 2026-09-18).alkerachat", id="chat"),
        pytest.param("Kickoff", "Kickoff (recovered 2026-09-18).alkerachat", id="no-extension"),
        pytest.param(
            "a/b.alkerachat", "a-b (recovered 2026-09-18).alkerachat", id="a-slash-is-not-a-parent"
        ),
        pytest.param(
            ".alkerachat", "chat (recovered 2026-09-18).alkerachat", id="nothing-but-an-extension"
        ),
        pytest.param("   ", "chat (recovered 2026-09-18).alkerachat", id="no-name-at-all"),
    ],
)
def test_the_stamp_lands_inside_the_name_whatever_the_folder_was_called(
    name: str, expected: str
) -> None:
    """One rule for every name a chat folder can arrive with."""
    landed = recovery_path(HOME_PATH, name, when=datetime(2026, 9, 18, 3, 0, tzinfo=UTC))

    assert landed == f"{HOME_PATH}/Chats/Recovered/{expected}"
    assert _opens_as(landed.rpartition("/")[2]) == "chat"


# ---------------------------------------------------------------------------
# A hand-back the server refuses
# ---------------------------------------------------------------------------


def _refusing(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    def refuse(**_kwargs: Any) -> Any:
        raise exc

    monkeypatch.setattr("alkera_cli.cloud.folder.unmount", refuse)


def test_a_refusal_the_server_spelled_out_stops_after_a_bounded_number_of_passes(
    tmp_path: Path, http: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 403 will be a 403 on the next pass too.

    So it is retried a few times and then given up on — with the count and the
    reason on the exception, which is what puts them in front of a person
    instead of only in this box's log.
    """
    files = FakeFiles()
    _wired(monkeypatch)
    folders, _root = _taken(tmp_path, http, files)
    _refusing(
        monkeypatch,
        AlkeraHTTPError(
            label="files/push", status=403, code="forbidden", message="no", trace_id=None
        ),
    )

    seen: list[FolderHandBackError] = []
    for _ in range(3):
        with pytest.raises(FolderHandBackError) as refused:
            folders.hand_back(CHAT)
        seen.append(refused.value)
        assert folders.owed() == ([] if refused.value.exhausted else [CHAT])

    assert [(r.attempts, r.exhausted, r.transient) for r in seen] == [
        (1, False, False),
        (2, False, False),
        (3, True, False),
    ]
    assert "403" in seen[2].reason
    # Once it has given up it neither holds the folder nor claims it owes one,
    # so a fourth pass has nothing to try and raises nothing at all.
    assert folders.held(CHAT) is None
    assert folders.hand_back(CHAT) is None


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(httpx.ConnectError("[Errno 111] Connection refused"), id="connection-refused"),
        pytest.param(httpx.ReadTimeout("too slow"), id="timeout"),
        pytest.param(
            AlkeraHTTPError(
                label="files/push", status=503, code="unavailable", message="", trace_id=None
            ),
            id="503",
        ),
    ],
)
def test_a_transient_failure_keeps_the_folder_and_waits_for_another_pass(
    tmp_path: Path, http: httpx.Client, monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> None:
    """The API restarting is not something the box runs out of patience with.

    A re-provision is exactly this, and giving up on it would strand the work
    on a machine that was about to be able to hand it back.
    """
    files = FakeFiles()
    _wired(monkeypatch)
    folders, _root = _taken(tmp_path, http, files)
    _refusing(monkeypatch, exc)

    for _ in range(5):
        with pytest.raises(FolderHandBackError) as refused:
            folders.hand_back(CHAT)
        assert refused.value.transient is True
        assert refused.value.exhausted is False

    assert folders.owed() == [CHAT], "a folder waiting on the API is still owed"
    assert folders.held(CHAT) is not None


def test_a_refused_hand_back_that_later_succeeds_clears_the_debt(
    tmp_path: Path, http: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = FakeFiles()
    pushes = Pushes()
    _wired(monkeypatch, pushes)
    folders, _root = _taken(tmp_path, http, files)
    _refusing(monkeypatch, httpx.ConnectError("[Errno 111] Connection refused"))

    with pytest.raises(FolderHandBackError):
        folders.hand_back(CHAT)
    assert folders.owed() == [CHAT]

    monkeypatch.setattr("alkera_cli.cloud.folder.unmount", _unmount_with(pushes))
    released = folders.hand_back(CHAT)

    assert released is not None
    assert pushes.files == [["notes.md"]]
    assert folders.owed() == []


# ---------------------------------------------------------------------------
# The URL a name becomes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("What model are you?.alkerachat", id="question-mark"),
        pytest.param("50% off#2.alkerachat", id="percent-and-hash"),
        pytest.param("Ünïcødé ✨.alkerachat", id="unicode"),
    ],
)
def test_a_path_lookup_asks_about_the_whole_name(name: str) -> None:
    """A name is percent-encoded before it becomes a URL.

    Unencoded, `What model are you?.alkerachat` is a request whose path stops
    at `are you` and whose query is `.alkerachat` — the server is asked about a
    folder that does not exist, and answers about one nobody meant.
    """
    from alkera_sdk.client import AlkeraClient

    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": NODE, "etag": "1"})

    client = AlkeraClient(base_url="http://files.test", token="t")
    client.raw_client.get_httpx_client()._transport = httpx.MockTransport(record)

    client.files.item_by_path(DRIVE, f"{HOME_PATH}/Chats/{name}")

    assert len(seen) == 1
    assert seen[0].url.query == b"", "the name must not spill into the query string"
    assert seen[0].url.path.endswith(f"/Chats/{name}")


# ---------------------------------------------------------------------------
# What the reader of the chat is told
# ---------------------------------------------------------------------------


class RecordingRest(CloudRestClient):
    """The one route the box uses to put a verdict on a chat."""

    def __init__(self) -> None:
        # Every route but the one below is refused in process: a closed loopback
        # port costs two seconds per attempt on the Windows runners.
        super().__init__(
            api_url="http://127.0.0.1:1",
            token="t",
            agent_id="machine:x",
            transport=httpx.MockTransport(unreachable),
        )
        self.reports: list[tuple[str, str, str]] = []

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
        self.reports.append((chat_id, state, reason))
        return {}


@pytest.mark.asyncio
async def test_a_hand_back_the_box_gave_up_on_says_why_on_the_chat(
    tmp_path: Path, http: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A warning in the box's log reaches nobody.

    The reader of the chat is the person whose files did not arrive, so the
    reason goes on the chat's refusal channel — once, when the box has stopped
    trying, and never while it is still waiting out an unreachable API.
    """
    files = FakeFiles()
    _wired(monkeypatch)
    folders, _root = _taken(tmp_path, http, files)
    rest = RecordingRest()
    service, _built = build_service(tmp_path / "svc", clock=Clock(), folders=folders, rest=rest)
    _refusing(
        monkeypatch,
        AlkeraHTTPError(
            label="files/push", status=403, code="forbidden", message="no", trace_id=None
        ),
    )

    for _ in range(2):
        await service._returns.hand_back(CHAT)
    assert rest.reports == [], "a box still retrying has nothing to announce"

    await service._returns.hand_back(CHAT)

    assert [(chat, state) for chat, state, _ in rest.reports] == [(CHAT, "refused")]
    assert "403" in rest.reports[0][2]


@pytest.mark.asyncio
async def test_a_folder_owed_from_a_failed_sleep_is_tried_again_on_the_next_pass(
    tmp_path: Path, http: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mirror is gone by then, so nothing else would ever look at it.

    Without this pass the work of a chat whose sleep met a restarting API sits
    on the box until it is re-provisioned — which is the day it is lost.
    """
    files = FakeFiles()
    pushes = Pushes()
    _wired(monkeypatch, pushes)
    folders, _root = _taken(tmp_path, http, files)
    service, _built = build_service(
        tmp_path / "svc", clock=Clock(), folders=folders, rest=RecordingRest()
    )
    _refusing(monkeypatch, httpx.ConnectError("[Errno 111] Connection refused"))

    await service._returns.hand_back(CHAT)
    assert pushes.dests == [], "nothing went up while the API was refusing"

    monkeypatch.setattr("alkera_cli.cloud.folder.unmount", _unmount_with(pushes))
    # The upkeep pass, not the heartbeat one: this retry pushes, and a push is
    # never allowed to sit between two folders' beats.
    await service._upkeep_folders()

    assert pushes.dests == [NODE_PATH]
    assert pushes.files == [["notes.md"]]
    assert folders.owed() == []


# ---------------------------------------------------------------------------
# The checkpoint push, when the folder went to the trash under it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "gone",
    [pytest.param("trashed", id="trashed"), pytest.param("purged", id="purged")],
)
def test_the_checkpoint_push_does_not_aim_at_a_folder_that_is_no_longer_in_the_drive(
    tmp_path: Path,
    http: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    gone: str,
) -> None:
    """A folder in the trash takes the beat's files nowhere, so none are sent.

    A create whose parent is trashed cannot be reached by path afterwards, so a
    push aimed there lands new versions on whatever still has a node and then
    raises on the first folder it has to make. The live box did exactly that
    every thirty seconds — ``'…/.alkerachat/.runtime/agent' is not there and
    making it left nothing at that path`` — while the chat's work sat on its
    disk. The hand-back is what lands the tree, in the owner's recovery folder.
    """
    files = FakeFiles()
    pushes = _wired(monkeypatch)
    folders, _root = _taken(tmp_path, http, files)
    assert folders.push(CHAT) is not None, "a folder that IS there is pushed"
    assert pushes.dests == [NODE_PATH]

    if gone == "trashed":
        files.trashed = True
    else:
        files.purged.add(NODE)

    with caplog.at_level(logging.WARNING, logger="alkera_cli.cloud.folder"):
        assert folders.push(CHAT) is None
        assert folders.push(CHAT) is None

    assert pushes.dests == [NODE_PATH], "the beat sent files into a folder nobody can reach"
    said = [record.getMessage() for record in caplog.records if gone in record.getMessage()]
    assert len(said) == 1, "the beat says it once, not on every tick"
    # The lease is still this box's, so the hand-back can still land the work.
    assert folders.held(CHAT) is not None


def test_a_folder_restored_from_the_trash_is_pushed_into_again(
    tmp_path: Path, http: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skipping the push is a verdict on the drive now, not a latch."""
    files = FakeFiles()
    pushes = _wired(monkeypatch)
    folders, _root = _taken(tmp_path, http, files)
    files.trashed = True
    assert folders.push(CHAT) is None

    files.trashed = False

    assert folders.push(CHAT) is not None
    assert pushes.dests == [NODE_PATH]


# ---------------------------------------------------------------------------
# The agent's database travels as one file
# ---------------------------------------------------------------------------


def test_the_hand_back_folds_the_agents_write_ahead_log_before_the_push(
    tmp_path: Path, http: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent keeps its session in a WAL-mode database under the folder. Its
    log holds everything since the last checkpoint and never travels; what the
    push carries is the main file, so the hand-back folds the log into it first
    — a copy of that one file, opened on the next box, is the whole session."""
    import sqlite3

    from alkera_cli.harness.opencode_db import AGENT_DB_RELATIVE

    class SnapshottingPushes(Pushes):
        """The push as the drive sees it: the main file's bytes at that moment
        (the hand-back removes the working copy once the folder is back)."""

        def __init__(self) -> None:
            super().__init__()
            self.agent_db: bytes | None = None

        def __call__(self, *, root: Path, dest: str, **kwargs: Any) -> PushSummary:
            db = Path(root) / AGENT_DB_RELATIVE
            if db.is_file():
                self.agent_db = db.read_bytes()
            return super().__call__(root=root, dest=dest, **kwargs)

    files = FakeFiles()
    pushes = _wired(monkeypatch, SnapshottingPushes())
    assert isinstance(pushes, SnapshottingPushes)
    folders, root = _taken(tmp_path, http, files)
    db = root / AGENT_DB_RELATIVE
    db.parent.mkdir(parents=True)
    writer = sqlite3.connect(db, isolation_level=None)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE session (id TEXT)")
    writer.execute("INSERT INTO session VALUES ('ses_pinned')")
    # The writer stays open through the hand-back, as a killed agent's does:
    # nothing has checkpointed the log by the time the folder goes back.

    released = folders.hand_back(CHAT)

    assert released is not None and pushes.dests, "the folder was not pushed"
    pushed = pushes.files[-1]
    assert ".runtime/agent/agent.db" in pushed, pushed
    assert pushes.agent_db is not None, "the push met no agent database"
    elsewhere = tmp_path / "next-box"
    elsewhere.mkdir()
    (elsewhere / "agent.db").write_bytes(pushes.agent_db)
    reader = sqlite3.connect(elsewhere / "agent.db")
    try:
        assert reader.execute("SELECT id FROM session").fetchall() == [("ses_pinned",)], (
            "the main file alone did not carry the session: the log was not folded"
        )
    finally:
        reader.close()
        writer.close()


# ---------------------------------------------------------------------------
# The lease is kept while it is handed back, and never past the release
# ---------------------------------------------------------------------------


class BeatingLeaseServer(LeaseServer):
    """Grants a short beat and logs every heartbeat beside the other routes."""

    def __init__(self, every: float, *, release_takes: float = 0.0) -> None:
        super().__init__()
        self.every = every
        #: How long the release is held open once the server has taken it —
        #: the window a beat still in flight, or still to come, lands in.
        self.release_takes = release_takes

    def __call__(self, request: httpx.Request) -> httpx.Response:
        import time

        if request.url.path.endswith("/lease/heartbeat"):
            self.log.append("heartbeat")
        answer = super().__call__(request)
        if request.url.path.endswith("/lease/release") and self.release_takes:
            time.sleep(self.release_takes)
        return answer

    def beats_after_release(self) -> int:
        if "release" not in self.log:
            return 0
        return self.log[self.log.index("release") :].count("heartbeat")

    def _grant(self) -> dict[str, Any]:
        return {**super()._grant(), "heartbeatEvery": self.every}

    def beats(self) -> int:
        return self.log.count("heartbeat")


class PushThatNeedsTheLease(Pushes):
    """A push that runs until the lease has been beaten under it, so the push
    outlives the lease's TTL."""

    def __init__(self, server: BeatingLeaseServer) -> None:
        super().__init__()
        self._server = server
        self.beaten_under_push = False

    def __call__(self, *, root: Path, dest: str, **kwargs: Any) -> PushSummary:
        import time

        before = self._server.beats()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and self._server.beats() <= before:
            time.sleep(0.01)
        self.beaten_under_push = self._server.beats() > before
        return super().__call__(root=root, dest=dest, **kwargs)


@pytest.mark.parametrize(
    "release_takes",
    [
        pytest.param(0.0, id="a-quick-release"),
        # Held open for several beat cadences: any beat the keeper still sends
        # once the release has reached the server lands inside it.
        pytest.param(0.3, id="a-slow-release"),
    ],
)
def test_the_lease_is_beaten_while_its_folder_is_pushed_back_and_never_after_the_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, release_takes: float
) -> None:
    """A sleeping chat has left the box's mirrors, so the beat pass no longer
    keeps its lease — and its hand-back drains the live queue (which sends only
    while a beat has landed) and pushes the tree (under the lease). Unbeaten,
    the drain waited out its window with every change withheld and the push
    ran on under a lapsed lease. The hand-back keeps the lease itself, from
    before the drain until the push returns, and stops before the release."""
    import time

    server = BeatingLeaseServer(every=0.05, release_takes=release_takes)
    http = httpx.Client(transport=httpx.MockTransport(server), base_url="http://files.test")
    pushes = PushThatNeedsTheLease(server)
    _wired(monkeypatch, pushes)
    folders, _root = _taken(tmp_path, http, FakeFiles())

    released = folders.hand_back(CHAT)
    # Long enough for a keeper left running to have beaten several times more.
    time.sleep(0.2)

    assert released is not None
    assert pushes.beaten_under_push, "nothing kept the lease while the push ran"
    assert server.beats_after_release() == 0, "a beat landed after the lease was given back"
    assert server.log[-1] == "release"
    assert server.holder is None


def test_a_beat_during_the_hand_back_reopens_the_fence_the_drain_sends_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live sync is stopped (off the run table) before it drains, and its
    fence had closed for want of a beat. The hand-back's beat must reach it
    there, or the drain can send nothing — the deadlock the box sat in."""
    from types import SimpleNamespace

    from alkera_cli.files.mount import SelfFence

    server = BeatingLeaseServer(every=15.0)
    http = httpx.Client(transport=httpx.MockTransport(server), base_url="http://files.test")
    _wired(monkeypatch)
    folders, _root = _taken(tmp_path, http, FakeFiles())
    held = folders.held(CHAT)
    assert held is not None
    now = [0.0]
    fence = SelfFence(grace=60.0, monotonic=lambda: now[0])
    now[0] = 74.0
    assert fence.expired()
    folders._stopping_live[CHAT] = SimpleNamespace(sync=SimpleNamespace(fence=fence))  # type: ignore[assignment]

    assert folders._hand_back_beat(CHAT, held) is True

    assert server.beats() == 1
    assert not fence.expired(), "the draining sync's fence stayed shut after a beat landed"


@pytest.mark.parametrize(
    ("org_path", "home_path", "shown"),
    [
        pytest.param("home/u-1/Chats/x", "home/u-1", "Home/Chats/x", id="inside-the-home"),
        pytest.param("/home/u-1/Chats/x", "/home/u-1", "Home/Chats/x", id="leading-slashes"),
        pytest.param("home/u-1", "home/u-1", "Home", id="the-home-itself"),
        pytest.param("home/u-10/Chats", "home/u-1", "home/u-10/Chats", id="a-sibling-prefix"),
        pytest.param("Shared/x", "home/u-1", "Shared/x", id="outside-the-home"),
        pytest.param("home/u-1/x", "", "home/u-1/x", id="no-home-known"),
    ],
)
def test_a_path_is_shown_under_home_only_when_it_is_inside_it(
    org_path: str, home_path: str, shown: str
) -> None:
    assert shown_in_home(org_path, home_path) == shown


def test_a_folder_the_backend_said_was_deleted_leaves_nothing_on_the_box(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The not-found alone is kept (a box that lost its sight of a live chat is
    told the same). The backend's own word that the chat was deleted is what
    turns it into the delete: nothing goes up and nothing stays behind."""
    files = FakeFiles()
    pushes = _wired(monkeypatch)
    folders, root = _taken(tmp_path, http, files)
    files.purged.add(NODE)

    released = folders.hand_back(CHAT, recover=False, gone=True)

    assert released is not None and released.discarded is True and released.kept is False
    assert pushes.dests == []
    assert folders.held(CHAT) is None
    assert not root.exists(), "a deleted chat's tree is not kept on the box"


def test_a_workspaces_shared_tree_is_never_landed_for_a_person(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chat's work is landed in its owner's recovery folder when its folder
    vanished under it. A workspace's shared tree belongs to no one person, so
    it is never landed there, whatever the caller asked."""
    files = FakeFiles()
    pushes = _wired(monkeypatch)
    folders = _folders(tmp_path, http, files)
    key = "ws:8a7c1f2e-0000-4000-8000-000000000001"
    held = folders.take(key, {"id": key, "files_node_id": NODE}, instance="box-7:ws")
    assert held is not None
    (held.root / "files").mkdir(parents=True, exist_ok=True)
    (held.root / "files" / "shared.md").write_text("ours", encoding="utf-8")
    files.purged.add(NODE)

    released = folders.hand_back(key)

    assert released is not None and released.recovered is False
    assert pushes.dests == [], "nothing was landed in anybody's recovery folder"
    assert released.kept is True


@pytest.mark.parametrize(
    ("gone", "kept"),
    [
        pytest.param(False, True, id="trashed-by-a-person"),
        pytest.param(True, False, id="the-workspace-was-deleted"),
    ],
)
def test_a_trashed_workspace_keeps_the_boxs_tree_unless_the_backend_said_it_was_deleted(
    tmp_path: Path,
    http: httpx.Client,
    server: LeaseServer,
    monkeypatch: pytest.MonkeyPatch,
    gone: bool,
    kept: bool,
) -> None:
    """Somebody trashed a folder above a workspace this box holds, and the box
    meets it in the trash with work it had not pushed (the trash waits for the
    box's flush, but a box that did not answer in time still holds some). A
    chat's trashed folder is its deletion; a workspace's is not, so the tree
    stays on the box, nothing is pushed into the trash, and a restore followed
    by the next take sends it. Only the backend's word that the workspace is
    deleted discards it."""
    files = FakeFiles()
    pushes = _wired(monkeypatch)
    folders = _folders(tmp_path, http, files)
    key = "ws:8a7c1f2e-0000-4000-8000-000000000001"
    held = folders.take(key, {"id": key, "files_node_id": NODE}, instance="box-7:ws")
    assert held is not None
    (held.root / "files").mkdir(parents=True, exist_ok=True)
    (held.root / "files" / "shared.md").write_text("the box's unsent work", encoding="utf-8")
    files.trashed = True

    released = folders.hand_back(key, recover=False, gone=gone)

    assert released is not None and released.kept is kept
    assert released.discarded is (not kept)
    assert pushes.dests == [], "nothing is written into a trashed folder"
    if kept:
        assert (held.root / "files" / "shared.md").read_text(encoding="utf-8") == (
            "the box's unsent work"
        )
    else:
        assert not held.root.exists()
