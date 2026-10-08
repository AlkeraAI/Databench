"""The object-store protocol, its error set, the key layout and drivers."""

from __future__ import annotations

from alkera_core.files.store._beneath import resolve_beneath
from alkera_core.files.store.errors import (
    ChecksumMismatch,
    InvalidKey,
    NoSuchBucket,
    NotFound,
    PreconditionFailed,
    StoreError,
    Throttled,
    Unavailable,
)
from alkera_core.files.store.filesystem import FILESYSTEM_CAPABILITIES, FilesystemStore
from alkera_core.files.store.keys import (
    absolute,
    deleted_key,
    erased_key,
    incoming_key,
    object_key,
    validate_relative_key,
)
from alkera_core.files.store.protocol import (
    ListPage,
    ObjectInfo,
    ObjectStore,
    PartResult,
    PutResult,
    ScopedCredentials,
    StoreCapabilities,
    UploadHandle,
)

__all__ = [
    "FILESYSTEM_CAPABILITIES",
    "ChecksumMismatch",
    "FilesystemStore",
    "InvalidKey",
    "ListPage",
    "NoSuchBucket",
    "NotFound",
    "ObjectInfo",
    "ObjectStore",
    "PartResult",
    "PreconditionFailed",
    "PutResult",
    "ScopedCredentials",
    "StoreCapabilities",
    "StoreError",
    "Throttled",
    "Unavailable",
    "UploadHandle",
    "absolute",
    "deleted_key",
    "erased_key",
    "incoming_key",
    "object_key",
    "resolve_beneath",
    "validate_relative_key",
]
