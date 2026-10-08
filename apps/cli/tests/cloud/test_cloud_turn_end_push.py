"""What a turn wrote reaches the drive when the turn ends, not on the next tick.

A file the agent wrote showed "newer on this box" in the browser's Files page
for a long time after the turn was over: the chat folder was pushed only by the
upkeep loop, every poll interval, after a pass over every other folder. The push
is still a checkpoint — it runs because a turn ENDED, never mid-turn — but it
now runs at that moment, for that chat, beside whatever else the box is doing.

The upkeep loop here runs on the service's injected sleep, which moves the test
clock by the interval it was asked to wait, so "when did the push start" is read
in the box's own time.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from _mirror_service import Clock, FakeMirror, build_service
from alkera_cli.cloud.mirror import ChatMirror

POLL = 15.0
#: The fake time within which a turn's push must start once it ends. The tick
#: is fifteen seconds away; the push is owed now.
LANDS_WITHIN = 1.0


class PushingFolders:
    """Custody that records when each push started, on the test's clock, and
    can hold one chat's push open on its worker thread."""

    def __init__(self, clock: Clock, *, held: set[str] | None = None) -> None:
        self.enabled = True
        self._clock = clock
        self._held_chats = set(held or ())
        self.gate = threading.Event()
        self.pushes: list[tuple[str, float]] = []
        self._lock = threading.Lock()
        self._in_flight: dict[str, int] = {}
        #: The most pushes of one chat that were ever running at once.
        self.peak: dict[str, int] = {}
        self._held: dict[str, Any] = {}

    def bind_machine(self, machine_id: str) -> None:
        return None

    def take(self, chat_id: str, chat: Any, *, instance: str) -> Any:
        held = SimpleNamespace(record=SimpleNamespace(node_id=f"node-{chat_id}"), live=None)
        self._held[chat_id] = held
        return held

    def held(self, chat_id: str) -> Any:
        return self._held.get(chat_id)

    def live(self, chat_id: str, working_dir: Path) -> Any:
        return None

    def owed(self) -> list[str]:
        return []

    def beat(self, chat_id: str) -> bool:
        return chat_id in self._held

    def push(self, chat_id: str) -> Any:
        with self._lock:
            self.pushes.append((chat_id, self._clock.now))
            self._in_flight[chat_id] = self._in_flight.get(chat_id, 0) + 1
            self.peak[chat_id] = max(self.peak.get(chat_id, 0), self._in_flight[chat_id])
        try:
            if chat_id in self._held_chats:
                assert self.gate.wait(timeout=60.0), "the test never let the held push go"
            return SimpleNamespace(uploaded=1)
        finally:
            with self._lock:
                self._in_flight[chat_id] -= 1

    def hand_back(
        self, chat_id: str, *, recover: bool = True, ending: str | None = None, gone: bool = False
    ) -> Any:
        self._held.pop(chat_id, None)
        return None

    def release(self, chat_id: str) -> bool:
        return self._held.pop(chat_id, None) is not None

    def pushed_at(self, chat_id: str) -> list[float]:
        return [at for chat, at in self.pushes if chat == chat_id]


class Rig:
    def __init__(self, tmp_path: Path, *, held: set[str] | None = None) -> None:
        self.clock = Clock()
        self.folders = PushingFolders(self.clock, held=held)
        self.service, self.built = build_service(
            tmp_path,
            clock=self.clock,
            folders=self.folders,
            sleep=self.tick_sleep,
            poll_interval=POLL,
        )
        self.upkeep: asyncio.Task[None] | None = None

    async def tick_sleep(self, seconds: float) -> None:
        """The box's wait: the clock moves by what was asked, and the real
        loop gets a moment so the test is not waiting on wall time."""
        await asyncio.sleep(0.02)
        self.clock.advance(seconds)

    async def serve(self, *chat_ids: str) -> dict[str, FakeMirror]:
        for chat_id in chat_ids:
            await self.service._ensure_mirror(chat_id, {"id": chat_id})
            self.built[chat_id].turn_running = True
        return {chat_id: self.built[chat_id] for chat_id in chat_ids}

    def start_upkeep(self) -> None:
        self.upkeep = asyncio.get_running_loop().create_task(self.service._folder_upkeep_loop())

    async def close(self) -> None:
        self.folders.gate.set()
        if self.upkeep is not None:
            self.upkeep.cancel()
            with pytest.raises(asyncio.CancelledError):
                await self.upkeep
        await self.service.settle_background()


async def _until(predicate: Callable[[], bool], *, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    built = Rig(tmp_path, held={"chat-b"})
    yield built
    built.folders.gate.set()


async def test_a_turn_that_ends_has_its_folder_pushed_at_once_not_on_the_next_tick(
    rig: Rig,
) -> None:
    """The upkeep loop is ticking every fifteen seconds. A turn ends just after
    a tick: its folder push starts at once, in the box's time, and the tick
    that follows has nothing left to push."""
    mirrors = await rig.serve("chat-a")
    rig.start_upkeep()
    assert await _until(lambda: rig.clock.now > 1_000.0), "the upkeep loop never ticked"

    ended_at = rig.clock.now
    mirrors["chat-a"].end_turn()

    assert await _until(lambda: bool(rig.folders.pushed_at("chat-a"))), "the folder never pushed"
    landed = rig.folders.pushed_at("chat-a")[0] - ended_at
    assert landed < LANDS_WITHIN, f"the turn's files left {landed:.0f}s after it ended"

    tick = rig.clock.now
    assert await _until(lambda: rig.clock.now >= tick + 2 * POLL)
    assert len(rig.folders.pushed_at("chat-a")) == 1, "the tick pushed an unchanged folder again"
    await rig.close()


async def test_a_turn_ending_does_not_wait_for_another_chats_slow_upkeep(rig: Rig) -> None:
    """Chat B's push is stuck on a drive that will not answer. Chat A's turn
    ends: A's push starts all the same, while B's is still held — and a second
    end on B while its push is in flight does not start another push of B."""
    mirrors = await rig.serve("chat-a", "chat-b")
    rig.start_upkeep()
    mirrors["chat-b"].end_turn()
    assert await _until(lambda: bool(rig.folders.pushed_at("chat-b"))), "B was never pushed"

    mirrors["chat-a"].end_turn()
    assert await _until(lambda: bool(rig.folders.pushed_at("chat-a"))), (
        "chat A's push waited for chat B's"
    )
    assert not rig.folders.gate.is_set()

    mirrors["chat-b"].turn_running = True
    mirrors["chat-b"].end_turn()
    await asyncio.sleep(0.1)
    assert rig.folders.peak["chat-b"] == 1, "one chat's folder was pushed twice at once"

    rig.folders.gate.set()
    assert await _until(lambda: len(rig.folders.pushed_at("chat-b")) == 2), (
        "B's second turn was never pushed once the first push let go"
    )
    assert rig.folders.peak["chat-b"] == 1
    await rig.close()


async def test_the_tick_still_pushes_a_folder_whose_turn_end_went_unheard(
    tmp_path: Path,
) -> None:
    """The turn-end notice is the fast path, not the only one: a turn whose
    end nobody heard of is pushed by the next tick."""
    rig = Rig(tmp_path)
    try:
        mirrors = await rig.serve("chat-a")
        rig.start_upkeep()
        assert await _until(lambda: rig.clock.now > 1_000.0), "the upkeep loop never ticked"

        ended_at = rig.clock.now
        mirrors["chat-a"].end_turn(told=False)

        assert await _until(lambda: bool(rig.folders.pushed_at("chat-a"))), (
            "a turn end nobody heard of was never pushed"
        )
        assert rig.folders.pushed_at("chat-a")[0] - ended_at <= POLL
    finally:
        await rig.close()


async def test_the_real_mirror_tells_the_service_its_turn_ended(tmp_path: Path) -> None:
    """The service builds every chat's mirror with the notice wired, and the
    mirror gives it when a turn ends — by the harness's terminal or by the
    budget stopping it — which is what starts the push in production."""
    clock = Clock()
    folders = PushingFolders(clock)
    service, _built = build_service(tmp_path, clock=clock, folders=folders)
    # The mirror exactly as the service builds it, held for a chat whose
    # folder this box has taken; started or not, its turn end is the same.
    folders.take("chat-a", {"id": "chat-a"}, instance="box:chat-a")
    real = service._default_mirror("chat-a", {"id": "chat-a"})
    service._mirrors["chat-a"] = real
    # A turn has said something since the last push.
    service._pushed_at["chat-a"] = -1
    try:
        real._begin_turn("turn-1")
        real._end_turn()
        assert await _until(lambda: bool(folders.pushed_at("chat-a"))), (
            "the mirror's turn end never reached the folder"
        )
    finally:
        await service.settle_background()


async def test_a_listener_that_fails_does_not_break_the_turn_end(tmp_path: Path) -> None:
    """The notice is best-effort: a listener that raises leaves the chat idle."""
    from _adapter_factory import FakeAdapterFactory
    from alkera_cli.cloud import CloudRestClient, CloudSocket
    from alkera_cli.harness import HarnessRuntime
    from alkera_cli.harness._fake import FakeAdapter
    from alkera_core.project.directory import ProjectDirectory

    heard: list[str] = []

    def listener(chat_id: str) -> None:
        heard.append(chat_id)
        raise RuntimeError("the listener fell over")

    mirror = ChatMirror(
        chat_id="chat-a",
        runtime=HarnessRuntime(
            ProjectDirectory(tmp_path / ".alkera"),
            adapter_factory=FakeAdapterFactory(FakeAdapter),
        ),
        socket=cast(CloudSocket, object()),
        rest=CloudRestClient(
            api_url="http://transcript.test",
            token="t",
            agent_id="chat-a",
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={})),
        ),
        on_turn_end=listener,
    )
    mirror._begin_turn("turn-1")
    mirror._end_turn()
    assert heard == ["chat-a"]
    assert not mirror.turn_running
