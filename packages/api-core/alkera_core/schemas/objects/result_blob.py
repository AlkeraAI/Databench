"""The result-blob shapes, spelled once for the cloud store.

These are the daemon's shapes (``alkera_cli.plugins.plugin_base.result_blob``)
carried across the process boundary unchanged: the producer writes a
``ResultBlobEnvelope`` into its local content-addressed blob store and hands
the model a :class:`BlobHandle`; a promote uploads the same envelope, under the
same handle, into the cloud result store.

They live here rather than being imported from the CLI because the backend
depends on ``alkera-core`` and must not depend on ``alkera-cli`` — the layering
rule in CLAUDE.md. The two spellings are pinned equal by a drift test
(``packages/api-core/tests/schemas/objects/test_result_blob_parity.py``), so a
field added on one side fails there instead of silently truncating an upload.

Persisted across a process boundary ⇒ ``VersionedModel``, carrying the same
``SCHEMA_VERSION`` the producer writes, so a blob written by a daemon of one
version stays readable by a cloud of another.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field

from alkera_core.versioning import VersionedModel


class BlobHandle(VersionedModel):
    """A pointer to a result payload: its digest, its size and its media type.

    The same three fields the daemon hands the model, so the cloud store can
    later move from Postgres rows to object storage without any shape a
    client sees changing.

    Versioned for the same reason the envelope it points at is: the handle is
    persisted inside a ``ResultSpec`` and crosses the process boundary on every
    promote, and a storage move behind an unchanged handle is exactly when it
    grows a field. As a plain model it silently dropped whatever a newer daemon
    put beside the digest, and the spec round-tripped looking complete.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    sha256: str
    size: int
    media_type: str = "application/json"


class ResultBlobEnvelope(VersionedModel):
    """The canonical payload both result producers write.

    ``kind="rows"`` populates ``columns``/``rows`` and pages by row;
    ``kind="text"`` populates ``text`` and pages by character. ``total`` is the
    full size before paging, so a reader knows what its page is a page of.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    kind: Literal["rows", "text"] = "text"
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    text: str = ""
    total: int = 0


__all__ = ["BlobHandle", "ResultBlobEnvelope"]
