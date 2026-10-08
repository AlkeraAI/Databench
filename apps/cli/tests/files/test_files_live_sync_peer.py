"""The live plane with its text peer, against a drive and a live route that
behave like the real ones.

The drive is the suites' fake drive (it keeps the bytes it is sent, read off
disk at upload time). The live route is an HTTP double served through the
peer's real REST client: it keeps each document's text, merges a send by
taking it whole, and answers ``saved`` the way the server does. A document
whose write back cannot be made answers ``saved: false``; the agent's edit
must then reach the drive through the ordinary upload, or the drive keeps
the text from before the agent's save for as long as nobody types into the
file. What is pinned is what the drive and the document end up holding.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli.files.chat_fs import ChatTree
from alkera_cli.files.live_peer import LivePeer, RestPeerApi
from alkera_cli.files.live_sync import LiveSync
from alkera_cli.files.mount import LeaseSupersededError
from alkera_cli.files.tree_watch import Change
from files._live_sync_fakes import FakeClock, FakeLiveApi
from files._live_sync_fakes import make_sync as _sync
from files._live_sync_fakes import write as _write

DRIVE = "drive-1"
_LIVE = re.compile(r"/api/v1/files/drives/[^/]+/items/(?P<node>[^/]+)/live$")


@dataclass
class LiveRoute:
    """``GET``/``POST .../live``: each open document's text and token."""

    docs: dict[str, str] = field(default_factory=dict)
    #: What ``saved`` says on every answer; ``None`` leaves the field out, as
    #: a server from before it does.
    saved: bool | None = True
    #: Nodes the route answers with an error: ``node -> (status, code)``.
    refuse: dict[str, tuple[int, str]] = field(default_factory=dict)
    versions: dict[str, int] = field(default_factory=dict)
    submitted: list[tuple[str, str]] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        found = _LIVE.search(request.url.path)
        assert found is not None, request.url
        node = found["node"]
        if node in self.refuse:
            status, code = self.refuse[node]
            return httpx.Response(status, json={"error": {"code": code, "message": code}})
        if node not in self.docs:
            return httpx.Response(404, json={"error": {"code": "files.not_found"}})
        answer: dict[str, Any] = {"live": True}
        if request.method == "POST":
            sent = json.loads(request.content)
            self.submitted.append((node, sent["text"]))
            self.docs[node] = sent["text"]
            self.versions[node] = self.versions.get(node, 0) + 1
            answer["atToken"] = self._token(node)
        answer |= {"token": self._token(node), "text": self.docs[node]}
        if self.saved is not None:
            answer["saved"] = self.saved
        return httpx.Response(200, json=answer)

    def _token(self, node: str) -> str:
        return f"{node}@{self.versions.get(node, 0)}"


@dataclass
class Folder:
    tree: Path
    api: FakeLiveApi
    route: LiveRoute
    sync: LiveSync
    peer: LivePeer

    def save(self, name: str, text: str) -> None:
        """The agent saves ``name``; the round after takes it."""
        _write(self.tree, name, text.encode("utf-8"))
        self.sync.classify(Change.modified, str(self.tree / name))
        self.sync.flush()

    def opened(self, name: str) -> str:
        """A person opens ``name`` live: its document holds the drive's text
        and the box joins it, as the ``live_text`` notice has it do."""
        node = self.api.nodes[name]
        self.route.docs[node] = (self.tree / name).read_text(encoding="utf-8")
        assert self.peer.join(node, name)
        return node


@pytest.fixture
def folder(tmp_path: Path) -> Folder:
    tree = tmp_path / "scratch"
    tree.mkdir()
    api = FakeLiveApi(root=tree)
    route = LiveRoute()
    http = httpx.Client(base_url="http://drive.test", transport=httpx.MockTransport(route))
    peer = LivePeer(api=RestPeerApi(http=http, drive_id=DRIVE), tree=ChatTree(tree))
    sync = _sync(tree, api, FakeClock(), peer=peer)
    for name in ("plan.txt", "notes.txt", "other.txt"):
        _write(tree, name, f"{name} from the drive\n".encode())
        sync.classify(Change.added, str(tree / name))
    sync.flush()
    api.uploads.clear()
    return Folder(tree=tree, api=api, route=route, sync=sync, peer=peer)


def test_an_edit_the_document_takes_but_the_drive_will_not_hold_is_uploaded(
    folder: Folder,
) -> None:
    """Nobody can be named to write the document back (a person only looked
    at the file): the live route answers ``saved: false``, and the agent's
    text reaches the drive through the ordinary upload, for every save."""
    node = folder.opened("plan.txt")
    folder.route.saved = False

    folder.save("plan.txt", "plan.txt from the drive\nagent's line\n")

    assert folder.route.docs[node] == "plan.txt from the drive\nagent's line\n"
    assert folder.api.uploads == ["plan.txt"]
    assert folder.api.stored["plan.txt"] == b"plan.txt from the drive\nagent's line\n"
    # A stopped plane's checkpoint push still sends it: nothing counts it as sent.
    assert folder.sync.sent_live("chat", running=False) == []

    folder.save("plan.txt", "plan.txt from the drive\nagent's line\nand another\n")
    assert folder.api.uploads == ["plan.txt", "plan.txt"]
    assert folder.api.stored["plan.txt"] == b"plan.txt from the drive\nagent's line\nand another\n"


@pytest.mark.parametrize(
    "saved",
    [
        pytest.param(True, id="saved"),
        # A server from before the field: its write back always runs.
        pytest.param(None, id="field-absent"),
    ],
)
def test_an_edit_the_drive_will_hold_is_sent_once_to_the_document_only(
    folder: Folder, saved: bool | None
) -> None:
    node = folder.opened("plan.txt")
    folder.route.saved = saved

    folder.save("plan.txt", "plan.txt from the drive\nagent's line\n")

    assert folder.route.docs[node] == "plan.txt from the drive\nagent's line\n"
    assert folder.api.uploads == []
    assert folder.sync.sent_live("chat", running=False) == ["chat/plan.txt"]


def test_a_document_saved_again_stops_the_uploads(folder: Folder) -> None:
    """Once a person writes the document back, the drive holds it again and
    the agent's next save goes to the document alone."""
    folder.opened("plan.txt")
    folder.route.saved = False
    folder.save("plan.txt", "one\n")
    assert folder.api.uploads == ["plan.txt"]

    folder.route.saved = True
    folder.save("plan.txt", "one\ntwo\n")
    assert folder.api.uploads == ["plan.txt"]
    assert folder.sync.sent_live("chat", running=False) == ["chat/plan.txt"]


@pytest.mark.parametrize(
    ("status", "code"),
    [
        pytest.param(500, "internal", id="server-error"),
        pytest.param(422, "files.crdt.sandbox_refused", id="refused"),
        pytest.param(409, "files.crdt.history_corrupt", id="conflict-not-the-lease"),
    ],
)
def test_one_file_the_live_route_refuses_leaves_the_peer_and_the_round_goes_on(
    folder: Folder, status: int, code: str
) -> None:
    """A refusal is that file's alone: it goes up the ordinary way, and the
    other files of the round, held or not, land as they would have."""
    broken = folder.opened("plan.txt")
    healthy = folder.opened("notes.txt")
    folder.route.refuse[broken] = (status, code)

    _write(folder.tree, "plan.txt", b"agent's plan\n")
    _write(folder.tree, "notes.txt", b"agent's notes\n")
    _write(folder.tree, "other.txt", b"agent's other\n")
    for name in ("plan.txt", "notes.txt", "other.txt"):
        folder.sync.classify(Change.modified, str(folder.tree / name))
    folder.sync.flush()

    assert sorted(folder.api.uploads) == ["other.txt", "plan.txt"]
    assert folder.api.stored["plan.txt"] == b"agent's plan\n"
    assert folder.api.stored["other.txt"] == b"agent's other\n"
    assert folder.route.docs[healthy] == "agent's notes\n"
    assert not folder.peer.owns(broken)
    assert folder.peer.owns(healthy)


def test_the_fence_refusing_the_live_route_stops_the_plane(folder: Folder) -> None:
    """The lease moving on is the folder's refusal, never one file's: no
    upload is attempted on the strength of it."""
    node = folder.opened("plan.txt")
    folder.route.refuse[node] = (409, "files.lease_fenced")

    _write(folder.tree, "plan.txt", b"agent's plan\n")
    folder.sync.classify(Change.modified, str(folder.tree / "plan.txt"))
    with pytest.raises(LeaseSupersededError):
        folder.sync.flush()
    assert folder.api.uploads == []
