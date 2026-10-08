"""A stub for the opencode adapter's HTTP client, so tests can drive the public
operations (`send_prompt`, `cancel`, `clear`) with no server.

opencode publishes a run's whole tail BEFORE answering the request that caused
it, so `on_post` runs a test's callback from inside the awaited call: a frame
meets the adapter's bookkeeping where the real server delivers it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

#: The native session the stubbed adapter is pinned to.
NATIVE_SESSION = "oc-1"


class _Response:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None: ...

    def json(self) -> dict[str, Any]:
        return self._payload


class _StreamResponse:
    """A streamed connection's response: the SSE text it serves, then EOF."""

    def __init__(self, chunks: list[str]) -> None:
        self.headers = {"content-type": "text/event-stream"}
        self._chunks = chunks

    async def aiter_text(self) -> AsyncIterator[str]:
        for chunk in self._chunks:
            yield chunk


class _StreamContext:
    """What `client.stream(...)` hands back — held open for the life of the read."""

    def __init__(self, response: _StreamResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _StreamResponse:
        return self._response

    async def __aexit__(self, *_exc: object) -> None: ...


class StubTransport:
    """Records every path posted to, answers session creation with a fresh id, and
    lets a test deliver frames from inside a chosen POST or fail it."""

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.on_post: dict[str, Callable[[], Awaitable[None]]] = {}
        self.raises: dict[str, BaseException] = {}
        #: The `timeout=` each POST carried, by path — what tells a call that may
        #: run a model turn apart from a control call.
        self.post_timeouts: dict[str, Any] = {}
        #: Status the next GET answers with, by path. A path mapped to `None`
        #: refuses the request the way an unreachable server does.
        self.get_status: dict[str, int | None] = {}
        #: The `timeout=` each STREAMED connection carried, by path — the event
        #: stream's own budget, which none of the control calls share.
        self.stream_timeouts: dict[str, Any] = {}
        #: SSE text a streamed path serves before the server closes it.
        self.stream_text: dict[str, list[str]] = {}

    def stream(self, method: str, path: str, **kwargs: Any) -> _StreamContext:
        self.paths.append(path)
        self.stream_timeouts[path] = kwargs.get("timeout")
        return _StreamContext(_StreamResponse(self.stream_text.get(path, [])))

    async def post(self, path: str, json: dict[str, Any] | None = None, **kwargs: Any) -> _Response:
        self.paths.append(path)
        self.post_timeouts[path] = kwargs.get("timeout")
        hook = self.on_post.pop(path, None)
        if hook is not None:
            await hook()
        error = self.raises.pop(path, None)
        if error is not None:
            raise error
        return _Response({"id": "oc-2"})

    async def get(self, path: str, **_kw: Any) -> _Response:
        self.paths.append(path)
        status = self.get_status.get(path, 200)
        if status is None:
            raise httpx.ConnectError(f"refused {path}")
        return _Response({}, status_code=status)


def native_frames(*kinds: str, session: str = NATIVE_SESSION) -> list[dict[str, Any]]:
    """Native status frames by shorthand, in opencode's `{type, properties}`
    envelope. `session.idle` is the deprecated twin emitted beside every idle;
    `abort` and `error` are the same envelope told apart by the error's name."""
    status, error = "session.status", "session.error"
    shapes = {
        "busy": (status, {"sessionID": session, "status": {"type": "busy"}}),
        "idle": (status, {"sessionID": session, "status": {"type": "idle"}}),
        "session.idle": ("session.idle", {"sessionID": session}),
        "abort": (error, {"sessionID": session, "error": {"name": "MessageAbortedError"}}),
        "error": (error, {"sessionID": session, "error": {"data": {"message": "boom"}}}),
    }
    return [{"type": shapes[k][0], "properties": shapes[k][1]} for k in kinds]
