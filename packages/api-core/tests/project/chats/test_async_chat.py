"""Parity tests for `AsyncChat` — exercises every method via the
async-with context manager. We use pytest-asyncio (auto mode in
`pyproject.toml`) so plain `async def test_*` functions just work.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alkera_core.project.chats.async_chat import AsyncChat
from alkera_core.project.chats.chat import ChatNotFoundError
from alkera_core.project.directory import ProjectDirectory
from alkera_core.project.locking import LockHeldError
from alkera_core.schemas.chat import (
    MessageCompleted,
    PartCreated,
    SessionCreated,
    TextPart,
)


def _store(tmp_path: Path):
    return ProjectDirectory(tmp_path / ".alkera").chats()


async def test_create_and_close_via_async(tmp_path: Path) -> None:
    store = _store(tmp_path)
    chat = await store.create_async(session_id="async-1", title="hello")
    assert isinstance(chat, AsyncChat)
    try:
        assert chat.session_id == "async-1"
        assert chat.manifest.title == "hello"
        # Initial session.created lives in jsonl already.
        events = [ev async for ev in chat.events()]
        assert len(events) == 1
        assert isinstance(events[0], SessionCreated)
    finally:
        await chat.close()


async def test_async_with_releases_on_exit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    async with await store.create_async(session_id="ctx") as chat:
        assert not chat.closed
    assert chat.closed
    # Lock file gone.
    assert not (store.path / "ctx" / ".lock").exists()


async def test_append_and_iter_events(tmp_path: Path) -> None:
    store = _store(tmp_path)
    async with await store.create_async(session_id="ev") as chat:
        sid = chat.session_id
        await chat.append_event(
            PartCreated(
                event_id="ev1",
                time=datetime.now(UTC),
                session_id=sid,
                part=TextPart(part_id="p1", message_id="m1", text="hi"),
            )
        )
        await chat.append_event(
            MessageCompleted(
                event_id="ev2",
                time=datetime.now(UTC),
                session_id=sid,
                message_id="m1",
                finish_reason="stop",
                tokens={"input": 10, "output": 5},
                cost=0.001,
            )
        )
        events = [ev async for ev in chat.events()]
        # session.created + part.created + message.completed
        assert len(events) == 3
        assert isinstance(events[0], SessionCreated)
        assert isinstance(events[1], PartCreated)
        assert isinstance(events[2], MessageCompleted)


async def test_add_blob_async(tmp_path: Path) -> None:
    store = _store(tmp_path)
    async with await store.create_async(session_id="blob") as chat:
        file_part = await chat.add_blob(b"async-bytes", filename="a.txt")
        assert file_part.size == 11
        assert store.blobs.exists(file_part.sha256)


async def test_open_async_after_close(tmp_path: Path) -> None:
    store = _store(tmp_path)
    async with await store.create_async(session_id="reopen") as chat:
        await chat.append_event(
            MessageCompleted(
                event_id="ev1",
                time=datetime.now(UTC),
                session_id=chat.session_id,
                message_id="m1",
                finish_reason="stop",
                tokens={"input": 1, "output": 1},
            )
        )

    async with await store.open_async("reopen") as chat2:
        events = [ev async for ev in chat2.events()]
        assert len(events) == 2  # session.created + message.completed
        assert chat2.manifest.tokens_total.input == 1


async def test_open_async_missing_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ChatNotFoundError):
        await store.open_async("nope")


async def test_open_async_when_already_locked_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    async with await store.create_async(session_id="busy"):
        with pytest.raises(LockHeldError):
            await store.open_async("busy")


async def test_fold_returns_event_list(tmp_path: Path) -> None:
    store = _store(tmp_path)
    async with await store.create_async(session_id="fold-test") as chat:
        await chat.append_event(
            PartCreated(
                event_id="ev1",
                time=datetime.now(UTC),
                session_id=chat.session_id,
                part=TextPart(part_id="p1", message_id="m1", text="A"),
            )
        )
        events = await chat.fold()
        assert len(events) == 2  # session.created + part.created
