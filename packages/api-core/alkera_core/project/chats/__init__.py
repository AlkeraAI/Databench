"""Per-chat handles + per-project chat store.

`ChatStore` is the public entry point — usually obtained via
``ProjectDirectory.chats()``. It returns sync `Chat` handles or
`AsyncChat` handles depending on the call (``open`` vs ``open_async``).
"""

from __future__ import annotations

from alkera_core.project.chats.async_chat import AsyncChat
from alkera_core.project.chats.blobs import DEFAULT_GC_GRACE_SECONDS, BlobStore, GcReport
from alkera_core.project.chats.chat import Chat, ChatNotFoundError
from alkera_core.project.chats.store import ChatStore

__all__ = [
    "DEFAULT_GC_GRACE_SECONDS",
    "AsyncChat",
    "BlobStore",
    "Chat",
    "ChatNotFoundError",
    "ChatStore",
    "GcReport",
]
