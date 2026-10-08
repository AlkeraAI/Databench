"""A real backend the CLI's tests can kill and start again on the same port.

:func:`files._live_backend.live_backend` serves the API for one whole test.
This serves it the same way (one event loop on a thread, a separate content
origin, a filesystem store under ``tmp_path``) but on a socket bound once and
kept, so :meth:`RestartableBackend.crash` can end the API abruptly, as a
killed process ends, and start a fresh application in its place: a new
realtime runtime with a cold sandbox pool, its startup sweep, and every client
reconnecting to the same address. The box and the people in a test hold that
address and never learn the server went away except as their sockets drop.

What a crash cannot take with it in one process is cleaned up after it, as an
operating system would: the dead runtime's sandbox workers, its listener and
its sweeper are stopped once the server task is gone, without running the
lifespan's graceful shutdown (which a killed process never runs). So is every
task the dead API had started (a request, a socket's loop, a write back): a
killed process's work stops with it, and the transactions it held open end,
where a task left running on the shared loop would hold its row locks for as
long as it lived.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import socket
import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import uvicorn
from alkera_core.config import settings
from alkera_core.files.clock import SystemClock
from alkera_core.files.store.scoped import FilesystemScoped
from backend.services.files.store import set_store_factory
from files._live_backend import (
    LiveBackend,
    RequestLog,
    _content_origin,
    _Counting,
    _dispose_in_loop,
    _fresh_pool,
    _open_the_quota,
    _seed_user,
)

#: How long a fresh API may take to bind and reach "this replica can deliver".
READY_SECONDS = 120.0

#: Which incarnation of the API a task belongs to: set around the server's
#: start, so every task it creates (each inherits its creator's context)
#: carries it, and a crash finds them all.
_INCARNATION: contextvars.ContextVar[int] = contextvars.ContextVar("api_incarnation", default=0)


@dataclass
class RestartableBackend(LiveBackend):
    _loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)
    _port: int = 0
    _api: tuple[uvicorn.Server, asyncio.Task[None], Any] | None = field(default=None, repr=False)
    restarts: int = 0
    _incarnation: int = field(default=0, repr=False)
    org_id: uuid.UUID | None = None

    def _run(self, coro: Any) -> Any:
        assert self._loop is not None
        done: Future[Any] = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return done.result(timeout=READY_SECONDS + 30)

    def crash(self) -> None:
        """End the API as a killed process ends, and start a fresh one."""
        self._run(self._crash_and_start())
        self.restarts += 1

    async def _crash_and_start(self) -> None:
        await self._kill()
        await self._start()

    async def _kill(self) -> None:
        from backend.services.realtime.runtime import runtime_of

        if self._api is None:
            return
        server, task, app = self._api
        self._api = None
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        incarnation = self._incarnation
        orphans = [
            other
            for other in asyncio.all_tasks()
            if other is not asyncio.current_task()
            and other.get_context().get(_INCARNATION) == incarnation
        ]
        for orphan in orphans:
            orphan.cancel()
        if orphans:
            await asyncio.wait(orphans, timeout=10)
        # The process is gone, and with it its listening socket (or the next
        # server cannot bind the port) and every connection it held: they are
        # cut, not closed, as a killed process's are.
        for listening in getattr(server, "servers", []):
            listening.close()
        for connection in list(server.server_state.connections):
            transport = getattr(connection, "transport", None)
            if transport is not None:
                transport.abort()
        runtime = runtime_of(app)
        if runtime is None:
            return
        if runtime.sweeper is not None:
            runtime.sweeper.cancel()
        if runtime.crdt is not None:
            with contextlib.suppress(Exception):
                await runtime.crdt.aclose()
            with contextlib.suppress(Exception):
                await runtime.crdt.pool.close()
        if runtime.listener is not None:
            with contextlib.suppress(Exception):
                await runtime.listener.stop()

    async def _start(self) -> None:
        from backend.app_factory import create_app
        from backend.services.realtime.runtime import runtime_of

        # A socket bound afresh on the same port each start: the server that
        # held the last one closed it as it went.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", self._port))
        self._incarnation += 1
        _INCARNATION.set(self._incarnation)
        app = create_app()
        server = uvicorn.Server(
            uvicorn.Config(
                _Counting(app, self.log),
                host="127.0.0.1",
                port=self._port,
                log_level="warning",
                lifespan="on",
                timeout_graceful_shutdown=5,
            )
        )
        task = asyncio.create_task(server.serve(sockets=[sock]))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + READY_SECONDS
        while not server.started:
            if task.done():
                task.result()
                raise RuntimeError("the API exited before it started")
            if loop.time() >= deadline:
                raise RuntimeError("the API did not start")
            await asyncio.sleep(0.02)
        runtime = runtime_of(app)
        if runtime is not None and runtime.listener is not None:
            await runtime.listener.wait_connected(READY_SECONDS)
        if runtime is not None and runtime.crdt is not None and self.org_id is not None:
            # The suite starts no sweeper (it would reach every earlier test's
            # sessions in this database); a restarted API's own, scoped to the
            # org this test seeded, is part of what a restart is.
            runtime.crdt.sweep_orgs = frozenset({self.org_id})
            runtime.crdt.start_sweeper()
        self._api = (server, task, app)

    async def _stop(self) -> None:
        if self._api is None:
            return
        server, task, _app = self._api
        self._api = None
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=15)
        except TimeoutError:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


@contextlib.contextmanager
def restartable_backend(tmp_path: Path) -> Iterator[RestartableBackend]:
    """The backend and the content origin on one loop, the API restartable."""
    store_root = tmp_path / "files-store"
    store_root.mkdir(parents=True, exist_ok=True)
    previous = (
        settings.files_enabled,
        settings.files_store_provider,
        settings.files_store_root,
        settings.files_inline_operations,
        settings.files_content_base_url,
        settings.files_lease_ttl_seconds,
    )
    settings.files_enabled = True
    settings.files_store_provider = "filesystem"
    settings.files_store_root = store_root
    settings.files_inline_operations = True
    settings.files_lease_ttl_seconds = 3600
    set_store_factory(FilesystemScoped(store_root, clock=SystemClock()))

    _fresh_pool()
    seeded = asyncio.run(_seed_user(box=False, machine=False))
    _fresh_pool()
    log = RequestLog()
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    content = uvicorn.Server(
        uvicorn.Config(
            _Counting(_content_origin(log), log),
            host="127.0.0.1",
            port=0,
            log_level="warning",
            lifespan="on",
        )
    )
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    backend = RestartableBackend(
        base_url=f"http://127.0.0.1:{port}",
        content_base_url="",
        token=seeded.token,
        log=log,
        _loop=loop,
        _port=port,
        org_id=seeded.org_id,
    )
    content_task: Future[None] = asyncio.run_coroutine_threadsafe(content.serve(), loop)
    try:
        for _ in range(5000):
            if content.started:
                break
            threading.Event().wait(0.02)
        backend.content_base_url = (
            f"http://127.0.0.1:{content.servers[0].sockets[0].getsockname()[1]}"
        )
        settings.files_content_base_url = backend.content_base_url
        backend._run(backend._start())
        _open_the_quota(backend.base_url, backend.token)
        log.clear()
        yield backend
    finally:
        with contextlib.suppress(Exception):
            backend._run(backend._stop())
        content.should_exit = True
        with contextlib.suppress(Exception):
            content_task.result(timeout=15)
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(_dispose_in_loop(), loop).result(timeout=15)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)
        _fresh_pool()
        set_store_factory(None)
        (
            settings.files_enabled,
            settings.files_store_provider,
            settings.files_store_root,
            settings.files_inline_operations,
            settings.files_content_base_url,
            settings.files_lease_ttl_seconds,
        ) = previous
