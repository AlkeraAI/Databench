"""A batch of a notebook's events the backend does not take is not lost.

The backend can be unreachable or busy for a while (a stalled database, a
restarting process). A batch it did not take stays at the front of the
notebook's queue, keeps the numbers it was sent with (so a repeat the
backend did take is ignored), and is sent again after a growing pause, ahead
of everything queued after it. A batch the backend refused outright is not
sent again. The queue itself is bounded: past the limit the oldest ordinary
events give way and the notebook posts its whole state instead; answers to
requests are never let go.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from alkera_cli.notebooks.box_events import EventOutbox, EventsNotDeliveredError, HttpKernelEvents
from alkera_cli.notebooks.store_loro import Located

WHERE = Located(drive_id="d", item_id="n")
KERNEL = "krn_delivery"


@dataclass
class FlakyBackend:
    """The events route: refuses the first ``failures`` posts with ``retry``."""

    failures: int = 0
    retry: bool = True
    taken: list[dict[str, Any]] = field(default_factory=list)
    attempts: int = 0

    async def post(
        self,
        where: Located,
        *,
        kernel_id: str,
        state: str | None,
        events: Sequence[Mapping[str, Any]],
    ) -> None:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise EventsNotDeliveredError("HTTP 503", retry=self.retry)
        self.taken.extend(dict(e) for e in events)


def _event(n: int) -> tuple[str, dict[str, Any]]:
    return KERNEL, {"type": "cell.status", "cell_id": f"c{n}", "status": "running"}


def _outbox(backend: FlakyBackend, **kw: Any) -> EventOutbox:
    return EventOutbox(backend, WHERE, "engine-x", interval=0.0, retry=(0.01, 0.04), **kw)


async def test_a_batch_the_backend_did_not_take_is_sent_again_in_order_with_its_numbers() -> None:
    backend = FlakyBackend(failures=3)
    outbox = _outbox(backend)
    try:
        await asyncio.wait_for(outbox.put([_event(n) for n in range(5)]), 10)
        await asyncio.wait_for(outbox.put([_event(n) for n in range(5, 8)]), 10)
    finally:
        await outbox.close()
    assert [e["cell_id"] for e in backend.taken] == [f"c{n}" for n in range(8)]
    assert [e["seq"] for e in backend.taken] == list(range(1, 9))
    assert backend.attempts >= 4


async def test_an_answer_queued_while_the_backend_is_away_still_arrives() -> None:
    backend = FlakyBackend(failures=2)
    outbox = _outbox(backend)
    try:
        answered = outbox.put([(KERNEL, {"type": "answer", "request_id": "r1"})], urgent=True)
        await asyncio.wait_for(answered, 10)
    finally:
        await outbox.close()
    assert [e["type"] for e in backend.taken] == ["answer"]


async def test_a_batch_the_backend_refused_is_not_sent_again() -> None:
    backend = FlakyBackend(failures=1, retry=False)
    outbox = _outbox(backend)
    try:
        await asyncio.wait_for(outbox.put([_event(0)]), 10)
        await asyncio.wait_for(outbox.put([_event(1)]), 10)
    finally:
        await outbox.close()
    assert [e["cell_id"] for e in backend.taken] == ["c1"]


async def test_a_queue_past_its_limit_lets_old_events_go_for_a_snapshot_and_keeps_answers() -> None:
    backend = FlakyBackend(failures=10_000)
    asked: list[bool] = []
    outbox = _outbox(backend, limit=10, on_overflow=lambda: asked.append(True))
    try:
        answer = outbox.put([(KERNEL, {"type": "answer", "request_id": "kept"})], urgent=True)
        for n in range(30):
            outbox.put([_event(n)])
        assert asked, "the notebook was asked for its whole state"
        queued = [one.event for one in outbox._pending]  # what is still to be sent
        ordinary = [e for e in queued if e["type"] != "answer"]
        assert len(ordinary) <= 10
        assert [e["cell_id"] for e in ordinary][-1] == "c29", "the newest are kept"
        assert any(e["type"] == "answer" for e in queued) or answer.done()
    finally:
        await outbox.close()


@pytest.mark.parametrize(
    ("status", "retry"),
    [pytest.param(503, True, id="busy"), pytest.param(422, False, id="refused")],
)
async def test_the_poster_says_whether_a_later_try_may_land(status: int, retry: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"code": "x"})

    poster = HttpKernelEvents(
        http=httpx.AsyncClient(base_url="http://backend", transport=httpx.MockTransport(handler)),
        sleep=lambda _s: asyncio.sleep(0),
    )
    with pytest.raises(EventsNotDeliveredError) as failed:
        await poster.post(WHERE, kernel_id=KERNEL, state=None, events=[{"type": "x"}])
    assert failed.value.retry is retry


def test_the_box_s_budgets_are_the_notebooks_one_budget() -> None:
    """The box's post and the engine's out-of-line threshold come from the
    one owner of every hop's ceiling, so the box never sends what the
    backend's outbox or a reader's frame would refuse."""
    from alkera_cli.notebooks.box_events import POST_BYTES
    from alkera_core.notebooks import limits
    from alkera_notebook.engine.config import OutputLimits

    assert POST_BYTES == limits.POST_MAX_BYTES
    assert OutputLimits().blob_threshold_bytes == limits.INLINE_VALUE_MAX_BYTES


async def test_an_output_too_large_for_one_event_is_queued_as_a_marker() -> None:
    import base64

    from alkera_core.notebooks.limits import EVENT_MAX_BYTES, TOO_LARGE_MIME, json_bytes

    backend = FlakyBackend()
    outbox = _outbox(backend)
    image = base64.b64encode(b"\x89PNG" + b"\x00" * 5_500_000).decode("ascii")
    try:
        await asyncio.wait_for(
            outbox.put(
                [(KERNEL, {"type": "cell.output", "cell_id": "c1", "output": {"image/png": image}})]
            ),
            10,
        )
    finally:
        await outbox.close()
    (sent,) = backend.taken
    assert json_bytes(sent) <= EVENT_MAX_BYTES
    assert TOO_LARGE_MIME in sent["output"]


async def test_a_large_output_travels_as_a_reference_to_a_blob_beside_the_notebook(
    tmp_path: Any,
) -> None:
    """The output is not lost: its value is stored where the notebook's
    saved outputs are (served by hash), and the event carries the reference."""
    import base64
    import hashlib
    from pathlib import Path
    from types import SimpleNamespace

    from alkera_cli.notebooks.box import _Relay
    from alkera_notebook.events.queue import ClientQueue
    from alkera_notebook.outputs.snapshot import REF_MIME, NotebookPlace, blob_dir

    notebook = Path(tmp_path) / "an.alknb.py"
    notebook.write_text("", encoding="utf-8")
    backend = FlakyBackend()

    async def no_view() -> Any:
        raise AssertionError("the queue never overflows here")

    client = SimpleNamespace(
        queue=ClientQueue(maxlen=100, view=no_view), detach=lambda: asyncio.sleep(0)
    )
    session = SimpleNamespace(
        attach=lambda actor: client,
        runtime=SimpleNamespace(
            kernel=SimpleNamespace(kernel_id=KERNEL),
            frames={},
            outputs={},
            place=NotebookPlace.beside(notebook),
        ),
    )
    relay = _Relay(session, WHERE, backend)  # type: ignore[arg-type]
    image = base64.b64encode(b"\x89PNG" + b"\x01" * 600_000).decode("ascii")
    try:
        await asyncio.wait_for(
            relay.post(
                [
                    {
                        "type": "cell.output",
                        "cell_id": "c1",
                        "output": {"image/png": image, "text/plain": "<Figure>"},
                    }
                ]
            ),
            10,
        )
    finally:
        await relay.close()
    (sent,) = [e for e in backend.taken if e.get("type") == "cell.output"]
    ref = sent["output"]["image/png"][REF_MIME]
    assert sent["output"]["text/plain"] == "<Figure>"
    stored = blob_dir(notebook) / f"{ref['sha256']}.png"
    assert stored.read_bytes() == base64.b64decode(image)
    assert hashlib.sha256(stored.read_bytes()).hexdigest() == ref["sha256"]


def test_the_largest_event_a_box_can_send_is_what_the_kernel_lets_one_cell_hold() -> None:
    """A box that does not fit its events yet sends one whole; the edge and
    the backend take it at its largest so the backend can fit it."""
    from _alkera_kernel.runtime import CELL_OUTPUT_LIMIT
    from alkera_core.notebooks import limits
    from alkera_notebook.engine.config import OutputLimits

    assert CELL_OUTPUT_LIMIT == limits.KERNEL_OUTPUT_MAX_BYTES
    assert OutputLimits().rich_bytes_per_cell == limits.KERNEL_OUTPUT_MAX_BYTES
