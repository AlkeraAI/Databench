"""Per-account short-TTL caches for authenticated backend reads."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

import structlog

log = structlog.get_logger(__name__)

T = TypeVar("T")
AccountKey = tuple[str, str]


class ReadThroughCache(Generic[T]):
    """Own one isolated lane of cached authenticated reads.

    A lane coalesces concurrent reads per account, serves a stale value when a
    refresh fails, and briefly cools failed keys before retrying. ``clear``
    detaches old flights without cancelling their waiters; a detached result may
    answer those existing waiters but can never repopulate the cleared lane.
    """

    def __init__(
        self,
        *,
        cooldown_seconds: float,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock or time.monotonic
        self._entries: dict[AccountKey, tuple[float, T]] = {}
        self._inflight: dict[AccountKey, asyncio.Task[T]] = {}
        self._retry_after: dict[AccountKey, float] = {}
        self._generation = 0

    def clear(self) -> None:
        """Forget cached state while allowing existing waiters to settle."""
        self._generation += 1
        self._entries.clear()
        self._inflight.clear()
        self._retry_after.clear()

    async def read(
        self,
        key: AccountKey,
        fetch: Callable[[], Awaitable[T]],
        *,
        ttl_seconds: float,
    ) -> T | None:
        """Return a fresh value or one shared fetch, degrading to stale on failure."""
        now = self._clock()
        cached = self._entries.get(key)
        if cached is not None and now - cached[0] < ttl_seconds:
            return cached[1]
        known = cached[1] if cached is not None else None
        if now < self._retry_after.get(key, 0.0):
            return known

        task = self._inflight.get(key)
        if task is None:
            generation = self._generation
            task = asyncio.create_task(self._fetch(key, fetch(), generation))
            task.add_done_callback(self._consume_exception)
            self._inflight[key] = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            reader = asyncio.current_task()
            if reader is None or reader.cancelling():
                raise
            return known
        except Exception:
            log.warning("auth.read_cache.fetch_failed", api_url=key[0], exc_info=True)
            return known

    async def _fetch(
        self,
        key: AccountKey,
        pending: Awaitable[T],
        generation: int,
    ) -> T:
        """Publish one owned flight before making the key available again."""
        task = asyncio.current_task()
        try:
            value = await pending
        except asyncio.CancelledError:
            self._record_failure(key, task, generation)
            raise
        except Exception:
            self._record_failure(key, task, generation)
            raise
        else:
            self._record_success(key, task, generation, value)
            return value
        finally:
            if self._owns(key, task, generation):
                del self._inflight[key]

    def _record_failure(
        self,
        key: AccountKey,
        task: asyncio.Task[object] | None,
        generation: int,
    ) -> None:
        """Start cooldown only for the flight that still owns this key."""
        if self._owns(key, task, generation):
            self._retry_after[key] = self._clock() + self._cooldown_seconds

    def _record_success(
        self,
        key: AccountKey,
        task: asyncio.Task[object] | None,
        generation: int,
        value: T,
    ) -> None:
        """Publish a successful value only from the current owned flight."""
        if self._owns(key, task, generation):
            self._entries[key] = (self._clock(), value)
            self._retry_after.pop(key, None)

    def _owns(
        self,
        key: AccountKey,
        task: asyncio.Task[object] | None,
        generation: int,
    ) -> bool:
        """Whether ``task`` may still publish state for this key and generation."""
        return self._generation == generation and self._inflight.get(key) is task

    @staticmethod
    def _consume_exception(task: asyncio.Task[T]) -> None:
        """Retrieve a detached flight's exception after all waiters cancel."""
        if not task.cancelled():
            task.exception()


__all__ = ["AccountKey", "ReadThroughCache"]
