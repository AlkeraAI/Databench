"""Old spellings of a document type, answered in the spelling the client used.

A web or VS Code build from before a rename (``chat_workspace`` became
``chat_draft``) still subscribes, sends envelopes and joins presence under the
old name. Inside the socket every channel is its current spelling; only what
this one client sends and receives is respelled. So an old tab keeps working
through a deprecation window, and it edits the same stored document a current
tab does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from alkera_core.schemas.realtime import (
    DocFrame,
    UnsubscribeFrame,
    canonical_channel,
    canonical_doc_type,
    legacy_channel,
    legacy_doc_type,
)


@dataclass
class Respeller:
    """One socket's memory of the channels its client spells the old way."""

    #: The current keys of the channels this client named with an old type.
    legacy: set[str] = field(default_factory=set)

    def inbound(self, frame: Any) -> Any:
        """``frame`` from the client, with an old document type respelled as
        the current one. A channel named the old way is remembered until the
        client unsubscribes from it."""
        if isinstance(frame, DocFrame):
            current = canonical_doc_type(frame.envelope.doc_type)
            if current == frame.envelope.doc_type:
                return frame
            envelope = frame.envelope.model_copy(update={"doc_type": current})
            self.legacy.add(envelope.channel)
            return frame.model_copy(update={"envelope": envelope})
        raw = getattr(frame, "channel", None)
        if not isinstance(raw, str):
            return frame
        current_key, was_legacy = canonical_channel(raw)
        if isinstance(frame, UnsubscribeFrame):
            self.legacy.discard(current_key)
        elif was_legacy:
            self.legacy.add(current_key)
        return frame.model_copy(update={"channel": current_key}) if was_legacy else frame

    def outbound(self, frame: Any) -> Any:
        """``frame`` for the client, with each channel it spells the old way
        respelled the way it said it."""
        if not self.legacy:
            return frame
        if isinstance(frame, DocFrame):
            if frame.envelope.channel not in self.legacy:
                return frame
            envelope = frame.envelope.model_copy(
                update={"doc_type": legacy_doc_type(frame.envelope.doc_type)}
            )
            return frame.model_copy(update={"envelope": envelope})
        raw = getattr(frame, "channel", None)
        if isinstance(raw, str) and raw in self.legacy:
            return frame.model_copy(update={"channel": legacy_channel(raw)})
        return frame


__all__ = ["Respeller"]
