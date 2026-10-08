"""A turn that is working says so for as long as it works.

The chat document's ``turn_state`` is the only thing on the wire that tells a
reader a turn is under way. It was stamped once, when the turn began — so a
reader who opened the chat three hours into a long answer met a stamp three
hours old, and a stamp that old is indistinguishable from a box that died
mid-sentence. A turn may legitimately run for hours (a warehouse query, a
build, a subagent reading a repository), and every one of those says nothing on
the event stream while it runs, so the stamp is the ONLY evidence there is.

It is now re-stamped at the machine's own heartbeat interval for as long as the
turn runs, which is what makes "working, and the machine is beating" a thing a
reader can believe.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import CloudRestClient, CloudSocket
from alkera_cli.cloud.mirror import TURN_STAMP_SECONDS, ChatMirror
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_core.project.directory import ProjectDirectory

CHAT_ID = "chat-heartbeat"
#: How long the silent turn below runs. Far past anything the browser used to
#: trust a stamp for.
HOURS = 3


class _Clock:
    """A clock the test moves, and the sleep that moves it.

    The budget watch is a ``while True`` over ``sleep(tick)``; handing it this
    sleep turns three hours of a silent turn into a few thousand iterations of
    the event loop, with no wall-clock wait and nothing to flake on.
    """

    def __init__(self) -> None:
        self.now = 1_000.0
        self.ticks = 0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.ticks += 1
        await asyncio.sleep(0)


def _mirror(tmp_path: Path, clock: _Clock) -> ChatMirror:
    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"), adapter_factory=FakeAdapterFactory(FakeAdapter)
    )
    rest = CloudRestClient(
        api_url="http://transcript.test",
        token="device-jwt",
        agent_id=CHAT_ID,
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})),
    )
    return ChatMirror(
        chat_id=CHAT_ID,
        runtime=runtime,
        socket=cast(CloudSocket, object()),
        rest=rest,
        clock=clock,
        sleep=clock.sleep,
    )


def _stamps(mirror: ChatMirror) -> list[dict[str, Any]]:
    """Every turn-state the mirror has put on its outbound lane, in order —
    what the publisher sends as ``set_meta`` and the browser reads."""
    out: list[dict[str, Any]] = []
    while not mirror._outbound.empty():
        entry = mirror._outbound.get_nowait()
        meta = entry.get("__meta__")
        if isinstance(meta, dict):
            out.append(meta)
    return out


async def _run_turn(mirror: ChatMirror, clock: _Clock, seconds: float) -> None:
    """Let the budget watch run for ``seconds`` of the test's clock."""
    watch = asyncio.create_task(mirror._budget_watch())
    spins = 0
    try:
        while clock.now < 1_000.0 + seconds:
            await asyncio.sleep(0)
            spins += 1
            assert spins < seconds * 100, "the budget watch stopped ticking"
    finally:
        watch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watch


async def test_a_turn_silent_for_hours_keeps_saying_it_is_working(tmp_path: Path) -> None:
    """The stamp tracks the machine's heartbeat, not the turn's first second."""
    clock = _Clock()
    mirror = _mirror(tmp_path, clock)
    mirror._begin_turn("turn-1")
    started = _stamps(mirror)
    assert [one["turn_state"]["state"] for one in started] == ["working"]

    await _run_turn(mirror, clock, HOURS * 3600)

    said = _stamps(mirror)
    assert {one["turn_state"]["state"] for one in said} == {"working"}
    # One every heartbeat interval for the whole three hours — not one at the
    # top and silence after it.
    assert len(said) == pytest.approx(HOURS * 3600 / TURN_STAMP_SECONDS, abs=1)
    assert all(one["turn_state"]["at"] for one in said), "every stamp carries when it was made"


async def test_a_chat_with_no_turn_running_stamps_nothing(tmp_path: Path) -> None:
    """The re-stamp is evidence a turn is alive, so it exists only while one
    is: a chat sitting idle must not look like it is answering."""
    clock = _Clock()
    mirror = _mirror(tmp_path, clock)
    assert _stamps(mirror) == []

    await _run_turn(mirror, clock, HOURS * 3600)

    assert _stamps(mirror) == []


async def test_a_turn_that_ends_stops_saying_it_is_working(tmp_path: Path) -> None:
    """The last word on the lane is the end of the turn, and nothing after it —
    a re-stamp that outlived its turn would hold the reader's composer open for
    ever."""
    clock = _Clock()
    mirror = _mirror(tmp_path, clock)
    mirror._begin_turn("turn-1")
    await _run_turn(mirror, clock, 10 * TURN_STAMP_SECONDS)
    assert len(_stamps(mirror)) > 1

    mirror._end_turn()
    ended = _stamps(mirror)
    assert [one["turn_state"]["state"] for one in ended] == ["idle"]

    await _run_turn(mirror, clock, HOURS * 3600)

    assert _stamps(mirror) == []
