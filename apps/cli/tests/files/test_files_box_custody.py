"""A box keeps custody of what it wrote through a drive outage and a restart.

Against the real backend on a real socket. Three ways a box used to stop
telling the drive about work that existed only on its disk, each pinned here:

* **A take that meets an outage.** While Postgres restarts the drive answers
  503. A box that read that as "this backend does not serve the folder" served
  the chat with no lease, no stream and no push. A take refused that way must
  be a take that did not happen: the chat is not served, the reader is told
  why, and the folder is taken again once the wait is up.
* **Beats that fail for a while.** A beat refused by an outage is not a lease
  that is gone. When the drive answers again, even after the lease ran out or
  was reaped meanwhile, the box holds the folder again and what it wrote still
  reaches the drive.
* **A restart.** A supervised restart keeps its leases for the next process,
  which takes back only the folders of the chats it serves. A folder no chat
  here takes back (a workspace whose members are all asleep) is settled by the
  next process: taken, everything on the disk pushed, the lease released.

The outage is a transport in front of the backend that answers 503 for the
chosen calls while ``down`` is set: what a box sees of a backend whose database
is restarting.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli.cloud.folder import ChatFolders
from alkera_cli.cloud.folder_returns import LEFT_GRACE_SECONDS, FolderReturns
from alkera_cli.cloud.folder_takes import FolderTakes
from alkera_cli.cloud.workspace_host import WorkspaceHost
from alkera_cli.commands.files import FILES_TRANSFER_TIMEOUT
from alkera_core.config import settings
from alkera_sdk import AlkeraClient
from files._live_backend import LiveBackend, live_backend
from sqlalchemy import create_engine, text

pytestmark = [pytest.mark.spread]

CHAT = "chat-many"
POLL = 5.0
LOCAL = b"written while the drive was down\n"


class Outage(httpx.BaseTransport):
    """The calls whose path ends in one of ``failing`` answer 503 while
    ``down`` is set; everything else reaches the backend."""

    def __init__(self, failing: tuple[str, ...]) -> None:
        self._inner = httpx.HTTPTransport()
        self.failing = failing
        self.down = True
        self.refused: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if self.down and any(path.endswith(suffix) for suffix in self.failing):
            self.refused.append(f"{request.method} {path.rsplit('/', 1)[-1]}")
            body = {"error": {"code": "db_unavailable", "message": "the database is restarting"}}
            return httpx.Response(503, content=json.dumps(body).encode(), request=request)
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def backend(tmp_path: Path) -> Iterator[LiveBackend]:
    with live_backend(tmp_path / "server") as running:
        yield running


def _client(backend: LiveBackend, transport: httpx.BaseTransport | None = None) -> AlkeraClient:
    return AlkeraClient(
        base_url=backend.base_url,
        token=backend.token,
        timeout=FILES_TRANSFER_TIMEOUT,
        httpx_args={"transport": transport} if transport is not None else None,
    )


def _folders(box: AlkeraClient, tmp_path: Path) -> ChatFolders:
    """The box's folder custody. ``tmp_path`` stands for the box's disk, so a
    second one built over the same path is the same box after a restart."""
    return ChatFolders(
        chats_root=tmp_path / "box" / "chats",
        files=box.files,
        http=box.raw_client.get_httpx_client(),
        home=tmp_path / "box-home",
    )


def _folder(person: AlkeraClient) -> tuple[str, str]:
    drive = person.files.drive()
    drive_id = str(drive["id"])
    folder = person.files.create_folder(drive_id, str(drive["homeId"]), f"many-{uuid.uuid4().hex}")
    return drive_id, str(folder["id"])


def _landed(person: AlkeraClient, tmp_path: Path, drive_id: str, parent: str, path: str) -> bytes:
    """The bytes the drive holds at ``path`` under ``parent``."""
    node = parent
    for name in path.split("/"):
        listed = {row["name"]: row for row in person.files.children(drive_id, node)}
        assert name in listed, f"{name} is not on the drive (found {sorted(listed)})"
        node = str(listed[name]["id"])
    dest = tmp_path / f"downloaded-{uuid.uuid4().hex}"
    return person.files.download(drive_id, node, dest).read_bytes()


def _lease(node_id: str) -> Any:
    """The lease row on ``node_id``, read straight off the database."""
    engine = create_engine(settings.database_url_sync)
    try:
        with engine.begin() as connection:
            return connection.execute(
                text(
                    "SELECT epoch, released_at, reaped_at, expires_at > now() AS live "
                    "FROM file_leases WHERE node_id = :n"
                ),
                {"n": node_id},
            ).one()
    finally:
        engine.dispose()


def _age_lease(node_id: str, *, reaped: bool) -> None:
    """The outage outlasted the lease: it ran out, and the reaper may have
    taken it apart since."""
    engine = create_engine(settings.database_url_sync)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE file_leases SET expires_at = now() - interval '1 second', "
                    "reaped_at = CASE WHEN :reaped THEN now() ELSE NULL END WHERE node_id = :n"
                ),
                {"n": node_id, "reaped": reaped},
            )
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# A take that meets an outage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failing",
    [
        pytest.param(("/lease",), id="the-grant-is-refused"),
        pytest.param(("/children", "/lease/release"), id="the-pull-and-the-give-back-are-refused"),
    ],
)
def test_a_take_refused_by_an_outage_is_retried_and_the_boxs_files_reach_the_drive(
    backend: LiveBackend, tmp_path: Path, failing: tuple[str, ...]
) -> None:
    outage = Outage(failing)
    with _client(backend) as person, _client(backend, outage) as box:
        drive_id, folder_id = _folder(person)
        folders = _folders(box, tmp_path)
        said: list[tuple[str, str, str]] = []

        async def report(chat_id: str, state: str, text: str = "") -> None:
            said.append((chat_id, state, text))

        clock = Clock()
        takes = FolderTakes(
            folders=folders,
            instance_of=lambda chat_id: f"box-7:{chat_id}",
            report=report,
            clock=clock,
            poll_interval=lambda: POLL,
        )
        chat: dict[str, Any] = {"files_node_id": folder_id}
        root = folders.local_root(CHAT)
        root.mkdir(parents=True, exist_ok=True)
        (root / "f1.txt").write_bytes(LOCAL)

        first = asyncio.run(takes.take(CHAT, chat))

        assert outage.refused, "the outage never reached the take"
        assert (first, folders.held(CHAT)) == (False, None)
        # A 503 is a passing fault: waited out, never put on the chat as a
        # refusal on its first try (``cloud.faults``).
        assert said == []

        outage.down = False
        assert asyncio.run(takes.take(CHAT, chat)) is False, "retried before its wait was up"
        clock.now += 2 * POLL * 2

        assert asyncio.run(takes.take(CHAT, chat)) is True
        assert folders.held(CHAT) is not None
        assert folders.push(CHAT) is not None, "the push did not land"
        assert _landed(person, tmp_path, drive_id, folder_id, "f1.txt") == LOCAL


# ---------------------------------------------------------------------------
# Beats that fail for a while
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lease_after",
    [
        pytest.param(None, id="the-lease-outlived-the-outage"),
        pytest.param("lapsed", id="the-lease-ran-out-during-it"),
        pytest.param("reaped", id="the-reaper-took-it-during-it"),
    ],
)
def test_beats_refused_by_an_outage_leave_the_folder_held_and_its_work_reaches_the_drive(
    backend: LiveBackend, tmp_path: Path, lease_after: str | None
) -> None:
    outage = Outage(("/lease/heartbeat",))
    outage.down = False
    with _client(backend) as person, _client(backend, outage) as box:
        drive_id, folder_id = _folder(person)
        folders = _folders(box, tmp_path)
        held = folders.take(CHAT, {"files_node_id": folder_id}, instance=f"box-7:{CHAT}")
        assert held is not None
        epoch = held.record.epoch

        outage.down = True
        for _ in range(3):
            assert folders.beat(CHAT) is True, "a beat the outage refused gave the folder up"
        # Each refused beat may be retried by the client; every refusal was a beat.
        assert len(outage.refused) >= 3
        assert set(outage.refused) == {"POST heartbeat"}
        (held.root / "f1.txt").write_bytes(LOCAL)
        if lease_after is not None:
            _age_lease(folder_id, reaped=lease_after == "reaped")

        outage.down = False
        assert folders.beat(CHAT) is True

        again = folders.held(CHAT)
        assert again is not None
        lease = _lease(folder_id)
        assert (lease.released_at, lease.reaped_at, lease.live) == (None, None, True)
        assert lease.epoch == again.record.epoch
        if lease_after is None:
            assert again.record.epoch == epoch, "a lease that never lapsed changed hands"
        assert folders.push(CHAT) is not None, "the push did not land"
        assert _landed(person, tmp_path, drive_id, folder_id, "f1.txt") == LOCAL


# ---------------------------------------------------------------------------
# A restart
# ---------------------------------------------------------------------------


def _returns(folders: ChatFolders, clock: Clock, serving: tuple[str, ...] = ()) -> FolderReturns:
    locks: dict[str, asyncio.Lock] = {}

    async def report(*_: Any, **__: Any) -> None:
        return None

    async def refuse(*_: Any) -> None:
        return None

    return FolderReturns(
        folders=folders,
        lock=lambda key: locks.setdefault(key, asyncio.Lock()),
        report=report,
        serving=lambda: serving,
        workspaces=WorkspaceHost(
            folders=folders, instance_of=lambda key: f"box-7:{key}", refuse=refuse, clock=clock
        ),
        clock=clock,
    )


def _left_by_an_earlier_life(
    backend: LiveBackend, tmp_path: Path, person: AlkeraClient
) -> tuple[str, str, str]:
    """A chat folder an earlier process held, whose agent wrote files that
    never left the box, and which that process kept on its way out. (A
    workspace's folder is held under the same records and settled the same
    way; a plain folder cannot be leased with the workspace purpose.)"""
    drive_id, folder_id = _folder(person)
    key = f"chat-{uuid.uuid4().hex}"
    with _client(backend) as box:
        earlier = _folders(box, tmp_path)
        held = earlier.take(key, {"files_node_id": folder_id}, instance=f"box-7:{key}")
        assert held is not None
        for index in range(3):
            path = held.root / "many" / f"f{index}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(LOCAL)
    return drive_id, folder_id, key


async def _settle(returns: FolderReturns, clock: Clock) -> list[str]:
    first = await returns.settle_left()
    assert first == [], "settled inside the grace a resuming chat is given"
    clock.now += LEFT_GRACE_SECONDS + 1
    return await returns.settle_left()


def test_a_folder_an_earlier_life_left_held_is_settled_and_its_files_reach_the_drive(
    backend: LiveBackend, tmp_path: Path
) -> None:
    with _client(backend) as person:
        drive_id, folder_id, key = _left_by_an_earlier_life(backend, tmp_path, person)
        assert _lease(folder_id).released_at is None

        with _client(backend) as box:
            folders = _folders(box, tmp_path)
            clock = Clock()
            settled = asyncio.run(_settle(_returns(folders, clock), clock))

            assert settled == [key]
            for index in range(3):
                landed = _landed(person, tmp_path, drive_id, folder_id, f"many/f{index}.txt")
                assert landed == LOCAL
            assert _lease(folder_id).released_at is not None
            assert folders.left_behind() == {}
            assert folders.held(key) is None


def test_a_folder_the_restarted_box_serves_again_is_left_to_its_chat(
    backend: LiveBackend, tmp_path: Path
) -> None:
    with _client(backend) as person:
        _drive_id, folder_id, key = _left_by_an_earlier_life(backend, tmp_path, person)

        with _client(backend) as box:
            folders = _folders(box, tmp_path)
            clock = Clock()
            settled = asyncio.run(_settle(_returns(folders, clock, serving=(key,)), clock))

            assert settled == []
            lease = _lease(folder_id)
            assert (lease.released_at, lease.live) == (None, True)
            assert list(folders.left_behind()) == [key]
