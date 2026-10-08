"""The chat workspace document: which tabs a reader has open beside a chat.

A chat page is a workspace — a chat beside a pane of tabs onto the chat's own
folder — and a reader who closes the page expects to find it as they left it.
That layout is persisted per chat and per reader, so it is versioned like every
other persisted shape here: a writer a year from now must still be readable by
today's server, and an older server must not drop a field a newer one wrote.

Two rules shape the document, and both are security rules rather than taste:

* **Ids only.** A tab names a node by its id; it never carries a URL, a token,
  a grant or bytes. A stored document is replayed by the reader's browser, so
  anything fetchable stored here would be a stored redirect. ``node_id`` is
  therefore refused unless it parses as a UUID — which refuses every URL,
  every relative path and every scheme.
* **Bounded.** The document is small and the route refuses an oversized one;
  ``MAX_WORKSPACE_TABS`` bounds how many tabs a document may carry and
  ``MAX_WORKSPACE_STATE_BYTES`` bounds the serialized size the server accepts.

Whether a stored ``kind`` is one this build knows is deliberately NOT checked:
a tab kind the reader's build has not learned yet is kept, not dropped, so a
newer client and an older one can share one document.
"""

from __future__ import annotations

from typing import Any, ClassVar, Final
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from alkera_core.versioning import VersionedModel

#: How many tabs one chat's workspace may hold. A reader who opens more is
#: hoarding, not working; the client closes the oldest rather than growing the
#: document without bound.
MAX_WORKSPACE_TABS: Final[int] = 32

#: The largest serialized document the server accepts for one chat. Small
#: enough that the row stays cheap to read on every chat open, and comfortably
#: above a full ``MAX_WORKSPACE_TABS`` document of real names and paths.
MAX_WORKSPACE_STATE_BYTES: Final[int] = 16 * 1024


class WorkspaceTab(VersionedModel):
    """One tab open beside a chat.

    ``kind`` routes the tab to whatever renders it (``file`` for a previewed
    node, ``files`` for the folder browser, and whatever a later build adds);
    ``params`` is the per-kind bag — the browser keeps ``{"folderId": "<uuid>"}``
    there. ``name`` and ``path`` are what the tab showed when it was saved, so
    a strip can be painted before the nodes are re-read; they are a label, not
    the truth, and the node is always re-resolved by ``node_id``.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    id: str = ""
    kind: str = "file"
    node_id: str | None = None
    name: str = ""
    path: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("node_id")
    @classmethod
    def _node_id_is_an_id(cls, value: str | None) -> str | None:
        """Refuse anything that is not a node id.

        The stored document is replayed by a browser, so a ``node_id`` that
        could be fetched — a URL, a scheme, a path — must never survive a
        write. Parsing as a UUID is the whole check: it admits an id and
        nothing else.
        """
        if value is None:
            return None
        try:
            UUID(value)
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("node_id must be a node id") from exc
        return value


class ChatWorkspaceState(VersionedModel):
    """The open tabs of one chat, for one reader.

    Read back on every chat open and replaced whole on every save, so the
    invariants a client relies on are checked here rather than at each call
    site: the ids are usable as React keys and as the target of a close, and
    ``active_tab_id`` always names a tab that is actually in the document.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    tabs: list[WorkspaceTab] = Field(default_factory=list)
    active_tab_id: str | None = None

    @model_validator(mode="after")
    def _tabs_are_addressable(self) -> ChatWorkspaceState:
        if len(self.tabs) > MAX_WORKSPACE_TABS:
            raise ValueError(f"a workspace holds at most {MAX_WORKSPACE_TABS} tabs")
        seen: set[str] = set()
        for tab in self.tabs:
            if not tab.id:
                raise ValueError("every tab needs an id")
            if tab.id in seen:
                raise ValueError(f"duplicate tab id {tab.id!r}")
            seen.add(tab.id)
        if self.active_tab_id is not None and self.active_tab_id not in seen:
            raise ValueError("active_tab_id must name one of the tabs")
        return self
