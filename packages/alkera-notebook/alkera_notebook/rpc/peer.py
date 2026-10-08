"""One RPC connection over asyncio streams, symmetric: either side sends
requests, answers the other's, and sends notifications.

The service base (``service.py``) and the client base (``client.py``) both
wrap an ``RpcPeer``; the framing and the value codec come from ``frames``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from . import frames as f

log = logging.getLogger(__name__)


@dataclass
class Call:
    """One inbound request as a handler sees it: params decoded, segments
    resolved, plus the ``ctx`` the sender stamped."""

    method: str
    params: dict[str, Any]
    ctx: dict[str, Any] | None
    request_id: int
    peer: RpcPeer
    #: Whatever the owning service attached (for example the run a request
    #: was attributed to).
    extra: dict[str, Any] = field(default_factory=dict)


Handler = Callable[[Call], Awaitable[Any]]
NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class MethodSpec:
    name: str
    handler: Handler
    #: Accepted only while the run named in ``ctx`` executes (see ``RunScope``).
    run_scoped: bool = False


class MethodRegistry:
    """The methods one side hosts. A method added after a client connected is
    callable by name on the next request (nothing is negotiated per method)."""

    def __init__(self) -> None:
        self._methods: dict[str, MethodSpec] = {}

    def register(self, name: str, handler: Handler, *, run_scoped: bool = False) -> None:
        if name in self._methods:
            raise ValueError(f"method {name!r} is already registered")
        if name.startswith("$/") or name == f.HELLO_METHOD:
            raise ValueError(f"{name!r} is reserved by the protocol")
        self._methods[name] = MethodSpec(name, handler, run_scoped)

    def method(self, name: str, *, run_scoped: bool = False) -> Callable[[Handler], Handler]:
        def deco(fn: Handler) -> Handler:
            self.register(name, fn, run_scoped=run_scoped)
            return fn

        return deco

    def get(self, name: str) -> MethodSpec | None:
        return self._methods.get(name)

    def names(self) -> list[str]:
        return sorted(self._methods)


#: Decides whether an inbound request may run; raises ``RpcError`` to refuse.
#: Returns extra attribution to attach to the ``Call``.
Gate = Callable[[MethodSpec, f.Request], Mapping[str, Any]]


class PeerClosedError(ConnectionError):
    """The connection is gone; pending requests fail with this."""


class RpcPeer:
    """Requests, responses and notifications over one stream pair.

    - outbound requests above ``max_inflight`` wait in a local FIFO;
    - an inbound peer keeping more than ``max_inflight`` requests open, or a
      frame above ``recv_limit``, closes the connection;
    - cancelling the awaiting task of ``request`` sends ``$/cancelRequest``;
      an inbound ``$/cancelRequest`` cancels the handler, answered ``cancelled``.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        send_limit: int,
        recv_limit: int,
        registry: MethodRegistry | None = None,
        on_notification: NotificationHandler | None = None,
        gate: Gate | None = None,
        max_inflight: int = f.MAX_INFLIGHT,
        allow_files_inbound: bool = False,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self.send_limit = send_limit
        self.recv_limit = recv_limit
        self.registry = registry or MethodRegistry()
        self._on_notification = on_notification
        self._gate = gate
        self.max_inflight = max_inflight
        self._allow_files = allow_files_inbound
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[f.Response]] = {}
        self._slots = asyncio.Semaphore(max_inflight)
        self._inbound: dict[int, asyncio.Task[None]] = {}
        self._write_lock = asyncio.Lock()
        self._closed = asyncio.Event()
        self.close_reason: str | None = None
        self._frames = f.FrameReader(limit=recv_limit)

    # ------------------------------------------------------------------ sending

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def _send(self, message: f.Message) -> None:
        data = f.encode_message(message, limit=self.send_limit)
        async with self._write_lock:
            if self.closed:
                raise PeerClosedError(self.close_reason or "closed")
            self._writer.write(data)
            await self._writer.drain()

    async def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        await self._send(f.notification(method, params))

    async def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        ctx: Mapping[str, Any] | None = None,
        allow_files: bool = False,
    ) -> Any:
        """Send a request and return its decoded result; raises ``RpcError``
        for an error answer and ``PeerClosedError`` when the connection ends."""
        if self.closed:
            raise PeerClosedError(self.close_reason or "closed")
        async with self._slots:
            rid = self._next_id
            self._next_id += 1
            message = f.request(rid, method, params, ctx)
            future: asyncio.Future[f.Response] = asyncio.get_running_loop().create_future()
            self._pending[rid] = future
            try:
                await self._send(message)
                try:
                    response = await asyncio.shield(future)
                except asyncio.CancelledError:
                    if not self.closed:
                        # Best effort: a failed cancel must not mask the cancellation.
                        with contextlib.suppress(Exception):
                            await self.notify(f.CANCEL_METHOD, {"id": rid})
                    raise
            finally:
                self._pending.pop(rid, None)
        if response.error is not None:
            raise response.error
        return f.decode_value(response.result, response.segments, allow_files=allow_files)

    # ------------------------------------------------------------------ receiving

    async def serve(self) -> None:
        """Read frames until the connection ends. Returns when closed."""
        try:
            while not self.closed:
                data = await self._reader.read(1 << 16)
                if not data:
                    reason = "eof" if self._frames.at_frame_boundary() else "truncated"
                    await self.close(reason)
                    return
                for frame in self._frames.feed(data):
                    await self._dispatch(frame)
        except f.ProtocolError as exc:
            log.warning("rpc protocol violation: %s", exc)
            await self.close(exc.reason)
        except (ConnectionError, OSError):
            await self.close("connection_lost")
        finally:
            if not self.closed:
                await self.close("eof")

    async def _dispatch(self, frame: f.Frame) -> None:
        message = f.parse_message(frame)
        if isinstance(message, f.Response):
            future = self._pending.get(message.id)
            if future is not None and not future.done():
                future.set_result(message)
            return
        if isinstance(message, f.Notification):
            if message.method == f.CANCEL_METHOD:
                rid = message.params.get("id")
                task = self._inbound.get(rid) if isinstance(rid, int) else None
                if task is not None:
                    task.cancel()
                return
            if self._on_notification is not None:
                try:
                    params = f.decode_params(message.params, message.segments)
                except f.TagError as exc:
                    log.warning("dropped notification %s: %s", message.method, exc)
                    return
                await self._on_notification(message.method, params)
            return
        if len(self._inbound) >= self.max_inflight:
            await self.close("too_many_requests")
            return
        task = asyncio.ensure_future(self._answer(message))
        self._inbound[message.id] = task
        rid = message.id

        def forget(_task: asyncio.Task[None]) -> None:
            self._inbound.pop(rid, None)

        task.add_done_callback(forget)

    async def _answer(self, req: f.Request) -> None:
        try:
            spec = self.registry.get(req.method)
            if spec is None:
                raise f.RpcError.method_not_found(req.method)
            extra = dict(self._gate(spec, req)) if self._gate is not None else {}
            try:
                params = f.decode_params(req.params, req.segments, allow_files=self._allow_files)
            except f.TagError as exc:
                raise f.RpcError.invalid_params(str(exc)) from exc
            call = Call(req.method, params, req.ctx, req.id, self, extra)
            result = await spec.handler(call)
            response = f.result_response(req.id, result)
        except asyncio.CancelledError:
            response = f.error_response(req.id, f.RpcError.cancelled())
        except f.RpcError as exc:
            response = f.error_response(req.id, exc)
        except Exception as exc:
            log.exception("rpc handler %s failed", req.method)
            response = f.error_response(
                req.id, f.RpcError(f.ErrorCode.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
            )
        try:
            await self._send(response)
        except f.FrameTooLargeError as exc:
            await self._send(f.error_response(req.id, f.RpcError.too_large(exc.limit)))
        except PeerClosedError:
            pass

    # ------------------------------------------------------------------ closing

    async def close(self, reason: str = "closed") -> None:
        if self.closed:
            return
        self.close_reason = reason
        self._closed.set()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(PeerClosedError(reason))
        for task in list(self._inbound.values()):
            task.cancel()
        with contextlib.suppress(Exception):
            self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()
