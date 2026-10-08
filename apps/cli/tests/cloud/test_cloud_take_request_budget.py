"""What taking a chat folder that has not changed costs on the wire.

A box that restarts takes back every chat it was holding, and each take is a
pull followed by the start of the live view. The pull already leaves alone a
file whose bytes are on disk. The live view did not know that: it started with
no record of what the drive holds, found every file in the working directory
new, and sent each one back through the checkpoint push — a drive lookup, an
anchor read, a folder skeleton and an attribute patch per file. Thirty chats of
a few hundred files each made thousands of requests through the box's tunnel,
and a reader's first message waited behind all of them.

The drive here is a real ``httpx`` client over a transport that answers the
Files routes the take and the live view use, and the count is of the requests
it served: the claim is about the wire, so nothing between the box and the
wire is stubbed.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli.cloud.folder import ChatFolders
from alkera_cli.files.live_sync import LiveCadence
from alkera_sdk.client import files_namespace
from blake3 import blake3

DRIVE = "11111111-1111-1111-1111-111111111111"
ROOT = "00000000-0000-0000-0000-000000000000"
CHAT = "chat-a"
CHAT_NODE = "22222222-2222-2222-2222-222222222222"
CHAT_PATH = "home/ana/Chats/Kickoff.alkerachat"
#: The directories the working tree's files are spread over. Fixed, so the
#: number of FILES is the only thing that grows between the two sizes below.
SUBDIRS = ("src", "data", "notes", "out")
#: An hour ago: well past the settle window, so every file is one the live
#: view would read at once rather than one it waits on.
AN_HOUR_AGO_NS = time.time_ns() - 3_600 * 1_000_000_000
#: The live cadence the drive grants. Served, so the numbers are the drive's.
LIVE_GRANT: dict[str, Any] = {"settleMs": 100, "batchEveryMs": 0, "metadataEveryMs": 0}


class Drive:
    """The Files routes a chat folder's take and its live view speak to.

    Every request is recorded by route shape (ids folded out), so a test can
    say both how many requests the take made and which kind grew.
    """

    def __init__(self, files: dict[str, bytes]) -> None:
        self.requests: Counter[str] = Counter()
        self.unexpected: list[str] = []
        #: Every file the box looked up by path — what the checkpoint push
        #: does, file by file, before it decides whether to send one.
        self.looked_up: list[str] = []
        #: Every node the box wrote new bytes onto.
        self.written: list[str] = []
        #: Every node whose live document the box asked after.
        self.offered: list[str] = []
        self.items: dict[str, dict[str, Any]] = {}
        self.by_path: dict[str, str] = {}
        self.children: dict[str, list[str]] = {}
        self._next = 0
        self._add(CHAT_NODE, CHAT_PATH, kind="folder", parent=None)
        for relative, data in sorted(files.items()):
            parent_path = CHAT_PATH
            parent = CHAT_NODE
            parts = relative.split("/")
            for part in parts[:-1]:
                parent_path = f"{parent_path}/{part}"
                existing = self.by_path.get(parent_path)
                parent = existing or self._add(
                    self._id(), parent_path, kind="folder", parent=parent
                )
            self._add(self._id(), f"{CHAT_PATH}/{relative}", kind="file", parent=parent, data=data)

    def _id(self) -> str:
        self._next += 1
        return f"33333333-3333-3333-3333-{self._next:012d}"

    def _add(
        self, node_id: str, path: str, *, kind: str, parent: str | None, data: bytes = b""
    ) -> str:
        item: dict[str, Any] = {
            "id": node_id,
            "etag": f"etag-{node_id[-4:]}",
            "kind": kind,
            "name": path.rpartition("/")[2],
            "pathBytes": "/" + path,
        }
        if kind == "file":
            item["file"] = {"size": len(data), "contentHash": blake3(data).hexdigest()}
            item["attrs"] = {"mode": 0o100644, "mtimeNs": AN_HOUR_AGO_NS}
        self.items[node_id] = item
        self.by_path[path] = node_id
        self.children.setdefault(node_id, [])
        if parent is not None:
            self.children[parent].append(node_id)
        return node_id

    @staticmethod
    def _shape(method: str, path: str) -> str:
        shape = re.sub(r"/root:/.*$", "/root:/<path>", path)
        shape = re.sub(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<id>", shape
        )
        return f"{method} {shape}"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        shape = self._shape(method, path)
        self.requests[shape] += 1
        base = f"/api/v1/files/drives/{DRIVE}"
        if method == "GET" and path == "/api/v1/files/drives":
            return httpx.Response(200, json={"id": DRIVE, "rootId": ROOT})
        if path.startswith(f"{base}/root:/"):
            wanted = path[len(f"{base}/root:/") :].strip("/")
            node_id = self.by_path.get(wanted)
            if node_id is None:
                return _not_found()
            if self.items[node_id]["kind"] == "file":
                self.looked_up.append(wanted[len(CHAT_PATH) + 1 :])
            return httpx.Response(200, json=self.items[node_id])
        match = re.fullmatch(rf"{base}/items/([^/]+)(/.*)?", path)
        if match is None:
            self.unexpected.append(shape)
            return _not_found()
        node_id, rest = match.group(1), match.group(2) or ""
        if rest == "" and method == "GET":
            item = self.items.get(node_id)
            return httpx.Response(200, json=item) if item else _not_found()
        if rest == "" and method == "PATCH":
            return httpx.Response(200, json=self.items.get(node_id, {}))
        if rest == "/children" and method == "GET":
            rows = [self.items[child] for child in self.children.get(node_id, [])]
            return httpx.Response(200, json={"value": rows, "nextMarker": None})
        if rest == "/content" and method == "GET":
            raise AssertionError("an unchanged folder downloads nothing")
        if rest == "/content" and method == "PUT":
            self.written.append(node_id)
            return httpx.Response(201, json=self.items[node_id])
        if rest == "/tree" and method == "POST":
            return httpx.Response(201, json=[])
        if rest == "/lease" and method == "POST":
            return httpx.Response(200, json=_grant())
        if rest == "/lease/heartbeat" and method == "POST":
            return httpx.Response(200, json=_grant())
        if rest == "/lease/tree" and method == "POST":
            body = json.loads(request.content or b"{}")
            return httpx.Response(
                200, json={"live_seq": 1, "applied": len(body.get("entries", []))}
            )
        if rest == "/lease/tree/digests" and method == "POST":
            return httpx.Response(200, json={"digests": {}, "children": {}})
        if rest == "/lease/live" and method == "POST":
            return httpx.Response(200, json={"liveSeq": 1, "pending": 0})
        if rest == "/lease/live" and method == "GET":
            return httpx.Response(200, json={"entries": []})
        if rest == "/live" and method == "GET":
            # A file found changed when the live view starts is offered to its
            # text peer once: no session is open on any file here.
            self.offered.append(node_id)
            return httpx.Response(200, json={"live": False})
        self.unexpected.append(shape)
        return _not_found()


def _grant() -> dict[str, Any]:
    return {
        "epoch": 3,
        "expiresAt": "2099-01-01T00:00:00+00:00",
        "heartbeatEvery": 15.0,
        "syncInterval": 5.0,
        "live": LIVE_GRANT,
    }


def _not_found() -> httpx.Response:
    return httpx.Response(404, json={"error": {"code": "not_found", "message": "no such item"}})


class OneLook:
    """A watch that reports one quiet moment and ends.

    The live view sweeps the tree on its first batch, so one batch is the
    whole start-up: whatever it sends for a folder nobody touched, it sends
    here.
    """

    def __init__(self, _root: Path, _cadence: LiveCadence, _stop: Any) -> None:
        return None

    async def changes(self) -> AsyncIterator[set[Any]]:
        async def batches() -> AsyncIterator[set[Any]]:
            yield set()

        return batches()


def _working_tree(count: int) -> dict[str, bytes]:
    """``count`` files under the chat's working directory, over fixed folders."""
    return {
        f"scratch/{SUBDIRS[n % len(SUBDIRS)]}/file-{n:04d}.txt": f"line {n}\n".encode()
        for n in range(count)
    }


def _already_on_disk(root: Path, files: dict[str, bytes]) -> None:
    """The box already has these bytes: the take is the one after a restart."""
    for relative, data in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        os.utime(target, ns=(AN_HOUR_AGO_NS, AN_HOUR_AGO_NS))


def _take_and_stream(
    tmp_path: Path, count: int, *, before_live: Callable[[Path], None] | None = None
) -> Drive:
    """Take the chat's folder and let its live view start, then count.

    ``before_live`` is handed the working directory between the two — the
    moment an agent could already be writing into it."""
    files = _working_tree(count)
    drive = Drive(files)
    http = httpx.Client(transport=httpx.MockTransport(drive), base_url="http://files.test")
    folders = ChatFolders(
        chats_root=tmp_path / f"n{count}" / "chats",
        files=files_namespace(http),
        http=http,
        machine_id="box-7",
        home=tmp_path / f"n{count}" / "home",
        watcher_factory=OneLook,
    )
    _already_on_disk(folders.local_root(CHAT), files)
    chat = {"id": CHAT, "files_node_id": CHAT_NODE, "folder_path": CHAT_PATH}
    held = folders.take(CHAT, chat, instance=f"box-7:{CHAT}")
    assert held is not None, "the folder was not taken"
    assert held.pull.unchanged == count, "the take downloaded bytes the box already had"
    if before_live is not None:
        before_live(folders.local_root(CHAT) / "scratch")
    sync = folders.live(CHAT, folders.local_root(CHAT) / "scratch")
    assert sync is not None, "the live view did not start"
    run = folders._live[CHAT]
    run.thread.join(timeout=60.0)
    assert not run.thread.is_alive(), "the live view's first look never finished"
    assert not drive.unexpected, (
        f"the take spoke to routes this drive does not serve: {drive.unexpected}"
    )
    return drive


@pytest.mark.parametrize("count", [pytest.param(200, id="200-files")])
def test_taking_an_unchanged_folder_costs_the_same_whatever_it_holds(
    tmp_path: Path, count: int
) -> None:
    """Twenty files or two hundred, the take of a folder whose bytes are
    already on the box costs the same handful of requests: the pull lists
    the folders, and the live view, told what the pull just proved, sends
    nothing back for a file it was handed."""
    small = _take_and_stream(tmp_path, 20)
    large = _take_and_stream(tmp_path, count)

    grew = {
        shape: large.requests[shape] - small.requests.get(shape, 0)
        for shape in large.requests
        if large.requests[shape] != small.requests.get(shape, 0)
    }
    assert not grew, (
        f"the take cost {sum(small.requests.values())} requests for 20 files and "
        f"{sum(large.requests.values())} for {count}; what grew with the files: {grew}"
    )
    assert sum(large.requests.values()) <= 20, dict(large.requests)


def test_a_file_that_changed_after_the_take_is_the_one_file_sent_back(tmp_path: Path) -> None:
    """What the pull proved stops being true the moment a file is written:
    a file whose size or time moved after the take is sent to the drive, and
    it is the only one the live view looks up or writes."""
    edited = "scratch/data/file-0001.txt"

    def edit(working_dir: Path) -> None:
        target = working_dir / "data" / "file-0001.txt"
        target.write_bytes(b"rewritten by the agent\n")
        os.utime(target, ns=(AN_HOUR_AGO_NS + 1_000_000_000,) * 2)

    drive = _take_and_stream(tmp_path, 40, before_live=edit)

    assert set(drive.looked_up) == {edited}, drive.looked_up
    assert drive.written == [drive.by_path[f"{CHAT_PATH}/{edited}"]]
    assert drive.offered == [drive.by_path[f"{CHAT_PATH}/{edited}"]]
