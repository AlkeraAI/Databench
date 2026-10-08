"""A box on its own machine credential holds a chat's folder through the real
routes, in both directions.

The credential is the bearer; there is no user behind it and no drive of its
own — the drive route answers it the opaque not-found. What the box has is the
chat record: the node the chat IS and the drive that node is on. Taking the
folder by that pair leases it as the machine the chat is bound to, pulls what
the drive holds (a file the reader put in ``uploads/``), and pushes what the
turn wrote back where the reader can see it. Every hop here is the real one —
the lease, the fence, the sealed folder's content route, the upload session —
against the backend on a socket, because a mock of any of them would only show
that the box calls what it calls.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.cloud.box_auth import BearerAuth
from alkera_cli.cloud.folder import ChatFolders, wire_path
from alkera_cli.commands.files import FILES_TRANSFER_TIMEOUT
from alkera_core.config import settings
from alkera_sdk.client import AlkeraClient, AlkeraHTTPError
from files._live_backend import LiveBackend, live_backend
from sqlalchemy import create_engine, text

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(64))


def _bind(chat_id: str, machine_id: str) -> None:
    """Bind the chat row to ``machine_id``, as placement and a rebind do."""
    sync = create_engine(settings.database_url_sync)
    try:
        with sync.begin() as connection:
            connection.execute(
                text(
                    "UPDATE workspace_objects SET spec = jsonb_set(spec, '{machine_id}', "
                    "to_jsonb(CAST(:machine AS text))) WHERE id = :id"
                ),
                {"id": uuid.UUID(chat_id), "machine": machine_id},
            )
    finally:
        sync.dispose()


def _record(chat: dict[str, Any]) -> dict[str, Any]:
    """The chat as the box reads it off the wire: the node and the drive."""
    return {
        "id": chat["id"],
        "files_node_id": chat["files_node_id"],
        "files_drive_id": chat["files_drive_id"],
    }


def _box(backend: LiveBackend, tmp_path: Path, name: str) -> ChatFolders:
    assert backend.box_token is not None and backend.box_machine is not None
    folders = ChatFolders.for_box(
        api_url=backend.base_url,
        auth=BearerAuth(lambda: backend.box_token),
        chats_root=tmp_path / name / "chats",
        home=tmp_path / name / "home",
        timeout=FILES_TRANSFER_TIMEOUT,
    )
    folders.bind_machine(backend.box_machine)
    return folders


@pytest.fixture
def machine_chat(tmp_path: Path) -> Iterator[tuple[LiveBackend, AlkeraClient, dict[str, Any]]]:
    """A real backend, the owner's client, and a chat the owner opened through
    the API — filed in their home, bound to the platform box's machine."""
    with (
        live_backend(tmp_path / "server", machine=True) as backend,
        AlkeraClient(
            base_url=backend.base_url, token=backend.token, timeout=FILES_TRANSFER_TIMEOUT
        ) as owner,
    ):
        assert backend.box_machine is not None
        made = owner.raw_client.get_httpx_client().post(
            "/api/v1/chats", json={"title": "Kickoff", "clientId": secrets.token_hex(8)}
        )
        assert made.status_code == 201, made.text
        chat = made.json()
        assert chat["files_node_id"] and chat["files_drive_id"], chat
        _bind(chat["id"], backend.box_machine)
        yield backend, owner, chat


def test_a_box_on_its_credential_holds_a_chats_folder_both_ways_by_the_drive_the_record_names(
    machine_chat: tuple[LiveBackend, AlkeraClient, dict[str, Any]], tmp_path: Path
) -> None:
    backend, owner, chat = machine_chat
    assert backend.box_token is not None
    chat_id, drive = chat["id"], chat["files_drive_id"]

    # The premise: on its own credential the box has no drive to ask for —
    # and the folder, addressed by the drive the record names, it may read.
    with AlkeraClient(base_url=backend.base_url, token=backend.box_token) as as_box:
        with pytest.raises(AlkeraHTTPError) as refused:
            as_box.files.drive()
        assert refused.value.status == 404
        boxs_view = wire_path(as_box.files.item(drive, chat["files_node_id"]))
    assert boxs_view is not None

    first = _box(backend, tmp_path, "box-1")
    held = first.take(chat_id, _record(chat), instance=f"box-1:{chat_id}")
    assert held is not None, "the record names the drive; nothing else was needed"
    assert (held.record.drive_id, held.record.node_id) == (drive, chat["files_node_id"])
    # The path recorded is where the drive says the node is, as it says it to
    # the box — not the ``Chats/<id>`` convention a record without a node gets.
    assert held.org_path == boxs_view
    assert held.org_path != f"Chats/{chat_id}"
    folder_path = wire_path(owner.files.item(drive, chat["files_node_id"]))
    assert folder_path is not None

    # Box → drive: what the turn wrote is in the chat's folder for the reader.
    # The pull brought the chat's own working directory down with the folder.
    (held.root / "scratch").mkdir(exist_ok=True)
    (held.root / "scratch" / "answer.txt").write_bytes(b"pong\n")
    pushed = first.push(chat_id)
    assert pushed is not None and pushed.uploaded == 1
    landed = owner.files.item_by_path(drive, f"{folder_path}/scratch/answer.txt")
    assert landed["name"] == "answer.txt"

    # Drive → box: what the reader put in ``scratch/uploads/`` — where the
    # composer puts it, under the working directory the box's lease admits
    # other writers into — is on the next box's disk the moment it takes the
    # chat, byte for byte. Written while the first box still holds the lease,
    # as a reader writes into a running chat.
    scratch = owner.files.item_by_path(drive, f"{folder_path}/scratch")
    uploads = owner.files.create_folder(drive, str(scratch["id"]), "uploads")
    source = tmp_path / "paste-1-ab12.png"
    source.write_bytes(PNG)
    owner.files.upload_file(source, drive_id=drive, parent_id=str(uploads["id"]))
    released = first.hand_back(chat_id)
    assert released is not None and not released.discarded

    second = _box(backend, tmp_path, "box-2")
    resumed = second.take(chat_id, _record(chat), instance=f"box-2:{chat_id}")
    assert resumed is not None and resumed.root != held.root
    assert (resumed.root / "scratch" / "uploads" / "paste-1-ab12.png").read_bytes() == PNG
    assert (resumed.root / "scratch" / "answer.txt").read_bytes() == b"pong\n"
    assert second.hand_back(chat_id) is not None


def test_a_record_that_names_no_drive_gives_a_machine_credential_no_custody(
    machine_chat: tuple[LiveBackend, AlkeraClient, dict[str, Any]],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The drive is the record's to name: without it the only drive left to
    ask for is the caller's, which this box has none of. The chat still runs,
    on scratch local to the box, and the box says so once."""
    backend, _owner, chat = machine_chat
    box = _box(backend, tmp_path, "box-1")
    record = {"id": chat["id"], "files_node_id": chat["files_node_id"]}

    with caplog.at_level(logging.INFO, logger="alkera_cli.cloud.folder"):
        assert box.take(chat["id"], record, instance="box-1:x") is None
        assert box.take(chat["id"], record, instance="box-1:x") is None

    assert box.held(chat["id"]) is None
    assert sum("is not served by this backend" in r.message for r in caplog.records) == 1


def test_a_box_takes_no_folder_of_a_chat_bound_to_another_machine(
    machine_chat: tuple[LiveBackend, AlkeraClient, dict[str, Any]], tmp_path: Path
) -> None:
    """Same drive, the same record, a proven box — and a chat that runs
    elsewhere. The drive answers the opaque not-found and the box keeps no
    custody; nothing is raised, nothing is written."""
    backend, _owner, chat = machine_chat
    _bind(chat["id"], str(uuid.uuid4()))
    box = _box(backend, tmp_path, "box-1")

    assert box.take(chat["id"], _record(chat), instance="box-1:x") is None
    assert box.held(chat["id"]) is None
