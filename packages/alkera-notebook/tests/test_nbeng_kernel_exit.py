"""The reason a kernel handle reports when its process and its connection end.

The kernel's exit and its socket closing land at the same moment, so the
order the engine's loop happens to see them in must not change the reason.
These drive the handle's watch with both orders made explicit.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from alkera_notebook.kernels import kernel as kernel_mod
from alkera_notebook.kernels.kernel import KernelHandle


class FakeLaunched:
    """A launched process whose exit the test decides."""

    def __init__(self) -> None:
        self.status: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self.killed = False

    def exit(self, code: int) -> None:
        self.status.set_result(code)

    async def wait(self) -> int:
        return await asyncio.shield(self.status)

    def kill(self) -> None:
        self.killed = True
        if not self.status.done():
            self.status.set_result(-9)

    @property
    def pid(self) -> int:
        return 4242


class FakePeer:
    close_reason: str | None = "eof"

    def __init__(self) -> None:
        self.closed = asyncio.Event()

    async def wait_closed(self) -> None:
        await self.closed.wait()


class FakeSession:
    def __init__(self) -> None:
        self.peer = FakePeer()
        self.hello: dict[str, Any] = {}

    async def wait_closed(self) -> None:
        await self.peer.wait_closed()


class FakeEndpoint:
    def remove(self) -> None:
        return None


class FakeService:
    endpoint = FakeEndpoint()

    async def close(self) -> None:
        return None


def handle_for(launched: FakeLaunched, session: FakeSession) -> KernelHandle:
    return KernelHandle(
        kernel_id="k1",
        kind="python",
        launched=launched,
        service=FakeService(),
        session=session,
        data_dir="/nonexistent",
        started_at=datetime(2026, 10, 6, tzinfo=UTC),
        env_id=None,
    )


async def finish(handle: KernelHandle) -> kernel_mod.ExitInfo:
    # A hang guard only: every case resolves without waiting on the clock.
    return await asyncio.wait_for(asyncio.shield(handle.exited), 5)


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        # The kernel exits 0 when its connection drops; seeing the exit before
        # the socket close is the order a busy engine loop sees it in.
        pytest.param(0, "connection_lost", id="clean-exit-is-a-dropped-connection"),
        pytest.param(-11, "crashed", id="signal-is-a-crash"),
        pytest.param(1, "crashed", id="error-status-is-a-crash"),
    ],
)
async def test_the_exit_seen_before_the_close_is_read_by_its_status(code: int, reason: str) -> None:
    launched, session = FakeLaunched(), FakeSession()
    handle = handle_for(launched, session)
    launched.exit(code)
    handle.watch()
    info = await finish(handle)
    session.peer.closed.set()
    assert (info.reason, info.exit_code) == (reason, code)


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        pytest.param(0, "connection_lost", id="clean-exit"),
        pytest.param(-11, "crashed", id="signal"),
        pytest.param(1, "crashed", id="error-status"),
    ],
)
async def test_the_exit_and_the_close_seen_together_are_read_by_the_status(
    code: int, reason: str
) -> None:
    launched, session = FakeLaunched(), FakeSession()
    handle = handle_for(launched, session)
    session.peer.closed.set()
    launched.exit(code)
    handle.watch()
    info = await finish(handle)
    assert (info.reason, info.exit_code) == (reason, code)


async def test_a_kernel_that_outlives_its_connection_is_killed_as_connection_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kernel_mod, "CRASH_GRACE_S", 0.0)
    launched, session = FakeLaunched(), FakeSession()
    handle = handle_for(launched, session)
    session.peer.closed.set()
    handle.watch()
    info = await finish(handle)
    assert launched.killed
    assert (info.reason, info.exit_code) == ("connection_lost", -9)


async def test_a_reason_the_engine_set_wins_over_the_exit_status() -> None:
    launched, session = FakeLaunched(), FakeSession()
    handle = handle_for(launched, session)
    handle.expected_reason = "restart"
    launched.exit(-11)
    handle.watch()
    info = await finish(handle)
    session.peer.closed.set()
    assert info.reason == "restart"
