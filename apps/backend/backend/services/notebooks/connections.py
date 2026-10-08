"""The connections a notebook's SQL cells may name, for the person reading it.

A notebook's kernel acts for the workspace that holds the notebook, and on the
box a cell's connection name resolves among the connections the box reads for
that workspace (``GET /workspaces/{id}/connections``): the workspace OWNER's
whole set, since sharing a workspace shares every connection its owner may
use. This module answers the same set, from the same builder, for a person who
can open the notebook, and says for each whether this reader can use it:

* a reader the notebook policy does not let run cells uses none of them (the
  policy's own sentence is the reason);
* a connection that is turned off is not found on the box;
* one whose credential the box would be refused (the builder's
  ``lease_refusal``: no grant from the owner, one that needs a new sign-in,
  a shared credential missing or unreadable) cannot run;
* two enabled connections under one name are ambiguous: the box cannot tell
  which one a cell means.

Every entry names the workspace owner: a statement runs on that person's
credentials whoever runs the cell.

A notebook in no workspace (a chat's own folder) resolves no connection on the
box, so it has none here.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Sequence
from typing import Final, Literal, Protocol

from alkera_core.connections.schemas import MemberTeamConnection
from alkera_core.connectors.catalog import get_descriptor
from alkera_core.files.objects_bridge import CHAT_TYPE, WORKSPACE_TYPE
from alkera_core.files.workspace_identity import workspace_folder_on, workspace_holding
from alkera_core.models import User, WorkspaceObject
from alkera_core.models.files.tree import FileNode
from alkera_core.notebooks.schemas import NotebookConnection, NotebookConnections
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.notebooks.callers import Target
from backend.services.notebooks.names import person_name

TURNED_OFF: Final = "This connection is turned off."
DUPLICATE_NAME: Final = "Another connection in this workspace has the same name."


class ConnectionFacts(Protocol):
    """What the deployment's connections say about one of them: the answers
    the open notebook code does not hold itself."""

    def engine_title(self, plugin: str) -> str:
        """The data system as people name it (``PostgreSQL``)."""
        ...


class CatalogConnectionFacts:
    """The answers the connector catalog holds: a plugin's title, or its name
    when the catalog has no such plugin."""

    def engine_title(self, plugin: str) -> str:
        try:
            return str(get_descriptor(plugin).title)
        except KeyError:
            return plugin


#: The facts the open notebook routes read.
CATALOG_FACTS: Final = CatalogConnectionFacts()


async def workspace_and_owner(
    db: AsyncSession, target: Target
) -> tuple[WorkspaceObject, User] | None:
    """The workspace holding the notebook and its owner, whose connections
    the notebook's kernel resolves; ``None`` for a notebook in no workspace."""
    return await _workspace_on(db, [*target.allowed.chain, target.node], target.org_id)


async def _workspace_on(
    db: AsyncSession, path: Sequence[FileNode], org_id: uuid.UUID
) -> tuple[WorkspaceObject, User] | None:
    workspace_id = workspace_holding(path)
    if workspace_id is None:
        return None
    workspace = await db.get(WorkspaceObject, workspace_id)
    if (
        workspace is None
        or workspace.type != WORKSPACE_TYPE
        or workspace.deleted_at != 0
        or workspace.org_team_id != org_id
    ):
        return None
    owner = await db.get(User, workspace.owner_user_id)
    if owner is None:
        return None
    return workspace, owner


async def holder_on(
    db: AsyncSession, path: Sequence[FileNode], org_id: uuid.UUID
) -> WorkspaceObject | None:
    """The object whose machine runs a notebook at the last node of ``path``
    (root first): the workspace whose folder holds it, or, outside every
    workspace folder, the chat whose own folder holds it. ``None`` where no
    kernel can run: in neither, in an ended workspace's folder, or under an
    object of another org. The node may be a notebook or the folder one would
    be created in."""
    if workspace_folder_on(path) is not None:
        held = await _workspace_on(db, path, org_id)
        return None if held is None else held[0]
    for node in reversed(path):
        if node.subtype != CHAT_TYPE or node.target_object_id is None:
            continue
        chat = await db.get(WorkspaceObject, uuid.UUID(str(node.target_object_id)))
        if (
            chat is None
            or chat.type != CHAT_TYPE
            or chat.deleted_at != 0
            or chat.org_team_id != org_id
        ):
            return None
        return chat
    return None


def _kind(row: MemberTeamConnection) -> Literal["team", "personal", "per_user"]:
    if row.owner_user_id is not None:
        return "personal"
    return "per_user" if row.auth_mode == "per_user" else "team"


def _refusal(row: MemberTeamConnection, duplicated: bool, run_refusal: str | None) -> str:
    """Why this reader cannot use ``row``; empty when they can."""
    if run_refusal:
        return run_refusal
    if not row.enabled:
        return TURNED_OFF
    if duplicated:
        return DUPLICATE_NAME
    return row.lease_refusal


def connection_choices(
    rows: Sequence[MemberTeamConnection],
    *,
    owner: User,
    run_refusal: str | None,
    facts: ConnectionFacts,
) -> NotebookConnections:
    """The workspace's connections as the notebook offers them, by name.

    ``rows`` is the set of ``owner``, the workspace's owner (what the box
    resolves names among), so every entry runs on that person's credentials
    and names them; ``run_refusal`` is the notebook policy's sentence when
    this reader may not run cells, ``None`` when they may; ``facts`` is the
    deployment's connections answering for each one."""
    credential_owner = person_name(owner)
    enabled_names = Counter(row.handle for row in rows if row.enabled)
    entries = []
    for row in sorted(rows, key=lambda r: (r.handle.casefold(), r.plugin, str(r.id))):
        reason = _refusal(row, enabled_names[row.handle] > 1, run_refusal)
        entries.append(
            NotebookConnection(
                id=str(row.id),
                name=row.handle,
                engine=row.plugin,
                engine_title=facts.engine_title(row.plugin),
                kind=_kind(row),
                team_name=row.team_name,
                credential_owner=credential_owner,
                can_use=not reason,
                reason=reason,
            )
        )
    return NotebookConnections(connections=entries)


__all__ = [
    "CATALOG_FACTS",
    "DUPLICATE_NAME",
    "TURNED_OFF",
    "CatalogConnectionFacts",
    "ConnectionFacts",
    "connection_choices",
    "workspace_and_owner",
]
