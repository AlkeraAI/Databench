"""An event stream read as the server writes it, from inside the process.

httpx's ``ASGITransport`` returns a response only once the application has
finished writing it, so a case that opens ``GET /api/v1/events`` through it can
read the stream only after it closed. A case that needed a frame therefore had
one lever: pin the stream's own deadline short and assert whatever had arrived
by the time it ran out. That makes the deadline the synchronisation, and a
wall-clock deadline is not a synchronisation primitive. Commit to ``NOTIFY`` to
the listener to the hub to the frame is several hops across a real database;
on a loaded box it takes longer than a window sized for an idle one, and the
case goes red for the machine with nothing about the plane changed.

This drives the ASGI application directly instead. One task runs the app, every
``http.response.body`` it writes lands in a queue as it is written, and the
reader hands frames out as they arrive. A case then waits FOR THE FRAME it is
about — under a ceiling loose enough that only a broken plane reaches it, and
with a message naming what never came — and ends the stream by disconnecting,
the way a browser closing a tab does, rather than by running the clock out.
Nothing here asks the stream's deadline to be short, so nothing here is decided
by how fast the host happened to be.

What a case still cannot get from this is the real socket: a disconnect a
kernel reports, a reconnect across a new connection. Those live over TCP in
``test_events_sse_live.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, MutableMapping
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlencode

from alkera_core.config import settings
from fastapi import FastAPI
from httpx import AsyncClient
from tests._live_window import live_window
from tests._wait_ceiling import wait_ceiling

STREAM = "/api/v1/events"

#: How long a wait for one frame may sit before it is called a hang. It proves
#: nothing by its length — the case asserts that the frame comes and what it
#: says, never that it came inside a particular second — so it is a share of
#: the run's own per-test budget rather than a number picked off an idle
#: laptop's clock (``_wait_ceiling``). Half the usual share, because a case
#: here waits on two boundaries in a row — the stream opening, then the frame —
#: and both still have to fail with their own sentence before the session
#: timeout fails the test with none.
FRAME_CEILING = wait_ceiling(0.05)

#: The moment a case keeps the stream open for after the frame it wanted, so
#: "and nothing else arrived" is a claim about a stream that stayed open rather
#: than about one closed the instant the first frame landed. A duplicate or a
#: second announcement would be one hub delivery behind the first, not seconds
#: behind it; the run's allowance covers the loaded box (``_live_window``).
QUIET = live_window(0.5)

#: What the route's own deadline is held at while a case here holds a stream
#: open. Comfortably above every ceiling above it, so a stream that owes a
#: frame is ended by the reader saying which frame — never by the clock running
#: out under it, which is the failure this module exists to remove.
STREAM_SECONDS = FRAME_CEILING * 3

#: The comment the route writes once it has subscribed and read its catch-up
#: page — the point after which a commit is a live event rather than history.
CONNECTED = "connected"


class EventStream:
    """One open event stream, read frame by frame as the server writes it.

    Built by :func:`open_event_stream`; a case never constructs one.
    """

    def __init__(self, app: FastAPI, scope: MutableMapping[str, Any]) -> None:
        self._app = app
        self._scope = scope
        self._chunks: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._started = asyncio.Event()
        self._disconnected = asyncio.Event()
        self._request_sent = False
        self._buffer = ""
        self._ended = False
        self._task: asyncio.Task[None] | None = None
        #: The status line and headers the server answered with.
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        #: Every frame read so far, in order, and the raw text they came from.
        self.frames: list[dict[str, str]] = []
        self.text = ""

    # -- what a case reads -------------------------------------------------

    @property
    def events(self) -> list[dict[str, str]]:
        """The named frames a client would act on — ``reset`` and ``error`` are
        control frames about the stream itself, not events in it."""
        return [f for f in self.frames if f.get("event") not in (None, "reset", "error")]

    @property
    def comments(self) -> list[str]:
        return [f["comment"] for f in self.frames if "comment" in f]

    async def opened(self) -> None:
        """Return once the server has written its opening frames.

        The route subscribes to the hub and reads its catch-up page BEFORE the
        response body begins, so a stream that has said ``connected`` is one
        that anything committed from here on reaches live rather than as
        history. That makes this a boundary the server crossed, where polling
        the hub's subscriber count is a level read that can miss the edge.
        """
        await self._started.wait()
        assert self.status == 200, f"the stream was refused with {self.status}"
        assert self.headers.get("content-type", "").startswith("text/event-stream"), self.headers
        await self.wait_for(
            lambda s: any(f.get("comment") == CONNECTED for f in s.frames),
            what=f"the opening {CONNECTED!r} comment",
        )

    async def wait_for(
        self,
        ready: Callable[[EventStream], bool],
        *,
        what: str,
        seconds: float = FRAME_CEILING,
    ) -> None:
        """Read frames until ``ready`` holds of what has arrived so far.

        The ceiling is a safety net, not the claim: it exists so a stream that
        will never deliver fails naming what it owed instead of stalling the
        run until the session timeout ends it with no sentence.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while not ready(self):
            if await self._next_frame(deadline) is None:
                raise AssertionError(
                    f"never saw {what}: {self._why(seconds)}. The stream carried {self._so_far()}"
                )

    async def wait_for_event(self, name: str, *, seconds: float = FRAME_CEILING) -> dict[str, str]:
        """The next frame named ``name``, however long the plane takes to
        deliver it — and a failure that says which event never came."""
        seen = len(self.frames)

        def arrived(stream: EventStream) -> bool:
            return any(f.get("event") == name for f in stream.frames[seen:])

        await self.wait_for(arrived, what=f"a {name!r} frame", seconds=seconds)
        return next(f for f in self.frames[seen:] if f.get("event") == name)

    async def quiet(self, seconds: float | None = None) -> None:
        """Keep reading until nothing has arrived for a moment (or the server
        closed the stream), collecting whatever does arrive.

        This is what makes "exactly one frame" a claim rather than an artefact
        of when the reader stopped looking.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (QUIET.seconds if seconds is None else seconds)
        while await self._next_frame(deadline) is not None:
            pass

    # -- the plumbing ------------------------------------------------------

    def _why(self, seconds: float | None = None) -> str:
        if self._ended:
            return "the server closed the stream"
        window = f"{seconds:.0f}s" if seconds is not None else f"{FRAME_CEILING:.0f}s"
        return f"nothing arrived for {window}"

    def _so_far(self) -> str:
        named = [f["event"] for f in self.frames if "event" in f]
        return f"events={named} comments={self.comments}"

    async def _next_frame(self, deadline: float) -> dict[str, str] | None:
        """The next complete frame, or ``None`` when the deadline passes or the
        server closed the stream (:attr:`_ended` says which)."""
        loop = asyncio.get_running_loop()
        while True:
            block, sep, rest = self._buffer.partition("\n\n")
            if sep:
                self._buffer = rest
                frame = _parse(block)
                if frame:
                    self.frames.append(frame)
                    return frame
                continue
            if self._ended:
                return None
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                chunk = await asyncio.wait_for(self._chunks.get(), remaining)
            except TimeoutError:
                return None
            if chunk is None:
                self._ended = True
                continue
            text = chunk.decode()
            self.text += text
            self._buffer += text

    async def _receive(self) -> MutableMapping[str, Any]:
        if not self._request_sent:
            self._request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._disconnected.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = int(message["status"])
            self.headers = {
                key.decode().lower(): value.decode() for key, value in message.get("headers", [])
            }
            self._started.set()
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
            if body:
                self._chunks.put_nowait(bytes(body))
            if not message.get("more_body", False):
                self._chunks.put_nowait(None)

    async def _run(self) -> None:
        try:
            await self._app(self._scope, self._receive, self._send)  # type: ignore[arg-type]
        finally:
            # A refusal that never reached ``http.response.start``, or an app
            # that raised, must still release a reader waiting on the queue.
            self._started.set()
            self._chunks.put_nowait(None)

    async def _start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="sse-reader")
        await self._started.wait()
        if self._task.done():  # a failure inside the app is the interesting one
            self._task.result()

    async def _aclose(self) -> None:
        """Disconnect and let the server tear the stream down, the way a closed
        tab does — so the slot and the subscription are released here rather
        than by a watchdog after the case has moved on."""
        self._disconnected.set()
        task, self._task = self._task, None
        if task is None:
            return
        try:
            await asyncio.wait_for(task, FRAME_CEILING)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _parse(block: str) -> dict[str, str]:
    """One ``text/event-stream`` block as its fields (``id`` / ``event`` /
    ``data`` / ``retry``, and ``comment`` for a ``:``-prefixed line)."""
    frame: dict[str, str] = {}
    for line in block.split("\n"):
        if not line:
            continue
        if line.startswith(":"):
            frame.setdefault("comment", line[1:].strip())
            continue
        key, _, value = line.partition(":")
        frame[key] = value.strip()
    return frame


@asynccontextmanager
async def open_event_stream(
    app: FastAPI,
    client: AsyncClient,
    *,
    path: str = STREAM,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> AsyncIterator[EventStream]:
    """Open ``path`` on ``app`` under ``client``'s session; close it on exit.

    ``client`` is read only for its cookie jar. The request itself never goes
    through httpx, because httpx's ASGI transport cannot hand back a body the
    application has not finished writing — which is the whole reason this
    module exists.

    For as long as the stream is open the route's own deadline is held above
    this module's ceiling, and put back on the way out. A reader that waits for
    a frame must not have the stream end under it — that IS the wall-clock
    window this module exists to remove — and a file whose other cases pin a
    short one, because the bound is what they are about, would otherwise hand
    it straight back to every case that opens one here.
    """
    raw: list[tuple[bytes, bytes]] = [(b"host", b"test"), (b"accept", b"text/event-stream")]
    cookie = "; ".join(f"{name}={value}" for name, value in client.cookies.items())
    if cookie:
        raw.append((b"cookie", cookie.encode()))
    raw.extend((key.lower().encode(), value.encode()) for key, value in (headers or {}).items())
    scope: MutableMapping[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": urlencode(params or {}).encode(),
        "root_path": "",
        "headers": raw,
        "server": ("test", 80),
        "client": ("127.0.0.1", 123),
    }
    stream = EventStream(app, scope)
    held = settings.realtime_sse_max_stream_seconds
    settings.realtime_sse_max_stream_seconds = STREAM_SECONDS
    try:
        await stream._start()
        yield stream
    finally:
        settings.realtime_sse_max_stream_seconds = held
        await stream._aclose()


__all__ = [
    "CONNECTED",
    "FRAME_CEILING",
    "QUIET",
    "STREAM",
    "STREAM_SECONDS",
    "EventStream",
    "open_event_stream",
]
