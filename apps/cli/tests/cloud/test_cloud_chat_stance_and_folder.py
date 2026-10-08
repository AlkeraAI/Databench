"""What a cloud chat comes back as after it has slept: its stance, and its folder.

Two facts about a chat live on the chat's own record rather than on the box, and
both are read at the moment a box opens the chat — a fresh one or a resume.

The **stance** is the permission mode a reader chose. It is durable on purpose:
the box that hears the mode relay is not necessarily the box that resumes the
chat an hour later, so a stance that lived only in the running mirror would
silently snap back to read-only every time the chat slept, and the browser would
go on showing the mode the reader picked.

The **folder** is the Files node the chat IS. A path is only a name for it, and
the node filed under a name is whatever is filed there at this instant — so a
box that leased by name would, after a rename or a restore, take a lease on and
pull down somebody else's folder. The id is the identity; the path is the
fallback for a chat the drive does not hold.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from _mirror_service import Clock, build_service
from alkera_cli.cloud.folder import ChatFolders, chat_folder_node
from alkera_cli.files.mount import fence_headers, load_record

DRIVE = "11111111-1111-1111-1111-111111111111"
#: The node the chat record names — the folder that IS this chat.
CHAT_NODE = "22222222-2222-2222-2222-222222222222"
#: The node filed under the conventional path. A different folder entirely: the
#: chat was renamed, or a second chat took the name, or a restore put one back.
IMPOSTOR_NODE = "33333333-3333-3333-3333-333333333333"
CHAT = "chat-a"


# ---------------------------------------------------------------------------
# The stance a chat comes back in
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param("default", id="a reader who let the agent act keeps that"),
        pytest.param("plan", id="a reader who put it in plan keeps that"),
        pytest.param("read_only", id="the floor is honoured as a choice too"),
        pytest.param("bypass", id="a reader who turned the prompts off keeps that"),
    ],
)
def test_a_chat_opens_in_the_stance_its_own_record_stores(tmp_path: Path, stored: str) -> None:
    """The stance survives a sleep because it is read off the chat, not kept in
    the mirror that slept. A box that opened every chat at the floor would undo
    the reader's choice on every resume, and the browser — which reads the same
    record — would keep claiming the mode still held."""
    service, _built = build_service(tmp_path, clock=Clock())
    chat = {"title": "Ops", "owner_user_id": "u-1", "permission_mode": stored}

    mirror = service._default_mirror(CHAT, chat)

    assert mirror.permission_mode == stored


@pytest.mark.parametrize(
    "chat",
    [
        pytest.param({}, id="a chat from before the stance existed"),
        pytest.param({"permission_mode": None}, id="no stance at all"),
        pytest.param({"permission_mode": "Default"}, id="a near-miss is not a match"),
        pytest.param({"permission_mode": "accept_edits"}, id="a stance no harness runs"),
        pytest.param({"permission_mode": 7}, id="not a word at all"),
    ],
)
def test_a_stance_the_box_does_not_run_cloud_chats_in_opens_at_the_floor(
    tmp_path: Path, chat: dict[str, Any]
) -> None:
    """Reading the record must not become a way to infer a stance from a word
    the box cannot run. Anything that is not one of the stances reads as
    read-only — permission nobody granted is never guessed at."""
    service, _built = build_service(tmp_path, clock=Clock())

    mirror = service._default_mirror(CHAT, {"owner_user_id": "u-1", **chat})

    assert mirror.permission_mode == "read_only"


# ---------------------------------------------------------------------------
# The folder a chat comes back to
# ---------------------------------------------------------------------------


class TwoFolders:
    """A drive where the chat's node and the conventional path disagree.

    ``item_by_path`` answers with the impostor — whatever is filed under the
    name today — and ``item`` answers with the node the chat record names. A
    mount that resolves the name has no way to notice; a mount handed the id
    cannot get it wrong.
    """

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.ids: list[str] = []

    def drive(self) -> dict[str, Any]:
        return {"id": DRIVE}

    def item(self, drive_id: str, item_id: str, *, select: str | None = None) -> dict[str, Any]:
        self.ids.append(item_id)
        return {"id": item_id, "etag": "7", "kind": "folder", "name": "the chat"}

    def item_by_path(self, drive_id: str, item_path: str) -> dict[str, Any]:
        self.paths.append(item_path)
        return {"id": IMPOSTOR_NODE, "etag": "9", "kind": "folder", "name": item_path}


class LeaseServer:
    """The lease routes, recording which node each acquire was for."""

    def __init__(self) -> None:
        self.holder: str | None = None
        self.epoch = 5
        self.leased_nodes: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        target = request.url.path
        body = json.loads(request.content) if request.content else {}
        if target.endswith("/lease"):
            self.holder = body.get("instanceId")
            self.epoch += 1
            # .../drives/<drive>/items/<node>/lease
            self.leased_nodes.append(target.rsplit("/", 2)[-2])
            return httpx.Response(
                200,
                json={
                    "epoch": self.epoch,
                    "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                    "heartbeatEvery": 15.0,
                    "syncInterval": 5.0,
                    "forced": False,
                },
            )
        return httpx.Response(404, json={})


@pytest.fixture
def server() -> LeaseServer:
    return LeaseServer()


@pytest.fixture
def http(server: LeaseServer) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(server), base_url="http://files.test")


def _recording_pull(seen: list[dict[str, Any]]) -> Any:
    """A tree walk that only records what it was asked to copy down.

    It also records the fence its client carried *while it ran*, which is the
    only moment it can be seen: the mount puts the epoch and instance headers
    on the client for the pull and takes them straight back off afterwards, so
    a test that looked at the client after the take would find nothing either
    way.
    """

    def pull(**kwargs: Any) -> Any:
        from alkera_cli.files.pull import PullSummary

        http = kwargs.get("http")
        carried = httpx.Headers(http.headers) if http is not None else httpx.Headers()
        seen.append({**kwargs, "fence": carried})
        return PullSummary()

    return pull


def _mount_with(pull_tree: Any, displaced: list[Any]) -> Any:
    """The real ``mount`` with only the tree walk replaced — the lease, the
    node resolution, the record and the fence headers all still happen.

    The walk the caller passed is put in ``displaced`` rather than dropped:
    *what a chat folder pulls with* is part of the contract too (a box's hold
    on the folder — its lock files — never travels), so it stays assertable
    after the take even though this stand-in is what actually ran.
    """
    from alkera_cli.files import mount as mount_module

    def call(**kwargs: Any) -> Any:
        displaced.append(kwargs.get("pull_tree"))
        return mount_module.mount(**{**kwargs, "pull_tree": pull_tree})

    return call


def _assert_pulled_without_local_state(displaced: list[Any]) -> None:
    """The take names its own tree walk, and that walk declines local state.

    The lock file a box writes about its own hold on the folder means nothing
    on any other box — and one stamped with a dead host can never be reclaimed
    here — so the pull a take runs is the one that leaves them on the server.
    """
    assert len(displaced) == 1
    walk = displaced[0]
    assert walk is not None, "the take must hand the mount the walk it wants, not take the default"
    assert getattr(walk, "keywords", {}).get("skip_local_state") is True


def _assert_pulled_under_the_fence(pulled: dict[str, Any], root: Path, home: Path) -> None:
    """The pull ran as the holder of the lease the take had just been granted.

    A chat's folder may not leave the platform, and the server tells the holder
    from a stranger by these two headers — so a pull that read unfenced would
    be told, on the folder it was just granted, that there is nothing here it
    may download.
    """
    record = load_record(root, home=home)
    assert record is not None
    wanted = fence_headers(record.epoch, record.instance_id)
    assert {name: pulled["fence"].get(name) for name in wanted} == wanted


def _folders(tmp_path: Path, http: httpx.Client, files: TwoFolders) -> ChatFolders:
    return ChatFolders(
        chats_root=tmp_path / "chats",
        files=files,
        http=http,
        machine_id="box-7",
        home=tmp_path / "home",
    )


@pytest.mark.parametrize(
    ("chat", "expected"),
    [
        pytest.param({"files_node_id": CHAT_NODE}, CHAT_NODE, id="snake-case"),
        pytest.param({"filesNodeId": CHAT_NODE}, CHAT_NODE, id="camel-case"),
        pytest.param({}, None, id="a chat the drive does not hold"),
        pytest.param({"files_node_id": "  "}, None, id="blank is not an id"),
        pytest.param({"files_node_id": 7}, None, id="not a string is not an id"),
    ],
)
def test_the_node_a_chat_record_names_is_read_off_either_spelling(
    chat: dict[str, Any], expected: str | None
) -> None:
    assert chat_folder_node(chat) == expected


def test_a_chat_that_names_its_node_leases_that_node_not_whatever_holds_the_name(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: the conventional path resolves to a DIFFERENT folder
    here, so a box that resolved the name would lease a stranger's node, pull
    their bytes onto this box and — on the next sleep — push this chat's work
    over them. The lease the server received names the chat's own node, the
    pull was asked for that node, and the path was never resolved at all."""
    seen: list[dict[str, Any]] = []
    displaced: list[Any] = []
    files = TwoFolders()
    monkeypatch.setattr(
        "alkera_cli.cloud.folder.mount", _mount_with(_recording_pull(seen), displaced)
    )
    folders = _folders(tmp_path, http, files)

    held = folders.take(CHAT, {"files_node_id": CHAT_NODE}, instance="box-7:chat-a")

    assert held is not None
    assert server.leased_nodes == [CHAT_NODE]
    assert files.ids and set(files.ids) == {CHAT_NODE}
    assert files.paths == []
    assert seen[0]["node_id"] == CHAT_NODE
    record = load_record(held.root, home=tmp_path / "home")
    assert record is not None
    assert record.node_id == CHAT_NODE
    # The take IS the pull, and it runs the way a chat folder is pulled: under
    # the lease just granted, leaving this box's own lock files behind.
    _assert_pulled_without_local_state(displaced)
    _assert_pulled_under_the_fence(seen[0], held.root, tmp_path / "home")


def test_a_chat_with_no_node_on_its_record_still_takes_the_path_route(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Files off, or a chat older than the drive: the name is all there is, and
    a chat served off its conventional folder beats a chat not served."""
    seen: list[dict[str, Any]] = []
    displaced: list[Any] = []
    files = TwoFolders()
    monkeypatch.setattr(
        "alkera_cli.cloud.folder.mount", _mount_with(_recording_pull(seen), displaced)
    )
    folders = _folders(tmp_path, http, files)

    held = folders.take(CHAT, {}, instance="box-7:chat-a")

    assert held is not None
    assert held.org_path == "Chats/chat-a"
    assert files.paths == ["Chats/chat-a"]
    assert files.ids == []
    assert server.leased_nodes == [IMPOSTOR_NODE]
    # Nothing was named, so both hops resolve the same name, as they always did.
    assert seen[0]["node_id"] is None
    assert seen[0]["source"] == "Chats/chat-a"
    # The route is the only difference: the path take pulls on the same terms.
    _assert_pulled_without_local_state(displaced)
    _assert_pulled_under_the_fence(seen[0], held.root, tmp_path / "home")
