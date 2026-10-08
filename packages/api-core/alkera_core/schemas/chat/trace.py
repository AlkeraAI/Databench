"""Integrity pin for a chat's local trace files."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from pydantic import Field

from alkera_core.versioning import VersionedModel


class TraceDigest(VersionedModel):
    """SHA-256 digests of one session's trace files.

    Written next to the files it pins, and its ``combined`` hash rides the
    ``agent.session_finished`` audit event. A trace can then be verified
    against the org's audit record; a deleted or edited trace becomes a
    visible mismatch instead of a silent gap.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.1.0"

    session_id: str = ""
    algorithm: str = "sha256"
    files: dict[str, str] = Field(default_factory=dict)
    """Filename to hex digest, for the trace files present at pin time."""
    sizes: dict[str, int] = Field(default_factory=dict)
    """Filename to the byte length the digest in ``files`` covers.

    The logs are append-only, so a file pinned on one box and carried on by
    the next is longer than it was and still starts with the bytes that were
    pinned. The length is what lets a reader check exactly that prefix instead
    of the whole file, and so tell an append from an edit. Added in 1.1.0;
    a digest written before it names no lengths, and a reader falls back to
    comparing the whole file.
    """
    combined: str = ""
    """Digest over the sorted ``name:digest`` lines. The value audit events carry."""
    created_at: datetime | None = None
