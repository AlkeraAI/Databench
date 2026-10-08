"""The doc-sync envelope: one shape for every message on a document channel.

A document (``doc:<type>:<id>``) is synchronised by exchanging envelopes.
Every envelope names the document, the epoch of the state it speaks about,
the peer that sent it, a sequence number, a ``kind`` and a kind-specific
``payload``. The server owns ``epoch`` and ``seq``: a client's operation is
accepted only against the current epoch (a stale one is answered with
``error`` + ``reload`` and the client re-``hello``s), and the server stamps
the sequence a durable operation was applied at before broadcasting it.

These models are persisted — a durable operation is written to the event
outbox as a ``doc.op`` row and a document's state lives in ``realtime_docs``
— so they are ``VersionedModel``s with fixtures and a lineage test, and the
payload stays an open dict so a newer writer's kind rides through an older
reader. The kind-specific payload models here are the contract a peer
validates against once it knows the kind.

A ``chat`` or ``artifact`` document syncs over the op log (``op`` / ``ack``
with server-stamped sequence numbers). A CRDT document type
(:data:`CRDT_DOC_TYPES`) syncs over the Loro lane instead: ``hello`` /
``snapshot`` / ``crdt`` / ``ack`` carry the payloads in
:mod:`alkera_core.schemas.realtime.crdt`, ``seq`` is always 0 and ordering is
Loro's causality. A reserved document type (:data:`RESERVED_DOC_TYPES`) is
named on the wire so a newer writer needs no protocol break, and the gateway
refuses it until it is enabled.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any, ClassVar, Final, Literal, get_args

from pydantic import Field, model_validator

from alkera_core.doc_type_names import LEGACY_DOC_TYPE_ALIASES as LEGACY_DOC_TYPE_ALIASES
from alkera_core.schemas.realtime.crdt import CrdtSaving
from alkera_core.versioning import Migration, VersionedModel

#: ``chat_workspace`` is the composer draft's spelling before 2.0.0, still
#: admitted on the wire for a deprecation window: a web or VS Code build from
#: before the rename subscribes under it. The socket maps it to ``chat_draft``
#: on the way in and back on the way out (:data:`LEGACY_DOC_TYPE_ALIASES`), so
#: nothing past the socket ever sees it. It goes at the next major.
DocType = Literal["chat", "artifact", "chat_draft", "file", "notebook", "chat_workspace"]
DOC_TYPES: Final[tuple[str, ...]] = get_args(DocType)
#: ``legacy spelling -> the document type it names now``, from the one table of
#: renamed document types (:mod:`alkera_core.doc_type_names`).
_LEGACY_SPELLING: Final[Mapping[str, str]] = {
    current: legacy for legacy, current in LEGACY_DOC_TYPE_ALIASES.items()
}
#: Document types synchronised over the Loro CRDT lane.
CRDT_DOC_TYPES: Final[frozenset[str]] = frozenset({"chat_draft", "file", "notebook"})
#: Document types the wire names but the gateway refuses until they ship.
#: Empty: ``file`` was reserved until co-edited files shipped.
RESERVED_DOC_TYPES: Final[frozenset[str]] = frozenset()

EnvelopeKind = Literal["hello", "snapshot", "op", "ack", "presence", "reload", "error", "crdt"]
ENVELOPE_KINDS: Final[tuple[str, ...]] = get_args(EnvelopeKind)

OpIntent = Literal["append", "set_meta", "user_message", "chunk", "set_fields"]
OP_INTENTS: Final[tuple[str, ...]] = get_args(OpIntent)

#: The peer id every server-originated envelope carries (a snapshot, a rebuild).
SERVER_PEER_ID: Final = "srv:0"
#: Kinds the envelope names but no gateway accepts yet. Empty: ``crdt`` was
#: reserved until the Loro lane shipped.
RESERVED_KINDS: Final[frozenset[str]] = frozenset()

MAX_DOC_ID_LENGTH: Final = 255
MAX_PEER_ID_LENGTH: Final = 64
MAX_OP_ID_LENGTH: Final = 64

#: The channel grammar: ``doc:<type>:<id>``. Spelled once here, from the doc
#: types, so the gateway and the protocol descriptor a client reads cannot
#: disagree. Longest type first, so ``chat_draft`` is never read as ``chat``.
CHANNEL_PATTERN: Final = (
    r"^doc:("
    + "|".join(sorted(DOC_TYPES, key=lambda name: (-len(name), name)))
    + r"):([A-Za-z0-9._:-]{1,255})$"
)
CHANNEL_RE: Final = re.compile(CHANNEL_PATTERN)


def canonical_doc_type(doc_type: str) -> str:
    """The document type a wire spelling names now: itself, or what a legacy
    alias stands for."""
    return LEGACY_DOC_TYPE_ALIASES.get(doc_type, doc_type)


def canonical_channel(channel: str) -> tuple[str, bool]:
    """``channel`` with a legacy document type respelled, and whether it was.

    Anything that is not ``doc:<legacy>:<id>`` comes back unchanged, so a
    caller can pass every channel string a client sends through it."""
    for legacy, current in LEGACY_DOC_TYPE_ALIASES.items():
        prefix = f"doc:{legacy}:"
        if channel.startswith(prefix):
            return f"doc:{current}:{channel[len(prefix) :]}", True
    return channel, False


def legacy_channel(channel: str) -> str:
    """``channel`` respelled the way a client from before a rename says it.

    The inverse of :func:`canonical_channel` for a channel that has a legacy
    spelling; any other channel comes back unchanged."""
    for current, legacy in _LEGACY_SPELLING.items():
        prefix = f"doc:{current}:"
        if channel.startswith(prefix):
            return f"doc:{legacy}:{channel[len(prefix) :]}"
    return channel


def legacy_doc_type(doc_type: str) -> str:
    """The legacy spelling of ``doc_type``, or itself when it has none."""
    return _LEGACY_SPELLING.get(doc_type, doc_type)


def _canonical_draft(data: dict[str, Any]) -> dict[str, Any]:
    """An envelope at 1.1.0 with the draft named the way 2.0.0 writes it.

    Pure, and a no-op for every other type, so an old ``chat`` or ``file``
    envelope passes through unchanged."""
    out = dict(data)
    doc_type = out.get("doc_type")
    if isinstance(doc_type, str):
        out["doc_type"] = canonical_doc_type(doc_type)
    out["schema_version"] = "2.0.0"
    return out


class DocEnvelope(VersionedModel):
    """The message on a document channel; see the module docstring."""

    # 1.1.0 admits the ``chat_workspace`` and ``file`` document types (additive).
    # 2.0.0 writes the draft as ``chat_draft`` (breaking for a reader at 1.1.0,
    #       which cannot read the new name): the draft is a chat's composer
    #       text, and "workspace" now names the object that holds chats. An
    #       envelope stored at 1.x reads as the new name; ``chat_workspace`` is
    #       still admitted on the wire as a deprecated alias (see ``DocType``).
    # 2.1.0 admits the ``notebook`` document type (additive).
    SCHEMA_VERSION: ClassVar[str] = "2.1.0"
    MIGRATIONS: ClassVar[dict[str, Migration]] = {
        "1.0.0": lambda data: {**data, "schema_version": "1.1.0"},
        "1.1.0": lambda data: _canonical_draft(data),
    }

    doc_id: str = Field(min_length=1, max_length=MAX_DOC_ID_LENGTH)
    doc_type: DocType
    #: The state epoch this envelope speaks about. ``0`` means "I have no
    #: state yet" and is valid only on a ``hello``; every other kind names a
    #: real epoch, which the server numbers from 1.
    epoch: int = Field(ge=0)
    peer_id: str = Field(min_length=1, max_length=MAX_PEER_ID_LENGTH)
    #: Server-assigned position of a durable op within its epoch; ``0`` on a
    #: client-sent op (the server stamps it), on an ephemeral op and on control
    #: kinds.
    seq: int = Field(ge=0)
    kind: EnvelopeKind
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _epoch_zero_only_on_hello(self) -> DocEnvelope:
        if self.epoch == 0 and self.kind != "hello":
            raise ValueError("epoch 0 is only valid on a hello (a peer with no state yet)")
        return self

    @property
    def channel(self) -> str:
        return f"doc:{self.doc_type}:{self.doc_id}"


class HelloPayload(VersionedModel):
    """Client → server: open a document. ``since_seq`` is accepted and
    ignored today (resume is a full snapshot); it exists so an op log can be
    added without a protocol break."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    since_seq: int | None = Field(default=None, ge=0)


class SnapshotPayload(VersionedModel):
    """Server → client (answer to ``hello``, and after a rebuild): the whole
    state at ``seq`` of the envelope's epoch. A ``can_write`` peer may also
    send one to REPLACE the state (a publisher rebuild)."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    state: dict[str, Any] = Field(default_factory=dict)
    seq: int = Field(default=0, ge=0)


class FieldWrite(VersionedModel):
    """One field of a ``set_fields`` op: the value and the writer's clock.
    The server stamps the writing peer; last writer wins per field by
    ``(ts, peer_id)``."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    value: Any = None
    ts: float


class OpPayload(VersionedModel):
    """Client → server (and rebroadcast): one operation.

    ``intent`` selects the strategy: ``append`` / ``set_meta`` / ``user_message``
    / ``chunk`` for a chat document, ``set_fields`` for an artifact. ``op_id``
    is the client's idempotency key; the ``ack`` echoes it.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    op_id: str = Field(min_length=1, max_length=MAX_OP_ID_LENGTH)
    intent: OpIntent
    events: list[dict[str, Any]] = Field(default_factory=list)
    fields: dict[str, FieldWrite] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)


class AckPayload(VersionedModel):
    """Server → the sending peer: its op was applied at ``seq`` (``changed``
    is ``False`` for a no-op such as a stale field write or a relay)."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    op_id: str = Field(min_length=1, max_length=MAX_OP_ID_LENGTH)
    seq: int = Field(ge=0)
    changed: bool


class ReloadPayload(VersionedModel):
    """Server → every subscriber: the state was rebuilt (or the peer is
    stale); ``hello`` again to receive a fresh snapshot at ``epoch``."""

    SCHEMA_VERSION: ClassVar[str] = "1.1.0"

    epoch: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=64)
    saving: CrdtSaving | None = None
    """On a CRDT document that rests somewhere (a file), whether its edits
    are reaching it, so a tab knows before its next hello is answered.
    ``None`` on every other document. Added in 1.1.0."""


class ErrorPayload(VersionedModel):
    """Server → the sending peer: its envelope was refused."""

    SCHEMA_VERSION: ClassVar[str] = "1.2.0"

    code: str = Field(min_length=1, max_length=64)
    message: str = ""
    op_id: str | None = Field(default=None, max_length=64)
    """The ``op_id`` of the operation this refuses, when the refused envelope
    was an op — so a client settles exactly that op and never guesses from
    the order it sent them in. ``None`` for a refused hello or snapshot.
    Added in 1.1.0."""
    update_id: str | None = Field(default=None, max_length=64)
    """The ``update_id`` of the CRDT update this refuses, for the same reason.
    Added in 1.2.0."""
    reason: str | None = Field(default=None, max_length=64)
    """A machine-readable detail under ``code`` — which rule a refused CRDT
    update broke, for instance. Added in 1.2.0."""
    retry_after_ms: int | None = Field(default=None, ge=0, le=600_000)
    """How long to wait before sending again, on a refusal that is about load
    rather than content (``crdt_busy``). Added in 1.2.0."""


#: How much of the text either side of a caret rides with it, so a reader whose
#: copy of the draft has moved on can put the caret back beside the words it was
#: at rather than at a stale offset.
MAX_CURSOR_CONTEXT_LENGTH: Final = 32


class PresenceCursor(VersionedModel):
    """Where a peer's caret is in the shared composer draft, as an offset into
    the text they were looking at, with the selection's other end and a little
    of the text around the caret to re-anchor by. Never persisted: it rides the
    ephemeral lane on a ``presence`` frame with ``event: cursor`` and is gone
    when the peer is."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    offset: int = Field(ge=0)
    """The caret's position in the draft text, in UTF-16 code units."""
    anchor: int = Field(ge=0)
    """The selection's other end; equal to ``offset`` when nothing is selected."""
    before: str = Field(default="", max_length=MAX_CURSOR_CONTEXT_LENGTH)
    after: str = Field(default="", max_length=MAX_CURSOR_CONTEXT_LENGTH)


class PresencePeer(VersionedModel):
    """One peer on a channel as the roster shows it."""

    SCHEMA_VERSION: ClassVar[str] = "1.3.0"

    peer_id: str = Field(min_length=1, max_length=MAX_PEER_ID_LENGTH)
    user_id: str = Field(min_length=1, max_length=64)
    last_seen_at: datetime
    email: str = Field(default="", max_length=320)
    """The person's login address, resolved by the server from the ``users``
    row. It is what every surface keys this person's COLOUR off — a face in a
    presence stack, a caret in a shared draft — because a user id is per
    deployment and an email is the person: the same colleague is the same
    colour in every chat, every tab and every session. Empty where the server
    could not resolve one (an older writer's frame, a peer whose user row is
    gone), which a surface falls back from onto the user id. Added in 1.3.0."""
    display_name: str = Field(default="", max_length=128)
    """What to call this person on screen, resolved by the server from the
    ``users`` row. Empty where the server could not resolve one (a peer whose
    user row is gone, an older writer's frame), which a surface renders as an
    anonymous reader rather than as a sliced user id. Added in 1.1.0."""
    cursor: PresenceCursor | None = None
    """The peer's caret in the shared draft, on a ``cursor`` delta only; the
    roster and the join / heartbeat / leave deltas carry none. Added in 1.2.0."""


class PresencePayload(VersionedModel):
    """The roster carried inside a ``presence`` envelope."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    peers: list[PresencePeer] = Field(default_factory=list)


def is_valid_channel(value: str) -> bool:
    # ``fullmatch``: with ``match`` a ``$`` also accepts a trailing newline.
    return CHANNEL_RE.fullmatch(value) is not None


__all__ = [
    "CHANNEL_PATTERN",
    "CHANNEL_RE",
    "CRDT_DOC_TYPES",
    "DOC_TYPES",
    "ENVELOPE_KINDS",
    "MAX_CURSOR_CONTEXT_LENGTH",
    "MAX_DOC_ID_LENGTH",
    "MAX_OP_ID_LENGTH",
    "MAX_PEER_ID_LENGTH",
    "OP_INTENTS",
    "RESERVED_DOC_TYPES",
    "RESERVED_KINDS",
    "SERVER_PEER_ID",
    "AckPayload",
    "DocEnvelope",
    "DocType",
    "EnvelopeKind",
    "ErrorPayload",
    "FieldWrite",
    "HelloPayload",
    "OpIntent",
    "OpPayload",
    "PresenceCursor",
    "PresencePayload",
    "PresencePeer",
    "ReloadPayload",
    "SnapshotPayload",
    "is_valid_channel",
]
