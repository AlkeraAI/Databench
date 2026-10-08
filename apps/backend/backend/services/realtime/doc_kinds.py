"""Live document kinds a domain serves over the realtime socket.

The socket serves chats itself, and the CRDT kinds through the Loro lane. Any
other document type the wire names is served only when a domain registers it
here, with both halves of what serving it takes: how a person's socket is
granted the channel, and the strategy that applies, seeds and persists its
operations. The knowledge base registers ``artifact`` this way. With nothing
registered, a channel of such a type is refused ``not_found``, the answer any
channel the server cannot find gets.

A registration is read the first time a socket asks for a kind, which freezes
the point, so every kind is registered during composition.
"""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol
from uuid import UUID

from alkera_core.extensions import ExtensionError, ExtensionPoint
from alkera_core.schemas.realtime import CRDT_DOC_TYPES, DOC_TYPES, LEGACY_DOC_TYPE_ALIASES

if TYPE_CHECKING:
    from alkera_core.models import User
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.services.realtime.channels import Channel, ChannelGrant
    from backend.services.realtime.docsync import DocStrategy


class DocKindAuthorizer(Protocol):
    """A person's grant on a channel of the kind, or ``ChannelError``.

    ``org_id`` is the connection's org (its credential's), and a document in
    any other org is refused ``not_found``. Called only for a person's socket:
    a box on its own machine credential holds chats and nothing else."""

    def __call__(
        self, db: AsyncSession, user: User, channel: Channel, *, org_id: UUID
    ) -> Awaitable[ChannelGrant]: ...


@dataclass(frozen=True, slots=True)
class RealtimeDocKind:
    """One document type a domain serves: who is granted its channel, and how
    its operations are applied."""

    doc_type: str
    authorize: DocKindAuthorizer
    strategy: DocStrategy


#: The types the socket serves without a registration: a chat, the legacy
#: spellings of a chat's documents an older client still names, and the CRDT
#: documents. None may be registered.
NATIVE_DOC_TYPES: Final[frozenset[str]] = (
    frozenset({"chat", *LEGACY_DOC_TYPE_ALIASES}) | CRDT_DOC_TYPES
)

#: The document kinds domains register, in registration order.
REALTIME_DOC_KINDS: ExtensionPoint[RealtimeDocKind] = ExtensionPoint("realtime_doc_kinds")


def registered_doc_kinds(
    point: ExtensionPoint[RealtimeDocKind] = REALTIME_DOC_KINDS,
) -> dict[str, RealtimeDocKind]:
    """Every kind registered on ``point`` by its document type. Refuses a type
    the wire cannot name, a type the socket serves itself, and a type
    registered twice, so a registration that could never be reached or that
    would shadow another is an error at the first read rather than a silent
    no-op."""
    kinds: dict[str, RealtimeDocKind] = {}
    for kind in point.items():
        if kind.doc_type not in DOC_TYPES:
            raise ExtensionError(f"no channel can name a {kind.doc_type!r} document")
        if kind.doc_type in NATIVE_DOC_TYPES:
            raise ExtensionError(f"{kind.doc_type!r} documents are served by the socket itself")
        if kind.doc_type in kinds:
            raise ExtensionError(f"{kind.doc_type!r} documents are registered twice")
        kinds[kind.doc_type] = kind
    return kinds


def doc_kind(doc_type: str) -> RealtimeDocKind | None:
    """The kind registered for ``doc_type``, or ``None``."""
    return registered_doc_kinds().get(doc_type)


def registered_strategy(doc_type: str) -> DocStrategy | None:
    """The strategy of the kind registered for ``doc_type``, or ``None``."""
    kind = doc_kind(doc_type)
    return None if kind is None else kind.strategy


__all__ = [
    "NATIVE_DOC_TYPES",
    "REALTIME_DOC_KINDS",
    "DocKindAuthorizer",
    "RealtimeDocKind",
    "doc_kind",
    "registered_doc_kinds",
    "registered_strategy",
]
