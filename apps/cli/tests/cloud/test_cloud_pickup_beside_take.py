"""A chat's first message is picked up while another chat's folder is being taken.

On the demo box a brand-new chat sat on "Waiting for the workspace…" for more
than six minutes while the box reported itself ready: the take pass was busy
pulling another chat's folder through the tunnel, one request at a time, and
the chat a frame named was only looked at between two takes. These tests hold
a folder take open on a real worker thread — what a large folder or a slow link
looks like from the service — and ask whether a chat that becomes owed in the
meantime is taken, and its owed turn started, while that take is still held.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _mirror_service import Clock, FakeMirror, build_service
from alkera_cli.cloud import service as service_module
from alkera_cli.cloud.service import CloudMirrorService

MACHINE = "machine:x"
SLOW = "chat-slow"
NEW = "chat-new"
#: How long a chat that becomes owed may wait while another chat's folder is
#: still being taken. A frame is answered at once and the re-read of the
#: newest page is looked at every second, so this is generous; the held take
#: it is measured against never ends on its own.
PICKUP_BOUND = 5.0


class HeldFolders:
    """Custody whose take of the held chats' folders blocks a real worker
    thread apiece until the test lets them go, and counts every take it is
    asked for."""

    def __init__(self, *, held: str | Iterable[str]) -> None:
        self.enabled = True
        self._held_chats = {held} if isinstance(held, str) else set(held)
        self.gate = threading.Event()
        self.reached = threading.Event()
        self.reached_count = 0
        self.takes: dict[str, int] = {}
        self._lock = threading.Lock()
        self._held: dict[str, Any] = {}

    def bind_machine(self, machine_id: str) -> None:
        return None

    def take(self, chat_id: str, chat: Any, *, instance: str) -> Any:
        with self._lock:
            self.takes[chat_id] = self.takes.get(chat_id, 0) + 1
        if chat_id in self._held_chats:
            with self._lock:
                self.reached_count += 1
            self.reached.set()
            assert self.gate.wait(timeout=60.0), "the test never let the held take go"
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
        return None

    def hand_back(
        self, chat_id: str, *, recover: bool = True, ending: str | None = None, gone: bool = False
    ) -> Any:
        self._held.pop(chat_id, None)
        return None

    def release(self, chat_id: str) -> bool:
        return self._held.pop(chat_id, None) is not None


class Rig:
    """A box serving chats from a listing the test edits, with one chat's
    folder take held open."""

    def __init__(
        self, tmp_path: Path, *, held: str | Iterable[str] = SLOW, **overrides: Any
    ) -> None:
        self.folders = HeldFolders(held=held)
        self.clock = Clock()
        self.chats: list[dict[str, Any]] = [
            {"id": SLOW, "machine_id": MACHINE, "last_seq": 1, "pending_turn": True},
            {"id": "chat-idle", "machine_id": MACHINE, "last_seq": 1},
        ]
        self.log: list[tuple[str, str]] = []
        self.service, self.built = build_service(
            tmp_path, clock=self.clock, folders=self.folders, **overrides
        )
        self.service._machine_id = MACHINE
        self.service._rest.list_chats = self.list_chats  # type: ignore[method-assign]
        self.service._rest.get_chat = self.get_chat  # type: ignore[method-assign]
        self.service._rest.report_publisher_state = self.report  # type: ignore[method-assign]
        plain = self.service._mirror_factory

        def factory(chat_id: str, chat: dict[str, Any]) -> Any:
            mirror: FakeMirror = plain(chat_id, chat)
            mirror.log = self.log
            self.log.append(("take", chat_id))
            return mirror

        self.service._mirror_factory = factory

    async def list_chats(self, *, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"items": list(self.chats), "next_cursor": None}

    async def get_chat(self, chat_id: str) -> dict[str, Any]:
        for chat in self.chats:
            if chat["id"] == chat_id:
                return chat
        raise AssertionError(f"no such chat {chat_id}")

    async def report(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def bind_new_chat(self) -> None:
        """The browser creates a chat bound here and sends its first message."""
        self.chats.insert(
            0, {"id": NEW, "machine_id": MACHINE, "last_seq": 1, "pending_turn": True}
        )

    async def held_take_reached(self, count: int = 1) -> None:
        assert await _until(lambda: self.folders.reached_count >= count), (
            f"only {self.folders.reached_count} of {count} held folder take(s) began"
        )

    def turn_started(self, chat_id: str) -> bool:
        return ("turn", chat_id) in self.log


async def _until(predicate: Callable[[], bool], *, within: float = PICKUP_BOUND) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    built = Rig(tmp_path)
    yield built
    built.folders.gate.set()


async def _finish(service: CloudMirrorService, work: asyncio.Task[None]) -> None:
    await asyncio.wait_for(work, 30.0)
    await service.settle_background()


async def test_a_chat_a_frame_names_is_taken_while_another_folder_take_is_held(rig: Rig) -> None:
    """The pass is inside a folder take that does not end. A frame names a
    chat that was just created and spoken to: that chat is taken and its
    owed turn started while the held take is still held — and a frame naming
    the held chat itself does not start a second take of its folder."""
    passing = asyncio.get_running_loop().create_task(rig.service.sync_once())
    await rig.held_take_reached()

    rig.bind_new_chat()
    await asyncio.wait_for(rig.service._chat_named(NEW), 2.0)
    await asyncio.wait_for(rig.service._chat_named(SLOW), 2.0)

    assert await _until(lambda: rig.turn_started(NEW)), (
        "the new chat's owed turn waited behind another chat's folder take"
    )
    assert not rig.folders.gate.is_set()
    assert SLOW not in rig.service.mirrors, "the held take finished early; the test proves nothing"

    rig.folders.gate.set()
    await _finish(rig.service, passing)

    assert rig.turn_started(SLOW)
    assert rig.folders.takes[SLOW] == 1, "the held chat's folder was taken twice at once"
    assert set(rig.service.mirrors) == {SLOW, NEW, "chat-idle"}
    assert not rig.built[NEW].stopped, "the pass that never listed the new chat stopped it"


async def test_a_chat_owing_a_turn_is_found_while_a_folder_take_is_held_without_a_frame(
    rig: Rig,
) -> None:
    """The frame is best-effort. With none, the newest page is read again
    once the pass has run for its re-read interval — and that happens while
    the pass waits on a take, not only between two of them."""
    passing = asyncio.get_running_loop().create_task(rig.service.sync_once())
    await rig.held_take_reached()

    rig.bind_new_chat()
    rig.clock.advance(service_module.PASS_RELIST_SECONDS + 1)

    assert await _until(lambda: rig.turn_started(NEW)), (
        "the chat owing a turn waited for the held folder take to end"
    )
    assert not rig.folders.gate.is_set()

    rig.folders.gate.set()
    await _finish(rig.service, passing)
    assert NEW in rig.service.mirrors and not rig.built[NEW].stopped


async def test_a_frame_for_a_slow_chat_does_not_hold_the_frame_after_it(rig: Rig) -> None:
    """No pass running. The event stream hands over a frame for a chat whose
    folder take will not end, then one for a chat just spoken to: the stream
    is not held by the first, and the second chat is taken at once."""
    rig.chats = [{"id": SLOW, "machine_id": MACHINE, "last_seq": 1, "pending_turn": True}]

    await asyncio.wait_for(rig.service._chat_named(SLOW), 2.0)
    await rig.held_take_reached()
    rig.bind_new_chat()
    await asyncio.wait_for(rig.service._chat_named(NEW), 2.0)

    assert await _until(lambda: rig.turn_started(NEW)), (
        "the second frame's chat waited behind the first frame's folder take"
    )
    assert SLOW not in rig.service.mirrors

    rig.folders.gate.set()
    assert await _until(lambda: SLOW in rig.service.mirrors, within=30.0)
    await rig.service.settle_background()
    assert set(rig.service.mirrors) == {SLOW, NEW}


async def test_a_chat_whose_folder_is_being_taken_holds_its_slot_of_the_cap(
    tmp_path: Path,
) -> None:
    """A box capped at one chat, with that one chat's folder still being
    taken. A frame names a second chat owing a turn: it is answered at once —
    there is no slot, because the chat being taken holds it and has nothing
    idle to give up — rather than after the take, and the box never ends up
    serving two."""
    rig = Rig(tmp_path, max_mirrors=1)
    rig.chats = [{"id": SLOW, "machine_id": MACHINE, "last_seq": 1, "pending_turn": True}]
    try:
        passing = asyncio.get_running_loop().create_task(rig.service.sync_once())
        await rig.held_take_reached()

        rig.bind_new_chat()
        await asyncio.wait_for(rig.service._chat_named(NEW), 2.0)

        assert await _until(lambda: NEW in rig.service._no_room or NEW in rig.service.mirrors), (
            "the new chat's take waited behind the held folder take"
        )
        assert NEW not in rig.service.mirrors, "a box capped at one served two chats"
        assert rig.folders.takes.get(NEW, 0) == 0, "a folder was taken past the cap"

        rig.folders.gate.set()
        await _finish(rig.service, passing)
        assert set(rig.service.mirrors) == {SLOW}
    finally:
        rig.folders.gate.set()


async def test_a_chat_a_frame_names_is_taken_while_every_take_slot_is_held(
    tmp_path: Path,
) -> None:
    """Five owed chats, each stuck in a folder take that does not end (a
    content origin that does not answer holds each one for its whole connect
    budget): the pass's own lane and all four slots beside it. A reader
    writes to a new chat. On a node that had claimed the chats two orgs had
    stranded, that chat waited its whole budget behind exactly this — the
    frame's take queued for a slot the re-read's takes held. It runs in the
    frames' own lane: the owed turn starts while all five are still held."""
    stuck = [f"chat-stuck-{n}" for n in range(1 + service_module.TAKES_BESIDE_THE_PASS)]
    rig = Rig(tmp_path, held=stuck)
    rig.chats = [
        {"id": chat_id, "machine_id": MACHINE, "last_seq": 1, "pending_turn": True}
        for chat_id in stuck
    ]
    try:
        passing = asyncio.get_running_loop().create_task(rig.service.sync_once())
        await rig.held_take_reached()
        # The re-read finds the other four owing a turn and takes them beside.
        rig.clock.advance(service_module.PASS_RELIST_SECONDS + 1)
        await rig.held_take_reached(len(stuck))
        assert rig.service._beside_slots.locked(), "the slots beside the pass are not all held"

        rig.bind_new_chat()
        await asyncio.wait_for(rig.service._chat_named(NEW), 2.0)

        assert await _until(lambda: rig.turn_started(NEW)), (
            "the chat a reader just wrote to waited behind five held folder takes"
        )
        assert not rig.folders.gate.is_set()
        assert not any(chat_id in rig.service.mirrors for chat_id in stuck)
    finally:
        rig.folders.gate.set()
    await _finish(rig.service, passing)
    assert set(rig.service.mirrors) == {*stuck, NEW}
    assert all(rig.folders.takes[chat_id] == 1 for chat_id in stuck)
