"""Tests for `BlobStore`."""

from __future__ import annotations

import hashlib
import io
import os
import time
from pathlib import Path

import pytest
from alkera_core.project.chats.blobs import BlobStore


def test_write_returns_sha_and_size(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    sha, size = store.write(b"hello world")
    assert size == 11
    assert sha == hashlib.sha256(b"hello world").hexdigest()
    assert store.path(sha).exists()


def test_write_is_idempotent(tmp_path: Path) -> None:
    """Same content written twice = same file, written exactly once."""
    store = BlobStore(tmp_path)
    sha1, _ = store.write(b"identical")
    mtime1 = store.path(sha1).stat().st_mtime
    time.sleep(0.05)  # ensure mtime would tick if we re-wrote
    sha2, _ = store.write(b"identical")
    assert sha1 == sha2
    # File mtime is unchanged → we didn't rewrite.
    assert store.path(sha1).stat().st_mtime == mtime1


def test_write_stream(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    sha, size = store.write(io.BytesIO(b"a" * 200_000))
    assert size == 200_000
    assert store.path(sha).stat().st_size == 200_000


def test_read_round_trip(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    sha, _ = store.write(b"payload")
    assert store.read(sha) == b"payload"


def test_read_missing_raises(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.read("0" * 64)


def test_invalid_sha_rejected(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    with pytest.raises(ValueError):
        store.path("too-short")
    with pytest.raises(ValueError):
        store.path("g" * 64)  # not hex


def test_path_uses_two_level_fanout(tmp_path: Path) -> None:
    """Layout: `<root>/<ab>/<cd>/<sha256>`."""
    store = BlobStore(tmp_path)
    sha = "ab" + "cd" + "e" * 60
    p = store.path(sha)
    assert p.parts[-3:] == ("ab", "cd", sha)


def test_sweep_deletes_unreferenced_old_blobs(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    sha_a, _ = store.write(b"A")
    sha_b, _ = store.write(b"B")
    sha_c, _ = store.write(b"C")
    # Backdate all three so they're past the grace period.
    past = time.time() - 3600
    for sha in (sha_a, sha_b, sha_c):
        os.utime(store.path(sha), (past, past))

    # Reference only A and B; C should be swept.
    report = store.sweep({sha_a, sha_b}, grace_period_seconds=60)
    assert report.deleted == 1
    assert report.kept == 2
    assert store.exists(sha_a)
    assert store.exists(sha_b)
    assert not store.exists(sha_c)


def test_sweep_respects_grace_period(tmp_path: Path) -> None:
    """A fresh, unreferenced blob is kept if it's within the grace
    period — covers in-flight writes that haven't been referenced
    by any chat.jsonl yet."""
    store = BlobStore(tmp_path)
    sha_new, _ = store.write(b"fresh")
    # Default grace = 24h; we just wrote it, so it should survive.
    report = store.sweep(set(), grace_period_seconds=24 * 60 * 60)
    assert report.deleted == 0
    assert store.exists(sha_new)


def test_sweep_with_concurrent_holder_returns_empty_report(tmp_path: Path) -> None:
    """A second sweep racing with the first should bail rather than
    double-delete. We simulate by manually planting a live GC lock
    file."""
    store = BlobStore(tmp_path)
    sha, _ = store.write(b"x")
    # Backdate so it would normally be deleted.
    past = time.time() - 3600
    os.utime(store.path(sha), (past, past))

    # Plant a "live" GC lock with our own PID.
    import json as _j
    import socket as _s

    lock_path = tmp_path / "blobs.gc.lock"
    lock_path.write_text(
        _j.dumps(
            {
                "pid": os.getpid(),
                "host": _s.gethostname(),
                "acquired_at": "2026-01-01T00:00:00Z",
                "nonce": "live",
            }
        )
    )
    report = store.sweep(set(), grace_period_seconds=60)
    assert report.deleted == 0
    # The unreferenced-and-old blob is STILL there — we didn't sweep it.
    assert store.exists(sha)
    # Clean up the planted lock so subsequent calls work.
    lock_path.unlink()


def test_sweep_releases_gc_lock_when_done(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    store.sweep(set(), grace_period_seconds=60)
    # Lock should NOT exist after a clean sweep.
    assert not (tmp_path / "blobs.gc.lock").exists()
