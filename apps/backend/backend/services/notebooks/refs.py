"""Names a notebook goes by on the backend."""

from __future__ import annotations

from typing import Final
from uuid import UUID

from backend.services.crdt import DocRef

#: The CRDT document type of a notebook's live document.
NOTEBOOK_DOC_TYPE: Final = "notebook"


def doc_ref(org_id: UUID, item_id: UUID) -> DocRef:
    """The notebook's live document: ``doc:notebook:<item_id>`` in its org."""
    return DocRef(org_id, NOTEBOOK_DOC_TYPE, str(item_id))


__all__ = [
    "NOTEBOOK_DOC_TYPE",
    "doc_ref",
]
