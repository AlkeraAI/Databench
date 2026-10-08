"""Content-addressed global blob store at `<.alkera>/blobs/`.

Layout and rules:
- Path layout: `<.alkera>/blobs/<ab>/<cd>/<sha256>` (two-level fan-out
  so a single directory never grows unbounded).
- Idempotent writes: same content twice → one file. Atomic temp+rename.
- Mark-and-sweep GC. Refcounts would invite drift; sweeping a few
  hundred small files is microseconds.
- Grace period (default 24h) protects in-flight writes that haven't
  been referenced in any chat.jsonl yet.

The store is shared across every chat in the project. A `Chat` handle
delegates blob writes to its store's `BlobStore`.

What a CHAT may name is narrower than what is on disk. Content-addressed dedup
means one file can hold several chats' results, and on a machine that serves
many people's chats a hash learned anywhere must not open another chat's
result. So every chat-facing read and write goes through a `ChatBlobs` view
(`BlobStore.for_chat`): the store keeps a small per-chat index of the hashes the
chat wrote or was handed (`<blobs>/.refs/<digest>`), and the view answers a hash
outside it exactly as it answers one that does not exist.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from collections.abc import Container, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from alkera_core.atomic_io import append_line_safe, replace_with_retry
from alkera_core.project.locking import FileLock, LockHeldError

#: Where the per-chat index of nameable hashes lives, under the blob root. Not a
#: two-character fan-out directory, so the sweep never walks it as blobs.
_REFS_DIRNAME = ".refs"

# How long a freshly-written blob is exempt from GC even if no chat
# references it yet. Covers in-flight writes (jsonl event not yet
# appended) + manual operator copies.
DEFAULT_GC_GRACE_SECONDS = 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class GcReport:
    """Result of a mark-and-sweep cycle."""

    scanned: int
    """How many blob files we examined."""
    kept: int
    """How many were retained (referenced or within grace period)."""
    deleted: int
    """How many we removed."""
    freed_bytes: int
    """Total bytes recovered."""


class BlobStore:
    """Sha256-addressed file store. Shared by every chat in the project.

    Writes are idempotent — the same bytes hashed by two different
    callers land on the same path, written exactly once.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    def path(self, sha256: str) -> Path:
        """Compute the on-disk path for a given content hash. No I/O."""
        # Validate just enough so we can't be coerced into traversing
        # out of the blob root via a crafted hash.
        if len(sha256) != 64 or not all(c in "0123456789abcdef" for c in sha256):
            raise ValueError(f"not a sha256 hex string: {sha256!r}")
        return self._root / sha256[:2] / sha256[2:4] / sha256

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def write(self, data: bytes | BinaryIO) -> tuple[str, int]:
        """Write ``data`` to the store, return ``(sha256, size)``.

        Idempotent: if the target already exists, returns the existing
        hash + size without rewriting. Atomic via temp+rename so a
        crash mid-write never leaves a partial blob.
        """
        if isinstance(data, (bytes, bytearray)):
            return self._write_bytes(bytes(data))
        return self._write_stream(data)

    def _write_bytes(self, data: bytes) -> tuple[str, int]:
        sha = hashlib.sha256(data).hexdigest()
        target = self.path(sha)
        if target.exists():
            return sha, len(data)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / f".{sha}.{secrets.token_hex(4)}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            # If another writer raced us to the same content, leave
            # theirs in place and drop ours — same content either way.
            # `replace_with_retry` rides out a brief Windows sharing violation
            # (a concurrent reader holding `target` open) before giving up.
            try:
                replace_with_retry(tmp, target)
            except OSError:
                tmp.unlink(missing_ok=True)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return sha, len(data)

    def _write_stream(self, stream: BinaryIO) -> tuple[str, int]:
        # Two-pass: hash the stream into a temp, then move into place.
        # We can't seek arbitrary readers, so we tee on first read.
        self._root.mkdir(parents=True, exist_ok=True)
        tmp = self._root / f".staging.{secrets.token_hex(8)}.tmp"
        h = hashlib.sha256()
        total = 0
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, "wb") as f:
                while True:
                    chunk = stream.read(64 * 1024)
                    if not chunk:
                        break
                    if isinstance(chunk, str):
                        # Defensive — caller passed a text-mode reader.
                        raise TypeError("BlobStore.write requires a binary stream; got text")
                    h.update(chunk)
                    f.write(chunk)
                    total += len(chunk)
                f.flush()
                os.fsync(f.fileno())
            sha = h.hexdigest()
            target = self.path(sha)
            if target.exists():
                tmp.unlink(missing_ok=True)
                return sha, total
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                replace_with_retry(tmp, target)
            except OSError:
                tmp.unlink(missing_ok=True)
            return sha, total
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def read(self, sha256: str) -> bytes:
        """Read the full blob. Raises `FileNotFoundError` if missing."""
        return self.path(sha256).read_bytes()

    def exists(self, sha256: str) -> bool:
        return self.path(sha256).is_file()

    def size(self, sha256: str) -> int:
        """The blob's size in bytes. Raises `FileNotFoundError` if missing."""
        return self.path(sha256).stat().st_size

    # ------------------------------------------------------------------
    # Per-chat views
    # ------------------------------------------------------------------

    def for_chat(self, chat_key: str) -> ChatBlobs:
        """This store as the chat ``chat_key`` may name it (see `ChatBlobs`)."""
        return ChatBlobs(self, chat_key)

    def held_elsewhere(self, sha256: str, *, chat_key: str) -> bool:
        """Whether any chat other than ``chat_key`` may still name ``sha256`` by
        its index. For deciding whether one chat's release may reclaim the
        bytes; never for an answer a chat reads."""
        own = self._refs_path(chat_key)
        refs = self._root / _REFS_DIRNAME
        if not refs.is_dir():
            return False
        return any(path != own and sha256 in _held(path) for path in refs.iterdir())

    def forget_chat(self, chat_key: str) -> None:
        """Drop a deleted chat's index. Its bytes are left to the next sweep."""
        self._refs_path(chat_key).unlink(missing_ok=True)

    def _refs_path(self, chat_key: str) -> Path:
        # Hashed, so no chat id reaches the path as text.
        digest = hashlib.sha256(chat_key.encode("utf-8")).hexdigest()
        return self._root / _REFS_DIRNAME / digest

    def delete(self, sha256: str) -> bool:
        """Remove a single blob by hash. Returns ``True`` if a file was deleted,
        ``False`` if it was already absent. Raises ``ValueError`` for a malformed
        sha. The caller is responsible for ensuring the blob is unreferenced (a
        chat tool deletes only when no OTHER chat references it)."""
        try:
            self.path(sha256).unlink()
        except FileNotFoundError:
            return False
        return True

    # ------------------------------------------------------------------
    # GC
    # ------------------------------------------------------------------

    def sweep(
        self,
        referenced: set[str],
        *,
        grace_period_seconds: int = DEFAULT_GC_GRACE_SECONDS,
    ) -> GcReport:
        """Mark-and-sweep. Caller computes ``referenced`` (set of
        sha256s mentioned by any chat.jsonl) and we delete anything
        not in it AND older than the grace period.

        Holds ``<blobs_root>/blobs.gc.lock`` for the duration so two
        concurrent sweeps don't double-delete. Lock contention is
        treated as a no-op — caller can retry later.
        """
        gc_lock_path = self._root / "blobs.gc.lock"
        try:
            gc_lock = FileLock(gc_lock_path)
            gc_lock.acquire()
        except LockHeldError:
            return GcReport(scanned=0, kept=0, deleted=0, freed_bytes=0)

        scanned = kept = deleted = freed = 0
        cutoff = time.time() - grace_period_seconds
        try:
            for path in self._iter_blobs():
                scanned += 1
                sha = path.name
                if sha in referenced:
                    kept += 1
                    continue
                try:
                    st = path.stat()
                except FileNotFoundError:
                    continue
                if st.st_mtime > cutoff:
                    kept += 1
                    continue
                size = st.st_size
                try:
                    path.unlink()
                except FileNotFoundError:
                    continue
                deleted += 1
                freed += size
        finally:
            gc_lock.release()

        return GcReport(scanned=scanned, kept=kept, deleted=deleted, freed_bytes=freed)

    def _iter_blobs(self) -> Iterable[Path]:
        """Walk the fan-out tree, yielding every blob file."""
        for ab in self._root.iterdir():
            if not ab.is_dir() or len(ab.name) != 2:
                continue
            for cd in ab.iterdir():
                if not cd.is_dir() or len(cd.name) != 2:
                    continue
                for blob in cd.iterdir():
                    if blob.is_file():
                        yield blob


def _held(refs: Path) -> set[str]:
    """The hashes an index admits: each ``+<sha>`` line admits, a later
    ``-<sha>`` releases. A torn or foreign line is skipped."""
    try:
        text = refs.read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    held: set[str] = set()
    for line in text.splitlines():
        if len(line) != 65 or line[0] not in "+-" or not _is_sha(line[1:]):
            continue
        if line[0] == "+":
            held.add(line[1:])
        else:
            held.discard(line[1:])
    return held


def _is_sha(text: str) -> bool:
    return len(text) == 64 and all(c in "0123456789abcdef" for c in text)


class ChatBlobs(BlobStore):
    """The shared store as one chat may name it.

    Writes land in the shared content-addressed store and admit the hash to the
    chat's index; reads, sizes, paths and deletes answer only for a hash the
    index admits. A hash outside it, whether another chat's or one that never
    existed, raises the same ``FileNotFoundError`` before the store is consulted,
    so the answer and the work done carry nothing about other chats.

    ``chat_key`` is the root chat: a subagent shares its parent's key, so a
    handle either one makes is one the other may follow.
    """

    def __init__(self, store: BlobStore, chat_key: str) -> None:
        super().__init__(store.root)
        self._store = store
        self._chat_key = chat_key
        self._refs = store._refs_path(chat_key)

    @property
    def chat_key(self) -> str:
        """The chat this view names blobs for."""
        return self._chat_key

    def holds(self, sha256: str) -> bool:
        """Whether this chat may name ``sha256``. Raises ``ValueError`` for a
        malformed hash, as every store method does."""
        self._store.path(sha256)
        return sha256 in _held(self._refs)

    def admit(self, sha256: str) -> None:
        """Hand ``sha256`` to this chat: from now on it may name it."""
        if not self.holds(sha256):
            append_line_safe(self._refs, f"+{sha256}")

    def release(self, sha256: str) -> bool:
        """Take ``sha256`` out of this chat's index. ``False`` when the chat
        never held it. The bytes stay; reclaiming them is the caller's call."""
        if not self.holds(sha256):
            return False
        append_line_safe(self._refs, f"-{sha256}")
        return True

    def write(self, data: bytes | BinaryIO) -> tuple[str, int]:
        sha, size = self._store.write(data)
        self.admit(sha)
        return sha, size

    def path(self, sha256: str) -> Path:
        """The blob's on-disk path, for a hash this chat holds. Unlike the
        store's, this raises ``FileNotFoundError`` for any other hash."""
        if not self.holds(sha256):
            raise FileNotFoundError(sha256)
        return self._store.path(sha256)

    def read(self, sha256: str) -> bytes:
        return self.path(sha256).read_bytes()

    def exists(self, sha256: str) -> bool:
        return self.holds(sha256) and self._store.exists(sha256)

    def size(self, sha256: str) -> int:
        return self.path(sha256).stat().st_size

    def delete(self, sha256: str, *, kept_for: Container[str] = frozenset()) -> bool:
        """Release ``sha256`` from this chat, and remove the bytes when no other
        chat's index still holds them and ``kept_for`` (hashes some other
        record still names, such as another chat's transcript) does not list
        it. ``True`` when this chat held it; whether the bytes went is not
        said, since that is a fact about other chats."""
        if not self.release(sha256):
            return False
        if sha256 not in kept_for and not self._store.held_elsewhere(
            sha256, chat_key=self._chat_key
        ):
            self._store.delete(sha256)
        return True

    def for_chat(self, chat_key: str) -> ChatBlobs:
        """A view of a view is the view: one chat never names blobs as another."""
        return self

    def sweep(
        self,
        referenced: set[str],
        *,
        grace_period_seconds: int = DEFAULT_GC_GRACE_SECONDS,
    ) -> GcReport:
        raise TypeError("a chat's view of the blob store cannot sweep it")


__all__ = ["DEFAULT_GC_GRACE_SECONDS", "BlobStore", "ChatBlobs", "GcReport"]
