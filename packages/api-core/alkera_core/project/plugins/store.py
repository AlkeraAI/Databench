"""``PluginStore[TState]`` — uniform, typed plugin persistence.

A plugin never touches the filesystem: it extends ``PluginState`` and is
handed a typed store. Reuses the exact chat-store plumbing — atomic
write+fsync+rename (``atomic_io``), a per-store ``FileLock`` (locks are NOT
inherited), crash-safe JSONL, the content-addressed ``BlobStore``.

Concurrency model: the single-writer daemon owns writes. ``update(fn)`` is
race-safe across same-process tasks (an in-process lock serializes them) AND
across processes (the ``FileLock`` with a bounded retry). All IO runs in a
worker thread so the daemon's async loop never blocks.

On-disk layout (a plugin owns exactly one subdir)::

    .alkera/plugins/<name>/
    ├── state.json                 # the plugin's PluginState
    ├── connections/<handle>/      # per-connection namespace (for_connection)
    │   ├── state.json
    │   └── blobs/ , *.jsonl
    ├── blobs/                     # big cached artifacts (sha256 dedup)
    ├── <log>.jsonl                # crash-safe append logs (watermark/audit)
    └── .lock                      # this store's OWN FileLock
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from alkera_core.atomic_io import append_line, write_json_atomic
from alkera_core.project.jsonl import iter_jsonl
from alkera_core.project.locking import FileLock, LockHeldError
from alkera_core.versioning import VersionedModel

if TYPE_CHECKING:
    from alkera_core.project.chats.blobs import BlobStore

TState = TypeVar("TState", bound=VersionedModel)

_STATE_FILE = "state.json"
_LOCK_FILE = ".lock"
_CONNECTIONS_SUBDIR = "connections"
_BLOBS_SUBDIR = "blobs"

# Same-process serialization for writers sharing a root. The cross-process
# guarantee is the FileLock; this one prevents two asyncio tasks in the same
# daemon from racing the read-modify-write window.
_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.Lock] = {}


def _process_lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _PROCESS_LOCKS_GUARD:
        lk = _PROCESS_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _PROCESS_LOCKS[key] = lk
        return lk


class PluginStore(Generic[TState]):
    """A typed store scoped to one ``(project, plugin)`` (or one connection).

    Construct via ``ProjectDirectory.plugins(name, StateType)`` — never by
    hand-resolving ``.alkera/`` paths.
    """

    def __init__(self, root: Path, state_type: type[TState]) -> None:
        self._root = Path(root)
        self._state_type = state_type
        self._root.mkdir(parents=True, exist_ok=True)

    # --- properties ----------------------------------------------------

    @property
    def path(self) -> Path:
        return self._root

    # --- state ---------------------------------------------------------

    async def load(self) -> TState:
        """Return the persisted state, or a default ``TState()`` on first run.
        Lock-free read (atomic writes guarantee a whole file)."""
        return await asyncio.to_thread(self._read_state)

    async def save(self, state: TState) -> None:
        """Persist ``state`` atomically under this store's lock."""
        await asyncio.to_thread(self._locked, lambda: self._write_state(state))

    async def update(self, fn: Callable[[TState], TState]) -> TState:
        """Race-safe load→mutate→save under one lock acquisition."""

        def _do() -> TState:
            current = self._read_state()
            updated = fn(current)
            self._write_state(updated)
            return updated

        return await asyncio.to_thread(self._locked, _do)

    # --- namespacing + blobs -------------------------------------------

    def for_connection(self, handle: str) -> PluginStore[TState]:
        """A per-connection namespace: one state per connection."""
        return PluginStore(self._root / _CONNECTIONS_SUBDIR / handle, self._state_type)

    def blobs(self) -> BlobStore:
        """Big cached artifacts (raw introspection dumps) by sha256."""
        from alkera_core.project.chats.blobs import BlobStore

        return BlobStore(self._root / _BLOBS_SUBDIR)

    # --- append-only logs ----------------------------------------------

    async def append(self, log: str, rec: VersionedModel) -> None:
        """Crash-safe JSONL append (watermark history / audit) under lock."""
        line = json.dumps(rec.model_dump(mode="json"))
        path = self._log_path(log)
        await asyncio.to_thread(self._locked, lambda: append_line(path, line))

    def read_log(self, log: str) -> Iterator[dict[str, Any]]:
        """Stream a log's records as raw dicts (caller validates with its own
        model). Crash-safe — skips a truncated trailing line."""
        return iter_jsonl(self._log_path(log))

    # --- internals -----------------------------------------------------

    def _read_state(self) -> TState:
        path = self._root / _STATE_FILE
        try:
            raw = path.read_text()
        except FileNotFoundError:
            return self._state_type()
        return self._state_type.model_validate_json(raw)

    def _write_state(self, state: TState) -> None:
        write_json_atomic(self._root / _STATE_FILE, state.model_dump(mode="json"))

    def _log_path(self, log: str) -> Path:
        if "/" in log or "\\" in log or log in ("", ".", ".."):
            raise ValueError(f"invalid log name {log!r}")
        return self._root / f"{log}.jsonl"

    def _locked(self, fn: Callable[[], Any]) -> Any:
        """Run ``fn`` while holding both the in-process lock (serializes
        same-process writers) and the cross-process ``FileLock``."""
        proc_lock = _process_lock_for(self._root / _LOCK_FILE)
        with proc_lock:
            file_lock = self._acquire_file_lock()
            try:
                return fn()
            finally:
                file_lock.release()

    def _acquire_file_lock(self) -> FileLock:
        """Acquire the cross-process lock with a small bounded retry — covers
        a transient race with another daemon's reclaim."""
        lock = FileLock(self._root / _LOCK_FILE)
        last: LockHeldError | None = None
        for attempt in range(5):
            try:
                lock.acquire()
                return lock
            except LockHeldError as exc:
                last = exc
                if attempt < 4:
                    time.sleep(0.02 * (attempt + 1))
        assert last is not None
        raise last


__all__ = ["PluginStore"]
