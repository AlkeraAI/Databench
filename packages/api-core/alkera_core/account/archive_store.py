"""The account archive store: objects that belong to a person, not an org,
kept at the root of the Files bucket (an export's archive under
``account-exports/``, the erasure ledger under its own prefix).

Such an object has no dedup domain and no place under ``domains/``; keeping it
outside that prefix is also what keeps the Files collector, which only ever
walks ``domains/``, from reading it as an orphan. The store is opened in the
domain key layout over the whole bucket, so keys are plain relative strings and
every driver's key validation still runs.

A test, or a deployment with its own bucket for archives, installs a different
store through :func:`set_archive_store`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from blake3 import blake3

from alkera_core.config import Settings
from alkera_core.config import settings as default_settings
from alkera_core.files.clock import SystemClock
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.files.store.protocol import ObjectStore
from alkera_core.files.store.s3_compatible import S3CompatibleStore, S3Config

PREFIX = "account-exports"
_CHUNK = 1024 * 1024

_store: ObjectStore | None = None


def set_archive_store(store: ObjectStore | None) -> None:
    """Install (or, with ``None``, drop) the store archives go to."""
    global _store
    _store = store


def build_archive_store(settings: Settings) -> ObjectStore:
    if settings.files_store_provider == "filesystem":
        root = settings.files_store_root_path / "account-lifecycle"
        return FilesystemStore(root, clock=lambda: datetime.now(UTC))
    if not settings.files_store_bucket:
        raise RuntimeError("files_store_bucket must be set for this files_store_provider")
    config = S3Config(
        endpoint_url=settings.files_store_endpoint,
        region=settings.files_store_region,
        bucket=settings.files_store_bucket,
        access_key=settings.files_store_access_key,
        secret_key=(
            settings.files_store_secret_key.get_secret_value()
            if settings.files_store_secret_key is not None
            else None
        ),
        addressing=settings.files_store_addressing,
    )
    return S3CompatibleStore(config, clock=SystemClock(), layout="domain")


def archive_store() -> ObjectStore:
    global _store
    if _store is None:
        _store = build_archive_store(default_settings)
    return _store


async def _chunks(path: Path) -> AsyncIterator[bytes]:
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            yield chunk


def _measure(path: Path) -> tuple[int, bytes]:
    """Size and blake3 digest (what every store driver checks a put against)."""
    hasher = blake3()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            hasher.update(chunk)
            size += len(chunk)
    return size, hasher.digest()


async def upload(store: ObjectStore, key: str, path: Path) -> int:
    """Put the file at ``path`` under ``key``. Returns its size."""
    size, digest = _measure(path)
    await store.put(key, _chunks(path), size=size, checksum=digest, if_absent=False)
    return size


__all__ = [
    "PREFIX",
    "archive_store",
    "build_archive_store",
    "set_archive_store",
    "upload",
]
