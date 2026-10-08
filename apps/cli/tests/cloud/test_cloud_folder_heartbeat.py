"""A box keeps the chat folders it is holding, whatever else it is slow at.

A chat folder is leased, the lease dies sixty seconds after its last heartbeat,
and a lease that dies under a running turn takes the chat's mirror with it: the
next box to open the chat finds an interrupted turn and tells the reader "the
workspace restarted while it was answering". A live box said that to six
readers at once, every three minutes, while its daemon ran undisturbed the
whole time — one pid, no restart, six healthy chats.

What actually happened is that the beats were the LAST thing on the discovery
pass. Thirty-four other chats on that box had folders whose take could not
resolve the Files content origin the backend advertises, each costing a DNS
timeout, each retried on every pass; and a folder whose turn had written a
runtime's worth of files pushed them one upload at a time between two of the
beats. So the six folders the box really was holding went minutes between
heartbeats and their leases lapsed.

Everything below drives the real service — the real poll pass, the real beat
task, the real backoff — against custody that really blocks, on a clock the
test moves rather than one it waits on. The claim in every case is the same
one: **a beat for every held folder lands inside its interval, no matter what
else the box is stuck on.**
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from _mirror_service import FakeMirror, NoSocket
from alkera_cli.cloud import service as service_module
from alkera_cli.cloud.faults import TRANSIENT_LIMIT
from alkera_cli.cloud.folder import FolderBusyError
from alkera_cli.cloud.folder_takes import TAKE_FAILURE_BACKOFF_CAP_SECONDS
from alkera_cli.cloud.limits import (
    ENV_CHAT_LIST_PAGES,
    ENV_FOLDER_BEAT_FLOOR_SECONDS,
    ENV_FOLDER_BEAT_SECONDS,
    ENV_FOLDER_BEAT_TIMEOUT_SECONDS,
)
from alkera_cli.cloud.rest import CloudRestClient
from alkera_cli.cloud.service import CloudMirrorService, MirrorSettings
from alkera_cli.files.push import PushSummary
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_core.project.directory import ProjectDirectory

MACHINE = "6a2c1b4d-3e1f-4b3c-8e6d-82b4d0a1c222"
#: Where a take that could not reach the drive is said.
TAKES_LOGGER = "alkera_cli.cloud.folder_takes"
HELD = ["chat-a", "chat-b", "chat-c"]
SLOW = "chat-d"

#: The cadence the lease grants, and so the interval the box must beat inside.
HEARTBEAT_EVERY = 5.0
#: The discovery pass's own interval — deliberately longer than the cadence
#: above, which is the whole point: the beats cannot be a step of that pass.
POLL_INTERVAL = 15.0

#: The content origin the backend advertised that the box could not resolve.
#: With a query that looks like the signed token a real content URL carries, so
#: "the line names the host and not the URL" is a claim the log can be held to.
CONTENT_URL = "http://files.localhost:20840/c/chat-a/report.html?sig=hunter2-signed-token"
SECRET = "hunter2-signed-token"

#: How long a blocked call waits to be let go before it gives up on the test.
#: Only ever reached when a test has already failed some other way; it exists
#: so a broken run ends rather than hangs.
BLOCK_CEILING = 30.0
#: How long a test waits for something it expects to happen.
SETTLE = 10.0

#: The name ``start()`` gives the task that keeps the leases. The rounds below
#: wait for THIS task to be asleep before moving the clock — see ``Ticker``.
BEAT_TASK = "cloud-mirror-folder-beat"
#: Heartbeat intervals each test drives, after the pass the loop runs on start.
ROUNDS = 3


@pytest.fixture(autouse=True)
def _box_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """An Alkera home of this test's own, and no agent pre-warm.

    A service start that fired the box's pre-warm would stage the bundled
    binary into the per-user cache and spawn a real opencode, in a directory
    every worker on this machine shares.
    """
    from alkera_cli.harness import prewarm
    from alkera_cli.host import paths

    home = tmp_path_factory.mktemp("alkera-home")
    monkeypatch.setenv("ALKERA_HOME", str(home))
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    monkeypatch.setenv("ALKERA_HARNESS_PREWARM", "0")
    prewarm._reset_for_tests()
    yield
    prewarm._reset_for_tests()


# --------------------------------------------------------------------------- #
# time the test states rather than waits for
# --------------------------------------------------------------------------- #


class Ticker:
    """The service's clock AND its sleep, both moved by the test.

    A sleep returns once the clock the test moves has passed its deadline, so
    "a heartbeat interval went by" is a fact this file states — never a real
    wait, and never a number that measures how loaded the machine is. The
    blocking below is real threads, so the loop keeps turning while a sleeper
    waits and a beat scheduled for this interval can still land.

    ``parked`` is the other half, and it is what makes a round deterministic
    rather than lucky. A beat is recorded inside the pass, BEFORE the loop that
    ran it gets back to its sleep — so a test that moved the clock the moment
    it saw the count rise could move it while the loop was between two sleeps.
    The loop would then compute its deadline off the clock as already advanced,
    a full interval further out than the test believes, and neither side would
    ever move again: the loop waits for a clock the test only moves after a
    beat, and the beat only comes when the clock moves. Every round below waits
    for the loop it is driving to be IN here first.
    """

    def __init__(self) -> None:
        self.now = 1_000.0
        #: The deadline each named task is asleep until, right now.
        self.parked: dict[str, float] = {}
        #: One event per sleeper, taken immediately before it waits. Per
        #: sleeper rather than one shared event the advance replaces: a single
        #: event means a sleeper that arrives between two advances waits on one
        #: nobody will ever set.
        self._waiters: list[asyncio.Event] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        deadline = self.now + max(0.0, seconds)
        task = asyncio.current_task()
        who = task.get_name() if task is not None else "?"
        try:
            while self.now < deadline:
                wake = asyncio.Event()
                self._waiters.append(wake)
                self.parked[who] = deadline
                await wake.wait()
        finally:
            self.parked.pop(who, None)

    def advance(self, seconds: float) -> None:
        self.now += seconds
        waiters, self._waiters = self._waiters, []
        for wake in waiters:
            wake.set()


async def _until(predicate: Callable[[], bool], what: str, *, within: float = SETTLE) -> None:
    """Turn the loop until ``predicate`` holds. The wait is real and generous;
    what it bounds is a failure, never a claim about how fast anything is."""
    deadline = time.monotonic() + within
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.001)


async def _settle(turns: int = 40) -> None:
    """Give everything that could run a chance to, so "it did NOT happen" is
    an assertion about the code rather than about scheduling luck."""
    for _ in range(turns):
        await asyncio.sleep(0.001)


async def _beaten_again(folders: BlockingFolders, ticker: Ticker) -> None:
    """One heartbeat interval goes by; every held folder must be beaten in it.

    The clock moves only once the beat loop is asleep, and only by exactly what
    it asked to be woken after — so what the round proves is a beat the loop was
    waiting to take, and not a pass that happened to be in flight anyway. The
    deadline it is parked on is asserted too: a loop that fell back to the
    default cadence while a take was in flight would be waiting three times as
    long as the lease it holds asked for, and would still look alive.
    """
    await _until(lambda: BEAT_TASK in ticker.parked, f"{BEAT_TASK} to be asleep on its cadence")
    assert ticker.parked[BEAT_TASK] == pytest.approx(ticker.now + HEARTBEAT_EVERY), (
        "the beat loop is waiting on something other than the cadence its leases were granted"
    )
    before = folders.beats_for(HELD)
    ticker.advance(HEARTBEAT_EVERY)
    await _until(
        lambda: all(folders.beats.get(chat_id, 0) > before[chat_id] for chat_id in HELD),
        f"every held folder to be beaten once more than {before}",
    )


# --------------------------------------------------------------------------- #
# custody that really blocks
# --------------------------------------------------------------------------- #


class BlockingFolders:
    """Custody as the SERVICE drives it, with a stopwatch on the beats.

    Not a call recorder: a take, a beat, a push or a hand-back armed with
    ``block`` holds its thread exactly as the mount chain's would, and what the
    tests assert is how many beats LANDED per chat while it did.
    """

    def fenced(self, key: str) -> bool:
        """No folder here is ever in doubt: the kernel fence has nothing to stop."""
        return False

    def __init__(self, *, heartbeat_every: float = HEARTBEAT_EVERY) -> None:
        self.enabled = True
        self.heartbeat_every = heartbeat_every
        self.machine: str | None = None
        self.takes: list[str] = []
        self.order: list[str] = []
        self.beats: dict[str, int] = {}
        self.pushes: dict[str, int] = {}
        self.handed_back: list[str] = []
        self.released: list[str] = []
        #: What a chat's take raises instead of granting a lease.
        self.unreachable: dict[str, BaseException] = {}
        #: Chats whose take meets the drive and is told another box holds it.
        self.busy: set[str] = set()
        self._held: dict[str, Any] = {}
        self._gates: dict[str, threading.Event] = {}
        self._entered: dict[str, threading.Event] = {}

    # -- what the test arms ------------------------------------------------

    def block(self, verb: str, chat_id: str) -> None:
        """``verb`` is ``take``/``beat``/``push``/``hand_back``: that call for
        that chat enters, says so, and holds its thread until ``unblock``."""
        key = f"{verb}:{chat_id}"
        self._gates[key] = threading.Event()
        self._entered[key] = threading.Event()

    def entered(self, verb: str, chat_id: str) -> bool:
        gate = self._entered.get(f"{verb}:{chat_id}")
        return gate is not None and gate.is_set()

    def unblock_all(self) -> None:
        for gate in self._gates.values():
            gate.set()

    def _gate(self, verb: str, chat_id: str) -> None:
        key = f"{verb}:{chat_id}"
        gate = self._gates.get(key)
        if gate is None:
            return
        self._entered[key].set()
        assert gate.wait(timeout=BLOCK_CEILING), f"{key} was never let go"

    def beats_for(self, chats: list[str]) -> dict[str, int]:
        return {chat_id: self.beats.get(chat_id, 0) for chat_id in chats}

    # -- the custody surface the service uses -------------------------------

    def bind_machine(self, machine_id: str) -> None:
        self.machine = machine_id

    def held(self, chat_id: str) -> Any:
        return self._held.get(chat_id)

    def take(self, chat_id: str, chat: Any, *, instance: str) -> Any:
        self.takes.append(chat_id)
        self.order.append(f"take:{chat_id}")
        self._gate("take", chat_id)
        failure = self.unreachable.get(chat_id)
        if failure is not None:
            raise failure
        if chat_id in self.busy:
            raise FolderBusyError(chat_id, holder="box-8")
        held = SimpleNamespace(
            record=SimpleNamespace(node_id=f"node-{chat_id}", heartbeat_every=self.heartbeat_every),
            live=None,
        )
        self._held[chat_id] = held
        return held

    def live(self, chat_id: str, working_dir: Path) -> Any:
        held = self._held.get(chat_id)
        if held is None:
            return None
        held.live = SimpleNamespace(pull_inbound=lambda: [], root=working_dir)
        return held.live

    def stop_live(self, chat_id: str, deadline: float = 5.0) -> None:
        return None

    def owed(self) -> list[str]:
        return []

    def beat(self, chat_id: str) -> bool:
        self.order.append(f"beat:{chat_id}")
        self._gate("beat", chat_id)
        self.beats[chat_id] = self.beats.get(chat_id, 0) + 1
        self.order.append(f"beat-done:{chat_id}")
        return chat_id in self._held

    def push(self, chat_id: str) -> Any:
        if chat_id not in self._held:
            return None
        self.order.append(f"push:{chat_id}")
        self._gate("push", chat_id)
        self.pushes[chat_id] = self.pushes.get(chat_id, 0) + 1
        self.order.append(f"push-done:{chat_id}")
        return PushSummary()

    def hand_back(
        self, chat_id: str, *, recover: bool = True, ending: str | None = None, gone: bool = False
    ) -> Any:
        self._gate("hand_back", chat_id)
        self._held.pop(chat_id, None)
        self.handed_back.append(chat_id)
        self.order.append(f"hand-back:{chat_id}")
        return None

    def release(self, chat_id: str) -> bool:
        if self._held.pop(chat_id, None) is None:
            return False
        self.released.append(chat_id)
        self.order.append(f"release:{chat_id}")
        return True


class StubRest(CloudRestClient):
    """The routes the box's loops speak, answered in process.

    Nothing here is the subject — the chat list exists so the REAL discovery
    pass has chats to reconcile, and the stream parks so the box's other tasks
    are running while the folders are being kept.
    """

    def __init__(self, chats: list[dict[str, Any]]) -> None:
        super().__init__(api_url="http://127.0.0.1:1", token="t", agent_id="machine:x")
        self.chats = chats
        self.reports: list[tuple[str, str]] = []
        self.reasons: list[str] = []

    def for_agent(self, agent_id: str) -> CloudRestClient:
        return self

    async def list_chats(self, *, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"items": list(self.chats)}

    async def get_chat(self, chat_id: str) -> dict[str, Any]:
        for chat in self.chats:
            if chat["id"] == chat_id:
                return chat
        raise AssertionError(f"no such chat {chat_id}")

    async def register_machine(self, **kwargs: Any) -> dict[str, Any]:
        return {"id": MACHINE}

    async def heartbeat_machine(self, machine_id: str, **kwargs: Any) -> dict[str, Any]:
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
        self.reports.append((chat_id, state))
        self.reasons.append(reason)
        return {}

    async def events(self, *, after: int | None = None) -> AsyncIterator[dict[str, Any]]:
        await asyncio.Event().wait()  # the org has nothing to say; the task parks
        yield {}


class EndlessPages(StubRest):
    """A chat list whose cursor never runs out — the shape a runaway server, or
    an org with more chats than anyone walks, presents to the discovery pass."""

    def __init__(self, chats: list[dict[str, Any]]) -> None:
        super().__init__(chats)
        self.pages_read = 0

    async def list_chats(self, *, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        self.pages_read += 1
        return {"items": list(self.chats), "next_cursor": f"page-{self.pages_read}"}


class QuietSchema:
    """The workspace's schema cards are another file's subject."""

    pending: tuple[str, ...] = ()

    async def sync_once(self) -> bool:
        return False

    async def stop(self) -> None:
        return None


def _build(
    tmp_path: Path,
    ticker: Ticker,
    folders: BlockingFolders,
    rest: StubRest,
    **overrides: Any,
) -> tuple[CloudMirrorService, dict[str, FakeMirror]]:
    settings = MirrorSettings(
        api_url="http://127.0.0.1:1",
        token="t",
        project_dir=tmp_path,
        machine_name="demo box",
        provider_pod_id="pod-demo",
        machine_type_code="cpu3c",
        poll_interval=POLL_INTERVAL,
        **overrides,
    )
    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"),
        adapter_factory=FakeAdapterFactory(FakeAdapter, available=True),
    )
    built: dict[str, FakeMirror] = {}

    def factory(chat_id: str, _chat: dict[str, Any]) -> Any:
        built[chat_id] = FakeMirror(chat_id, tmp_path / ".alkera" / "chats" / chat_id / "scratch")
        return built[chat_id]

    service = CloudMirrorService(
        settings,
        runtime,
        rest=rest,
        socket=NoSocket(rest),
        mirror_factory=factory,
        schema_loader=QuietSchema(),  # type: ignore[arg-type]
        folders=folders,
        clock=ticker,
        sleep=ticker.sleep,
    )
    return service, built


def _rows(*chat_ids: str) -> list[dict[str, Any]]:
    return [{"id": chat_id, "machine_id": MACHINE} for chat_id in chat_ids]


async def _serving(service: CloudMirrorService, chat_ids: list[str]) -> None:
    """Take these chats' folders through the real path, before anything blocks."""
    await service._adopt_machine(MACHINE)
    for chat_id in chat_ids:
        await service._ensure_mirror(chat_id, {"id": chat_id, "machine_id": MACHINE})


# --------------------------------------------------------------------------- #
# a folder the box holds is beaten on time, whatever else the box is stuck on
# --------------------------------------------------------------------------- #


async def test_held_folders_are_beaten_while_another_chats_take_blocks(tmp_path: Path) -> None:
    """Chats whose folder cannot be taken must not spend the pass while the
    folders the box really holds lose their leases behind them. A take is
    allowed to be slow; it is not allowed to be what keeps a lease from being
    kept."""
    ticker = Ticker()
    folders = BlockingFolders()
    rest = StubRest(_rows(*HELD, SLOW))
    service, _built = _build(tmp_path, ticker, folders, rest)
    await _serving(service, HELD)
    assert all(folders.held(chat_id) is not None for chat_id in HELD)

    folders.block("take", SLOW)
    await service.start()
    try:
        await _until(lambda: folders.entered("take", SLOW), "the fourth chat's take to block")

        for _ in range(ROUNDS):
            await _beaten_again(folders, ticker)

        assert SLOW not in folders.beats, "the blocked chat never got a folder to beat for"
        assert folders.takes.count(SLOW) == 1, "the blocked take is still the one in flight"
        beaten = folders.beats_for(HELD)
        assert all(count >= ROUNDS + 1 for count in beaten.values()), beaten
    finally:
        folders.unblock_all()
        await service.stop()


async def test_held_folders_are_beaten_while_one_of_them_is_pushing(tmp_path: Path) -> None:
    """A push uploads a file at a time, so a chat whose folder holds a
    runtime's worth of them can push for minutes. Run between two folders'
    beats, it must not starve the rest, or its own next beat."""
    ticker = Ticker()
    folders = BlockingFolders()
    rest = StubRest(_rows(*HELD))
    service, built = _build(tmp_path, ticker, folders, rest)
    await _serving(service, HELD)
    # A turn ended on chat-a: its folder has something to push.
    built[HELD[0]].published_count = 3
    folders.block("push", HELD[0])

    await service.start()
    try:
        await _until(lambda: folders.entered("push", HELD[0]), "the slow chat's push to start")

        for _ in range(ROUNDS):
            await _beaten_again(folders, ticker)

        assert folders.pushes.get(HELD[0], 0) == 0, "the push is still in flight"
        beaten = folders.beats_for(HELD)
        assert beaten[HELD[0]] >= ROUNDS + 1, f"the pushing chat lost its own beat: {beaten}"
        assert all(count >= ROUNDS + 1 for count in beaten.values()), beaten
    finally:
        folders.unblock_all()
        await service.stop()


async def test_a_folder_being_handed_back_is_not_beaten_underneath_the_hand_back(
    tmp_path: Path,
) -> None:
    """The beats run on their own task now, so "the sweep already gave this
    folder back" is no longer true by construction — it has to be enforced. A
    beat that landed on top of a hand-back would assert a lease the box has
    just given up, against the box that took it next."""
    ticker = Ticker()
    folders = BlockingFolders()
    rest = StubRest(_rows(HELD[0]))
    service, _built = _build(tmp_path, ticker, folders, rest)
    await _serving(service, [HELD[0]])

    folders.block("beat", HELD[0])
    beating = asyncio.create_task(service._beat_folders())
    await _until(lambda: folders.entered("beat", HELD[0]), "the beat to be in flight")

    sleeping = asyncio.create_task(service._release_mirror(HELD[0], ending="idle"))
    await _settle()
    assert folders.handed_back == [], "the hand-back ran on top of a beat in flight"

    folders.unblock_all()
    await beating
    await sleeping

    assert folders.order == [
        f"take:{HELD[0]}",
        f"beat:{HELD[0]}",
        f"beat-done:{HELD[0]}",
        f"hand-back:{HELD[0]}",
    ]

    # And nothing beats for it afterwards: the folder is not this box's.
    await service._beat_folders()
    assert folders.beats[HELD[0]] == 1
    assert folders.order[-1] == f"hand-back:{HELD[0]}"
    await service.stop()


# --------------------------------------------------------------------------- #
# a beat that hangs costs its own chat, and nothing else
# --------------------------------------------------------------------------- #

#: Chats whose drive accepts the beat and never answers it.
HUNG = ["chat-hung-1", "chat-hung-2"]


async def test_a_hung_beat_does_not_hold_the_threads_the_other_folders_beat_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A small box holds as many folders as the org has chats.

    Nothing caps how many chats one box serves — a chat held back is a reader
    waiting with no way to tell it apart from one nobody is serving — but the
    beats ran on a pool cut to the capacity the box ADVERTISES, which on a
    two-vCPU box is two threads however many folders are leased. Two drives
    that accept the beat and never answer then hold both threads, and every
    other chat on the box goes unbeaten until its lease lapses under a running
    turn.
    """
    monkeypatch.setattr(service_module, "default_max_mirrors", lambda: 2)
    ticker = Ticker()
    folders = BlockingFolders()
    chats = [*HUNG, *HELD]
    rest = StubRest(_rows(*chats))
    service, _built = _build(tmp_path, ticker, folders, rest)
    await _serving(service, chats)
    assert service._capacity() == 2, "the box under test is a two-vCPU one"

    for chat_id in HUNG:
        folders.block("beat", chat_id)
    beating = asyncio.create_task(service._beat_folders())
    try:
        await _until(
            lambda: all(folders.entered("beat", chat_id) for chat_id in HUNG),
            "both unreachable drives to be holding a beat",
            within=4.0,
        )
        await _until(
            lambda: all(folders.beats.get(chat_id, 0) >= 1 for chat_id in HELD),
            "the folders behind the hung ones to be beaten anyway",
            within=4.0,
        )
    finally:
        folders.unblock_all()
        await beating
        await service.stop()


async def test_a_beat_that_never_answers_is_abandoned_and_the_pass_carries_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A beat carries the Files client's two-minute request budget, which is
    sized for pulling and pushing a whole folder rather than for one call. A
    drive that accepts the connection and never answers held a beat for all of
    it — several cadences — so the chat's own next beats queued behind a beat
    that was already too late to keep anything. It is given up on instead, the
    lease is NOT declared gone (it is not known to be), and the next pass asks
    again.
    """
    monkeypatch.setenv(ENV_FOLDER_BEAT_TIMEOUT_SECONDS, "0.05")
    ticker = Ticker()
    folders = BlockingFolders()
    rest = StubRest(_rows(*HELD))
    service, _built = _build(tmp_path, ticker, folders, rest)
    await _serving(service, HELD)

    folders.block("beat", HELD[0])
    try:
        await asyncio.wait_for(service._beat_folders(), 3.0)
        assert folders.entered("beat", HELD[0]), "the hung beat did start"
        assert folders.beats.get(HELD[0], 0) == 0, "and it never answered"
        assert all(folders.beats.get(chat_id, 0) == 1 for chat_id in HELD[1:])
        assert HELD[0] in service.mirrors, (
            "a beat nobody answered is not a lease known to be gone, so the chat is still served"
        )

        # The next pass asks again rather than queueing behind the first.
        await asyncio.wait_for(service._beat_folders(), 3.0)
        assert all(folders.beats.get(chat_id, 0) == 2 for chat_id in HELD[1:])
    finally:
        folders.unblock_all()
        await service.stop()


# --------------------------------------------------------------------------- #
# a take that cannot reach the drive is not paid for on every pass
# --------------------------------------------------------------------------- #


def _unreachable() -> httpx.ConnectError:
    return httpx.ConnectError(
        "[Errno -2] Name or service not known", request=httpx.Request("GET", CONTENT_URL)
    )


async def test_a_folder_that_cannot_be_reached_is_tried_again_on_a_widening_wait(
    tmp_path: Path,
) -> None:
    """A content origin that does not resolve on this box resolves no better
    fifteen seconds later, and every ask costs a DNS timeout inside the pass
    that the folders this box IS holding are beaten behind."""
    ticker = Ticker()
    folders = BlockingFolders()
    folders.unreachable[HELD[0]] = _unreachable()
    rest = StubRest(_rows(HELD[0]))
    service, _built = _build(tmp_path, ticker, folders, rest)
    await service._adopt_machine(MACHINE)
    row = {"id": HELD[0], "machine_id": MACHINE}

    await service._ensure_mirror(HELD[0], row)
    assert folders.takes == [HELD[0]] and service.mirrors == {}

    # Every wait in the ladder: twice the poll interval, doubling to the cap.
    for expected in (30.0, 60.0, 120.0, 240.0, TAKE_FAILURE_BACKOFF_CAP_SECONDS):
        asked = len(folders.takes)
        ticker.advance(expected - 0.5)
        await service._ensure_mirror(HELD[0], row)
        assert len(folders.takes) == asked, f"it was asked again inside its {expected:.0f}s wait"

        ticker.advance(1.0)
        await service._ensure_mirror(HELD[0], row)
        assert len(folders.takes) == asked + 1, f"it was never asked again after {expected:.0f}s"

    assert folders.takes == [HELD[0]] * 6


async def test_an_unreachable_take_names_the_host_once_per_wait_and_never_the_url(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """944 identical lines in twenty-three minutes is what the old behaviour
    left in the box's journal, and a reader looking for the cause had to find
    it in there. One line per wait, naming the host the box could not reach —
    and only the host: a content URL carries a signed token, and a log is the
    one place it must never land."""
    ticker = Ticker()
    folders = BlockingFolders()
    folders.unreachable[HELD[0]] = _unreachable()
    rest = StubRest(_rows(HELD[0]))
    service, _built = _build(tmp_path, ticker, folders, rest)
    await service._adopt_machine(MACHINE)
    row = {"id": HELD[0], "machine_id": MACHINE}

    with caplog.at_level(logging.INFO, logger=TAKES_LOGGER):
        for _ in range(4):
            await service._ensure_mirror(HELD[0], row)
        ticker.advance(31.0)
        await service._ensure_mirror(HELD[0], row)

    said = [r.getMessage() for r in caplog.records if "could not be taken" in r.getMessage()]
    assert len(said) == 2, f"one line per wait, not one per pass: {said}"
    assert all("files.localhost" in line for line in said), said
    assert not any(SECRET in line for line in said), "the log carried the signed content URL"


async def test_a_drive_that_answers_at_all_ends_the_wait(tmp_path: Path) -> None:
    """The wait is for a box that cannot REACH the drive. A drive that answers
    — even to say another box is holding the folder — is a drive this box can
    reach, so the next pass must be free to take a folder the other box has
    just let go rather than sitting out the rest of a five-minute wait."""
    ticker = Ticker()
    folders = BlockingFolders()
    folders.unreachable[HELD[0]] = _unreachable()
    rest = StubRest(_rows(HELD[0]))
    service, _built = _build(tmp_path, ticker, folders, rest)
    await service._adopt_machine(MACHINE)
    row = {"id": HELD[0], "machine_id": MACHINE}

    await service._ensure_mirror(HELD[0], row)
    ticker.advance(31.0)

    # The drive is reachable again; the folder is simply somebody else's.
    del folders.unreachable[HELD[0]]
    folders.busy.add(HELD[0])
    await service._ensure_mirror(HELD[0], row)
    assert service.mirrors == {} and len(folders.takes) == 2

    # No clock movement at all: the wait died with the unreachable answer.
    folders.busy.clear()
    await service._ensure_mirror(HELD[0], row)
    assert folders.takes == [HELD[0]] * 3, "the wait outlived the failure that set it"
    assert HELD[0] in service.mirrors


async def _take_until_said(
    service: CloudMirrorService,
    rest: StubRest,
    ticker: Ticker,
    row: dict[str, Any],
    caplog: pytest.LogCaptureFixture | None = None,
) -> int:
    """Take the chat's folder once per wait until the failure is put on the
    chat, and return how many tries that took: one for a verdict, the passing
    fault limit for a host that never answered. ``caplog`` keeps only the last
    try's records."""
    for tries in range(1, TRANSIENT_LIMIT + 1):
        if caplog is not None:
            caplog.clear()
        await service._ensure_mirror(HELD[0], row)
        if rest.reasons:
            return tries
        ticker.advance(TAKE_FAILURE_BACKOFF_CAP_SECONDS + 1)
    raise AssertionError(f"the failure was never said in {TRANSIENT_LIMIT} tries")


async def test_an_unreachable_take_is_said_to_the_reader_and_cleared_when_a_take_lands(
    tmp_path: Path,
) -> None:
    """A chat whose folder cannot be pulled runs no turn. The acceptance test
    waited its whole budget on a chat that read "ready" while its box logged
    the unreachable content origin to itself three times over. The box's
    verdict goes on the chat — the host named, never the URL, which carries a
    signed token — and clears the moment a take lands. An unreachable host
    is a passing fault, so it is said only once it has outlasted its tries."""
    ticker = Ticker()
    folders = BlockingFolders()
    folders.unreachable[HELD[0]] = _unreachable()
    rest = StubRest(_rows(HELD[0]))
    service, _built = _build(tmp_path, ticker, folders, rest)
    await service._adopt_machine(MACHINE)
    row = {"id": HELD[0], "machine_id": MACHINE}

    await service._ensure_mirror(HELD[0], row)
    assert rest.reports == [], "a first unreachable take is waited out, not said"
    assert await _take_until_said(service, rest, ticker, row) == TRANSIENT_LIMIT
    assert rest.reports == [(HELD[0], "refused")]
    (reason,) = rest.reasons
    assert "could not be taken" in reason and "could not be reached" in reason
    assert "files.localhost" not in reason, "the reader is never shown the host"
    assert SECRET not in reason, "the reader is never shown the signed content URL"
    # Inside the wait nothing is asked and nothing is said again.
    await service._ensure_mirror(HELD[0], row)
    assert rest.reports == [(HELD[0], "refused")]

    ticker.advance(TAKE_FAILURE_BACKOFF_CAP_SECONDS + 1)
    del folders.unreachable[HELD[0]]
    await service._ensure_mirror(HELD[0], row)
    assert HELD[0] in service.mirrors
    assert rest.reports == [(HELD[0], "refused"), (HELD[0], "publishing")]


def _answered_by_drive(status: int = 409, code: str | None = "files.live_pending") -> Exception:
    """A take that failed on the drive's ANSWER, not on reaching it: the
    content route's refusal, as ``raise_for_status`` raises it."""
    request = httpx.Request("GET", CONTENT_URL)
    body: dict[str, str] = {"message": "not synced yet"}
    if code is not None:
        body["code"] = code
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError(
        f"Client error '{status}' for url '{CONTENT_URL}'", request=request, response=response
    )


@pytest.mark.parametrize(
    ("failure", "told", "journaled"),
    [
        pytest.param(
            _answered_by_drive(),
            "the file store answered 409 (files.live_pending)",
            "files.localhost answered 409 (files.live_pending)",
            id="a-409-names-its-code",
        ),
        pytest.param(
            _answered_by_drive(403, None),
            "the file store answered 403",
            "files.localhost answered 403",
            id="an-answer-with-no-code-names-the-status",
        ),
        pytest.param(
            _unreachable(),
            "the file store could not be reached from the machine serving this chat",
            "files.localhost is not reachable from this box",
            id="no-answer-is-unreachable",
        ),
    ],
)
async def test_a_drives_answer_is_told_apart_from_an_unreachable_host(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
    told: str,
    journaled: str,
) -> None:
    """A node whose take met the drive's 409 for one unsynced file logged, and
    would have told the reader, that the API host "is not reachable" — a host
    that answered was not unreachable, and a reader sent to check the network
    for an answer the drive gave finds nothing. The reader is told what was
    answered; only a failure with no answer reads as unreachable. The journal
    names the host; the reader's text names no host, and neither carries the
    URL, which holds the signed token."""
    ticker = Ticker()
    folders = BlockingFolders()
    folders.unreachable[HELD[0]] = failure
    rest = StubRest(_rows(HELD[0]))
    service, _built = _build(tmp_path, ticker, folders, rest)
    await service._adopt_machine(MACHINE)

    with caplog.at_level(logging.INFO, logger=TAKES_LOGGER):
        await _take_until_said(
            service, rest, ticker, {"id": HELD[0], "machine_id": MACHINE}, caplog
        )

    (reason,) = rest.reasons
    assert reason == f"this chat's folder could not be taken: {told}"
    assert SECRET not in reason
    (journal,) = [
        record.getMessage()
        for record in caplog.records
        if record.name == TAKES_LOGGER and "could not be taken" in record.getMessage()
    ]
    assert journaled in journal and SECRET not in journal
    if isinstance(failure, httpx.HTTPStatusError):
        assert "not reachable" not in journal and "could not be reached" not in reason


#: The proof rig's file store, as a node on it would have asked for a folder.
RIG_URL = "http://203.0.113.7:8444/api/v1/files/folders/chat-a/content?sig=hunter2-signed-token"
_DOTTED_QUAD = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
_PORT = re.compile(r":\d{2,5}\b")


def _rig_failure(kind: str, code: str | None = "files.live_pending") -> Exception:
    request = httpx.Request("GET", RIG_URL)
    if kind == "connect":
        return httpx.ConnectError("[Errno 61] Connection refused", request=request)
    body: dict[str, str] = {"message": f"not synced yet at {RIG_URL}"}
    if code is not None:
        body["code"] = code
    response = httpx.Response(409, json=body, request=request)
    return httpx.HTTPStatusError(
        f"Client error '409' for url '{RIG_URL}'", request=request, response=response
    )


def _assert_names_no_address(reason: str) -> None:
    assert not _DOTTED_QUAD.search(reason), f"an IP reached the reader: {reason!r}"
    assert "http" not in reason.lower(), f"a URL reached the reader: {reason!r}"
    assert not _PORT.search(reason), f"a port reached the reader: {reason!r}"
    assert "/api/" not in reason and "?" not in reason and SECRET not in reason, reason


@pytest.mark.parametrize(
    ("failure", "shown"),
    [
        pytest.param(
            _rig_failure("connect"),
            "the file store could not be reached from the machine serving this chat",
            id="connect-error",
        ),
        pytest.param(
            _rig_failure("status"),
            "the file store answered 409 (files.live_pending)",
            id="409-with-code",
        ),
        pytest.param(
            _rig_failure("status", code="http://203.0.113.7:8444/x"),
            "the file store answered 409",
            id="409-whose-code-is-not-a-drive-code",
        ),
    ],
)
async def test_a_refused_chat_names_no_host_ip_port_or_url_to_its_reader(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, failure: Exception, shown: str
) -> None:
    """A chat on a gVisor node whose folder could not be pulled published
    "203.0.113.7 is not reachable from the machine serving it" as its refusal,
    and every surface that shows the chat (API, Slack, the CLI) repeats it. The
    reader is told what kind of failure it was; where the file store lives is
    the journal's business, and the journal line still says it."""
    ticker = Ticker()
    folders = BlockingFolders()
    folders.unreachable[HELD[0]] = failure
    rest = StubRest(_rows(HELD[0]))
    service, _built = _build(tmp_path, ticker, folders, rest)
    await service._adopt_machine(MACHINE)

    with caplog.at_level(logging.INFO, logger=TAKES_LOGGER):
        await _take_until_said(
            service, rest, ticker, {"id": HELD[0], "machine_id": MACHINE}, caplog
        )

    (reason,) = rest.reasons
    assert reason == f"this chat's folder could not be taken: {shown}"
    _assert_names_no_address(reason)
    (journal,) = [
        record.getMessage()
        for record in caplog.records
        if record.name == TAKES_LOGGER and "could not be taken" in record.getMessage()
    ]
    assert "203.0.113.7" in journal, "the journal lost the host an operator needs"
    assert SECRET not in journal


# --------------------------------------------------------------------------- #
# the beat timings and the discovery walk are the deployment's, not constants
# --------------------------------------------------------------------------- #


async def test_the_fallback_beat_cadence_is_the_deployments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing held names a cadence, so the box picks its own — and a
    deployment whose lease TTL is not the shipped one has to be able to move it
    without waiting for a grant to say so."""
    monkeypatch.setenv(ENV_FOLDER_BEAT_SECONDS, "7")
    ticker = Ticker()
    service, _built = _build(tmp_path, ticker, BlockingFolders(), StubRest([]))
    assert service._folder_beat_interval() == 7.0


async def test_a_served_cadence_is_held_above_the_deployments_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server that asks for a beat every fraction of a second turns the loop
    into a spin, and how fast is too fast is the box's call, not a constant's:
    the floor is applied to the SERVED cadence, so a grant under it is raised
    and a grant over it is obeyed."""
    monkeypatch.setenv(ENV_FOLDER_BEAT_FLOOR_SECONDS, "4")
    ticker = Ticker()
    folders = BlockingFolders()
    rest = StubRest(_rows(HELD[0]))
    service, _built = _build(tmp_path, ticker, folders, rest)
    await _serving(service, [HELD[0]])

    folders.held(HELD[0]).record.heartbeat_every = 0.25
    assert service._folder_beat_interval() == 4.0, "a spin cadence is raised to the floor"
    folders.held(HELD[0]).record.heartbeat_every = 9.0
    assert service._folder_beat_interval() == 9.0, "a sane grant is still obeyed"


async def test_the_discovery_walk_stops_where_the_deployment_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cursor that never ends must not loop for ever, but an org with more
    chats than the shipped window carries has the ones past it torn down as if
    they were deleted — so how far the walk goes is the deployment's."""
    monkeypatch.setenv(ENV_CHAT_LIST_PAGES, "3")
    ticker = Ticker()
    rest = EndlessPages(_rows(HELD[0]))
    service, _built = _build(tmp_path, ticker, BlockingFolders(), rest)
    await service._adopt_machine(MACHINE)

    await service.sync_once()
    assert rest.pages_read == 3, "the walk read the deployment's pages and stopped"

    rest.pages_read = 0
    monkeypatch.setenv(ENV_CHAT_LIST_PAGES, "5")
    await service.sync_once()
    assert rest.pages_read == 5
