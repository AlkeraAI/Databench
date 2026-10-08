"""The live-editing acceptance run, against a real backend and a real box sync.

People type into a file while the agent edits it on the box, and the box
restarts in the middle. Every keystroke the server acknowledged and every
edit the agent made must end up in the file exactly once: in the live
document, on the drive and on the box's disk alike, with no conflicted copy
anywhere. The people are Loro documents on the gateway's socket, as a browser
is; the box is the CLI's own chat-folder sync, holding the folder's lease.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import statistics
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import websockets
from alkera_cli.cloud.folder import ChatFolders
from alkera_sdk import AlkeraClient
from files._live_backend import LiveBackend
from files._live_typist import Typist
from files._restartable_backend import RestartableBackend, restartable_backend

pytestmark = [pytest.mark.spread]

NAME = "plan.txt"
PLAN = "".join(f"{n}. item {n}\n" for n in range(1, 13))


@pytest.fixture
def backend(tmp_path: Path) -> Iterator[RestartableBackend]:
    with restartable_backend(tmp_path / "server") as running:
        yield running


@pytest.fixture
def client(backend: LiveBackend) -> Iterator[AlkeraClient]:
    with AlkeraClient(base_url=backend.base_url, token=backend.token, timeout=60.0) as api:
        yield api


def _drive_text(backend: LiveBackend, drive_id: str, node_id: str) -> str:
    answer = httpx.get(
        f"{backend.base_url}/api/v1/files/drives/{drive_id}/items/{node_id}/content",
        headers={"Authorization": f"Bearer {backend.token}"},
        follow_redirects=True,
        timeout=30.0,
    )
    answer.raise_for_status()
    return answer.text


async def _document_text(backend: LiveBackend, node_id: str) -> str:
    reader = Typist(backend, node_id, 0, "z")
    await reader._connect()
    try:
        assert reader.doc is not None
        return str(reader.doc.get_text("content").to_string())
    finally:
        await reader.ws.close()


class Box:
    """The box: the CLI's chat-folder sync holding the folder, taking what
    the drive sends as the service does when told of it, and restartable."""

    def __init__(
        self, client: AlkeraClient, root: Path, home: Path, chat_id: str, node_id: str
    ) -> None:
        self.client = client
        self.root = root
        self.home = home
        self.chat_id = chat_id
        self.node_id = node_id
        self.folders: ChatFolders | None = None
        self.stop = threading.Event()
        self.drainer: threading.Thread | None = None
        self.generation = 0

    def start(self) -> Path:
        self.generation += 1
        self.folders = ChatFolders(
            chats_root=self.root,
            files=self.client.files,
            http=self.client.raw_client.get_httpx_client(),
            home=self.home,
        )
        held = self.folders.take(
            self.chat_id, {"files_node_id": self.node_id}, instance=f"box:{self.chat_id}"
        )
        assert held is not None
        working = held.root / "scratch"
        working.mkdir(exist_ok=True)
        assert self.folders.live(self.chat_id, working) is not None
        self.stop = threading.Event()
        self.drainer = threading.Thread(target=self._drain, daemon=True)
        self.drainer.start()
        return working

    def _drain(self) -> None:
        """What the service does for a held chat: take what the drive sends,
        and beat for the folder on the lease's cadence. Without the beats the
        live plane's own fence closes a few seconds after each take and the
        box stops sending anything until it is restarted."""
        folders, stop = self.folders, self.stop
        last_beat = time.monotonic()
        while not stop.is_set():
            held = folders.held(self.chat_id) if folders is not None else None
            if held is not None and held.live is not None:
                with contextlib.suppress(Exception):
                    held.live.pull_inbound()
            if (
                folders is not None
                and held is not None
                and time.monotonic() - last_beat >= held.record.heartbeat_every / 2
            ):
                last_beat = time.monotonic()
                with contextlib.suppress(Exception):
                    folders.beat(self.chat_id)
            stop.wait(0.3)

    def crash(self) -> None:
        """The box process dies: its sync stops where it is, nothing handed
        back, the lease still held."""
        self.stop.set()
        if self.drainer is not None:
            self.drainer.join(timeout=10)
        if self.folders is not None:
            self.folders.stop_live(self.chat_id, deadline=0.0)
        self.folders = None


def _agent(root_of: Any, stop: threading.Event, written: list[str], at: dict[str, float]) -> None:
    """The agent's edit tool: read the whole file, change it, write it whole.
    ``at`` records when each line was written (monotonic)."""
    count = 0
    while not stop.is_set():
        stop.wait(1.3)
        path = root_of() / NAME
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        token = f"agent{count}"
        lines = text.split("\n")
        lines.insert(min(6, len(lines)), token)
        temporary = path.with_name(f".{NAME}.{token}.tmp")
        temporary.write_text("\n".join(lines), encoding="utf-8")
        temporary.replace(path)
        at[token] = time.monotonic()
        written.append(token)
        count += 1


#: ``ALKERA_LIVE_CHAOS_KEEP`` names a directory each run leaves its evidence
#: in: the three copies last compared, the drive's version list and what each
#: typist had acknowledged, so a failed soak can be read after the fact.
KEEP = os.environ.get("ALKERA_LIVE_CHAOS_KEEP", "")


def _keep(
    backend: LiveBackend,
    drive_id: str,
    node_id: str,
    last: dict[str, str],
    typists: list[Typist],
) -> None:
    if not KEEP:
        return
    where = Path(KEEP) / time.strftime("%Y%m%dT%H%M%S")
    where.mkdir(parents=True, exist_ok=True)
    for name, text in last.items():
        (where / f"{name}.txt").write_text(text, encoding="utf-8")
    for typist in typists:
        (where / f"acked-{typist.mark}.txt").write_text("\n".join(typist.acked), encoding="utf-8")
    with contextlib.suppress(httpx.HTTPError, ValueError):
        versions = httpx.get(
            f"{backend.base_url}/api/v1/files/drives/{drive_id}/items/{node_id}/versions",
            headers={"Authorization": f"Bearer {backend.token}"},
            timeout=30.0,
        ).json()
        (where / "versions.json").write_text(json.dumps(versions, indent=1), encoding="utf-8")


#: ``ALKERA_LIVE_CHAOS_SOAK`` (seconds) runs the soak instead of skipping it.
SOAK_SECONDS = float(os.environ.get("ALKERA_LIVE_CHAOS_SOAK", "0") or 0)


@pytest.mark.timeout(240)
def test_people_and_the_agent_lose_nothing_through_box_and_backend_restarts(
    client: AlkeraClient,
    backend: RestartableBackend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """About fifteen seconds of typing, one box crash and one backend crash."""
    _chaos(
        client,
        backend,
        tmp_path,
        monkeypatch,
        seconds=15.0,
        every=6.0,
    )


@pytest.mark.skipif(not SOAK_SECONDS, reason="the soak runs when ALKERA_LIVE_CHAOS_SOAK is set")
@pytest.mark.timeout(0)
def test_the_chaos_run_soaks_with_restarts_all_through(
    client: AlkeraClient,
    backend: RestartableBackend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same run for ``ALKERA_LIVE_CHAOS_SOAK`` seconds, the box and the
    backend each crashing every half minute or so."""
    _chaos(
        client,
        backend,
        tmp_path,
        monkeypatch,
        seconds=SOAK_SECONDS,
        every=30.0,
    )


def _chaos(
    client: AlkeraClient,
    backend: RestartableBackend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    seconds: float,
    every: float,
) -> None:
    from alkera_core.schemas.objects.specs import ChatModelPin
    from backend.services.chats import catalog as chat_catalog

    async def pinned(*_args: Any, **_kwargs: Any) -> ChatModelPin:
        # No gateway runs beside this backend; the chat is only a folder here.
        return ChatModelPin(id="test-model", display_name="Test", wire="anthropic")

    monkeypatch.setattr(chat_catalog, "default_pin_for", pinned)
    drive = client.files.drive()
    drive_id = str(drive["id"])
    http = client.raw_client.get_httpx_client()
    made = http.post("/api/v1/chats", json={"title": "chaos"})
    assert made.status_code == 201, made.text
    chat = made.json()
    chat_node = str(chat["files_node_id"])
    scratch = next(
        row for row in client.files.children(drive_id, chat_node) if row["name"] == "scratch"
    )
    box = Box(client, tmp_path / "box" / "chats", tmp_path / "home", str(chat["id"]), chat_node)
    root = box.start()
    (root / NAME).write_text(PLAN, encoding="utf-8")
    node_id = ""
    for _ in range(100):
        listed = {row["name"]: row for row in client.files.children(drive_id, str(scratch["id"]))}
        if NAME in listed:
            node_id = str(listed[NAME]["id"])
            break
        time.sleep(0.2)
    assert node_id, "the box never sent the file up"
    landed = ""
    for _ in range(150):
        with contextlib.suppress(httpx.HTTPStatusError):
            landed = _drive_text(backend, drive_id, node_id)
        if landed == PLAN:
            break
        time.sleep(0.2)
    assert landed == PLAN, "the file's bytes never landed"

    agent_written: list[str] = []
    agent_at: dict[str, float] = {}
    agent_stop = threading.Event()
    agent = threading.Thread(
        target=_agent, args=(lambda: root, agent_stop, agent_written, agent_at), daemon=True
    )

    async def scenario() -> tuple[list[Typist], str]:
        typists = [Typist(backend, node_id, 0, "a"), Typist(backend, node_id, 11, "b")]
        stop = asyncio.Event()
        running = [asyncio.create_task(t.run(stop)) for t in typists]
        agent.start()
        loop = asyncio.get_running_loop()
        ends = loop.time() + seconds
        turn = 0
        while loop.time() < ends:
            await asyncio.sleep(min(every / 2, max(0.0, ends - loop.time())))
            if loop.time() >= ends:
                break
            if turn % 2 == 0:
                await asyncio.to_thread(box.crash)
                await asyncio.sleep(2.0)
                await asyncio.to_thread(box.start)
            else:
                await asyncio.to_thread(backend.crash)
            turn += 1
        stop.set()
        agent_stop.set()
        await asyncio.gather(*running)
        await asyncio.to_thread(agent.join, 10)
        settled = ""
        last: dict[str, str] = {}
        for _ in range(60):
            await asyncio.sleep(2.0)
            try:
                document = await _document_text(backend, node_id)
                on_drive = await asyncio.to_thread(_drive_text, backend, drive_id, node_id)
            except (OSError, TimeoutError, websockets.WebSocketException, httpx.HTTPError):
                continue  # a loaded machine; the three are compared again
            disk = (root / NAME).read_text(encoding="utf-8")
            last.update(document=document, disk=disk, drive=on_drive)
            if document == disk == on_drive:
                settled = document
                break
        _keep(backend, drive_id, node_id, last, typists)
        return typists, settled

    typists, settled = asyncio.run(scenario())
    box.crash()
    assert settled, "the document, the drive and the box never agreed"
    (tmp_path / "settled.txt").write_text(settled, encoding="utf-8")
    words = settled.split()
    wrong = {
        who: {token: words.count(token) for token in tokens if words.count(token) != 1}
        for who, tokens in [
            *((typist.mark, typist.acked) for typist in typists),
            ("agent", agent_written),
        ]
    }
    # The file's own lines too, typed into or not: each is in it once. A
    # line a person typed into must never come back beside its older copy.
    wrong["plan"] = {
        line: count
        for line in PLAN.splitlines()
        if (count := len(re.findall(rf"^{re.escape(line)}(?: |$)", settled, re.MULTILINE))) != 1
    }
    assert all(typist.acked for typist in typists) and agent_written
    assert wrong == {who: {} for who in wrong}, (wrong, str(tmp_path / "settled.txt"))
    # How long an agent's edit took to reach a person's open document, from
    # the write on the box's disk to the line in the tab's copy: printed, never
    # asserted. It is a number about the machine the run lands on, and the
    # restarts (the box down for seconds, the API starting cold) are in it.
    # What the run guards is above: every edit lands once and the three agree.
    latencies = sorted(
        typists[0].seen_at[token] - agent_at[token]
        for token in agent_written
        if token in typists[0].seen_at and token in agent_at
    )
    if latencies:
        print(
            f"agent edit to open document: n={len(latencies)} "
            f"median={statistics.median(latencies):.2f}s "
            f"p90={latencies[int(len(latencies) * 0.9)]:.2f}s "
            f"p95={latencies[int(len(latencies) * 0.95)]:.2f}s max={latencies[-1]:.2f}s"
        )
    else:
        print("agent edit to open document: no line arrived while the tabs were open")
    names = [row["name"] for row in client.files.children(drive_id, str(scratch["id"]))]
    assert [n for n in names if "conflicted copy" in n] == []
    assert [p.name for p in root.iterdir() if "conflicted copy" in p.name] == []
