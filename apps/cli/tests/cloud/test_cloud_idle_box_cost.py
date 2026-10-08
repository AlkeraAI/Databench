"""What an idle chat costs the API: the regression net for a box's idle traffic.

One box running 34 idle chats made about 500 requests a minute — a folder beat
per chat every fifteen seconds, a transcript read per chat on every discovery
pass and an inbound ask per folder on every upkeep pass, twelve a minute for
each chat doing nothing. Production puts hundreds of chats on one machine, so
that cost grew past any per-address ceiling.

This drives the real service — every loop ``start()`` launches, on a clock the
test moves — with twenty idle chats for ten simulated minutes and counts every
request the box makes. What the box asks on a chat's behalf is counted where
it leaves: the rest client's routes, a transcript read per catch-up the service
asks a mirror for, one beat per per-lease beat or per batched beat, one inbound
ask per drain. Against a server that serves the stream and the batched beat an
idle chat must cost under one request a minute and the box's traffic must not
grow with its chats; against an older server the same box falls back to the
polling it did before, which is the "before" this net measures from.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from _mirror_service import FakeMirror, NoSocket
from alkera_cli.cloud.rest import STREAM_OPENED, CloudRestClient
from alkera_cli.cloud.service import CloudMirrorService, MirrorSettings
from alkera_cli.files.live_sync import KEEPALIVE_EVERY
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_core.project.directory import ProjectDirectory

MACHINE = "6a2c1b4d-3e1f-4b3c-8e6d-82b4d0a1c222"
#: How long the box is left idle, and how many chats it holds.
MINUTES = 10
#: The most an idle chat may cost, in requests a minute across everything.
TARGET_PER_CHAT_PER_MINUTE = 1.0


@pytest.fixture(autouse=True)
def _box_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    from alkera_cli.harness import prewarm
    from alkera_cli.host import paths

    home = tmp_path_factory.mktemp("alkera-home")
    monkeypatch.setenv("ALKERA_HOME", str(home))
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    monkeypatch.setenv("ALKERA_HARNESS_PREWARM", "0")
    prewarm._reset_for_tests()
    yield
    prewarm._reset_for_tests()


class Ticker:
    """The service's clock and its sleep, both moved by the test."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self._waiters: list[asyncio.Event] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        deadline = self.now + max(0.0, seconds)
        while self.now < deadline:
            wake = asyncio.Event()
            self._waiters.append(wake)
            await wake.wait()

    def advance(self, seconds: float) -> None:
        self.now += seconds
        waiters, self._waiters = self._waiters, []
        for wake in waiters:
            wake.set()


class Api(CloudRestClient):
    """The box's routes, each call counted by what it asks."""

    def __init__(self, chats: list[str], *, stream: bool) -> None:
        super().__init__(api_url="http://127.0.0.1:1", token="t", agent_id="machine:x")
        self.rows = {c: {"id": c, "last_seq": 7, "machine_id": MACHINE} for c in chats}
        self.stream = stream
        self.calls: Counter[str] = Counter()

    def for_agent(self, agent_id: str) -> CloudRestClient:
        return self

    async def list_chats(self, *, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        self.calls["GET /chats"] += 1
        return {"items": [dict(row) for row in self.rows.values()]}

    async def get_chat(self, chat_id: str) -> dict[str, Any]:
        self.calls["GET /chats/{id}"] += 1
        return dict(self.rows[chat_id])

    async def register_machine(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["POST /machines"] += 1
        return {"id": MACHINE}

    async def heartbeat_machine(self, machine_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls["POST /machines/{id}/heartbeat"] += 1
        return {}

    async def report_publisher_state(
        self,
        chat_id: str,
        *,
        state: str,
        reason: str = "",
        refusal_kind: str | None = None,
        ending: str | None = None,
    ) -> dict[str, Any]:
        self.calls["PUT /chats/{id}/publisher"] += 1
        return {}

    async def events(self, *, after: int | None = None) -> AsyncIterator[dict[str, Any]]:
        self.calls["GET /events"] += 1
        if self.stream:
            yield {"id": None, "type": STREAM_OPENED, "data": None}
        await asyncio.Event().wait()  # an idle org says nothing
        yield {}


class Mirror(FakeMirror):
    """A mirror whose catch-up is the transcript read the real one makes."""

    def __init__(self, chat_id: str, working_dir: Path, calls: Counter[str]) -> None:
        super().__init__(chat_id, working_dir)
        self._calls = calls

    def request_catch_up(self, *, last_seq: int | None = None) -> None:
        super().request_catch_up(last_seq=last_seq)
        self._calls["GET /chats/{id}/messages"] += 1


class Custody:
    """The chat folders the box holds, each streaming, every call counted as
    the request the real custody makes for it."""

    def fenced(self, key: str) -> bool:
        """No folder here is ever in doubt: the kernel fence has nothing to stop."""
        return False

    def __init__(self, calls: Counter[str], *, batch: bool) -> None:
        self.enabled = True
        self._calls = calls
        self._batch = batch
        self._batch_refused = False
        self._beats: dict[str, int] = {}
        self._keepalives: dict[str, int] = {}
        self._held: dict[str, Any] = {}

    def bind_machine(self, machine_id: str) -> None:
        return None

    def take(self, chat_id: str, chat: Any, *, instance: str) -> Any:
        calls = self._calls

        def pull_inbound() -> list[Any]:
            calls["GET /lease/live?inbound"] += 1
            return []

        held = SimpleNamespace(
            record=SimpleNamespace(node_id=f"node-{chat_id}", heartbeat_every=15.0),
            live=SimpleNamespace(pull_inbound=pull_inbound),
        )
        self._held[chat_id] = held
        return held

    def held(self, chat_id: str) -> Any:
        return self._held.get(chat_id)

    def live(self, chat_id: str, working_dir: Path) -> Any:
        return None

    def stop_live(self, chat_id: str, deadline: float = 5.0) -> None:
        return None

    def owed(self) -> list[str]:
        return []

    def push(self, chat_id: str) -> Any:
        self._calls["push"] += 1
        return None

    def beat(self, chat_id: str) -> bool:
        """The per-lease beat, and the live plane's keepalive it carries: an
        empty live batch once :data:`KEEPALIVE_EVERY` has passed, which at the
        beat's cadence is every other beat."""
        self._calls["POST /lease/heartbeat"] += 1
        self._beats[chat_id] = self._beats.get(chat_id, 0) + 1
        if self._beats[chat_id] * 15.0 >= KEEPALIVE_EVERY * (self._keepalives.get(chat_id, 0) + 1):
            self._keepalives[chat_id] = self._keepalives.get(chat_id, 0) + 1
            self._calls["POST /lease/live (keepalive)"] += 1
        return True

    def beat_all(self, chat_ids: list[str]) -> dict[str, bool] | None:
        """One request per pass, carrying the plane's word for every folder. A
        server without the route is asked once, as the real custody asks it."""
        if self._batch_refused:
            return None
        self._calls["POST /leases/heartbeat"] += 1
        if not self._batch:
            self._batch_refused = True
            return None
        return dict.fromkeys(chat_ids, True)

    def release(self, chat_id: str) -> bool:
        return self._held.pop(chat_id, None) is not None


class QuietSchema:
    pending: tuple[str, ...] = ()

    async def sync_once(self) -> bool:
        return False

    async def stop(self) -> None:
        return None


def _service(
    tmp_path: Path,
    api: Api,
    folders: Any,
    ticker: Ticker,
    *,
    mirror_calls: Counter[str] | None = None,
) -> CloudMirrorService:
    settings = MirrorSettings(
        api_url="http://127.0.0.1:1",
        token="t",
        project_dir=tmp_path,
        machine_name="idle box",
        provider_pod_id="pod-idle",
        machine_type_code="cpu3c",
    )
    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"),
        adapter_factory=FakeAdapterFactory(FakeAdapter, available=True),
    )
    calls = mirror_calls if mirror_calls is not None else api.calls

    def factory(chat_id: str, _chat: dict[str, Any]) -> Any:
        return Mirror(chat_id, tmp_path / ".alkera" / "chats" / chat_id / "scratch", calls)

    return CloudMirrorService(
        settings,
        runtime,
        rest=api,
        socket=NoSocket(api),
        mirror_factory=factory,
        schema_loader=QuietSchema(),  # type: ignore[arg-type]
        folders=folders,
        clock=ticker,
        sleep=ticker.sleep,
    )


async def _run(ticker: Ticker, seconds: int) -> None:
    """``seconds`` of the box's life, one second at a time, each settled."""
    for _ in range(seconds):
        for _ in range(100):
            await asyncio.sleep(0)
        ticker.advance(1.0)


class Handoffs:
    """The work the service hands to a thread, counted while it runs.

    A folder's take, its live start, a push, every lease beat: each runs on a
    real thread, which the clock the test moves knows nothing of. Moving that
    clock while a take is still on its thread lets a loaded machine fall behind
    a fast one — the same minute holds every chat on one host and one on
    another. A beat still on its thread when the clock moves is worse: the beat
    loop sleeps its fifteen seconds from the second the pass finished in, so
    every late pass stretches the cadence by a second and the window holds
    fewer rounds. The clock the rig drives moves only once nothing handed off
    is still running.

    Counted at the loop's ``run_in_executor``, the one door every handoff
    takes: ``asyncio.to_thread`` goes through it, and so do the beats, which
    run on a pool of their own.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.running = 0
        #: Moves on every handoff and every return, so a quiet stretch of the
        #: loop is one in which nothing was handed off and nothing came back.
        self.moves = 0
        loop = asyncio.get_running_loop()
        original = loop.run_in_executor

        def done(_future: asyncio.Future[Any]) -> None:
            self.running -= 1
            self.moves += 1

        def run_in_executor(executor: Any, func: Any, /, *args: Any) -> asyncio.Future[Any]:
            future = original(executor, func, *args)
            self.running += 1
            self.moves += 1
            future.add_done_callback(done)
            return future

        monkeypatch.setattr(loop, "run_in_executor", run_in_executor)

    async def settled(self) -> None:
        """Turn the loop until no handed-off work is running and the work that
        came back has run on to its next wait.

        Nothing running is not enough on its own: a beat that returns on the
        last of a stretch of turns leaves its pass still to gather and go back
        to sleep, and a clock moved first makes that sleep start a second late.
        So the stretch that ends the wait is one in which nothing was handed
        off and nothing came back. The wait is real and generous; what it
        bounds is a hang, never how fast anything is."""
        deadline = time.monotonic() + HANDOFF_BOUND
        while True:
            before = self.moves
            for _ in range(100):
                await asyncio.sleep(0)
            if not self.running and self.moves == before:
                return
            assert time.monotonic() < deadline, "work handed to a thread never finished"
            await asyncio.sleep(0.001)


#: The longest one step's handed-off work may take before the rig calls it hung.
HANDOFF_BOUND = 30.0


async def _run_settled(ticker: Ticker, seconds: int, handoffs: Handoffs) -> None:
    """``seconds`` of the box's life, one second at a time, each second's
    threaded work finished before the next second starts."""
    for _ in range(seconds):
        await handoffs.settled()
        ticker.advance(1.0)
    await handoffs.settled()


async def _idle_box(
    tmp_path: Path, chats: int, handoffs: Handoffs, *, new_server: bool
) -> Counter[str]:
    """Every request a box holding ``chats`` idle chats makes in the window,
    after the minute it takes to settle in. The clock moves only once nothing
    handed to a thread is still running, so a loaded host counts the same
    beats as a fast one."""
    ticker = Ticker()
    names = [f"chat-{n:02d}" for n in range(chats)]
    api = Api(names, stream=new_server)
    calls = api.calls
    custody = Custody(calls, batch=new_server)
    service = _service(tmp_path, api, custody, ticker, mirror_calls=calls)

    await service.start()
    try:
        await _run_settled(ticker, 60, handoffs)
        assert len(service.mirrors) == chats, "every chat is served before the window opens"
        calls.clear()
        await _run_settled(ticker, MINUTES * 60, handoffs)
        return Counter(calls)
    finally:
        for task in service._tasks:
            task.cancel()
        await asyncio.gather(*service._tasks, return_exceptions=True)


def _per_chat_per_minute(calls: Counter[str], chats: int) -> float:
    return sum(calls.values()) / chats / MINUTES


async def test_an_idle_chat_costs_under_one_request_a_minute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = await _idle_box(tmp_path / "twenty", 20, Handoffs(monkeypatch), new_server=True)

    cost = _per_chat_per_minute(calls, 20)
    assert cost < TARGET_PER_CHAT_PER_MINUTE, (
        f"{cost:.2f} requests per idle chat per minute: {calls}"
    )
    for per_chat in (
        "GET /chats/{id}/messages",
        "GET /lease/live?inbound",
        "POST /lease/heartbeat",
        "GET /chats/{id}",
        "push",
    ):
        assert calls[per_chat] == 0, f"an idle chat made {per_chat} on a timer: {calls}"


async def test_an_idle_box_makes_the_same_requests_for_five_chats_as_for_twenty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoffs = Handoffs(monkeypatch)
    few = await _idle_box(tmp_path / "five", 5, handoffs, new_server=True)
    many = await _idle_box(tmp_path / "twenty", 20, handoffs, new_server=True)

    # The same routes, each the same number of times: the clock moves only once
    # every beat has come back, so both boxes beat on the same seconds.
    assert many == few, (few, many)


async def test_against_an_older_server_the_box_polls_as_it_did(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback, and the figure the idle cost is measured down from."""
    calls = await _idle_box(tmp_path / "old", 20, Handoffs(monkeypatch), new_server=False)

    cost = _per_chat_per_minute(calls, 20)
    assert cost > 10.0, f"{cost:.2f}: {calls}"
    # One beat per lease every 15 s: forty rounds in the window, exactly — the
    # clock moves only once a pass's beats have come back and the pass has gone
    # back to sleep, so a slow host beats on the same seconds as a fast one.
    assert calls["POST /lease/heartbeat"] == MINUTES * 4 * 20, calls
    assert calls["POST /leases/heartbeat"] == 0, "the refused batch is not asked again"


# ---------------------------------------------------------------------------
# The folders themselves: the real custody, every drive read counted
# ---------------------------------------------------------------------------

DRIVE = "11111111-1111-1111-1111-111111111111"


class CountingFiles:
    """The Files namespace the custody reads through: every read is counted,
    the children listing and the item reads by folder."""

    def __init__(self, calls: Counter[str]) -> None:
        self._calls = calls

    def drive(self) -> dict[str, Any]:
        self._calls["GET /files/drives"] += 1
        return {"id": DRIVE, "rootId": "root"}

    def item(self, drive_id: str, item_id: str, *, select: str | None = None) -> dict[str, Any]:
        self._calls["GET /files/items/{n}"] += 1
        return {
            "id": item_id,
            "etag": "7",
            "kind": "folder",
            "name": f"{item_id}.alkerachat",
            "pathBytes": f"/home/ana/Chats/{item_id}.alkerachat",
        }

    def item_by_path(self, drive_id: str, item_path: str) -> dict[str, Any]:
        self._calls["GET /files/root:/{path}"] += 1
        return {"id": item_path, "etag": "7", "kind": "folder"}

    def children(self, drive_id: str, item_id: str, **_kwargs: Any) -> Any:
        self._calls["GET /files/items/{n}/children"] += 1
        return iter(())


class LeaseWire:
    """The lease routes the custody speaks on the wire, every call counted."""

    def __init__(self, calls: Counter[str], *, batch: bool) -> None:
        self._calls = calls
        self._batch = batch
        self._epochs: dict[str, int] = {}

    def _grant(self, node: str) -> dict[str, Any]:
        return {
            "epoch": self._epochs.get(node, 5),
            "expiresAt": "2099-01-01T00:00:00+00:00",
            "heartbeatEvery": 15.0,
            "syncInterval": 5.0,
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        node = path.split("/items/")[1].split("/")[0] if "/items/" in path else ""
        if path.endswith("/leases/heartbeat"):
            self._calls["POST /leases/heartbeat"] += 1
            if not self._batch:
                return httpx.Response(404, json={})
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "leases": [
                        {
                            "nodeId": e["nodeId"],
                            "verdict": "renewed",
                            "grant": self._grant(e["nodeId"]),
                        }
                        for e in body["leases"]
                    ]
                },
            )
        if path.endswith("/lease/heartbeat"):
            self._calls["POST /lease/heartbeat"] += 1
            return httpx.Response(200, json=self._grant(node))
        if path.endswith("/lease"):
            self._calls["POST /lease (take)"] += 1
            self._epochs[node] = self._epochs.get(node, 4) + 1
            return httpx.Response(200, json=self._grant(node))
        self._calls[f"{request.method} {path}"] += 1
        return httpx.Response(404, json={})


async def _idle_box_holding_folders(
    tmp_path: Path, chats: int, handoffs: Handoffs, *, new_server: bool
) -> tuple[Counter[str], Counter[str]]:
    """The box of :func:`_idle_box`, its chats' folders held by the real
    custody over the real mount chain. Answers what the take cost and what
    the idle window cost."""
    from alkera_cli.cloud.folder import ChatFolders

    ticker = Ticker()
    names = [f"chat-{n:02d}" for n in range(chats)]
    api = Api(names, stream=new_server)
    for name in names:
        api.rows[name]["files_node_id"] = f"node-{name}"
    calls = api.calls
    folders = ChatFolders(
        chats_root=tmp_path / ".alkera" / "chats",
        files=CountingFiles(calls),  # type: ignore[arg-type]
        http=httpx.Client(
            transport=httpx.MockTransport(LeaseWire(calls, batch=new_server)),
            base_url="http://files.test",
        ),
        machine_id=MACHINE,
        home=tmp_path / "home",
    )
    service = _service(tmp_path, api, folders, ticker)
    await service.start()
    try:
        await _run_settled(ticker, 60, handoffs)
        assert len(service.mirrors) == chats, "every chat is served before the window opens"
        assert all(folders.held(name) is not None for name in names)
        taken = Counter(calls)
        calls.clear()
        await _run_settled(ticker, MINUTES * 60, handoffs)
        return taken, Counter(calls)
    finally:
        for task in service._tasks:
            task.cancel()
        await asyncio.gather(*service._tasks, return_exceptions=True)


#: How many folders the real custody holds for the idle window. Every assertion
#: below is per folder, so more folders buy no more coverage, only more disk:
#: the batched beat saves each folder's lease record (an fsync'd atomic replace)
#: one after another on one thread, and the rig waits for every beat before the
#: clock moves. At ten folders the window's 44 beats were 440 serialized fsyncs,
#: 20-40 s on a Windows runner and past the 90 s test timeout on a loaded one.
HELD_FOLDERS = 3

#: The drive reads a held folder can cost: a listing, or an item read.
_DRIVE_READS = (
    "GET /files/items/{n}/children",
    "GET /files/items/{n}",
    "GET /files/root:/{path}",
    "GET /files/drives",
)


@pytest.mark.parametrize(
    "new_server",
    [pytest.param(True, id="current-server"), pytest.param(False, id="older-server")],
)
async def test_an_idle_held_folder_lists_and_reads_nothing_on_the_drive(
    tmp_path: Path, new_server: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The listings and item reads a held folder costs are the take's, once;
    idle, it costs none of either — against a current server or an older one."""
    taken, idle = await _idle_box_holding_folders(
        tmp_path, HELD_FOLDERS, Handoffs(monkeypatch), new_server=new_server
    )

    assert taken["GET /files/items/{n}/children"] == HELD_FOLDERS, (
        "one listing per folder, at its take"
    )
    for read in _DRIVE_READS:
        assert idle[read] == 0, f"an idle folder made {read}: {idle}"
    if new_server:
        assert idle["POST /lease/heartbeat"] == 0, "a folder was beaten on its own"
