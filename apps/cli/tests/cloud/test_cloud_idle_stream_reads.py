"""While the event stream is up, an idle chat asks the API nothing.

A box serving many chats read every chat's transcript on every discovery pass
and asked every folder for its inbound drops on every upkeep pass, so an idle
box's traffic grew with the chats it held. The box already holds one event
stream, and the server rings it whenever a chat is said something in or a
folder is given something to apply — so while the stream is up the passes
read nothing on their own, a frame wakes the one chat it names, and a stream
that is down (or just came back) is the only time the box polls.

Driven through the real service: its real discovery pass, stream loop and
upkeep pass, against a rest client whose stream the test opens, rings and
drops.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _mirror_service import Clock, FakeMirror, build_service
from alkera_cli.cloud.rest import STREAM_OPENED, CloudRestClient

MACHINE = "6a2c1b4d-3e1f-4b3c-8e6d-82b4d0a1c222"
CHATS = ["chat-a", "chat-b", "chat-c"]
DROP = object()


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


class StreamRest(CloudRestClient):
    """The chat routes and the org's event stream, the stream under the test's
    hand: ``open`` accepts it, ``ring`` sends a frame, ``drop`` ends it."""

    def __init__(self, chats: list[str]) -> None:
        super().__init__(api_url="http://127.0.0.1:1", token="t", agent_id="machine:x")
        self.rows = {
            chat_id: {"id": chat_id, "last_seq": 7, "machine_id": MACHINE} for chat_id in chats
        }
        self.frames: asyncio.Queue[Any] = asyncio.Queue()
        self.chat_reads: list[str] = []
        self.opened = 0

    def for_agent(self, agent_id: str) -> CloudRestClient:
        return self

    async def list_chats(self, *, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"items": [dict(row) for row in self.rows.values()]}

    async def get_chat(self, chat_id: str) -> dict[str, Any]:
        self.chat_reads.append(chat_id)
        return dict(self.rows[chat_id])

    async def report_publisher_state(
        self,
        chat_id: str,
        *,
        state: str,
        reason: str = "",
        refusal_kind: str | None = None,
        ending: str | None = None,
    ) -> dict[str, Any]:
        return {}

    async def events(self, *, after: int | None = None) -> AsyncIterator[dict[str, Any]]:
        self.opened += 1
        yield {"id": None, "type": STREAM_OPENED, "data": None}
        while True:
            frame = await self.frames.get()
            if frame is DROP:
                return
            yield frame

    def ring(self, kind: str, entity_id: str, **payload: Any) -> None:
        self.frames.put_nowait(
            {"id": None, "type": kind, "data": {"type": kind, "entity_id": entity_id, **payload}}
        )


async def _settle(turns: int = 50) -> None:
    for _ in range(turns):
        await asyncio.sleep(0)


async def _parked(_seconds: float) -> None:
    """The stream's reconnect wait, held shut: a dropped stream stays down."""
    await asyncio.Event().wait()


def _catch_ups(built: dict[str, FakeMirror]) -> int:
    return sum(mirror.catch_ups for mirror in built.values())


async def _serving(
    tmp_path: Path, folders: Any | None = None, clock: Clock | None = None
) -> tuple[Any, dict[str, FakeMirror], StreamRest]:
    rest = StreamRest(CHATS)
    service, built = build_service(
        tmp_path, clock=clock or Clock(), rest=rest, sleep=_parked, folders=folders
    )
    await service._adopt_machine(MACHINE)
    await service.sync_once()
    assert set(built) == set(CHATS)
    return service, built, rest


async def test_the_pass_reads_no_transcript_while_the_stream_is_up(tmp_path: Path) -> None:
    service, built, rest = await _serving(tmp_path)
    stream = asyncio.create_task(service._stream_loop())
    try:
        await _settle()
        on_connect = _catch_ups(built)
        assert on_connect >= len(CHATS), "the stream coming up reads every chat once"

        for _ in range(5):
            await service.sync_once()
        assert _catch_ups(built) == on_connect, "an idle chat was read on a poll"
        assert rest.chat_reads == []
    finally:
        stream.cancel()


async def test_a_frame_wakes_only_the_chat_it_names(tmp_path: Path) -> None:
    service, built, rest = await _serving(tmp_path)
    stream = asyncio.create_task(service._stream_loop())
    try:
        await _settle()
        before = {chat_id: built[chat_id].catch_ups for chat_id in CHATS}

        rest.rows["chat-b"]["last_seq"] = 8
        rest.ring("chat.updated", "chat-b")
        await _settle()
        await service.settle_background()

        after = {chat_id: built[chat_id].catch_ups for chat_id in CHATS}
        assert after["chat-b"] == before["chat-b"] + 1
        assert after["chat-a"] == before["chat-a"] and after["chat-c"] == before["chat-c"]
        assert rest.chat_reads == ["chat-b"]
    finally:
        stream.cancel()


async def test_a_dropped_stream_puts_the_pass_back_on_the_transcripts(tmp_path: Path) -> None:
    service, built, rest = await _serving(tmp_path)
    stream = asyncio.create_task(service._stream_loop())
    try:
        await _settle()
        rest.frames.put_nowait(DROP)
        await _settle()
        before = _catch_ups(built)

        await service.sync_once()

        assert _catch_ups(built) == before + len(CHATS)
    finally:
        stream.cancel()


async def test_a_box_that_never_heard_the_stream_open_polls_as_before(tmp_path: Path) -> None:
    """No stream task at all — a server whose stream refuses this box — and the
    pass is the only way to hear of a message, so it reads every chat."""
    service, built, _rest = await _serving(tmp_path)
    before = _catch_ups(built)

    await service.sync_once()

    assert _catch_ups(built) == before + len(CHATS)


# ---------------------------------------------------------------------------
# What the drive holds for a folder: asked on its frame, not on every pass
# ---------------------------------------------------------------------------


class _Plane:
    """A held folder's live plane: every ask for its inbound drops is counted."""

    def __init__(self, custody: Custody, chat_id: str) -> None:
        self._custody = custody
        self._chat_id = chat_id

    def pull_inbound(self) -> list[Any]:
        self._custody.asks.append(self._chat_id)
        self._custody.asked.set()
        return []


class Custody:
    """The folders the box holds, each streaming, as the service drives them."""

    def __init__(self) -> None:
        self.enabled = True
        self.asks: list[str] = []
        #: Set on every ask, so a test can wait for a drain instead of counting turns.
        self.asked = asyncio.Event()
        self.pushes: list[str] = []
        #: Whether a push lands; one that does not answers ``None``.
        self.pushes_land = True
        self._held: dict[str, Any] = {}

    def bind_machine(self, machine_id: str) -> None:
        return None

    def take(self, chat_id: str, chat: Any, *, instance: str) -> Any:
        held = SimpleNamespace(
            record=SimpleNamespace(node_id=f"node-{chat_id}", heartbeat_every=15.0),
            live=_Plane(self, chat_id),
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
        from alkera_cli.files.push import PushSummary

        self.pushes.append(chat_id)
        return PushSummary() if self.pushes_land else None

    def beat(self, chat_id: str) -> bool:
        return True

    def release(self, chat_id: str) -> bool:
        return self._held.pop(chat_id, None) is not None


async def _drained(custody: Custody, expected: list[str], *, seconds: float = 5.0) -> None:
    """Wait until the custody's asks read ``expected``, or ``seconds`` pass.

    A fixed number of loop turns is enough on an idle machine and too few on a
    loaded CI worker, where the frame's drain competes with the rest of the
    shard for the loop; the bound keeps a genuinely missing drain a fast failure."""
    deadline = time.monotonic() + seconds
    while True:
        custody.asked.clear()
        if custody.asks == expected:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(custody.asked.wait(), remaining)


async def _upkeep_passes(service: Any, passes: int = 5) -> None:
    for _ in range(passes):
        await service._upkeep_folders()


async def test_no_folder_is_asked_for_its_drops_while_the_stream_is_up(tmp_path: Path) -> None:
    custody = Custody()
    service, _built, _rest = await _serving(tmp_path, custody)
    stream = asyncio.create_task(service._stream_loop())
    try:
        await _settle()
        await service.settle_background()
        assert sorted(custody.asks) == CHATS, "the stream coming up asks every folder once"
        custody.asks.clear()

        await _upkeep_passes(service)

        assert custody.asks == []
    finally:
        stream.cancel()


async def test_an_inbound_frame_drains_the_folder_it_names_and_a_report_frame_does_not(
    tmp_path: Path,
) -> None:
    custody = Custody()
    service, _built, rest = await _serving(tmp_path, custody)
    stream = asyncio.create_task(service._stream_loop())
    try:
        await _settle()
        await service.settle_background()
        custody.asks.clear()

        rest.ring("file_lease.changed", "node-chat-b", reason="report")
        await _settle()
        assert custody.asks == [], "the holder's own report owes it nothing"

        rest.ring("file_lease.changed", "node-chat-b", reason="inbound")
        await _drained(custody, ["chat-b"])
        assert custody.asks == ["chat-b"]

        # A server that names no reason is drained on every frame, as before.
        rest.ring("file_lease.changed", "node-chat-c")
        await _drained(custody, ["chat-b", "chat-c"])
        assert custody.asks == ["chat-b", "chat-c"]
    finally:
        stream.cancel()


async def test_a_dropped_stream_puts_the_upkeep_back_on_asking(tmp_path: Path) -> None:
    custody = Custody()
    service, _built, rest = await _serving(tmp_path, custody)
    stream = asyncio.create_task(service._stream_loop())
    try:
        await _settle()
        await service.settle_background()
        rest.frames.put_nowait(DROP)
        await _settle()
        custody.asks.clear()

        await _upkeep_passes(service, passes=2)

        assert sorted(custody.asks) == sorted(CHATS * 2)
    finally:
        stream.cancel()


# ---------------------------------------------------------------------------
# A checkpoint push runs when a turn ends, not on every pass after a miss
# ---------------------------------------------------------------------------


async def test_a_push_that_did_not_land_is_not_retried_on_every_pass(tmp_path: Path) -> None:
    """The folder is unreachable for thirteen passes. The same state is pushed
    again after a wait that doubles, not on each of them — and the next turn's
    end is pushed at once whatever the wait, and a push that lands ends it."""
    clock = Clock()
    custody = Custody()
    custody.pushes_land = False
    service, built, _rest = await _serving(tmp_path, custody, clock)
    built["chat-a"].end_turn(told=False)

    def pushes() -> int:
        return custody.pushes.count("chat-a")

    for _ in range(13):
        await service._upkeep_folders()
        clock.advance(15.0)
    # At 0 s, at 60 s after the first wait, and at 180 s after the doubled one.
    assert pushes() == 3

    built["chat-a"].end_turn(told=False)
    await service._upkeep_folders()
    assert pushes() == 4, "a turn that ended is pushed at once"

    custody.pushes_land = True
    clock.advance(3600.0)
    await service._upkeep_folders()
    assert pushes() == 5
    for _ in range(10):
        clock.advance(3600.0)
        await service._upkeep_folders()
    assert pushes() == 5, "a push that landed is not repeated for the same state"
    assert "chat-b" not in custody.pushes, "a chat whose turn never moved is never pushed"
