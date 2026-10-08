"""A box reads a chat's drive off the chat record, never as its own.

A pool box serves chats from many orgs, each in its own drive, and a box on
its machine credential is a member of no org: asked for "the caller's drive"
the server answers the opaque not-found, and a box that asked that way took no
folder, fetched no attachment and pushed nothing back — every chat ran on
scratch local to one machine. The record a chat is opened from names the drive
its folder is on beside the node the chat IS, so everything the box does with
the folder is addressed by that drive: the take, the path it is filed at, the
attachments it reads. Only a record that names none falls back to the caller's
own drive, which an org box on its operator's session still has.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from _mirror_service import Clock, build_service
from alkera_cli.cloud import CloudRestClient
from alkera_cli.cloud.folder import ChatFolders, ReleasedFolder, chat_folder_drive, recovery_path
from alkera_sdk.client import AlkeraHTTPError
from test_cloud_chat_folder import (
    CHAT,
    DRIVE,
    NODE,
    NODE_PATH,
    FakeFiles,
    LeaseServer,
    _folders,
    _mount_with,
    _pull_nothing,
)
from test_cloud_folder_handback import CHAT as HANDBACK_CHAT
from test_cloud_folder_handback import DRIVE as HANDBACK_DRIVE
from test_cloud_folder_handback import NODE as HANDBACK_NODE
from test_cloud_folder_handback import FakeFiles as HandbackFiles
from test_cloud_folder_handback import _folders as _handback_folders
from test_cloud_folder_handback import _wired

#: The record as the wire serves it to a box: the node the chat IS and the
#: drive that node is on.
RECORD: dict[str, Any] = {"id": CHAT, "files_node_id": NODE, "files_drive_id": DRIVE}


def _no_drive() -> AlkeraHTTPError:
    """What ``GET /files/drives`` answers a box on its machine credential."""
    return AlkeraHTTPError(
        label="files/drives", status=404, code="not_found", message="Not found", trace_id=None
    )


class MachineFiles(FakeFiles):
    """The Files namespace as a box on its machine credential meets it: no
    drive of its own, and every node addressed by the drive the caller names."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.drive_reads = 0
        self.item_drives: list[str] = []

    def drive(self) -> dict[str, Any]:
        self.drive_reads += 1
        raise _no_drive()

    def item(self, drive_id: str, item_id: str, *, select: str | None = None) -> dict[str, Any]:
        self.item_drives.append(drive_id)
        return super().item(drive_id, item_id, select=select)


@pytest.fixture
def server() -> LeaseServer:
    return LeaseServer()


@pytest.fixture
def http(server: LeaseServer) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(server), base_url="http://files.test")


# ---------------------------------------------------------------------------
# What the record says
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("chat", "expected"),
    [
        pytest.param({"files_drive_id": DRIVE}, DRIVE, id="snake-case"),
        pytest.param({"filesDriveId": DRIVE}, DRIVE, id="camel-case"),
        pytest.param({"files_drive_id": f"  {DRIVE} "}, DRIVE, id="whitespace-trimmed"),
        pytest.param({}, None, id="a-record-from-before-the-field"),
        pytest.param({"files_drive_id": None}, None, id="null-is-no-drive"),
        pytest.param({"files_drive_id": "   "}, None, id="blank-is-no-drive"),
        pytest.param({"files_drive_id": 7}, None, id="not-a-string-is-no-drive"),
    ],
)
def test_the_drive_a_chat_names_is_read_off_its_record(
    chat: dict[str, Any], expected: str | None
) -> None:
    assert chat_folder_drive(chat) == expected


# ---------------------------------------------------------------------------
# The take
# ---------------------------------------------------------------------------


def test_a_box_on_its_machine_credential_takes_the_folder_on_the_drive_the_record_names(
    tmp_path: Path, http: httpx.Client, server: LeaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server answers this box that it has no drive; the record says which
    drive the chat's folder is on, and that is where the folder is looked up,
    leased and recorded — without the drive ever being asked for."""
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    files = MachineFiles()
    folders = _folders(tmp_path, http, files=files)

    held = folders.take(CHAT, RECORD, instance="box-7:chat-a")

    assert held is not None
    assert held.record.drive_id == DRIVE
    assert held.org_path == NODE_PATH, "the path is where the record's node really is"
    assert files.drive_reads == 0
    assert files.item_drives == [DRIVE, DRIVE], "the path lookup and the mount both named it"
    assert server.acquires[0]["instanceId"] == "box-7:chat-a"


def test_a_record_that_names_no_drive_leaves_a_machine_credential_with_no_custody(
    tmp_path: Path, http: httpx.Client, caplog: pytest.LogCaptureFixture
) -> None:
    """A chat from before the record carried its drive: the only drive left to
    ask for is the caller's, which this box has none of. The chat still runs,
    on scratch local to this box, and the box says so once."""
    files = MachineFiles()
    folders = _folders(tmp_path, http, files=files)

    with caplog.at_level(logging.INFO, logger="alkera_cli.cloud.folder"):
        assert folders.take(CHAT, {"id": CHAT, "files_node_id": NODE}, instance="i") is None
        assert folders.take(CHAT, {"id": CHAT, "files_node_id": NODE}, instance="i") is None

    assert folders.held(CHAT) is None
    assert files.drive_reads == 2
    assert sum("is not served by this backend" in r.message for r in caplog.records) == 1


def test_an_org_box_still_reads_its_own_drive_for_a_record_that_names_none(
    tmp_path: Path, http: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback an org box on its operator's session has kept: its own
    drive, asked for once, is the drive the folder is taken on."""
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    folders = _folders(tmp_path, http)

    held = folders.take(CHAT, {"id": CHAT, "files_node_id": NODE}, instance="box-7:chat-a")

    assert held is not None
    assert held.record.drive_id == DRIVE


class MachineHandbackFiles(HandbackFiles):
    """The hand-back suite's backend, met on a machine credential."""

    def drive(self) -> dict[str, Any]:
        raise _no_drive()


def test_a_gone_folder_on_a_machine_credential_is_recovered_at_the_root_not_dropped(
    tmp_path: Path, http: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A box with no drive of its own has no home to anchor a recovery on. The
    work is still aimed somewhere — the root — and the refusal it will meet
    there is the drive's to give: an error on the way to asking would have
    dropped the turn's files with nothing in the log."""
    files = MachineHandbackFiles()
    pushes = _wired(monkeypatch)
    folders = _handback_folders(tmp_path, http, files)
    record = {
        "id": HANDBACK_CHAT,
        "files_node_id": HANDBACK_NODE,
        "files_drive_id": HANDBACK_DRIVE,
    }
    held = folders.take(HANDBACK_CHAT, record, instance="box-7:chat-a")
    assert held is not None
    (held.root / "notes.md").write_text("the turn's work", encoding="utf-8")
    files.trashed = True

    released = folders.hand_back(HANDBACK_CHAT)

    assert isinstance(released, ReleasedFolder)
    assert released.recovered is True
    expected = recovery_path("", "Kickoff.alkerachat")
    assert pushes.dests == [expected]
    assert pushes.files == [["notes.md"]]
    assert released.org_path == expected
    assert folders.held(HANDBACK_CHAT) is None


# ---------------------------------------------------------------------------
# Attachments: the reader is bound to the chat's drive
# ---------------------------------------------------------------------------

CONTENT_ORIGIN = "http://origin.test"


class FilesWire:
    """The Files API as a box meets it, plus the content origin: the drives
    route answers as it does for the caller (a machine has none; an org box
    has its own), the content route mints for a node on ANY drive named, and
    the origin serves the bytes."""

    def __init__(self, bodies: dict[str, bytes], *, own_drive: str | None) -> None:
        self.bodies = bodies
        self.own_drive = own_drive
        self.drive_reads = 0
        #: ``(drive id, node id)`` per mint, in order.
        self.minted: list[tuple[str, str]] = []
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = request.url.path
        if route == "/api/v1/files/drives":
            self.drive_reads += 1
            if self.own_drive is None:
                return httpx.Response(404, json={"code": "not_found"})
            return httpx.Response(200, json={"id": self.own_drive, "rootId": "r", "homeId": "h"})
        if route.endswith("/content"):
            parts = route.split("/")
            drive_id, node_id = parts[-4], parts[-2]
            self.minted.append((drive_id, node_id))
            if node_id not in self.bodies:
                return httpx.Response(404, json={"code": "not_found"})
            return httpx.Response(302, headers={"location": f"{CONTENT_ORIGIN}/signed/{node_id}"})
        if route.startswith("/signed/"):
            return httpx.Response(200, content=self.bodies[route.rsplit("/", 1)[-1]])
        if route == "/api/v1/machines/register":
            return httpx.Response(200, json={"id": "m1"})
        return httpx.Response(404, json={"code": "not_found"})


async def _read(fetcher: Any, node_id: str) -> bytes:
    async with fetcher.fetch(node_id) as chunks:
        return b"".join([chunk async for chunk in chunks])


@pytest.mark.asyncio
async def test_a_reader_bound_to_a_chats_drive_never_asks_for_the_callers() -> None:
    wire = FilesWire({"n1": b"rows"}, own_drive=None)
    client = CloudRestClient(
        api_url="http://api.test",
        token="alk_machine_x",
        agent_id=None,
        transport=httpx.MockTransport(wire),
    )

    fetcher = await client.attachment_fetcher(drive_id="drive-of-the-chat")

    assert wire.drive_reads == 0
    assert await _read(fetcher, "n1") == b"rows"
    assert wire.minted == [("drive-of-the-chat", "n1")]


@pytest.mark.asyncio
async def test_a_reader_for_a_chat_that_names_no_drive_asks_for_the_callers_once() -> None:
    wire = FilesWire({"n1": b"rows"}, own_drive="org-drive")
    client = CloudRestClient(
        api_url="http://api.test",
        token="device-jwt",
        agent_id="chat-1",
        transport=httpx.MockTransport(wire),
    )

    fetcher = await client.attachment_fetcher()

    assert wire.drive_reads == 1
    assert await _read(fetcher, "n1") == b"rows"
    assert wire.minted == [("org-drive", "n1")]


def _file_part(node_id: str, filename: str, size: int) -> dict[str, Any]:
    return {
        "type": "file",
        "part_id": f"p-{node_id}",
        "message_id": "m1",
        "source": "file",
        "node_id": node_id,
        "filename": filename,
        "size": size,
        "mime": "text/plain",
        "sha256": "",
    }


def _message(*parts: dict[str, Any]) -> dict[str, Any]:
    return {"text": "look at this", "attachments": list(parts)}


@pytest.mark.asyncio
async def test_the_box_fetches_a_chats_attachments_from_the_drive_its_record_names(
    tmp_path: Path,
) -> None:
    """Through the service, from the record the mirror was opened from: two
    chats in two drives on one box, each read from its own; the drives route,
    which answers this box nothing, is never asked."""
    wire = FilesWire({"n1": b"first org", "n2": b"second org"}, own_drive=None)
    rest = CloudRestClient(
        api_url="http://api.test",
        token="alk_machine_x",
        agent_id=None,
        transport=httpx.MockTransport(wire),
    )
    service, _mirrors = build_service(tmp_path, clock=Clock(), rest=rest)
    service._mirrors["chat-1"] = service._default_mirror(
        "chat-1", {"id": "chat-1", "files_drive_id": "drive-1", "owner_user_id": "u1"}
    )
    service._mirrors["chat-2"] = service._default_mirror(
        "chat-2", {"id": "chat-2", "files_drive_id": "drive-2", "owner_user_id": "u2"}
    )

    first = await service.prepare_attachments("chat-1", _message(_file_part("n1", "a.txt", 9)))
    second = await service.prepare_attachments("chat-2", _message(_file_part("n2", "b.txt", 10)))

    assert [f.path.read_bytes() for f in first.files] == [b"first org"]
    assert [f.path.read_bytes() for f in second.files] == [b"second org"]
    assert first.notices == () and second.notices == ()
    assert wire.minted == [("drive-1", "n1"), ("drive-2", "n2")]
    assert wire.drive_reads == 0


@pytest.mark.asyncio
async def test_a_chat_whose_record_names_no_drive_is_read_through_the_held_folders_lease(
    tmp_path: Path, http: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mirror opened by a factory that knows no drive still has one to read
    from while the box holds the chat's folder: the lease names it."""
    monkeypatch.setattr("alkera_cli.cloud.folder.mount", _mount_with(_pull_nothing))
    folders = ChatFolders(
        chats_root=tmp_path / "box" / "chats",
        files=MachineFiles(),
        http=http,
        machine_id="box-7",
        home=tmp_path / "box" / "home",
    )
    assert folders.take("chat-1", {"id": "chat-1", **RECORD}, instance="box-7:chat-1") is not None
    wire = FilesWire({"n1": b"leased"}, own_drive=None)
    rest = CloudRestClient(
        api_url="http://api.test",
        token="alk_machine_x",
        agent_id=None,
        transport=httpx.MockTransport(wire),
    )
    service, _mirrors = build_service(tmp_path, clock=Clock(), rest=rest, folders=folders)
    service._mirrors["chat-1"] = service._default_mirror(
        "chat-1", {"id": "chat-1", "owner_user_id": "u1"}
    )

    landed = await service.prepare_attachments("chat-1", _message(_file_part("n1", "a.txt", 6)))

    assert [f.path.read_bytes() for f in landed.files] == [b"leased"]
    assert wire.minted == [(DRIVE, "n1")]
    assert wire.drive_reads == 0


@pytest.mark.asyncio
async def test_a_machine_with_no_drive_to_fall_back_on_says_which_file_did_not_arrive(
    tmp_path: Path,
) -> None:
    """Nothing names the drive — a record from before the field, no folder
    held — so the caller's own drive is asked for and refused. The turn still
    runs, and the reader is told which file is missing rather than nothing."""
    wire = FilesWire({"n1": b"rows"}, own_drive=None)
    rest = CloudRestClient(
        api_url="http://api.test",
        token="alk_machine_x",
        agent_id=None,
        transport=httpx.MockTransport(wire),
    )
    service, _mirrors = build_service(tmp_path, clock=Clock(), rest=rest)
    service._mirrors["chat-1"] = service._default_mirror(
        "chat-1", {"id": "chat-1", "owner_user_id": "u1"}
    )

    landed = await service.prepare_attachments("chat-1", _message(_file_part("n1", "a.txt", 4)))

    assert landed.files == ()
    assert len(landed.notices) == 1 and landed.notices[0].startswith("a.txt: could not be fetched")
    assert wire.drive_reads == 1 and wire.minted == []


@pytest.mark.asyncio
async def test_one_reader_per_drive_is_kept_for_the_life_of_the_box(tmp_path: Path) -> None:
    wire = FilesWire({"n1": b"x", "n2": b"y"}, own_drive=None)
    rest = CloudRestClient(
        api_url="http://api.test",
        token="alk_machine_x",
        agent_id=None,
        transport=httpx.MockTransport(wire),
    )
    service, _mirrors = build_service(tmp_path, clock=Clock(), rest=rest)
    for chat in ("chat-1", "chat-2"):
        service._mirrors[chat] = service._default_mirror(
            chat, {"id": chat, "files_drive_id": "drive-1", "owner_user_id": "u1"}
        )

    first = await service.prepare_attachments("chat-1", _message(_file_part("n1", "a.txt", 1)))
    second = await service.prepare_attachments("chat-2", _message(_file_part("n2", "b.txt", 1)))

    assert [f.path.read_bytes() for f in (*first.files, *second.files)] == [b"x", b"y"]
    assert wire.minted == [("drive-1", "n1"), ("drive-1", "n2")]
    assert len(service._inputs.readers) == 1, "two chats on one drive share one reader"
