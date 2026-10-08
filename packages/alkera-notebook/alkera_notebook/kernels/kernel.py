"""One live kernel: its process (through the launcher) and its connection.

:func:`start_kernel` opens a fresh endpoint and an ``RpcService`` with a
single-use token, launches the process with the allowlisted environment and
waits for the ``hello``. The returned :class:`KernelHandle` resolves
``exited`` exactly once with the reason the kernel went away:

- a reason the engine set before acting (``shutdown``, ``restart``,
  ``suspended``, ``out_of_memory``, ``interrupt_restart``);
- ``connection_lost`` when the connection drops: the kernel exits 0 by
  itself, or it lives on and the engine kills the process group (a kernel
  without its connection must not keep running);
- ``crashed`` when the process dies by a signal or with an error status.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from alkera_notebook.kernels.launcher import (
    KernelLauncher,
    KernelTransport,
    LaunchedKernel,
    LaunchSpec,
    kernel_env,
)
from alkera_notebook.rpc import frames as f
from alkera_notebook.rpc.peer import Call, MethodRegistry, PeerClosedError
from alkera_notebook.rpc.service import RpcService, ServiceSession, new_token
from alkera_notebook.tree_io import Tree

# How long a closed connection waits for the process to exit by itself
# before the kernel counts as having lost its connection.
CRASH_GRACE_S = 0.5


class KernelStartError(Exception):
    def __init__(self, reason: str, message: str) -> None:
        # Said to the person whose run it refused, as a sentence.
        message = message[:1].upper() + message[1:]
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class ExitInfo:
    reason: str
    exit_code: int | None = None
    message: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)


NotificationSink = Callable[["KernelHandle", str, dict[str, Any]], None]
RequestSink = Callable[["KernelHandle", Call], Awaitable[Any]]


class KernelHandle:
    def __init__(
        self,
        *,
        kernel_id: str,
        kind: str,
        launched: LaunchedKernel,
        service: RpcService,
        session: ServiceSession,
        data_dir: str,
        started_at: datetime,
        env_id: str | None,
    ) -> None:
        self.kernel_id = kernel_id
        self.kind = kind
        self.launched = launched
        self.service = service
        self.session = session
        self.data_dir = data_dir
        self.started_at = started_at
        self.env_id = env_id
        self.expected_reason: str | None = None
        self.exit_data: dict[str, Any] = {}
        self.exited: asyncio.Future[ExitInfo] = asyncio.get_running_loop().create_future()
        self._watch: asyncio.Task[None] | None = None

    @property
    def hello(self) -> dict[str, Any]:
        return self.session.hello

    @property
    def pid(self) -> int:
        return self.launched.pid

    @property
    def alive(self) -> bool:
        return not self.exited.done()

    def watch(self) -> None:
        self._watch = asyncio.get_running_loop().create_task(self._watch_loop())

    async def _watch_loop(self) -> None:
        wait_task = asyncio.ensure_future(self.launched.wait())
        closed_task = asyncio.ensure_future(self.session.wait_closed())
        done, _ = await asyncio.wait({wait_task, closed_task}, return_when=asyncio.FIRST_COMPLETED)
        code: int | None = None
        if wait_task not in done:
            # The connection closed first. A crashing process closes its socket
            # as it dies, so give it a moment to be seen exiting on its own.
            with contextlib.suppress(TimeoutError):
                code = await asyncio.wait_for(asyncio.shield(wait_task), CRASH_GRACE_S)
        if wait_task.done():
            code = wait_task.result()
            # The exit status alone tells the two apart. A kernel whose
            # connection drops exits 0 by itself; one that crashed died by a
            # signal (a segfault) or with an error status. Which of the two
            # the loop noticed first is no evidence: the kernel's exit and its
            # socket closing land together, in either order.
            reason = self.expected_reason or ("connection_lost" if code == 0 else "crashed")
        else:
            reason = self.expected_reason or "connection_lost"
        # The group always goes: a kernel without its connection must not keep
        # running, and children a kernel started must not outlive it.
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            self.launched.kill()
        if code is None:
            try:
                code = await asyncio.wait_for(asyncio.shield(wait_task), 10)
            except TimeoutError:
                code = None
        closed_task.cancel()
        message = self.session.peer.close_reason
        # Best effort: the kernel is already gone, and its exit must still be reported.
        with contextlib.suppress(Exception):
            await self.service.close()
        self.service.endpoint.remove()
        if not self.exited.done():
            self.exited.set_result(ExitInfo(reason, code, message, dict(self.exit_data)))

    def kill(self, reason: str, **data: Any) -> None:
        """Kill the process group now; ``exited`` reports ``reason``."""
        if self.expected_reason is None:
            self.expected_reason = reason
            self.exit_data.update(data)
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            self.launched.kill()

    def interrupt_signal(self) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            self.launched.signal(signal.SIGINT)

    async def request(self, method: str, params: Mapping[str, Any]) -> Any:
        return await self.session.peer.request(method, params)

    async def shutdown(self, reason: str, grace_s: float = 2.0) -> ExitInfo:
        if self.expected_reason is None:
            self.expected_reason = reason
        if self.alive:
            with contextlib.suppress(f.RpcError, PeerClosedError, TimeoutError, OSError):
                await asyncio.wait_for(self.request(f.KERNEL_SHUTDOWN, {}), grace_s)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(self.exited), grace_s)
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            self.launched.kill()
        return await self.exited

    def rss_bytes(self) -> int:
        if not self.alive:
            return 0
        try:
            return int(self.launched.rss_bytes())
        except (ProcessLookupError, OSError):
            return 0


async def start_kernel(
    *,
    kernel_id: str,
    kind: str,
    interpreter: str,
    notebook_dir: str,
    mount: str,
    launcher: KernelLauncher,
    transport: KernelTransport,
    base_env: Mapping[str, str],
    data_dir: str,
    hello_result: Mapping[str, Any],
    registry: MethodRegistry,
    connect_timeout_s: float,
    on_notification: NotificationSink,
    started_at: datetime,
    env_id: str | None,
    token: str | None = None,
) -> KernelHandle:
    if not Path(interpreter).exists():  # noqa: ASYNC240 - one stat before a launch
        raise KernelStartError("interpreter_missing", f"interpreter not found: {interpreter}")
    data = Path(data_dir)
    # The kernel's own directory, under the engine's data root: made without
    # following a link where the kernel's directory goes.
    Tree(data.parent, new_dir_mode=0o700).make_dirs(data.name)
    endpoint = transport.endpoint(kernel_id)
    token = token or new_token()
    holder: dict[str, KernelHandle] = {}
    early: list[tuple[str, dict[str, Any]]] = []

    async def _notified(method: str, params: dict[str, Any]) -> None:
        handle = holder.get("h")
        if handle is None:
            early.append((method, params))
        else:
            on_notification(handle, method, params)

    service = RpcService(
        endpoint,
        token=token,
        registry=registry,
        hello_result=dict(hello_result),
        on_notification=_notified,
        data_dir=data_dir,
    )
    await service.start()
    spec = LaunchSpec(
        interpreter=interpreter,
        notebook_dir=Path(notebook_dir),
        kernel_id=kernel_id,
        endpoint=endpoint.uri,
        token=token,
        mount=Path(mount),
        env=kernel_env(base_env),
        log_path=Path(data_dir) / "kernel.log",
        data_dir=Path(data_dir),
    )
    try:
        # A launch returns at once (the container launcher starts the kernel
        # on a thread of its own): the wait for the kernel is the handshake.
        launched: LaunchedKernel = launcher.launch(spec)
    except (OSError, ValueError) as exc:
        await service.close()
        endpoint.remove()
        raise KernelStartError("launch_failed", str(exc)) from exc

    async def _fail(reason: str, message: str) -> KernelStartError:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            launched.kill()
        # Best effort: the start already failed, and that error is the one to raise.
        with contextlib.suppress(Exception):
            await service.close()
        endpoint.remove()
        return KernelStartError(reason, message)

    accept = asyncio.ensure_future(service.accept())
    died = asyncio.ensure_future(launched.wait())
    done, _ = await asyncio.wait(
        {accept, died}, timeout=connect_timeout_s, return_when=asyncio.FIRST_COMPLETED
    )
    if accept not in done:
        accept.cancel()
        if died in done:
            log = _log_tail(launched)
            raise await _fail(
                "start_failed", f"kernel exited with code {died.result()} before connecting{log}"
            )
        died.cancel()
        raise await _fail("start_failed", "kernel did not connect in time")
    died.cancel()
    try:
        session = accept.result()
    except (PeerClosedError, OSError) as exc:
        raise await _fail("start_failed", f"handshake failed: {exc}") from exc
    handle = KernelHandle(
        kernel_id=kernel_id,
        kind=kind,
        launched=launched,
        service=service,
        session=session,
        data_dir=data_dir,
        started_at=started_at,
        env_id=env_id,
    )
    holder["h"] = handle
    for method, params in early:
        on_notification(handle, method, params)
    handle.watch()
    return handle


def _log_tail(launched: LaunchedKernel) -> str:
    tail = getattr(launched, "log_tail", None)
    if callable(tail):
        text = str(tail(2000)).strip()
        if text:
            return f": {text}"
    return ""
