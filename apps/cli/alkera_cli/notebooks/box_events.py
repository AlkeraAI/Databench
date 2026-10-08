"""A notebook's kernel events on their way from the box to the backend.

The events route (:class:`HttpKernelEvents`), what a box's relay hands it
(:class:`KernelEventSink`), and the queue that batches, numbers, retries and
bounds one notebook's events between the two (:class:`EventOutbox`). Every
size here comes from the notebooks' one budget per hop
(``alkera_core.notebooks.limits``)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Protocol

import httpx
from alkera_core.notebooks.limits import POST_MAX_BYTES, fit_event

from alkera_cli.host.backoff import doubled, exponential_delay
from alkera_cli.notebooks.store_loro import NOTEBOOKS, Located

if TYPE_CHECKING:
    from alkera_notebook.events.models import AnyEvent

logger = logging.getLogger(__name__)

#: The most events one post carries (the backend takes up to 500).
POST_BATCH: Final = 200
#: The most bytes of events (as JSON) one post carries, past its first event:
#: the notebooks' one budget per hop (``alkera_core.notebooks.limits``).
POST_BYTES: Final = POST_MAX_BYTES
#: The most text consecutive stream appends of one cell are merged into.
STREAM_MERGED_BYTES: Final = 64 * 1024
#: The least time between the starts of two posts of one notebook's events,
#: unless an answer is waiting or a full post's worth is queued.
POST_INTERVAL_SECONDS: Final = 0.25

# ---------------------------------------------------------------------------
# Posting the kernel's events
# ---------------------------------------------------------------------------


class KernelEventSink(Protocol):
    """Where a notebook's events go: the backend's events route."""

    async def post(
        self,
        where: Located,
        *,
        kernel_id: str,
        state: str | None,
        events: Sequence[Mapping[str, Any]],
    ) -> None: ...


#: The answers to a batch of events worth sending it again for: a bearer
#: replaced while it was on its way, a busy or restarting backend.
RETRIED_STATUSES: Final = frozenset({401, 408, 425, 429, 500, 502, 503, 504})
#: The most ordinary events one notebook keeps queued while the backend does
#: not take them. Past it the oldest are let go and the notebook's whole state
#: is posted instead (a snapshot), which is what a joining reader starts from
#: anyway. Answers to requests are never let go.
MAX_QUEUED_EVENTS: Final = 5_000
#: The pause before a batch the backend did not take is sent again: it doubles
#: from the first to the second. It paces both the route's own few attempts
#: and the outbox, where the batch keeps its place at the front.
EVENT_RETRY_SECONDS: Final = (0.5, 30.0)


class EventsNotDeliveredError(Exception):
    """A batch the backend did not take. ``retry`` says a later try may land
    (the backend was unreachable, busy or failing); otherwise it refused the
    batch itself and sending it again would only be refused again."""

    def __init__(self, why: str, *, retry: bool) -> None:
        super().__init__(why)
        self.retry = retry


@dataclass
class HttpKernelEvents:
    """``POST /api/v1/notebooks/{drive}/{item}/events`` on the box's own
    credential (the backend binds a new kernel to the folder's holder). A
    batch is sent again, a few times, when the answer says a later try may
    land (each event's number makes a repeat harmless); one that still does
    not land raises :class:`EventsNotDeliveredError`, and the outbox keeps it
    at the front and tries again."""

    http: httpx.AsyncClient
    attempts: int = 3
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    async def post(
        self,
        where: Located,
        *,
        kernel_id: str,
        state: str | None,
        events: Sequence[Mapping[str, Any]],
    ) -> None:
        why = ""
        for attempt in range(max(1, self.attempts)):
            if attempt:
                await self.sleep(
                    exponential_delay(
                        attempt - 1, first=EVENT_RETRY_SECONDS[0], cap=EVENT_RETRY_SECONDS[1]
                    )
                )
            try:
                response = await self.http.post(
                    f"{NOTEBOOKS}/{where.drive_id}/{where.item_id}/events",
                    json={"kernel_id": kernel_id, "state": state, "events": list(events)},
                )
            except httpx.TransportError as exc:
                why = f"{type(exc).__name__}: {exc}"
                continue
            if response.status_code == 200:
                return
            why = f"HTTP {response.status_code}"
            if response.status_code not in RETRIED_STATUSES:
                raise EventsNotDeliveredError(why, retry=False)
        raise EventsNotDeliveredError(why, retry=True)


def event_json(event: AnyEvent) -> dict[str, Any]:
    """An engine event as the wire carries it: buffers as standard base64."""
    buffers = getattr(event, "buffers", None)
    if buffers is None:
        return event.model_dump(mode="json")
    data = event.model_dump(mode="json", exclude={"buffers"})
    data["buffers"] = [base64.b64encode(bytes(b)).decode("ascii") for b in buffers]
    return data


def _state_of(events: Sequence[Mapping[str, Any]]) -> str | None:
    """The kernel state a batch leaves the kernel in, when it says one."""
    state: str | None = None
    for event in events:
        if event.get("type") == "kernel.state":
            state = str(event.get("state"))
        elif event.get("type") == "kernel.exited":
            state = "stopped"
    return state


# ---------------------------------------------------------------------------
# One notebook's outbox
# ---------------------------------------------------------------------------


@dataclass
class _Pending:
    kernel: str
    event: dict[str, Any]
    size: int
    urgent: bool = False
    #: Numbered for its kernel already: a post the backend did not take, now
    #: going again under the same number.
    numbered: bool = False
    #: Resolved once the post carrying this event has been made.
    waiters: list[asyncio.Future[None]] = field(default_factory=list)


def _size(event: Mapping[str, Any]) -> int:
    return len(json.dumps(event, separators=(",", ":"), default=str))


def _mergeable(last: _Pending, kernel: str, event: Mapping[str, Any]) -> bool:
    """Whether ``event`` is a stream append that may join ``last``: the same
    kernel, cell, run and stream, and the text kept under its bound."""
    before = last.event
    return (
        not last.urgent
        and last.kernel == kernel
        and event.get("type") == "cell.stream"
        and before.get("type") == "cell.stream"
        and before.get("cell_id") == event.get("cell_id")
        and before.get("run_id") == event.get("run_id")
        and before.get("name") == event.get("name")
        and len(str(before.get("text", ""))) + len(str(event.get("text", "")))
        <= STREAM_MERGED_BYTES
    )


class EventOutbox:
    """One notebook's events on their way to the backend.

    A kernel can make events far faster than one POST each can carry them (a
    cell printing in a loop, an interrupt's tracebacks): posted one by one
    they were a thousand requests in a minute and a half, which held up the
    box until the backend refused the next run as a silent machine. Events
    are queued instead and sent by one sender, one post in flight at a time
    and at most one begun per :data:`POST_INTERVAL_SECONDS`: each post
    carries what has queued since the last (up to :data:`POST_BATCH` events
    and :data:`POST_BYTES`), and consecutive stream appends of one cell are
    merged into one event. Order is kept and every
    kernel's events are numbered as they are sent, so the numbers stay
    consecutive however many were merged.

    An answer to a request is urgent: it goes in the next post, ahead of
    whatever events are still queued, so a burst never makes a person (or the
    backend's wait for the answer) sit behind it."""

    def __init__(
        self,
        sink: KernelEventSink,
        where: Located,
        engine_channel: str,
        *,
        interval: float = POST_INTERVAL_SECONDS,
        limit: int = MAX_QUEUED_EVENTS,
        on_overflow: Callable[[], None] | None = None,
        retry: tuple[float, float] = EVENT_RETRY_SECONDS,
    ) -> None:
        self._sink = sink
        self.where = where
        self.engine_channel = engine_channel
        self._interval = interval
        self._limit = limit
        self._on_overflow = on_overflow
        self._retry = retry
        #: How many queued events are ordinary (not answers), so the bound is
        #: checked without walking the queue.
        self._ordinary = 0
        self._next_at = 0.0
        self._pending: deque[_Pending] = deque()
        self._seq: dict[str, int] = {}
        self._wake = asyncio.Event()
        self._sender = asyncio.get_running_loop().create_task(self._send())

    def put(
        self, events: Sequence[tuple[str, Mapping[str, Any]]], *, urgent: bool = False
    ) -> asyncio.Future[None]:
        """Queue ``(kernel, event)`` pairs in order; the future resolves once
        the last of them has been posted (or its post failed for good)."""
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        if not events:
            done.set_result(None)
            return done
        if urgent:
            # Ahead of every queued event, behind earlier urgent ones.
            at = 0
            while at < len(self._pending) and self._pending[at].urgent:
                at += 1
            for offset, (kernel, event) in enumerate(events):
                one = fit_event(event)
                self._pending.insert(at + offset, _Pending(kernel, one, _size(one), urgent=True))
            self._pending[at + len(events) - 1].waiters.append(done)
        else:
            for kernel, event in events:
                last = self._pending[-1] if self._pending else None
                if last is not None and _mergeable(last, kernel, event):
                    last.event["text"] = str(last.event.get("text", "")) + str(event["text"])
                    last.size += len(str(event["text"]))
                else:
                    # Fitted as it is queued: an output too large for an
                    # event travels as a marker, and the post is sized right.
                    one = fit_event(event)
                    self._pending.append(_Pending(kernel, one, _size(one)))
                    self._ordinary += 1
            self._pending[-1].waiters.append(done)
            self._bound()
        self._wake.set()
        return done

    def _bound(self) -> None:
        """Let the oldest ordinary events go while more are queued than the
        limit, and ask for the notebook's whole state to stand in for them."""
        if self._ordinary <= self._limit:
            return
        kept: deque[_Pending] = deque()
        dropped: list[_Pending] = []
        excess = self._ordinary - self._limit
        for one in self._pending:
            if excess > 0 and not one.urgent and not one.numbered:
                dropped.append(one)
                excess -= 1
            else:
                kept.append(one)
        self._pending = kept
        self._ordinary -= len(dropped)
        _resolve(dropped)
        logger.warning(
            "notebook %s: %d events were not sent in time and give way to a snapshot",
            self.where.item_id,
            len(dropped),
        )
        if self._on_overflow is not None:
            self._on_overflow()

    def _take(self) -> list[_Pending]:
        """The next post: from the front, one kernel's events, bounded."""
        batch = self._take_batch()
        self._ordinary -= sum(1 for one in batch if not one.urgent)
        return batch

    def _take_batch(self) -> list[_Pending]:
        first = self._pending.popleft()
        batch, size = [first], first.size
        while (
            self._pending
            and len(batch) < POST_BATCH
            and self._pending[0].kernel == first.kernel
            and size + self._pending[0].size <= POST_BYTES
        ):
            one = self._pending.popleft()
            batch.append(one)
            size += one.size
        return batch

    async def _send(self) -> None:
        loop = asyncio.get_running_loop()
        pause = self._retry[0]
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._pending:
                wait = self._next_at - loop.time()
                if wait > 0 and not self._pending[0].urgent and len(self._pending) < POST_BATCH:
                    # Let more join this post; an answer or a full post's
                    # worth arriving meanwhile ends the wait.
                    self._wake.clear()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._wake.wait(), wait)
                    continue
                self._next_at = loop.time() + self._interval
                batch = self._take()
                kernel = batch[0].kernel
                events = [one.event for one in batch]
                for one in batch:
                    if one.numbered:
                        continue  # sent before and not taken: the same number again
                    one.numbered = True
                    self._seq[kernel] = self._seq.get(kernel, 0) + 1
                    one.event["seq"] = self._seq[kernel]
                    one.event["kernel_id"] = kernel
                state = _state_of(events)
                if kernel == self.engine_channel:
                    # No kernel runs: a start under way says so, nothing
                    # else may read as a kernel that runs.
                    state = "starting" if state == "starting" else "absent"
                try:
                    await self._sink.post(self.where, kernel_id=kernel, state=state, events=events)
                except EventsNotDeliveredError as exc:
                    if exc.retry:
                        # Back at the front, in order, numbered as they were:
                        # nothing after them goes first, and a repeat the
                        # backend did take is ignored by its number.
                        self._pending.extendleft(reversed(batch))
                        self._ordinary += sum(1 for one in batch if not one.urgent)
                        logger.info(
                            "notebook %s: events not taken (%s); again in %.1f s",
                            self.where.item_id,
                            exc,
                            pause,
                        )
                        await asyncio.sleep(pause)
                        pause = doubled(pause, floor=self._retry[0], cap=self._retry[1])
                        continue
                    logger.warning(
                        "notebook %s: a batch of %d events was refused (%s)",
                        self.where.item_id,
                        len(events),
                        exc,
                    )
                    _resolve(batch)
                except Exception:
                    logger.exception(
                        "notebook %s: a batch of events was not posted", self.where.item_id
                    )
                    _resolve(batch)
                else:
                    pause = self._retry[0]
                    _resolve(batch)

    async def close(self, *, within: float = 2.0) -> None:
        """Send what is queued (for up to ``within`` seconds, with no wait
        between posts), then stop."""
        if self._pending and not self._sender.done():
            last: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._pending[-1].waiters.append(last)
            self._interval = 0.0
            self._next_at = 0.0
            self._wake.set()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(last, within)
        self._sender.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._sender
        _resolve(list(self._pending))
        self._pending.clear()


def _resolve(items: Sequence[_Pending]) -> None:
    for one in items:
        for waiter in one.waiters:
            if not waiter.done():
                waiter.set_result(None)
