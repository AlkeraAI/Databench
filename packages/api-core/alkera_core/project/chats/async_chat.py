"""`AsyncChat` — thin async wrapper around `Chat`.

Why a wrapper instead of paired sync/async methods on `Chat`: the chat
file I/O is genuinely synchronous (filesystem syscalls), so the async
surface delegates via ``asyncio.to_thread``. Keeping the two
interfaces on separate classes makes each one obvious to the caller —
no `_async` suffixes everywhere.

The wrapped `Chat` holds the same OS lock that the sync flow would
hold. Either context manager is fine; just pick one per session.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

if TYPE_CHECKING:
    from alkera_core.project.chats.chat import Chat
    from alkera_core.schemas.chat import ChatManifest, Event, FilePart


class AsyncChat:
    """Async-friendly facade over a `Chat`.

    Constructed by ``ChatStore.create_async`` / ``ChatStore.open_async``.
    Don't instantiate directly.
    """

    def __init__(self, chat: Chat) -> None:
        self._chat = chat

    # ------------------------------------------------------------------
    # Properties pass through synchronously — no I/O.
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._chat.session_id

    @property
    def manifest(self) -> ChatManifest:
        return self._chat.manifest

    @property
    def path(self) -> Path:
        return self._chat.path

    @property
    def closed(self) -> bool:
        return self._chat.closed

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        await asyncio.to_thread(self._chat.close)

    async def __aenter__(self) -> AsyncChat:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Append + read
    # ------------------------------------------------------------------

    async def append_event(self, event: Event) -> None:
        await asyncio.to_thread(self._chat.append_event, event)

    async def events(self) -> AsyncIterator[Event]:
        """Async iterator over events.

        Pulls one event per `to_thread` call — fine for reasonable
        chat sizes; if we ever hit perf issues on giant logs we can
        switch to chunked batching.
        """
        # We can't `yield from` a synchronous iterator into an async
        # generator cleanly, so we step the underlying generator one
        # value at a time on a worker thread.
        sync_iter = self._chat.events()
        sentinel = object()
        while True:
            value = await asyncio.to_thread(next, sync_iter, sentinel)
            if value is sentinel:
                return
            yield value  # type: ignore[misc]

    async def fold(self) -> list[Event]:
        return await asyncio.to_thread(self._chat.fold)

    # ------------------------------------------------------------------
    # Blobs
    # ------------------------------------------------------------------

    async def add_blob(
        self,
        data: bytes | BinaryIO,
        *,
        filename: str,
        mime: str = "application/octet-stream",
    ) -> FilePart:
        return await asyncio.to_thread(self._chat.add_blob, data, filename=filename, mime=mime)


__all__ = ["AsyncChat"]
