"""A path an agent wrote in a chat -> the file in that chat's Files, and its page.

The agent writes references relative to the directory it runs in -- the chat's
working folder -- because that is what it sees: ``charts/revenue.png``,
``./plot.py``, and sometimes the absolute path on its box,
``/opt/alkera-work/.alkera/chats/<chat>/scratch/charts/revenue.png``. None of
those mean anything to a reader outside the web transcript (a Slack thread, an
email, a notification), so every surface that shows agent output elsewhere
asks this module, and only this module, what such a reference names.

The rule mirrors the web transcript's (``chatPaths.ts`` + ``chatFiles.ts``):

* A reference is a path INSIDE the chat's folder or it is nothing. A URL of any
  scheme, a ``..`` step, a backslash, a query or fragment, an absolute path
  that is not this chat's folder on a box -- all answer ``None``, and a caller
  shows the words as plain text. A reference can therefore never name another
  chat's file or another tenant's: the walk starts at this chat's node, inside
  a repo scoped to the chat's org, and a name is only ever a step down.
* A relative path is looked for under the working folder first (where the agent
  wrote it), then under the chat folder itself (where the box's absolute path
  is rooted, and where the web's walk also starts).

Two halves, so a caller can test the interesting one without a database:
:func:`chat_path` is pure string logic, shared with the box in
:mod:`alkera_core.chat_paths`; :func:`resolve_chat_file` walks Files.
:func:`file_web_url` is the web page a located file opens at.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from alkera_core.authz.principal import ActingContext
from alkera_core.chat_paths import CHAT_IMAGE_EXTENSIONS, Anchor, ChatPath, chat_path
from alkera_core.config import settings
from alkera_core.files.clock import SystemClock
from alkera_core.files.ids import DriveId, NodeId, OrgScope
from alkera_core.files.namespace import Namespace
from alkera_core.files.objects_bridge import live_node_for, working_folder_node
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.tree import FileNode
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class ChatFile:
    """A file a chat reference resolved to, in the chat's own Files."""

    node_id: uuid.UUID
    drive_id: uuid.UUID
    name: str
    path: ChatPath

    @property
    def is_image(self) -> bool:
        return self.path.is_image

    @property
    def web_url(self) -> str:
        return file_web_url(self.node_id)


def file_web_url(node_id: object, *, base_url: str | None = None) -> str:
    """The web app's page for one Files node -- ``/files/<node>``, the address
    the Files pane and every "open in Alkera" link use. It is the authenticated
    page, never a content URL: opening it still takes the reader's sign-in and
    their own grant on the file."""
    base = (base_url if base_url is not None else settings.frontend_base_url).rstrip("/")
    return f"{base}/files/{node_id}"


async def resolve_chat_file(
    db: AsyncSession,
    *,
    ctx: ActingContext,
    org_team_id: uuid.UUID,
    chat_id: uuid.UUID,
    path: ChatPath,
) -> ChatFile | None:
    """The FILE ``path`` names in this chat's Files, or ``None``.

    ``None`` for a chat with no folder, a path that walks to nothing, to a
    folder, or to a trashed node. The repo is scoped to the chat's org, and the
    walk starts at the chat's own node, so nothing outside that chat can come
    back. Whether a particular reader may OPEN the file is not decided here:
    the page the link leads to decides that, per reader, and a surface that
    moves bytes (a Slack upload) runs its own authorized read.
    """
    repo = FilesRepo.joined(db, OrgScope(org_team_id=org_team_id))
    async with repo.transaction():
        chat_node = await live_node_for(repo, chat_id)
        if chat_node is None:
            return None
        starts: list[FileNode] = []
        if path.anchor == "working":
            sandbox = await working_folder_node(repo, chat_node)
            if sandbox is not None and sandbox.id != chat_node.id:
                starts.append(sandbox)
        starts.append(chat_node)
        namespace = Namespace(repo, ctx, SystemClock())
        for start in starts:
            node = await namespace.resolve_path(
                DriveId(chat_node.drive_id), path.path, below=NodeId(start.id)
            )
            if node is None or node.trashed_at is not None or node.kind != "file":
                continue
            return ChatFile(
                node_id=node.id,
                drive_id=node.drive_id,
                name=node.name.decode("utf-8", "replace"),
                path=path,
            )
    return None


__all__ = [
    "CHAT_IMAGE_EXTENSIONS",
    "Anchor",
    "ChatFile",
    "ChatPath",
    "chat_path",
    "file_web_url",
    "resolve_chat_file",
]
