"""A kernel's burst of events reaches the backend in a few posts, in order,
and never holds up the answer to a request.

The browser proof saw a box post about a thousand event batches, one after
another, in a minute and a half after a streaming cell and an interrupt; the
box stalled behind them and the next run was refused as a silent machine.
Here a relay is fed the same kind of burst (events arriving while the
backend takes its time over each post) and the backend's events route is a
recorder that answers slowly.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from alkera_cli.notebooks.box import _Relay
from alkera_cli.notebooks.box_events import POST_BATCH, EventOutbox
from alkera_cli.notebooks.store_loro import Located
from alkera_notebook.events.models import CellStatusEvent, CellStreamEvent
from alkera_notebook.events.queue import ClientQueue

WHERE = Located(drive_id="d", item_id="n")
KERNEL = "krn_burst"
BURST = 1_000


@dataclass
class Post:
    kernel_id: str
    state: str | None
    events: list[dict[str, Any]]
    began: float


@dataclass
class SlowBackend:
    """The events route, taking ``latency`` over each post."""

    latency: float = 0.05
    posts: list[Post] = field(default_factory=list)
    in_flight: int = 0
    most_in_flight: int = 0

    async def post(
        self,
        where: Located,
        *,
        kernel_id: str,
        state: str | None,
        events: Sequence[Mapping[str, Any]],
    ) -> None:
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        try:
            self.posts.append(Post(kernel_id, state, [dict(e) for e in events], time.monotonic()))
            await asyncio.sleep(self.latency)
        finally:
            self.in_flight -= 1

    def events(self) -> list[dict[str, Any]]:
        return [e for p in self.posts for e in p.events]


class _Client:
    def __init__(self) -> None:
        self.queue = ClientQueue(maxlen=100_000, view=self._view)

    async def _view(self) -> Any:
        raise AssertionError("the burst never overflows the queue")

    async def detach(self) -> None:
        self.queue.close()


def _relay(backend: SlowBackend) -> tuple[_Relay, _Client]:
    client = _Client()
    session = SimpleNamespace(
        attach=lambda actor: client,
        runtime=SimpleNamespace(kernel=SimpleNamespace(kernel_id=KERNEL), frames={}, outputs={}),
    )
    return _Relay(session, WHERE, backend), client  # type: ignore[arg-type]


def _stream(n: int, cell: str = "c1", name: str = "stdout") -> CellStreamEvent:
    return CellStreamEvent(cell_id=cell, run_id="r1", name=name, text=f"line {n}\n")


async def _burst(client: _Client, *, answer_at: int, on_answer: Any) -> None:
    """``BURST`` events arriving one at a time, each a little after the one
    before (as a cell prints), with a status change every hundred and a
    second stream mixed in, so not everything is one cell's stdout;
    ``on_answer`` runs when event ``answer_at`` is out. Posted as they came,
    each would be a post of its own."""
    for n in range(BURST):
        if n % 100 == 50:
            client.queue.put(CellStatusEvent(cell_id="c1", run_id="r1", status="running"))
        client.queue.put(_stream(n, name="stderr" if n % 250 == 0 else "stdout"))
        if n == answer_at:
            on_answer()
        await asyncio.sleep(0.002)


async def _eventually(check: Any, within: float = 20.0) -> None:
    async with asyncio.timeout(within):
        while True:
            if check():
                return
            await asyncio.sleep(0.02)


def _streamed(events: list[dict[str, Any]], name: str) -> str:
    return "".join(
        e["text"] for e in events if e.get("type") == "cell.stream" and e.get("name") == name
    )


async def test_a_burst_is_a_few_posts_one_at_a_time_in_order() -> None:
    backend = SlowBackend(latency=0.001)
    relay, client = _relay(backend)
    try:
        await _burst(client, answer_at=-1, on_answer=lambda: None)
        await _eventually(
            lambda: (
                _streamed(backend.events(), "stdout").count("\n")
                + _streamed(backend.events(), "stderr").count("\n")
                >= BURST
            )
        )
    finally:
        await relay.close()
    events = backend.events()
    # Every line, in the order it was printed, stream by stream.
    expected = [_stream(n, name="stderr" if n % 250 == 0 else "stdout") for n in range(BURST)]
    for name in ("stdout", "stderr"):
        assert _streamed(events, name) == "".join(e.text for e in expected if e.name == name)
    # Far fewer posts than events, and never two at once.
    assert len(backend.posts) <= 60, len(backend.posts)
    assert backend.most_in_flight == 1
    assert all(len(p.events) <= POST_BATCH for p in backend.posts)
    # One kernel's numbers count up by one across the posts, whatever merged.
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert {e["kernel_id"] for e in events} == {KERNEL}
    # A status change stays between the lines printed either side of it.
    kinds = [e["type"] for e in events]
    assert kinds.count("cell.status") == BURST // 100


async def test_an_answer_during_a_burst_goes_in_the_next_post() -> None:
    backend = SlowBackend(latency=0.1)
    relay, client = _relay(backend)
    answered: list[asyncio.Task[None]] = []
    sent_at: list[float] = []

    def answer() -> None:
        sent_at.append(time.monotonic())
        answered.append(
            asyncio.get_running_loop().create_task(
                relay.post([{"type": "answer", "request_id": "q1", "result": {}}], urgent=True)
            )
        )

    try:
        await _burst(client, answer_at=500, on_answer=answer)
        async with asyncio.timeout(5):
            await answered[0]
        carrying = next(
            i
            for i, p in enumerate(backend.posts)
            if any(e.get("request_id") == "q1" for e in p.events)
        )
        # Out as soon as the one post in flight ended, not after the burst,
        # and first in its post.
        assert backend.posts[carrying].began - sent_at[0] < 0.5
        assert backend.posts[carrying].events[0]["type"] == "answer"
        before = [p for p in backend.posts if p.began < sent_at[0]]
        assert carrying <= len(before)
    finally:
        await relay.close()
    events = backend.events()
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))


async def test_closing_sends_what_is_still_queued() -> None:
    backend = SlowBackend(latency=0.01)
    outbox = EventOutbox(backend, WHERE, "engine-x", interval=60.0)
    first = outbox.put([(KERNEL, {"type": "kernel.state", "state": "idle"})])
    await first
    outbox.put([(KERNEL, {"type": "kernel.exited", "status": 0})])
    await outbox.close()
    assert [e["type"] for e in backend.events()] == ["kernel.state", "kernel.exited"]
    assert backend.posts[-1].state == "stopped"


async def test_stream_appends_of_different_cells_or_streams_are_not_merged() -> None:
    backend = SlowBackend(latency=0.01)
    outbox = EventOutbox(backend, WHERE, "engine-x", interval=60.0)
    await outbox.put([(KERNEL, {"type": "kernel.state", "state": "busy"})])
    appends = [
        ("c1", "stdout", "a"),
        ("c1", "stdout", "b"),
        ("c2", "stdout", "c"),
        ("c2", "stderr", "d"),
        ("c2", "stderr", "e"),
        ("c1", "stdout", "f"),
    ]
    for cell, name, text in appends:
        outbox.put(
            [
                (
                    KERNEL,
                    {
                        "type": "cell.stream",
                        "cell_id": cell,
                        "run_id": "r",
                        "name": name,
                        "text": text,
                    },
                )
            ]
        )
    await outbox.close()
    streamed = [
        (e["cell_id"], e["name"], e["text"]) for e in backend.events() if e["type"] == "cell.stream"
    ]
    assert streamed == [
        ("c1", "stdout", "ab"),
        ("c2", "stdout", "c"),
        ("c2", "stderr", "de"),
        ("c1", "stdout", "f"),
    ]
