"""Upload session state and part records.

Both are written to Postgres JSONB and read back by whichever backend version
happens to serve the next part of the same upload, so they are `VersionedModel`s:
a rolling deploy always has two readers.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from alkera_core.files.ids import DriveId, NodeId, SessionId
from alkera_core.versioning import VersionedModel

TransferMode = Literal["proxied", "direct", "single"]
"""How the bytes reach the store: through the API, straight from the client to a
signed URL, or as the one-shot PUT that still opens a session row first."""


class PartRecord(VersionedModel):
    """One accepted part of a multi-part upload.

    The checksum is kept so a re-sent part with different bytes is refused
    rather than silently overwriting an accepted one.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    part_no: int = 0
    size: int = 0
    checksum: str = ""
    store_etag: str | None = None
    received_at: datetime | None = None


class UploadSessionState(VersionedModel):
    """Everything an upload needs to be resumed by a different backend process."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    session_id: SessionId | None = None
    drive_id: DriveId | None = None
    parent_id: NodeId | None = None
    name: str = ""
    declared_size: int = 0
    mime_hint: str | None = None
    transfer_mode: TransferMode = "proxied"
    idempotency_key: str | None = None
    conflict_behavior: Literal["fail", "replace", "rename"] = "fail"
    parts: list[PartRecord] = Field(default_factory=list)
    bytes_received: int = 0
    opened_at: datetime | None = None
    expires_at: datetime | None = None
    hold_id: str | None = None
    lease_epoch: int | None = None
    aborted: bool = False


class SessionStatus(BaseModel):
    """The wire answer to `GET …/sessions/{id}` — what the client must resume from."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    session_id: str
    offset: int
    length: int
    complete: bool
    parts_done: int
    expires_at: datetime | None = None


FIXTURE_EXAMPLES: list[tuple[str, Callable[[], VersionedModel]]] = [
    (
        "upload_part_record",
        lambda: PartRecord(
            part_no=7,
            size=33_554_432,
            checksum="9f" * 32,
            store_etag="etag-7",
            received_at=datetime.fromisoformat("2026-01-01T12:00:00+00:00"),
        ),
    ),
    (
        "upload_session_state",
        lambda: UploadSessionState(
            name="report.pdf",
            declared_size=67_108_864,
            mime_hint="application/pdf",
            transfer_mode="proxied",
            idempotency_key="c0ffee",
            parts=[PartRecord(part_no=1, size=33_554_432, checksum="ab" * 32)],
            bytes_received=33_554_432,
            opened_at=datetime.fromisoformat("2026-01-01T12:00:00+00:00"),
            expires_at=datetime.fromisoformat("2026-01-08T12:00:00+00:00"),
        ),
    ),
]
