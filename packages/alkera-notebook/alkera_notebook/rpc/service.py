"""The service side of the RPC: a Unix socket for exactly one client, the
``hello`` handshake with a single-use token, the method registry and the run
scope that decides run-scoped requests.

A service owner (the notebook engine) creates one ``RpcService`` per kernel::

    endpoint = UnixEndpoint.create()
    service = RpcService(endpoint, token=new_token(), registry=methods,
                         hello_result={"kernel_id": ..., "settings": ...})
    await service.start()
    # launch the kernel with ALKERA_RPC_ENDPOINT=endpoint.uri and the token on stdin
    session = await service.accept(timeout=30)
    service.scope.begin(run_id)
    await session.peer.request("run.execute", {...})
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import os
import secrets
import shutil
import socket
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import frames as f
from .peer import MethodRegistry, MethodSpec, NotificationHandler, PeerClosedError, RpcPeer

#: macOS refuses Unix socket paths of 104 bytes or more.
SOCKET_PATH_MAX = 100
#: Short enough for that limit, unlike macOS's per-user ``$TMPDIR``; ``mkdtemp``
#: makes a private directory under it.
SOCKET_BASE = "/tmp"  # noqa: S108 - see above


def new_token() -> str:
    """A capability token: base64url of 32 random bytes, no padding."""
    return base64.urlsafe_b64encode(secrets.token_bytes(f.TOKEN_BYTES)).rstrip(b"=").decode()


@dataclass
class UnixEndpoint:
    """A private directory holding one socket path.

    The directory is created under ``/tmp`` (short enough for macOS) with mode
    0750; ``group`` restricts it further where the kernel runs as another uid.
    """

    directory: Path
    path: Path

    @classmethod
    def create(cls, *, base: str = SOCKET_BASE, group: int | None = None) -> UnixEndpoint:
        directory = Path(tempfile.mkdtemp(prefix="alknb-", dir=base))
        os.chmod(directory, 0o750)  # noqa: S103 - owner and the kernel group only
        if group is not None:
            os.chown(directory, -1, group)
        path = directory / "k.sock"
        if len(os.fsencode(str(path))) >= SOCKET_PATH_MAX:
            shutil.rmtree(directory, ignore_errors=True)
            raise ValueError(f"socket path {path} is too long for a Unix socket")
        return cls(directory, path)

    @property
    def uri(self) -> str:
        return f"unix:{self.path}"

    def remove(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)


def parse_endpoint(uri: str) -> str:
    """The socket path of an ``ALKERA_RPC_ENDPOINT`` value (``unix:<path>``)."""
    scheme, sep, path = uri.partition(":")
    if scheme != "unix" or not sep or not path:
        raise ValueError(f"unsupported endpoint {uri!r}; protocol 1 speaks only unix:<path>")
    return path


class RunScope:
    """Which runs are executing on this kernel, as the service knows it.

    A run-scoped request is accepted only while the run named in its ``ctx``
    is active, and it is attributed to that run. This is a time window, not
    containment: any code in the kernel can act within the active run. The
    owner opens a run before sending ``run.execute`` (``begin``); the kernel's
    ``run.finished`` closes it (``end``), and so does the owner.
    """

    def __init__(self) -> None:
        self._active: set[str] = set()
        self._ended: set[str] = set()

    def begin(self, run_id: str) -> None:
        if run_id in self._ended:
            raise ValueError(f"run {run_id!r} already ended")
        self._active.add(run_id)

    def end(self, run_id: str) -> None:
        if run_id in self._active:
            self._active.discard(run_id)
            self._ended.add(run_id)

    def end_all(self) -> None:
        for run_id in list(self._active):
            self.end(run_id)

    def is_active(self, run_id: str) -> bool:
        return run_id in self._active

    @property
    def active(self) -> frozenset[str]:
        return frozenset(self._active)

    def check(self, ctx: Mapping[str, Any] | None) -> dict[str, Any]:
        """The attribution for a run-scoped request, or ``forbidden``."""
        run_id = ctx.get("run_id") if ctx else None
        if not isinstance(run_id, str) or run_id not in self._active:
            raise f.RpcError.forbidden("outside_run", "this method is available only during a run")
        cell_id = ctx.get("cell_id") if ctx else None
        return {"run_id": run_id, "cell_id": cell_id if isinstance(cell_id, str) else None}


@dataclass
class ServiceSession:
    """The one connected client, after a successful ``hello``."""

    peer: RpcPeer
    hello: dict[str, Any]
    serve_task: asyncio.Task[None]
    #: Codecs the client said it can decode.
    codecs: frozenset[str] = field(default_factory=frozenset)

    async def wait_closed(self) -> None:
        await self.peer.wait_closed()


HelloCheck = Callable[[dict[str, Any]], None]


class RpcService:
    """Listens on ``endpoint`` for one client presenting ``token``.

    - the listener is closed after the first connection, so a second one is
      refused by the operating system; a racing second connection is closed;
    - a connection that sends nothing (or no ``hello``) within
      ``hello_timeout`` seconds is closed;
    - the token is single use: after one successful ``hello`` it is spent;
    - ``run.finished`` notifications from the client close the run in
      ``scope`` before the owner's notification handler sees them.
    """

    def __init__(
        self,
        endpoint: UnixEndpoint,
        *,
        token: str,
        registry: MethodRegistry,
        hello_result: Mapping[str, Any] | None = None,
        on_notification: NotificationHandler | None = None,
        check_hello: HelloCheck | None = None,
        hello_timeout: float = f.HELLO_TIMEOUT_S,
        recv_limit: int = f.FRAME_LIMIT_CLIENT,
        send_limit: int = f.FRAME_LIMIT_SERVICE,
        max_inflight: int = f.MAX_INFLIGHT,
        data_dir: str | None = None,
    ) -> None:
        self.endpoint = endpoint
        self._token: str | None = token
        self.registry = registry
        self._hello_result = dict(hello_result or {})
        self._on_notification = on_notification
        self._check_hello = check_hello
        self.hello_timeout = hello_timeout
        self.recv_limit = recv_limit
        self.send_limit = send_limit
        self.max_inflight = max_inflight
        self.data_dir = data_dir
        self.scope = RunScope()
        self._server: asyncio.AbstractServer | None = None
        self._session: asyncio.Future[ServiceSession] | None = None
        self._claimed = False
        self.refused: list[str] = []

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._session = loop.create_future()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(self.endpoint.path))
        os.chmod(self.endpoint.path, 0o660)
        sock.listen(1)
        self._server = await asyncio.start_unix_server(self._on_connect, sock=sock)

    async def accept(self) -> ServiceSession:
        """The session once a client completed ``hello`` (bound it with
        ``asyncio.wait_for``)."""
        if self._session is None:
            raise RuntimeError("start() the service first")
        return await asyncio.shield(self._session)

    def _stop_listening(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.endpoint.path)

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._claimed:
            self.refused.append("second_connection")
            writer.close()
            return
        self._claimed = True
        self._stop_listening()
        hello: dict[str, Any] = {}
        gate_open = asyncio.Event()

        def gate(spec: MethodSpec, req: f.Request) -> Mapping[str, Any]:
            if spec.run_scoped:
                return self.scope.check(req.ctx)
            return {}

        async def on_notification(method: str, params: dict[str, Any]) -> None:
            if not gate_open.is_set():
                await peer.close("notification_before_hello")
                return
            if method == f.EVENT_RUN_FINISHED and isinstance(params.get("run_id"), str):
                self.scope.end(params["run_id"])
            if self._on_notification is not None:
                await self._on_notification(method, params)

        hello_registry = _HelloRegistry(self.registry, gate_open)
        peer = RpcPeer(
            reader,
            writer,
            send_limit=self.send_limit,
            recv_limit=self.recv_limit,
            registry=hello_registry,
            on_notification=on_notification,
            gate=gate,
            max_inflight=self.max_inflight,
        )

        async def handle_hello(call: Any) -> dict[str, Any]:
            params = call.params
            token = params.get("token")
            expected = self._token
            if (
                expected is None
                or not isinstance(token, str)
                or not hmac.compare_digest(token.encode(), expected.encode())
            ):
                _close_soon(peer, "unauthorized")
                raise f.RpcError.unauthorized("the token is not valid")
            client = params.get("client")
            protocols = client.get("protocols") if isinstance(client, dict) else None
            if not isinstance(protocols, list) or f.PROTOCOL not in protocols:
                raise f.RpcError(f.ErrorCode.INVALID_REQUEST, "no common protocol version")
            if self._check_hello is not None:
                self._check_hello(params)
            self._token = None  # single use
            hello.update(params)
            gate_open.set()
            result: dict[str, Any] = {
                "protocol": f.PROTOCOL,
                "methods": self.registry.names(),
                "limits": {
                    "max_inflight": self.max_inflight,
                    "frame_in": self.recv_limit,
                    "frame_out": self.send_limit,
                },
                "data_dir": self.data_dir,
            }
            result.update(self._hello_result)
            return result

        hello_registry.hello = handle_hello
        serve_task = asyncio.ensure_future(peer.serve())
        opened = asyncio.ensure_future(gate_open.wait())
        ended = asyncio.ensure_future(peer.wait_closed())
        await asyncio.wait(
            {opened, ended}, timeout=self.hello_timeout, return_when=asyncio.FIRST_COMPLETED
        )
        opened.cancel()
        ended.cancel()
        if not gate_open.is_set():
            reason = peer.close_reason or "hello_timeout"
            self.refused.append(reason)
            await peer.close(reason)
            self._fail(PeerClosedError(reason))
            return
        session = ServiceSession(
            peer,
            hello,
            serve_task,
            frozenset(c for c in hello.get("codecs", []) if isinstance(c, str)),
        )
        if self._session is not None and not self._session.done():
            self._session.set_result(session)
        await peer.wait_closed()
        self.scope.end_all()

    def _fail(self, exc: BaseException) -> None:
        if self._session is not None and not self._session.done():
            self._session.set_exception(exc)

    async def close(self) -> None:
        self._stop_listening()
        if self._session is not None and self._session.done() and not self._session.cancelled():
            exc = self._session.exception()
            if exc is None:
                await self._session.result().peer.close("service_closed")
        elif self._session is not None:
            self._fail(PeerClosedError("service_closed"))
        self.scope.end_all()


class _HelloRegistry(MethodRegistry):
    """Before ``hello`` succeeds only ``hello`` is callable; afterwards
    ``hello`` is refused and the owner's registry answers."""

    def __init__(self, inner: MethodRegistry, gate_open: asyncio.Event) -> None:
        super().__init__()
        self._inner = inner
        self._gate_open = gate_open
        self.hello: Callable[[Any], Awaitable[Any]] | None = None

    def get(self, name: str) -> MethodSpec | None:
        if not self._gate_open.is_set():
            if name == f.HELLO_METHOD and self.hello is not None:
                return MethodSpec(name, self.hello)
            return MethodSpec(name, _refuse_before_hello)
        if name == f.HELLO_METHOD:
            return MethodSpec(name, _refuse_second_hello)
        return self._inner.get(name)

    def names(self) -> list[str]:
        return self._inner.names()


def _close_soon(peer: RpcPeer, reason: str) -> None:
    """Close after the error answer now being sent has been written."""
    loop = asyncio.get_running_loop()
    loop.call_soon(lambda: loop.create_task(peer.close(reason)))


async def _refuse_before_hello(call: Any) -> Any:
    _close_soon(call.peer, "request_before_hello")
    raise f.RpcError.unauthorized("hello first")


async def _refuse_second_hello(call: Any) -> Any:
    raise f.RpcError(f.ErrorCode.INVALID_REQUEST, "hello was already sent")
