"""What a chat folder's hand-back push lands on a real drive.

The live sync never streams the tool caches a chat's working directory grows
(``.venv``, ``node_modules``, ``__pycache__``, at any depth), but the push that
hands the folder back at sleep used to walk the working directory with no
preset at all, so a ``.venv`` the agent made there still reached the drive in
one burst when the chat slept. The push under test is the box's own
(``cloud.folder._PUSH_LOCAL``), aimed at a real backend, and every assertion
is about what the drive then holds.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alkera_cli.cloud import folder as folder_mod
from alkera_cli.commands.files import FILES_TRANSFER_TIMEOUT
from alkera_sdk import AlkeraClient
from files._live_backend import LiveBackend, home_path, live_backend

pytestmark = [pytest.mark.spread]

#: The chat's work and records: all of it travels.
TRAVELS = (
    "scratch/plot.py",
    "scratch/app/src/index.js",
    "scratch/venv-notes/readme.md",
    "manifest.json",
    "chat.jsonl",
    ".runtime/agent/agent.db",
)

#: What the working directory grew as the agent's home: none of it travels.
STAYS = (
    "scratch/.venv/bin/python",
    "scratch/.venv/lib/python3.13/site-packages/numpy/__init__.py",
    "scratch/app/node_modules/left-pad/index.js",
    "scratch/app/src/__pycache__/index.cpython-313.pyc",
    ".runtime/envs/alkera/bin/python",
)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[AlkeraClient]:
    backend: LiveBackend
    with (
        live_backend(tmp_path / "server") as backend,
        AlkeraClient(
            base_url=backend.base_url, token=backend.token, timeout=FILES_TRANSFER_TIMEOUT
        ) as api,
    ):
        yield api


def test_the_hand_back_leaves_the_working_directorys_tool_caches_behind(
    client: AlkeraClient, tmp_path: Path
) -> None:
    root = tmp_path / "chats" / "chat-a"
    for relative in (*TRAVELS, *STAYS):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative)
    dest = home_path(client, "Chats/chat-a.alkerachat")

    folder_mod._PUSH_LOCAL(
        files=client.files,
        http=client.raw_client.get_httpx_client(),
        root=root,
        dest=dest,
        home=tmp_path / "home",
    )

    drive_id = str(client.files.drive()["id"])
    for relative in TRAVELS:
        assert client.files.item_by_path(drive_id, f"{dest}/{relative}")["kind"] == "file", relative
    for relative in STAYS:
        with pytest.raises(RuntimeError, match="404"):
            client.files.item_by_path(drive_id, f"{dest}/{relative}")
