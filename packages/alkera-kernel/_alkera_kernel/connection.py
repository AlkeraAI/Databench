"""The kernel's side of the RPC connection: a blocking Unix socket, a reader
thread that owns it, and thread-safe sends.

The reader parses frames and hands each inbound request or notification to
the dispatcher; it never runs user code. Requests the kernel sends (for
example ``sql.execute`` from a cell) block the calling thread until the
answer arrives; above ``max_inflight`` they wait for a slot.
"""

from __future__ import annotations

import contextlib
import os
import socket
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from typing import Any

from . import _frames as f

#: The smaller of the platforms' ``sun_path`` sizes (macOS), less the NUL.
SUN_PATH_MAX = 103

RequestHandler = Callable[[f.Request], None]
NotificationHandler = Callable[[f.Notification], None]


class ConnectionClosedError(ConnectionError):
    pass


class Connection:
    def __init__(self, sock: socket.socket, *, max_inflight: int = f.MAX_INFLIGHT) -> None:
        self._sock = sock
        self._send_lock = threading.Lock()
        self._id_lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, Future[f.Response]] = {}
        self._slots = threading.BoundedSemaphore(max_inflight)
        self._closed = threading.Event()
        self.send_limit = f.FRAME_LIMIT_CLIENT
        self.recv_limit = f.FRAME_LIMIT_SERVICE
        self.on_request: RequestHandler | None = None
        self.on_notification: NotificationHandler | None = None
        self.on_close: Callable[[str], None] | None = None
        self._reader: threading.Thread | None = None
        self._frames = f.FrameReader(limit=self.recv_limit)
        #: Frames that arrived with the hello answer, for the reader thread.
        self._backlog: list[f.Frame] = []

    @classmethod
    def connect(cls, path: str, *, then_chdir: str | None = None) -> Connection:
        """Connect to the Unix socket at ``path``. A path longer than
        ``sun_path`` allows (104 bytes on macOS, 108 on Linux) is reached by
        changing into its directory and connecting by relative name; the
        working directory is then ``then_chdir`` (the notebook's folder)."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if len(os.fsencode(path)) < SUN_PATH_MAX:
            sock.connect(path)
        else:
            directory, name = os.path.split(path)
            os.chdir(directory)
            try:
                sock.connect(name)
            finally:
                os.chdir(then_chdir or "/")
        return cls(sock)

    def fileno(self) -> int:
        return self._sock.fileno()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def set_max_inflight(self, n: int) -> None:
        self._slots = threading.BoundedSemaphore(max(1, n))

    # ------------------------------------------------------------------ sending

    def send(self, message: f.Message) -> None:
        data = f.encode_message(message, limit=self.send_limit)
        with self._send_lock:
            if self.closed:
                raise ConnectionClosedError("closed")
            try:
                self._sock.sendall(data)
            except OSError as exc:
                self._close("send_failed")
                raise ConnectionClosedError(str(exc)) from exc

    def notify(self, method: str, params: Mapping[str, Any]) -> None:
        self.send(f.notification(method, params))

    def respond(self, rid: int, result: Any) -> None:
        try:
            self.send(f.result_response(rid, result))
        except f.FrameTooLargeError as exc:
            self.send(f.error_response(rid, f.RpcError.too_large(exc.limit)))

    def respond_error(self, rid: int, error: f.RpcError) -> None:
        self.send(f.error_response(rid, error))

    def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        ctx: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Send a request and block for its decoded result."""
        with self._slots:
            with self._id_lock:
                rid = self._next_id
                self._next_id += 1
            future: Future[f.Response] = Future()
            self._pending[rid] = future
            try:
                self.send(f.request(rid, method, params, ctx))
                try:
                    response = future.result(timeout)
                except BaseException:
                    # Interrupted (KeyboardInterrupt) or timed out: tell the peer.
                    if not self.closed:
                        with contextlib.suppress(Exception):
                            self.notify(f.CANCEL_METHOD, {"id": rid})
                    raise
            finally:
                self._pending.pop(rid, None)
        if response.error is not None:
            raise response.error
        return f.decode_value(response.result, response.segments, allow_files=True)

    # ------------------------------------------------------------------ receiving

    def start_reader(self) -> None:
        self._reader = threading.Thread(
            target=self._read_loop, name="alkera-rpc-reader", daemon=True
        )
        self._reader.start()

    def read_one(self, timeout: float) -> f.Message:
        """Block for exactly one message (the ``hello`` answer, before the
        reader thread starts)."""
        self._sock.settimeout(timeout)
        try:
            while not self._backlog:
                data = self._sock.recv(1 << 16)
                if not data:
                    raise ConnectionClosedError("closed during hello")
                self._backlog.extend(self._frames.feed(data))
            # The service may send its first request right behind the answer;
            # those frames stay queued for the reader thread.
            return f.parse_message(self._backlog.pop(0))
        finally:
            self._sock.settimeout(None)

    def _read_loop(self) -> None:
        reader = self._frames
        reason = "eof"
        try:
            backlog, self._backlog = self._backlog, []
            for frame in backlog:
                self._dispatch(f.parse_message(frame))
            while True:
                data = self._sock.recv(1 << 16)
                if not data:
                    break
                for frame in reader.feed(data):
                    self._dispatch(f.parse_message(frame))
        except f.ProtocolError as exc:
            reason = exc.reason
        except OSError:
            reason = "connection_lost"
        self._close(reason)

    def _dispatch(self, message: f.Message) -> None:
        if isinstance(message, f.Response):
            future = self._pending.get(message.id)
            if future is not None and not future.done():
                future.set_result(message)
        elif isinstance(message, f.Notification):
            if self.on_notification is not None:
                self.on_notification(message)
        elif self.on_request is not None:
            self.on_request(message)

    def _close(self, reason: str) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(ConnectionClosedError(reason))
        try:
            self._sock.close()
        except OSError:
            pass
        if self.on_close is not None:
            self.on_close(reason)

    def close_in_child(self) -> None:
        """After ``fork``: drop the inherited socket without touching the
        parent's connection (no shutdown, no frames)."""
        self._closed.set()
        try:
            os.close(self._sock.detach())
        except OSError:
            pass
