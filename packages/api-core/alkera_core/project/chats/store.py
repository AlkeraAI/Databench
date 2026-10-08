"""`ChatStore` — top-level chat lifecycle.

Holds a reference to the parent `ProjectDirectory` (for path resolution
+ access to the shared `BlobStore`). All chats under a single project
share one `BlobStore`; per-chat data lives at `<project>/chats/<sid>/`.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alkera_core.atomic_io import write_json_atomic, write_text_atomic
from alkera_core.project.chats.blobs import BlobStore, GcReport
from alkera_core.project.chats.chat import (
    Chat,
    ChatNotFoundError,
    iter_chat_events,
    load_manifest,
    preview_line,
)
from alkera_core.project.chats.references import collect_blob_references
from alkera_core.project.jsonl import iter_jsonl
from alkera_core.project.locking import FileLock
from alkera_core.schemas.chat import ChatManifest, Event, SessionCreated

if TYPE_CHECKING:
    from alkera_core.project.chats.async_chat import AsyncChat
    from alkera_core.project.directory import ProjectDirectory


class ChatStore:
    """Per-project chat lifecycle.

    Construction is cheap — just bundles the project + a fresh
    `BlobStore` handle. Holds no per-chat state.
    """

    def __init__(self, project: ProjectDirectory) -> None:
        self._project = project
        self._blobs = BlobStore(project.blobs_path)

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._project.chats_path

    @property
    def project(self) -> ProjectDirectory:
        return self._project

    @property
    def blobs(self) -> BlobStore:
        return self._blobs

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def list_session_ids(self) -> list[str]:
        """Return all session_ids present in the chats directory."""
        if not self.path.is_dir():
            return []
        return sorted(p.name for p in self.path.iterdir() if p.is_dir())

    def chat_sandbox_dir(self, session_id: str) -> Path:
        """The per-chat scratch dir (`chats/<sid>/sandbox/`). NOT created here —
        the writer (blob.materialize / plan mode) `mkdir(parents=True)`s it on
        demand, so a read-only session never materializes the dir. Lives inside
        the chat dir, so `delete()` reclaims it with the rest of the chat."""
        from alkera_core.project.directory import SANDBOX_SUBDIR

        return self.path / session_id / SANDBOX_SUBDIR

    def read_events(self, session_id: str) -> Iterator[Event]:
        """Lock-free replay of a chat's persisted events by id — for observers /
        viewers that must NOT block (or be blocked by) the chat's writer. Mirrors
        ``list_summaries``' no-lock discovery contract: a point-in-time read of
        ``chat.jsonl`` (crash-safe). Yields nothing for an unknown session_id."""
        return iter_chat_events(self.path / session_id / "chat.jsonl")

    def list_summaries(self) -> list[ChatManifest]:
        """Read every chat's manifest, falling back to fold-from-JSONL
        if a manifest is missing or corrupt.

        This is a cheap-ish "for the list UI" path — it doesn't take
        per-chat locks. Caller should treat the returned manifests as
        a point-in-time snapshot; mutations from concurrent writers
        are visible on the next call.
        """
        summaries: list[ChatManifest] = []
        for sid in self.list_session_ids():
            chat_dir = self.path / sid
            manifest = load_manifest(chat_dir / "manifest.json")
            if manifest is None:
                manifest = self._reconstruct_manifest_from_jsonl(sid, chat_dir)
            summaries.append(manifest)
        return summaries

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        session_id: str | None = None,
        title: str | None = None,
        cwd: str | None = None,
        model: dict[str, Any] | None = None,
        agent: str | None = None,
        harness_type: str = "agent",
        harness: dict[str, Any] | None = None,
        adopt_dir: bool = False,
    ) -> Chat:
        """Create a fresh chat. Returns a locked `Chat` handle.

        ``session_id``: caller-provided or generated. Production code
        should pass a ULID; we use a random hex token if unset.

        ``harness_type``: which adapter is bound to this chat — IMMUTABLE.
        Default is the only currently-shipped value. Stored in the
        manifest; read on every resume to pick the matching adapter.

        ``adopt_dir``: the chat's directory may already exist with no record
        in it — no manifest and no event log — and the record is written into
        it. A cloud box pulls a chat's files down into the chat's own directory
        before it opens the chat, so the directory is there before the chat
        is. A directory that already holds a record is still refused: that is
        an existing chat, and ``open`` is the way to it.
        """
        sid = session_id or _new_session_id()
        chat_dir = self.path / sid
        if chat_dir.exists() and not (adopt_dir and not self.has_record(sid)):
            raise FileExistsError(f"chat {sid!r} already exists")
        chat_dir.mkdir(parents=True, exist_ok=True)

        manifest = ChatManifest(
            session_id=sid,
            title=title,
            created_at=_now(),
            updated_at=_now(),
            cwd=cwd,
            model=model or {},
            agent=agent,
            harness_type=harness_type,
            harness=harness or {},
        )
        # Seed manifest on disk so concurrent listers see something.
        write_json_atomic(chat_dir / "manifest.json", manifest.model_dump(mode="json"))

        lock = FileLock(chat_dir / ".lock")
        lock.acquire()
        chat = Chat(
            chat_dir=chat_dir,
            manifest=manifest,
            lock=lock,
            blobs=self._blobs,
        )
        # Stamp the first event so the JSONL is non-empty.
        chat.append_event(
            SessionCreated(
                event_id=_new_event_id(),
                time=_now(),
                session_id=sid,
                title=title,
                cwd=cwd,
                model=model or {},
                agent=agent,
                harness=harness or {},
            )
        )
        return chat

    def has_record(self, session_id: str) -> bool:
        """Whether a chat RECORD exists under ``session_id`` — a manifest or an
        event log — as opposed to a bare directory something else made."""
        chat_dir = self.path / session_id
        return (chat_dir / "manifest.json").is_file() or (chat_dir / "chat.jsonl").is_file()

    def open(self, session_id: str) -> Chat:
        """Open an existing chat. Returns a locked `Chat` handle.

        Raises `ChatNotFoundError` if no chat exists with the given id;
        `LockHeldError` if another client currently holds the lock.
        """
        chat_dir = self.path / session_id
        if not chat_dir.is_dir():
            raise ChatNotFoundError(f"chat {session_id!r} not found in {self.path}")

        lock = FileLock(chat_dir / ".lock")
        lock.acquire()
        try:
            manifest = load_manifest(chat_dir / "manifest.json")
            if manifest is None:
                manifest = self._reconstruct_manifest_from_jsonl(session_id, chat_dir)
            if manifest.session_id != session_id:
                # A chat folder that was copied whole carries its source's id in
                # the manifest and on every event in its log. The directory is
                # the identity — it is what the cloud named the copy and what
                # the box files the chat under — so both take the directory's
                # name here, once, and the copy resumes as its own chat over
                # the transcript it brought.
                self._adopt_copied(chat_dir, manifest, session_id)
        except BaseException:
            lock.release()
            raise

        return Chat(
            chat_dir=chat_dir,
            manifest=manifest,
            lock=lock,
            blobs=self._blobs,
        )

    def delete(self, session_id: str, *, recursive: bool = False) -> None:
        """Delete a chat. Requires the chat to NOT be currently locked
        by another client (we don't preempt active writers).

        If ``recursive=True``, also deletes every chat whose
        ``parent_session_id`` chain leads to this one. Used to clean
        up subagent (child) chats when their parent is removed.

        Blobs referenced only by the deleted chat are not removed
        synchronously — they're swept by the next `gc()` call.
        """
        chat_dir = self.path / session_id
        if not chat_dir.is_dir():
            raise ChatNotFoundError(f"chat {session_id!r} not found")

        if recursive:
            # Identify direct children first via cheap manifest scan,
            # then recurse. Use list_summaries since we need each
            # manifest's parent_session_id.
            children = [
                m.session_id for m in self.list_summaries() if m.parent_session_id == session_id
            ]
            for child in children:
                self.delete(child, recursive=True)

        # Acquire + release the lock so we abort cleanly if a writer
        # is active. The lock is gone by the time we rmtree (the
        # release will try to delete a file inside the dir we're
        # about to remove — that's fine, it's a no-op if the dir's
        # already gone).
        lock = FileLock(chat_dir / ".lock")
        lock.acquire()
        try:
            # Use shutil.rmtree, but BE CAREFUL not to follow symlinks
            # (defensive, even though we own the dir layout).
            import shutil

            shutil.rmtree(chat_dir, ignore_errors=False)
            self._blobs.forget_chat(session_id)
        finally:
            # Lock file is already gone with the dir — but `release()`
            # tolerates that.
            lock.release()

    # ------------------------------------------------------------------
    # Async variants
    # ------------------------------------------------------------------

    async def create_async(self, **kwargs: object) -> AsyncChat:
        """Async variant of `create()` — runs the sync version in a
        worker thread."""
        import asyncio

        from alkera_core.project.chats.async_chat import AsyncChat

        chat = await asyncio.to_thread(self.create, **kwargs)  # type: ignore[arg-type]
        return AsyncChat(chat)

    async def open_async(self, session_id: str) -> AsyncChat:
        import asyncio

        from alkera_core.project.chats.async_chat import AsyncChat

        chat = await asyncio.to_thread(self.open, session_id)
        return AsyncChat(chat)

    # ------------------------------------------------------------------
    # GC
    # ------------------------------------------------------------------

    def gc(self, *, grace_period_seconds: int | None = None) -> GcReport:
        """Sweep unreferenced blobs. Reads every chat's `chat.jsonl`
        to compute the live set (every sha256 referenced by a `file`
        part), then defers to `BlobStore.sweep`.

        Cheap — JSONL scan is sequential, no fold required.
        """
        live = self.referenced_blobs()
        if grace_period_seconds is None:
            return self._blobs.sweep(live)
        return self._blobs.sweep(live, grace_period_seconds=grace_period_seconds)

    def referenced_blobs(self, *, exclude_session_id: str | None = None) -> set[str]:
        """Every blob sha256 referenced by any chat's `chat.jsonl` — both `file`
        parts (attachments) AND spilled tool-result handles (the `blob` field a
        large result is replaced by). `exclude_session_id` drops one chat's
        contribution, so a tool can ask "is this blob referenced by any OTHER
        chat?" before reclaiming its bytes."""
        live: set[str] = set()
        for sid in self.list_session_ids():
            if sid == exclude_session_id:
                continue
            for raw in iter_jsonl(self.path / sid / "chat.jsonl"):
                # Roots are re-derived at every sweep, never trusted from a
                # stamp written earlier: an under-rooting bug stays repairable
                # for logs already on disk, and over-rooting only keeps bytes.
                live |= collect_blob_references(raw)
        return live

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _adopt_copied(chat_dir: Path, manifest: ChatManifest, session_id: str) -> None:
        """Give a copied chat folder the id of the directory it now lives in.

        The log is rewritten atomically with every event's ``session_id``
        restamped and nothing else touched: event ids, times, parts and blob
        references are the transcript, and the transcript is exactly what the
        copy exists to carry. A trailing line a crash cut short is dropped the
        way every read already drops it.
        """
        log = chat_dir / "chat.jsonl"
        if log.exists():
            lines: list[str] = []
            for raw in iter_jsonl(log):
                raw["session_id"] = session_id
                lines.append(json.dumps(raw, ensure_ascii=False, separators=(",", ":")))
            write_text_atomic(log, "".join(f"{line}\n" for line in lines))
        manifest.session_id = session_id
        write_json_atomic(chat_dir / "manifest.json", manifest.model_dump(mode="json"))

    def _reconstruct_manifest_from_jsonl(self, session_id: str, chat_dir: Path) -> ChatManifest:
        """Fold-based manifest recovery for missing/corrupt manifests.

        The JSONL is the source of
        truth. We scan the events for SessionCreated + the last
        SessionUpdated + every MessageCompleted to reconstruct enough
        of the manifest for listing UIs.
        """
        manifest = ChatManifest(session_id=session_id)
        for raw in iter_jsonl(chat_dir / "chat.jsonl"):
            event_type = raw.get("event_type")
            if event_type == "session.created":
                manifest.title = raw.get("title")
                manifest.cwd = raw.get("cwd")
                manifest.model = raw.get("model") or {}
                manifest.agent = raw.get("agent")
                manifest.harness = raw.get("harness") or {}
                manifest.parent_session_id = raw.get("parent_session_id")
                t = raw.get("time")
                if isinstance(t, str):
                    try:
                        manifest.created_at = datetime.fromisoformat(t)
                    except ValueError:
                        pass
            elif event_type == "session.updated":
                if raw.get("title") is not None:
                    manifest.title = raw["title"]
                if raw.get("model") is not None:
                    manifest.model = raw["model"]
                if raw.get("agent") is not None:
                    manifest.agent = raw["agent"]
            elif event_type == "message.completed":
                t = raw.get("tokens") or {}
                manifest.tokens_total.input += int(t.get("input", 0))
                manifest.tokens_total.output += int(t.get("output", 0))
                manifest.tokens_total.cache_read += int(t.get("cache_read", 0))
                manifest.tokens_total.cache_write += int(t.get("cache_write", 0))
                cost = raw.get("cost")
                if isinstance(cost, (int, float)):
                    manifest.cost_total += float(cost)
            elif event_type == "part.created":
                part = raw.get("part") or {}
                # Mirror the writer path: synthetic parts never preview.
                if part.get("type") == "text" and part.get("synthetic") is not True:
                    preview = preview_line(str(part.get("text") or ""))
                    if preview:
                        manifest.last_message_preview = preview
            # updated_at + the persisted tail — bump on every event
            t = raw.get("time")
            if isinstance(t, str):
                try:
                    manifest.updated_at = datetime.fromisoformat(t)
                except ValueError:
                    pass
            event_id = raw.get("event_id")
            if isinstance(event_id, str) and event_id:
                manifest.last_event_id = event_id
        return manifest


def _new_session_id() -> str:
    """Generate a session id. Production should use ULIDs."""
    return secrets.token_hex(12)


def _new_event_id() -> str:
    return secrets.token_hex(10)


def _now() -> datetime:
    return datetime.now(UTC)


__all__ = ["ChatStore"]
