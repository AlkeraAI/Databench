"""`Chat` — per-chat sync handle.

Holds the per-chat write lock for its lifetime. Append events, read
back via the events iterator, attach blobs, fold to a materialized
view. The async wrapper `AsyncChat` (in `async_chat.py`) delegates
to this class via `asyncio.to_thread`.

Layout for a single chat::

    <.alkera>/chats/<session_id>/
        manifest.json    # cache, atomic temp+rename
        chat.jsonl       # append-only event log, source of truth
        .lock            # FileLock — held while a Chat is open

The handle MUST be released to free the lock. Use as a context
manager (recommended) or call ``.close()`` explicitly.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

from pydantic import TypeAdapter, ValidationError

from alkera_core.atomic_io import append_line, write_json_atomic
from alkera_core.project.jsonl import iter_jsonl
from alkera_core.project.locking import FileLock
from alkera_core.schemas.chat import (
    NON_PERSISTED_EVENT_TYPES,
    ChatManifest,
    Event,
    FilePart,
    TokenTotals,
)

if TYPE_CHECKING:
    from alkera_core.project.chats.blobs import BlobStore, ChatBlobs


class ChatNotFoundError(FileNotFoundError):
    """Raised by `ChatStore.open` when the session_id has no chat dir."""


# Pre-build the TypeAdapter so we don't recompile the discriminator
# every event read. TypeAdapter is concurrency-safe for reading.
_EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)


def iter_chat_events(chat_jsonl_path: Path) -> Iterator[Event]:
    """Iterate a chat's persisted events from its ``chat.jsonl`` — LOCK-FREE.

    The single reader for a chat's event log: ``iter_jsonl`` (crash-safe — skips a
    truncated trailing line + mid-file malformed lines) feeds each record through the
    typed ``Event`` adapter (an unknown ``event_type`` routes to ``RawEvent`` via the
    discriminator). Takes NO per-chat lock, so an observer can replay a chat that
    another holder is actively writing — the atomic per-line appends (``append_line``)
    make a concurrent read safe. ``Chat.events()`` delegates here; observers read by
    path directly."""
    for raw in iter_jsonl(chat_jsonl_path):
        try:
            yield _EVENT_ADAPTER.validate_python(raw)
        except ValidationError:
            # The discriminator already routes unknown tags to RawEvent; reaching
            # here means a deeper malformation (e.g. missing event_id). Skip + move
            # on — iter_jsonl promised mid-file malformed lines aren't fatal.
            continue


class Chat:
    """Per-chat sync handle.

    Constructed by `ChatStore.create` / `ChatStore.open`. Don't
    instantiate directly. Holds the chat's write lock until ``close()``
    or context manager exit.

    Not threadsafe — pass one instance per thread.
    """

    def __init__(
        self,
        *,
        chat_dir: Path,
        manifest: ChatManifest,
        lock: FileLock,
        blobs: BlobStore,
    ) -> None:
        self._chat_dir = chat_dir
        self._manifest = manifest
        self._lock = lock
        self._blobs = blobs
        self._closed = False

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._manifest.session_id

    @property
    def manifest(self) -> ChatManifest:
        """Live in-memory manifest. Mutated by `append_event` (updates
        `updated_at`, `tokens_total`, `cost_total` for known event
        types). Persisted to disk on `close()`.
        """
        return self._manifest

    @property
    def blobs(self) -> ChatBlobs:
        """The project-shared blob store as this chat may name it: what the chat
        (or, for a subagent, its root chat) wrote or was handed, and nothing
        another chat on the same machine holds."""
        root = self._manifest.parent_session_id or self._manifest.session_id
        return self._blobs.for_chat(root)

    @property
    def path(self) -> Path:
        return self._chat_dir

    @property
    def chat_jsonl_path(self) -> Path:
        return self._chat_dir / "chat.jsonl"

    @property
    def manifest_path(self) -> Path:
        return self._chat_dir / "manifest.json"

    @property
    def tasks_path(self) -> Path:
        """The per-chat task DAG file (the unified TODO system). Backed by
        ``alkera_core.project.chats.tasks.TaskStore``."""
        return self._chat_dir / "tasks.json"

    @property
    def closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Persist the manifest and release the lock. Idempotent."""
        if self._closed:
            return
        try:
            self._flush_manifest()
        finally:
            self._lock.release()
            self._closed = True

    def __enter__(self) -> Chat:
        if self._closed:
            raise RuntimeError("Chat is already closed; reopen via the store")
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Append + read events
    # ------------------------------------------------------------------

    def append_event(self, event: Event) -> None:
        """Append one event to chat.jsonl + update in-memory manifest.

        Storage filter: events whose `event_type` is in
        `NON_PERSISTED_EVENT_TYPES` (per-token chunks, heartbeats) are
        dropped on the floor — they flow over the live firehose only.
        Everything semantically meaningful IS persisted.

        The event is serialized via Pydantic so unknown extras ride
        through (forward-compat). After the line lands on disk, we
        mirror selected fields onto the manifest cache (timestamps,
        token totals). On close, the manifest is atomically rewritten.
        """
        self._check_open()
        # Pydantic-side `event_type` is the discriminator — pull it via
        # the dumped dict so unknown variants (RawEvent) still gate.
        event_type = getattr(event, "event_type", None)
        if event_type in NON_PERSISTED_EVENT_TYPES:
            # Drop on the floor — these are live-only signals.
            self._apply_to_manifest(event)
            return
        dumped = event.model_dump(mode="json")
        append_line(self.chat_jsonl_path, json.dumps(dumped, separators=(",", ":")))
        self._apply_to_manifest(event)

    def events(self) -> Iterator[Event]:
        """Iterate events in append order, falling back to `RawEvent`
        for any unknown event_type. Skips truncated trailing lines and
        mid-file malformed lines. Delegates to the lock-free module
        reader ``iter_chat_events`` (the single source of the read+validate
        contract — shared with observers that read without the lock)."""
        return iter_chat_events(self.chat_jsonl_path)

    # ------------------------------------------------------------------
    # Blob attach
    # ------------------------------------------------------------------

    def add_blob(
        self, data: bytes | BinaryIO, *, filename: str, mime: str = "application/octet-stream"
    ) -> FilePart:
        """Write ``data`` to the project's global blob store and return
        a `FilePart` the caller can wrap in a `PartCreated` event.

        Idempotent at the blob layer: same bytes written twice → one
        file, two `FilePart`s pointing at the same sha256.
        """
        self._check_open()
        sha, size = self.blobs.write(data)
        return FilePart(
            part_id=_new_id("p"),
            message_id="",  # caller fills in
            time=_now(),
            sha256=sha,
            filename=filename,
            mime=mime,
            size=size,
        )

    # ------------------------------------------------------------------
    # Materialized view
    # ------------------------------------------------------------------

    def fold(self) -> list[Event]:
        """Return the active events after applying reverts +
        tombstones at fold time.

        For now this is just the raw event list. Revert / tombstone
        filtering will land alongside the first consumer (the daemon's
        chat-rendering path).
        """
        return list(self.events())

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("Chat is closed")

    def flush_manifest(self) -> None:
        """Atomically rewrite ``manifest.json`` from the in-memory copy.

        Called automatically on ``close()``. Callers that need to pin a
        manifest mutation immediately (e.g. the runtime persisting the
        harness's ``native_state()`` so the next ``open_chat`` resumes
        deterministically even after a crash) invoke this explicitly."""
        self._check_open()
        write_json_atomic(self.manifest_path, self._manifest.model_dump(mode="json"))

    def _flush_manifest(self) -> None:
        """Backwards-compat private alias; same as ``flush_manifest``
        but without the open-state check (used from ``close()`` where
        we're still inside the open window)."""
        write_json_atomic(self.manifest_path, self._manifest.model_dump(mode="json"))

    def _apply_to_manifest(self, event: Event) -> None:
        """Mirror manifest-level fields from a freshly-appended
        (persisted) event.

        We only update fields that have an obvious mapping. Anything
        ambiguous stays cache-only until the next manifest reconcile.
        """
        # Always bump updated_at — the manifest is otherwise just a
        # cache, but the timestamp is what `chat list`-style callers
        # use for sort order.
        self._manifest.updated_at = _now()

        # The persisted tail — chat-list UIs compare this against
        # last_seen_event_id for unseen badges without opening JSONL.
        event_id = getattr(event, "event_id", None)
        if isinstance(event_id, str) and event_id:
            self._manifest.last_event_id = event_id

        # The latest prose (user or assistant) caches as the one-line
        # preview shown in chat lists. Synthetic parts (steering nudges,
        # plan-mode reminders) are excluded — transcripts never display
        # them, and the preview must mirror what the user sees.
        from alkera_core.schemas.chat import PartCreated, TextPart

        if (
            isinstance(event, PartCreated)
            and isinstance(event.part, TextPart)
            and not event.part.synthetic
        ):
            preview = preview_line(event.part.text or "")
            if preview:
                self._manifest.last_message_preview = preview

        # MessageCompleted carries token + cost telemetry; aggregate
        # into the manifest's TokenTotals so listing UIs can show
        # session-level numbers without folding.
        from alkera_core.schemas.chat import MessageCompleted

        if isinstance(event, MessageCompleted):
            t = event.tokens or {}
            self._manifest.tokens_total = TokenTotals(
                input=self._manifest.tokens_total.input + int(t.get("input", 0)),
                output=self._manifest.tokens_total.output + int(t.get("output", 0)),
                cache_read=self._manifest.tokens_total.cache_read + int(t.get("cache_read", 0)),
                cache_write=self._manifest.tokens_total.cache_write + int(t.get("cache_write", 0)),
            )
            if event.cost is not None:
                self._manifest.cost_total += event.cost

        # SessionUpdated → mirror title/model/agent into the manifest.
        from alkera_core.schemas.chat import SessionUpdated

        if isinstance(event, SessionUpdated):
            if event.title is not None:
                self._manifest.title = event.title
            if event.model is not None:
                self._manifest.model = event.model
            if event.agent is not None:
                self._manifest.agent = event.agent


# ---------------------------------------------------------------------------
# Internal helpers (also used by store.py)
# ---------------------------------------------------------------------------


PREVIEW_MAX_CHARS = 120
"""Cap on the cached `last_message_preview`. Display-only — list UIs
truncate further to their own width; JSONL keeps the full text."""


def preview_line(text: str) -> str:
    """First non-empty line of ``text``, capped at PREVIEW_MAX_CHARS."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:PREVIEW_MAX_CHARS]
    return ""


def load_manifest(path: Path) -> ChatManifest | None:
    """Read + parse the manifest. Returns ``None`` if missing or
    unparseable — callers fall back to folding JSONL."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError:
        return None
    try:
        return ChatManifest.model_validate(data)
    except ValidationError:
        return None


def _new_id(prefix: str) -> str:
    """Generate a tiny opaque ID. Production code should use ULIDs
    (we don't depend on a ULID lib yet); this is good enough for
    blob-attach part IDs that go straight back to the caller."""
    import secrets

    return f"{prefix}_{secrets.token_hex(8)}"


def _now() -> datetime:
    return datetime.now(UTC)


__all__ = ["Chat", "ChatNotFoundError", "load_manifest"]
