"""The payloads of the Loro CRDT lane.

A CRDT document (``doc:chat_draft:<id>``, ``doc:file:<node_id>``) is
synchronised with Loro's own primitives rather than the op log's sequence
numbers. A peer opens it with ``hello`` carrying the version vector it already
holds; the server answers with ``snapshot`` carrying whatever the peer is
missing (Loro updates since that vector, or a whole snapshot); from then on a
peer sends its edits as ``crdt{t: update}`` and the server answers the sender
with ``ack`` carrying the server's version vector AFTER the durable commit — a
local edit is settled once that vector covers it. Every other subscriber
receives the canonical delta as ``crdt{t: update}``. Carets and selections ride
``crdt{t: ephemeral}``: a Loro ``EphemeralStore`` update that is relayed and
never stored.

There is no sequence number on this lane: the envelope's ``seq`` is always 0,
ordering and missing history are Loro's causality (an import that reports
``pending`` asks for a version-vector resync), and ``epoch`` is the only
lifecycle counter — it moves on history rotation, a quarantine re-seed or a
schema upgrade, and a peer rebases onto it.

Binary Loro data travels as standard base64 in JSON text frames. A blob over
:data:`CRDT_CHUNK_BYTES` is split into ordered chunks so every frame stays
small enough to interleave with pings and carets and to pass every proxy on
the way. These models are persisted (a broadcast is a ``doc.op`` outbox row),
so they are versioned and carried by the realtime lineage corpus.
"""

from __future__ import annotations

import base64
from typing import Annotated, Any, ClassVar, Final, Literal

from pydantic import Discriminator, Field, Tag, TypeAdapter, field_validator, model_validator

from alkera_core.versioning import VersionedModel, make_unknown_tag_discriminator

#: The CRDT lane's wire protocol. A peer sends the one it speaks in ``hello``;
#: the server refuses one it cannot serve with ``crdt_unsupported`` and the
#: peer stays on the fallback lane.
CRDT_PROTOCOL: Final = 1

#: The largest raw blob one frame carries. Larger blobs are chunked.
CRDT_CHUNK_BYTES: Final = 32 * 1024
#: The most raw bytes one chunked transfer may add up to. A sync carries a
#: whole document in one transfer, so this covers the largest document any
#: type may hold (``max_doc_bytes``, 8 MiB for a file) twice over: a snapshot
#: can encode a little larger than the stored bytes it was built from. An
#: update is held to its type's far smaller ``max_update_bytes`` on top.
CRDT_MAX_TRANSFER_BYTES: Final = 16 * 1024 * 1024
CRDT_MAX_CHUNKS: Final = CRDT_MAX_TRANSFER_BYTES // CRDT_CHUNK_BYTES
#: The largest raw delta carried inline in a stored broadcast; a larger one is
#: stored by reference and read from the update log when it is sent.
CRDT_MAX_INLINE_BYTES: Final = 256 * 1024
#: An encoded Loro version vector: a varint per peer that ever wrote.
CRDT_MAX_VV_BYTES: Final = 16 * 1024
#: An encoded ``EphemeralStore`` update. The relay rides ``NOTIFY``, whose
#: payload Postgres caps at 8000 bytes, alongside the server's identity stamp.
CRDT_MAX_EPHEMERAL_BYTES: Final = 2 * 1024

#: Loro peer ids the server keeps for itself (seeding, rotation, re-seeding).
#: Every id it hands a client is above this and below 2**53, so it survives a
#: JavaScript number.
CRDT_SERVER_PEER_MAX: Final = 1023
CRDT_MAX_PEER: Final = 2**53 - 1

MAX_UPDATE_ID_LENGTH: Final = 64
UPDATE_ID_PATTERN: Final = r"^[A-Za-z0-9._:-]{1,64}$"

CrdtSyncMode = Literal["updates", "snapshot"]
CRDT_SYNC_MODES: Final[tuple[str, ...]] = ("updates", "snapshot")


def b64_length(raw_bytes: int) -> int:
    """The length of the standard (padded) base64 text of ``raw_bytes`` bytes."""
    return 4 * ((raw_bytes + 2) // 3)


def decode_b64(value: str) -> bytes:
    """Strict standard base64 (no whitespace, no URL alphabet, correct padding);
    raises ``ValueError`` otherwise."""
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ValueError("not standard base64") from exc


def encode_b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _strict_b64(value: str | None, *, max_raw: int, allow_empty: bool) -> str | None:
    if value is None:
        return None
    if not value and not allow_empty:
        raise ValueError("must not be empty")
    # The text length is checked first so a huge string is refused before it
    # is decoded, and the decoded length after: padding means one base64
    # length covers up to three raw lengths.
    if len(value) > b64_length(max_raw) or len(decode_b64(value)) > max_raw:
        raise ValueError(f"longer than {max_raw} bytes")
    return value


class CrdtChunk(VersionedModel):
    """One piece of a blob too large for one frame.

    The pieces of a transfer share ``xfer_id``, arrive in ``index`` order and
    each carry the whole transfer's size and digest, so a receiver can refuse a
    transfer the moment one piece disagrees with the first. A transfer is
    complete when piece ``count - 1`` arrives, its pieces add up to
    ``total_bytes`` and their SHA-256 is ``sha256_b64``.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    xfer_id: str = Field(pattern=UPDATE_ID_PATTERN)
    index: int = Field(ge=0)
    count: int = Field(ge=2, le=CRDT_MAX_CHUNKS)
    total_bytes: int = Field(gt=CRDT_CHUNK_BYTES, le=CRDT_MAX_TRANSFER_BYTES)
    sha256_b64: str = Field(min_length=44, max_length=44)
    data_b64: str

    @field_validator("sha256_b64")
    @classmethod
    def _digest(cls, value: str) -> str:
        if len(decode_b64(value)) != 32:
            raise ValueError("not a SHA-256 digest")
        return value

    @field_validator("data_b64")
    @classmethod
    def _data(cls, value: str) -> str:
        checked = _strict_b64(value, max_raw=CRDT_CHUNK_BYTES, allow_empty=False)
        assert checked is not None
        return checked

    @model_validator(mode="after")
    def _shape(self) -> CrdtChunk:
        if self.index >= self.count:
            raise ValueError("index must be below count")
        if self.count != -(-self.total_bytes // CRDT_CHUNK_BYTES):
            raise ValueError("count must be total_bytes split into full chunks")
        return self


class _Blob(VersionedModel):
    """A Loro blob carried inline (``data_b64``) or as one chunk of a transfer."""

    __abstract__: ClassVar[bool] = True
    #: The largest raw blob allowed inline in this payload.
    MAX_INLINE_BYTES: ClassVar[int] = CRDT_CHUNK_BYTES

    data_b64: str | None = None
    chunk: CrdtChunk | None = None

    @model_validator(mode="after")
    def _inline_or_chunk(self) -> _Blob:
        if (self.data_b64 is None) == (self.chunk is None):
            raise ValueError("exactly one of data_b64 and chunk")
        _strict_b64(self.data_b64, max_raw=self.MAX_INLINE_BYTES, allow_empty=True)
        return self


class CrdtHelloPayload(VersionedModel):
    """Client → server: open a CRDT document.

    ``vv_b64`` is the encoded Loro version vector the peer already holds for
    ``epoch_seen`` (absent on a first open); the server answers with only what
    that vector is missing. ``loro_peer`` is the Loro peer id the server handed
    this tab before, offered for reuse; the server confirms or replaces it.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    proto: int = Field(ge=1)
    #: The ``loro-crdt`` version the peer runs, for diagnosing skew.
    loro: str = Field(min_length=1, max_length=32)
    #: The highest document schema the peer understands.
    doc_schema: int = Field(ge=1)
    vv_b64: str | None = None
    loro_peer: int | None = Field(default=None, gt=CRDT_SERVER_PEER_MAX, le=CRDT_MAX_PEER)
    epoch_seen: int | None = Field(default=None, ge=1)

    @field_validator("vv_b64")
    @classmethod
    def _vv(cls, value: str | None) -> str | None:
        return _strict_b64(value, max_raw=CRDT_MAX_VV_BYTES, allow_empty=True)

    @model_validator(mode="after")
    def _vv_names_its_epoch(self) -> CrdtHelloPayload:
        if self.vv_b64 is not None and self.epoch_seen is None:
            raise ValueError("a version vector is only meaningful with the epoch it belongs to")
        return self


class CrdtLimits(VersionedModel):
    """What the server accepts on this document, so a peer splits and caps
    before it sends rather than after it is refused."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    chunk_bytes: int = Field(ge=1)
    max_update_bytes: int = Field(ge=1)
    max_doc_bytes: int = Field(ge=1)
    #: The soft cap on the text a person types; 0 when the type has none.
    max_text_bytes: int = Field(default=0, ge=0)


class CrdtSaving(VersionedModel):
    """Whether a document's edits are reaching where it rests (a file's
    drive), as its row records it: ``paused`` with why, or ``ok``. Carried
    by every sync and every ``reload`` of a document that rests somewhere,
    so a tab that opens or reconnects after saving paused is told at once
    rather than only by the next ``crdt{t: saving}`` notice."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    state: Literal["ok", "paused"] = "ok"
    #: Why it is paused (see :class:`CrdtSavingPayload`). Empty when ``ok``.
    reason: str = Field(default="", max_length=64)

    @classmethod
    def of(cls, paused_reason: str | None) -> CrdtSaving:
        """The state a row's ``save_paused_reason`` records: ``None`` is
        ``ok``, anything else ``paused`` with it as the reason."""
        if paused_reason is None:
            return cls(state="ok")
        return cls(state="paused", reason=paused_reason[:64])


class CrdtSyncPayload(_Blob):
    """Server → the peer that said ``hello``: what it is missing.

    ``mode`` is ``updates`` (Loro updates since the peer's vector, possibly
    empty) or ``snapshot`` (the whole document: a first open, another epoch, or
    a vector the server cannot serve from). ``vv_b64`` is the server's vector
    after it, ``loro_peer`` the Loro peer id this tab must write as, and
    ``doc_schema`` the document's schema. A chunked sync carries these on every
    piece.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.1.0"
    MAX_INLINE_BYTES: ClassVar[int] = CRDT_CHUNK_BYTES

    mode: CrdtSyncMode
    vv_b64: str
    loro_peer: int = Field(gt=CRDT_SERVER_PEER_MAX, le=CRDT_MAX_PEER)
    doc_schema: int = Field(ge=1)
    limits: CrdtLimits
    #: Whether the document's edits are reaching its source; ``None`` for a
    #: document that rests nowhere, and from a server before 1.1.0.
    saving: CrdtSaving | None = None

    @field_validator("vv_b64")
    @classmethod
    def _vv(cls, value: str) -> str:
        checked = _strict_b64(value, max_raw=CRDT_MAX_VV_BYTES, allow_empty=True)
        assert checked is not None
        return checked


class CrdtUpdatePayload(_Blob):
    """``crdt{t: update}``: Loro updates.

    Client → server: the peer's local changes since the server's last vector,
    under ``update_id`` (its idempotency key; the ``ack`` or ``error`` echoes
    it). Server → every other subscriber: the canonical delta the server
    committed, stamped with the Loro peer and the user who wrote it and the
    server's vector after it.

    ``log_ref`` is server-internal: a stored broadcast whose delta is too
    large to carry inline names its row in the update log instead, and the
    gateway reads it from there before sending. A client never sends or
    receives it.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"
    MAX_INLINE_BYTES: ClassVar[int] = CRDT_MAX_INLINE_BYTES

    t: Literal["update"] = "update"
    update_id: str = Field(pattern=UPDATE_ID_PATTERN)
    #: Stamped by the server; a server-authored change carries a reserved id.
    loro_peer: int | None = Field(default=None, ge=1, le=CRDT_MAX_PEER)
    user_id: str | None = Field(default=None, max_length=64)
    vv_b64: str | None = None
    log_ref: int | None = Field(default=None, ge=1)

    @field_validator("vv_b64")
    @classmethod
    def _vv(cls, value: str | None) -> str | None:
        return _strict_b64(value, max_raw=CRDT_MAX_VV_BYTES, allow_empty=True)

    @model_validator(mode="before")
    @classmethod
    def _by_reference_carries_no_bytes(cls, data: Any) -> Any:
        # A stored-by-reference broadcast is the one shape with neither inline
        # bytes nor a chunk; give the blob check an empty inline body for it.
        if isinstance(data, dict) and data.get("log_ref") is not None:
            if data.get("data_b64") not in (None, "") or data.get("chunk") is not None:
                raise ValueError("a by-reference update carries no bytes")
            return {**data, "data_b64": ""}
        return data


class CrdtEphemeralPayload(VersionedModel):
    """``crdt{t: ephemeral}``: a Loro ``EphemeralStore`` update (carets and
    selections as Loro cursors), relayed to the other subscribers and never
    stored. A peer may only write its own key; the server checks that and
    stamps who it is (``loro_peer``, ``user_id``, ``display_name``, ``email``)
    so nobody can speak as somebody else."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    t: Literal["ephemeral"] = "ephemeral"
    data_b64: str
    loro_peer: int | None = Field(default=None, gt=CRDT_SERVER_PEER_MAX, le=CRDT_MAX_PEER)
    user_id: str | None = Field(default=None, max_length=64)
    display_name: str = Field(default="", max_length=128)
    email: str = Field(default="", max_length=320)

    @field_validator("data_b64")
    @classmethod
    def _data(cls, value: str) -> str:
        checked = _strict_b64(value, max_raw=CRDT_MAX_EPHEMERAL_BYTES, allow_empty=False)
        assert checked is not None
        return checked


class CrdtGonePayload(VersionedModel):
    """``crdt{t: gone}``: a tab left the document — its socket closed, it left
    the channel, or it came back as a new Loro peer. Sent by the server alone,
    to the document's other subscribers, so the tab's caret goes at once
    rather than when it times out. A client may not send one."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    t: Literal["gone"] = "gone"
    loro_peer: int = Field(gt=CRDT_SERVER_PEER_MAX, le=CRDT_MAX_PEER)


class CrdtSavingPayload(VersionedModel):
    """``crdt{t: saving}``: whether the document's edits are reaching where
    it rests (a file's drive). Sent by the server alone, to every subscriber,
    when writing it back is refused (``paused``, with why) and again when it
    lands after a refusal (``ok``). A subscriber re-reads its own grant on it:
    a refusal can mean nobody may write the file now. A client may not send
    one."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    t: Literal["saving"] = "saving"
    state: Literal["ok", "paused"]
    #: Why it is paused: ``no_writer`` (nobody who wrote it may still write
    #: the file), ``leased`` (a machine holds the folder and takes no outside
    #: write), ``gone`` (the file is in the trash), ``too_large``, ``binary``
    #: or a merge refusal such as ``text_too_large`` (the file on the drive
    #: can no longer be edited live), ``quarantined_lost`` (a broken history
    #: lost the edits since the last save), or the drive's refusal code
    #: (``files.frozen``, ``files.quota_*``). Empty when ``ok``.
    reason: str = Field(default="", max_length=64)


class RawCrdtPayload(VersionedModel):
    """A ``crdt`` payload whose ``t`` this reader does not know."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    t: str = Field(min_length=1, max_length=64)


class CrdtAckPayload(VersionedModel):
    """Server → the sending peer: ``update_id`` is durable. ``vv_b64`` is the
    server's version vector after the commit; ``changed`` is ``False`` when the
    update added nothing the server did not already hold (a retry)."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    update_id: str = Field(pattern=UPDATE_ID_PATTERN)
    changed: bool
    vv_b64: str

    @field_validator("vv_b64")
    @classmethod
    def _vv(cls, value: str) -> str:
        checked = _strict_b64(value, max_raw=CRDT_MAX_VV_BYTES, allow_empty=True)
        assert checked is not None
        return checked


CRDT_PAYLOAD_TAGS: Final[frozenset[str]] = frozenset({"update", "ephemeral", "gone", "saving"})
UNKNOWN_CRDT_TAG: Final = "__unknown__"

CrdtPayload = Annotated[
    (
        Annotated[CrdtUpdatePayload, Tag("update")]
        | Annotated[CrdtEphemeralPayload, Tag("ephemeral")]
        | Annotated[CrdtGonePayload, Tag("gone")]
        | Annotated[CrdtSavingPayload, Tag("saving")]
        | Annotated[RawCrdtPayload, Tag(UNKNOWN_CRDT_TAG)]
    ),
    Discriminator(make_unknown_tag_discriminator(set(CRDT_PAYLOAD_TAGS), field="t")),
]
CRDT_PAYLOAD_ADAPTER: TypeAdapter[CrdtPayload] = TypeAdapter(CrdtPayload)


def parse_crdt_payload(payload: dict[str, Any]) -> CrdtPayload:
    """The payload of a ``crdt`` envelope; an unknown ``t`` yields a
    :class:`RawCrdtPayload`, a known one with a bad body raises
    ``pydantic.ValidationError``."""
    return CRDT_PAYLOAD_ADAPTER.validate_python(payload)


__all__ = [
    "CRDT_CHUNK_BYTES",
    "CRDT_MAX_CHUNKS",
    "CRDT_MAX_EPHEMERAL_BYTES",
    "CRDT_MAX_INLINE_BYTES",
    "CRDT_MAX_PEER",
    "CRDT_MAX_TRANSFER_BYTES",
    "CRDT_MAX_VV_BYTES",
    "CRDT_PAYLOAD_ADAPTER",
    "CRDT_PAYLOAD_TAGS",
    "CRDT_PROTOCOL",
    "CRDT_SERVER_PEER_MAX",
    "CRDT_SYNC_MODES",
    "MAX_UPDATE_ID_LENGTH",
    "UNKNOWN_CRDT_TAG",
    "UPDATE_ID_PATTERN",
    "CrdtAckPayload",
    "CrdtChunk",
    "CrdtEphemeralPayload",
    "CrdtGonePayload",
    "CrdtHelloPayload",
    "CrdtLimits",
    "CrdtPayload",
    "CrdtSaving",
    "CrdtSavingPayload",
    "CrdtSyncMode",
    "CrdtSyncPayload",
    "CrdtUpdatePayload",
    "RawCrdtPayload",
    "b64_length",
    "decode_b64",
    "encode_b64",
    "parse_crdt_payload",
]
