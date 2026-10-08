"""What a chat's work survives when it sleeps and wakes on another machine.

A cloud chat's durable state is its folder in the org drive. The box serving
the chat takes the folder when it wakes the chat and hands it back (push, then
release) when the chat sleeps, so the next box to wake it pulls the same bytes.

With Files off there is no drive. A chat record then names no folder node, the
box falls back to the ``Chats/<chat id>`` path convention, and the backend
answers that path with the opaque 404 every Files route gives while the flag is
off. The box serves the chat anyway, with its working directory local to its own
disk. These tests pin what that means against the real backend: the work, and
the agent's own session store, are still on the box that slept the chat, and a
wake on any other box starts from an empty directory.

That is why the production example runs with Files on.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alkera_cli.cloud.folder import ChatFolders
from alkera_cli.commands.files import FILES_TRANSFER_TIMEOUT
from alkera_cli.harness.opencode_db import AGENT_DB_RELATIVE
from alkera_core.config import settings
from alkera_sdk import AlkeraClient
from files._live_backend import LiveBackend, live_backend

pytestmark = [pytest.mark.spread]

CHAT = "chat-moves"
WORK = Path("scratch/report.csv")
WORK_BYTES = b"region,total\nnorth,42\n"
AGENT_DB = Path("scratch") / AGENT_DB_RELATIVE
AGENT_DB_BYTES = b"the agent's own memory of the conversation"


@pytest.fixture
def backend(tmp_path: Path) -> Iterator[LiveBackend]:
    with live_backend(tmp_path / "server") as running:
        yield running


@pytest.fixture
def files_off() -> Iterator[None]:
    """The backend with ``FILES_ENABLED=false``, as the production example
    shipped it. Read per request, so flipping it after boot is the flag off."""
    previous = settings.files_enabled
    settings.files_enabled = False
    try:
        yield
    finally:
        settings.files_enabled = previous


def _client(backend: LiveBackend) -> AlkeraClient:
    return AlkeraClient(
        base_url=backend.base_url, token=backend.token, timeout=FILES_TRANSFER_TIMEOUT
    )


def _box(api: AlkeraClient, disk: Path) -> ChatFolders:
    """One machine's folder custody; ``disk`` is that machine's disk."""
    return ChatFolders(
        chats_root=disk / "chats",
        files=api.files,
        http=api.raw_client.get_httpx_client(),
        home=disk / "home",
    )


def _chat_folder(api: AlkeraClient) -> str:
    """A chat folder in the caller's home, as Files on gives every chat."""
    drive = api.files.drive()
    folder = api.files.create_folder(
        str(drive["id"]), str(drive["homeId"]), f"moves-{uuid.uuid4().hex}"
    )
    return str(folder["id"])


def _do_a_turn(root: Path) -> None:
    """What a turn leaves in the chat's directory: a file the agent wrote and
    the agent's session store."""
    for relative, data in ((WORK, WORK_BYTES), (AGENT_DB, AGENT_DB_BYTES)):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def test_with_files_on_the_work_follows_the_chat_to_another_machine(
    backend: LiveBackend, tmp_path: Path
) -> None:
    with _client(backend) as api:
        chat = {"files_node_id": _chat_folder(api)}
        first, second = _box(api, tmp_path / "box-a"), _box(api, tmp_path / "box-b")

        held = first.take(CHAT, chat, instance=f"box-a:{CHAT}")
        assert held is not None
        _do_a_turn(held.root)
        assert first.hand_back(CHAT) is not None, "the sleep did not give the folder back"

        woken = second.take(CHAT, chat, instance=f"box-b:{CHAT}")
        assert woken is not None
        assert (woken.root / WORK).read_bytes() == WORK_BYTES
        assert (woken.root / AGENT_DB).read_bytes() == AGENT_DB_BYTES


def test_with_files_off_a_chat_is_served_without_a_folder(
    backend: LiveBackend, files_off: None, tmp_path: Path
) -> None:
    with _client(backend) as api:
        box = _box(api, tmp_path / "box-a")

        # A chat record names no folder node with Files off, so the box asks for
        # the path convention and is answered 404: no custody, the chat is still
        # served.
        assert box.take(CHAT, {}, instance=f"box-a:{CHAT}") is None
        assert box.held(CHAT) is None


def test_with_files_off_the_work_stays_on_the_machine_that_slept_the_chat(
    backend: LiveBackend, files_off: None, tmp_path: Path
) -> None:
    with _client(backend) as api:
        first, second = _box(api, tmp_path / "box-a"), _box(api, tmp_path / "box-b")

        assert first.take(CHAT, {}, instance=f"box-a:{CHAT}") is None
        _do_a_turn(first.local_root(CHAT))
        # The sleep has nothing to give back and pushes nothing.
        assert first.hand_back(CHAT) is None

        # The same machine wakes the chat on what it left on its own disk.
        assert first.take(CHAT, {}, instance=f"box-a:{CHAT}") is None
        assert (first.local_root(CHAT) / WORK).read_bytes() == WORK_BYTES

        # Another machine wakes it on nothing: neither the agent's file nor its
        # session store came with it.
        assert second.take(CHAT, {}, instance=f"box-b:{CHAT}") is None
        moved = second.local_root(CHAT)
        assert not (moved / WORK).exists()
        assert not (moved / AGENT_DB).exists()
