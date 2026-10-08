"""A folder whose lease is in doubt is written by nothing on the box: the
custody's verdict and the loop that hands it to every writer."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from alkera_cli.cloud import folder as folder_mod
from alkera_cli.cloud.folder import ChatFolders
from alkera_cli.files import folder_fence
from alkera_cli.files.mount import SelfFence


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _custody_with_live_plane(tmp_path: Path, clock: Clock) -> tuple[ChatFolders, SelfFence]:
    """A box's custody holding ``ws:a`` behind a running live sync, whose own
    fence closes after 30 s without a beat (two 15 s beats)."""
    folders = ChatFolders(chats_root=tmp_path / "chats", home=tmp_path / "home")
    fence = SelfFence(grace=30.0, monotonic=clock)
    run = folder_mod._LiveRun(
        sync=cast("Any", SimpleNamespace(fence=fence)),
        stop=threading.Event(),
        thread=threading.Thread(target=lambda: None),
    )
    folders._live["ws:a"] = run
    return folders, fence


def test_the_verdict_is_the_live_syncs_own_fence(tmp_path: Path) -> None:
    clock = Clock()
    folders, fence = _custody_with_live_plane(tmp_path, clock)
    assert not folders.fenced("ws:a")
    clock.now += 29.0
    assert not folders.fenced("ws:a")
    clock.now += 1.0
    assert folders.fenced("ws:a")  # the sync stopped sending: so must the kernels
    fence.beat()
    assert not folders.fenced("ws:a")
    assert not folders.fenced("ws:not-held")


class _DoneError(Exception):
    pass


@pytest.mark.asyncio
async def test_every_holder_is_told_on_each_pass_and_one_failing_never_stops_the_rest() -> None:
    told: list[bool] = []
    verdicts = [False, True]
    passes: list[float] = []

    async def broken(fenced: folder_fence.Fenced) -> None:
        raise RuntimeError("cgroup gone")

    async def kernels(fenced: folder_fence.Fenced) -> None:
        told.append(fenced("ws:a"))

    async def sleep(seconds: float) -> None:
        passes.append(seconds)
        if len(passes) == len(verdicts):
            raise _DoneError

    folder_fence.register_fence_holder(broken)
    folder_fence.register_fence_holder(kernels)
    try:
        with pytest.raises(_DoneError):
            await folder_fence.run(lambda key: verdicts[len(passes)], sleep=sleep, interval=2.0)
    finally:
        folder_fence._HOLDERS.remove(broken)
        folder_fence._HOLDERS.remove(kernels)
    assert told == [False, True]
    assert passes == [2.0, 2.0]
