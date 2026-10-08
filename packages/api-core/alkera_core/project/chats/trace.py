"""Compute, write, read and verify a chat's trace digest.

The digest pins the chat's append-only logs — the transcript, the decisions,
the spend — beside them, so a reader who did not write them can tell whether
what is on disk is what the writer left. The box re-pins before it pushes a
chat's folder and again when the session closes, so the copy on the drive
always carries a digest that covers the logs it travelled with.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from alkera_core.atomic_io import write_json_atomic
from alkera_core.chat_records import DIGEST_FILENAME, TRACE_FILES
from alkera_core.schemas.chat.trace import TraceDigest

#: How much of a log is read at a time while hashing it.
_CHUNK = 1 << 20


class TraceVerdict(StrEnum):
    """What checking one log against the pinned digest found."""

    #: The file starts with exactly the bytes the digest pinned. Anything
    #: after them was appended since the pin, which is what a log does.
    INTACT = "intact"
    #: The file is shorter than the pinned length, no longer starts with the
    #: pinned bytes, or is gone while the digest says it was there.
    TAMPERED = "tampered"
    #: There is nothing to check against: no digest, a digest that does not
    #: name this file, or a digest from before lengths were recorded whose
    #: whole-file hash no longer matches — which an append explains as well as
    #: an edit does.
    UNPINNED = "unpinned"


def _hash_prefix(path: Path, length: int | None) -> tuple[str, int] | None:
    """The SHA-256 of the first ``length`` bytes of ``path`` and how many bytes
    were hashed; the whole file when ``length`` is ``None``. ``None`` when the
    file is shorter than ``length``."""
    digest = hashlib.sha256()
    hashed = 0
    with path.open("rb") as handle:
        while length is None or hashed < length:
            want = _CHUNK if length is None else min(_CHUNK, length - hashed)
            chunk = handle.read(want)
            if not chunk:
                break
            digest.update(chunk)
            hashed += len(chunk)
    if length is not None and hashed < length:
        return None
    return digest.hexdigest(), hashed


def compute_trace_digest(
    chat_dir: Path, session_id: str, *, now: datetime | None = None
) -> TraceDigest:
    """Digest the trace files present in ``chat_dir``. Absent files are
    omitted, so their later appearance also changes ``combined``."""
    files: dict[str, str] = {}
    sizes: dict[str, int] = {}
    for name in TRACE_FILES:
        path = chat_dir / name
        if path.is_file():
            hashed = _hash_prefix(path, None)
            assert hashed is not None  # a whole-file read is never short
            files[name], sizes[name] = hashed
    lines = "\n".join(f"{name}:{files[name]}" for name in sorted(files))
    combined = hashlib.sha256(lines.encode("utf-8")).hexdigest()
    stamp = now or datetime.now(UTC)
    return TraceDigest(
        session_id=session_id, files=files, sizes=sizes, combined=combined, created_at=stamp
    )


def pin_trace(chat_dir: Path, session_id: str, *, now: datetime | None = None) -> TraceDigest:
    """Compute the digest and write it beside the files it pins."""
    digest = compute_trace_digest(chat_dir, session_id, now=now)
    write_json_atomic(chat_dir / DIGEST_FILENAME, digest.model_dump(mode="json"))
    return digest


def pin_trace_if_changed(
    chat_dir: Path, session_id: str, *, now: datetime | None = None
) -> TraceDigest:
    """Pin the trace, unless the digest on disk already pins exactly these bytes.

    For the box's checkpoint push, which comes round every few seconds: a
    digest rewritten each time would carry a new timestamp and so a new
    version to the drive on every beat of an idle chat. One written before
    lengths were recorded is rewritten once, so it gains them.
    """
    digest = compute_trace_digest(chat_dir, session_id, now=now)
    pinned = read_trace_digest(chat_dir)
    if (
        pinned is not None
        and pinned.session_id == session_id
        and pinned.files == digest.files
        and pinned.sizes == digest.sizes
    ):
        return pinned
    write_json_atomic(chat_dir / DIGEST_FILENAME, digest.model_dump(mode="json"))
    return digest


def read_trace_digest(chat_dir: Path) -> TraceDigest | None:
    path = chat_dir / DIGEST_FILENAME
    if not path.is_file():
        return None
    try:
        return TraceDigest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def verify_trace_file(chat_dir: Path, name: str) -> TraceVerdict:
    """Check one log in ``chat_dir`` against the pinned digest.

    The logs are append-only, so the question is not "is the file what was
    pinned" but "does the file still START with what was pinned": the pinned
    length says how far the pinned hash reaches, and the bytes past it are
    whatever the current holder has appended since. A file the digest names
    that is shorter than that, starts differently, or is missing is tampered.
    """
    pinned = read_trace_digest(chat_dir)
    if pinned is None or name not in pinned.files:
        return TraceVerdict.UNPINNED
    path = chat_dir / name
    if not path.is_file():
        return TraceVerdict.TAMPERED
    length = pinned.sizes.get(name)
    try:
        hashed = _hash_prefix(path, length)
    except OSError:
        return TraceVerdict.TAMPERED
    if hashed is None:
        return TraceVerdict.TAMPERED
    if hashed[0] == pinned.files[name]:
        return TraceVerdict.INTACT
    return TraceVerdict.TAMPERED if length is not None else TraceVerdict.UNPINNED


__all__ = [
    "DIGEST_FILENAME",
    "TRACE_FILES",
    "TraceVerdict",
    "compute_trace_digest",
    "pin_trace",
    "pin_trace_if_changed",
    "read_trace_digest",
    "verify_trace_file",
]
