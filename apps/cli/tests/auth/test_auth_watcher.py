"""Tests for the auth.yml mtime watcher."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alkera_cli.account.auth_watcher import AuthFileWatcher


@pytest.mark.asyncio
async def test_watcher_fires_on_file_create(tmp_path: Path):
    target = tmp_path / "auth.yml"
    fired = asyncio.Event()

    watcher = AuthFileWatcher(target, on_change=lambda: fired.set(), poll_interval_seconds=0.05)
    watcher.start_async()
    try:
        # File doesn't exist yet; the watcher's baseline is None.
        await asyncio.sleep(0.1)
        assert not fired.is_set()
        target.write_text("token: x\n")
        await asyncio.wait_for(asyncio.shield(_wait_event(fired)), timeout=1.0)
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_watcher_fires_on_file_delete(tmp_path: Path):
    target = tmp_path / "auth.yml"
    target.write_text("token: x\n")
    fired = asyncio.Event()

    watcher = AuthFileWatcher(target, on_change=lambda: fired.set(), poll_interval_seconds=0.05)
    watcher.start_async()
    try:
        await asyncio.sleep(0.1)  # let the baseline settle
        target.unlink()
        await asyncio.wait_for(_wait_event(fired), timeout=1.0)
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_watcher_fires_on_content_change(tmp_path: Path):
    target = tmp_path / "auth.yml"
    target.write_text("token: original\n")
    fired = asyncio.Event()

    watcher = AuthFileWatcher(target, on_change=lambda: fired.set(), poll_interval_seconds=0.05)
    watcher.start_async()
    try:
        await asyncio.sleep(0.1)
        # New token: different size + (probably) different mtime.
        target.write_text("token: refreshed-much-longer-value\n")
        await asyncio.wait_for(_wait_event(fired), timeout=1.0)
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_watcher_does_not_fire_when_nothing_changes(tmp_path: Path):
    target = tmp_path / "auth.yml"
    target.write_text("token: x\n")
    fired = asyncio.Event()

    watcher = AuthFileWatcher(target, on_change=lambda: fired.set(), poll_interval_seconds=0.05)
    watcher.start_async()
    try:
        await asyncio.sleep(0.3)
        assert not fired.is_set(), "watcher should not fire on a static file"
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_async_on_change_is_awaited(tmp_path: Path):
    target = tmp_path / "auth.yml"
    calls = 0

    async def _async_handler() -> None:
        nonlocal calls
        calls += 1

    watcher = AuthFileWatcher(target, on_change=_async_handler, poll_interval_seconds=0.05)
    watcher.start_async()
    try:
        await asyncio.sleep(0.1)
        target.write_text("a\n")
        for _ in range(150):
            if calls >= 1:
                break
            await asyncio.sleep(0.02)
        target.write_text("ab\n")
        for _ in range(150):
            if calls >= 2:
                break
            await asyncio.sleep(0.02)
        assert calls >= 2
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_stop_is_idempotent(tmp_path: Path):
    target = tmp_path / "auth.yml"
    watcher = AuthFileWatcher(target, on_change=lambda: None, poll_interval_seconds=0.05)
    watcher.start_async()
    await asyncio.sleep(0.05)
    await watcher.stop()
    await watcher.stop()  # must not raise


async def _wait_event(event: asyncio.Event) -> None:
    await event.wait()
