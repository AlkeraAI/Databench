"""Renamed realtime document types, in one table with no imports.

``legacy spelling -> the document type it names now``. The socket respells a
client's old spelling by it, envelopes written under an old name migrate by
it, and ``crdt_docs`` stores a renamed type under its legacy spelling (so a
deploy that migrates before it rolls never meets rows a running task cannot
read). It lives apart from the envelope schema so the ORM models can read it
without importing the realtime package.

The wire alias is removed at ``DocEnvelope`` 3.0.0, once no supported web or VS
Code build subscribes under it; renaming the stored value is an
expand-then-contract of its own.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Literal

LEGACY_DOC_TYPE_ALIASES: Final[Mapping[str, str]] = {"chat_workspace": "chat_draft"}

#: ``current doc type -> the doc_type its rows are stored under``, where the two
#: differ: a renamed type keeps its legacy spelling as its stored value.
STORED_DOC_TYPES: Final[Mapping[str, str]] = {
    current: legacy for legacy, current in LEGACY_DOC_TYPE_ALIASES.items()
}

#: ``current doc type -> the spelling replicas exchange it under``, while a
#: roll can put the previous build beside this one: an event one replica
#: writes is read by every other, and the previous build knows only the old
#: name. Written old, read back as current (:func:`respell_event`); emptied
#: when the wire alias goes.
REPLICA_SPELLING: Final[Mapping[str, str]] = STORED_DOC_TYPES

Direction = Literal["to_replicas", "from_replicas"]


def _table(direction: Direction) -> Mapping[str, str]:
    return REPLICA_SPELLING if direction == "to_replicas" else LEGACY_DOC_TYPE_ALIASES


def respell_channel(channel: str | None, direction: Direction) -> str | None:
    """``doc:<type>:<id>`` with a renamed type respelled for ``direction``;
    anything else unchanged."""
    if channel is None:
        return None
    for source, target in _table(direction).items():
        prefix = f"doc:{source}:"
        if channel.startswith(prefix):
            return f"doc:{target}:{channel[len(prefix) :]}"
    return channel


def respell_payload(payload: Mapping[str, Any], direction: Direction) -> dict[str, Any]:
    """An event payload with its envelope's document type respelled for
    ``direction``, as a new dict; a payload with no envelope is copied."""
    body = dict(payload)
    envelope = body.get("envelope")
    if isinstance(envelope, Mapping):
        doc_type = envelope.get("doc_type")
        target = _table(direction).get(doc_type) if isinstance(doc_type, str) else None
        if target is not None:
            body["envelope"] = {**envelope, "doc_type": target}
    return body


__all__ = [
    "LEGACY_DOC_TYPE_ALIASES",
    "REPLICA_SPELLING",
    "STORED_DOC_TYPES",
    "respell_channel",
    "respell_payload",
]
