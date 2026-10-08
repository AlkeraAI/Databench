"""A file a person hands a chat, written where the chat's agent will find it.

The web composer's upload does this in the browser: it stages the file in the
chat's working folder under ``uploads/`` and the message then names it by that
path (``![Image 1](uploads/paste-1-ab12.png)``, ``[File 1: q.csv](uploads/…)``).
A surface that receives bytes on the server -- a Slack message with a file on
it -- has no browser to do that, so it calls :func:`put_chat_upload`, which
takes the same decisions the web's path takes and lands the file in the same
place:

* the write is aimed AT the chat and lands where a write aimed at the chat
  lands (``objects_bridge.drop_target_for`` -- the working folder, minted if
  the chat predates it), under ``uploads/``, the folder the agent is told to
  look in;
* it is authorized as the person, ``WRITE`` on that folder, through the same
  Files policy a route runs, and filed on the record;
* the bytes go through ``ContentService.put_version`` with the org's and the
  person's storage ceilings, so a file too big for the deployment or past a
  quota is refused exactly as an upload is;
* a chat whose box holds its folder takes the write as an inbound admission,
  the way it takes a file dropped on it in the browser.

It runs in the caller's transaction (a joined repo), so a chat opened in the
same unit of work already has the folder the file goes into.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from alkera_core.authz.principal import ActingContext
from alkera_core.config import settings
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.clock import SystemClock
from alkera_core.files.content import ContentService
from alkera_core.files.errors import Conflict
from alkera_core.files.ids import DomainId, DriveId, NodeId
from alkera_core.files.namespace import Namespace
from alkera_core.files.objects_bridge import drop_target_for, live_node_for
from alkera_core.models.files.tree import FileNode
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services import org as org_services
from backend.services.files.decided import authorized_node
from backend.services.files.store import store_factory
from backend.services.org import teams as team_service

#: The folder under the chat's working folder every hand-over lands in -- the
#: web's ``UPLOADS_FOLDER``, which the agent's brief names.
UPLOADS_FOLDER = "uploads"

#: What the decision row records instead of an HTTP verb.
UPLOAD_METHOD = "CHAT_UPLOAD"


class ChatUploadRefusedError(RuntimeError):
    """The file could not be written into the chat: no folder, no grant."""


@dataclass(frozen=True, slots=True)
class ChatUpload:
    """Where the file landed: ``path`` is what a message names it by, relative
    to the chat's working folder, exactly as the web composer's paths are."""

    path: str
    name: str
    node_id: uuid.UUID


async def put_chat_upload(
    db: AsyncSession,
    *,
    ctx: ActingContext,
    chat_id: uuid.UUID,
    filename: str,
    content: bytes,
    mime_hint: str | None = None,
) -> ChatUpload:
    """Write ``content`` into the chat's ``uploads/`` as ``filename``.

    An existing file of the same name is kept and the new one takes the next
    free conflict name, so a re-sent attachment never overwrites what the agent
    may already have read. Raises :class:`ChatUploadRefusedError` when the chat
    has no folder or the person may not write into it, and lets a Files
    refusal of the bytes (too large, over quota) propagate for the caller to
    report.
    """
    name = filename.replace("/", "_").strip() or "file"
    async with team_service.files_transaction(db, ctx) as repo:
        chat_node = await live_node_for(repo, chat_id)
        if chat_node is None:
            raise ChatUploadRefusedError("the chat has no folder")
        namespace = Namespace(repo, ctx, SystemClock())
        working = await drop_target_for(repo, ctx, namespace, chat_node)
        drive_id = DriveId(working.drive_id)
        drive = await repo.drive(drive_id)
        if drive is None:
            raise ChatUploadRefusedError("the chat's drive is gone")
    allowed = await authorized_node(
        db,
        repo,
        ctx,
        NodeId(working.id),
        FilesAction.WRITE,
        method=UPLOAD_METHOD,
        path=f"/chats/{chat_id}/{UPLOADS_FOLDER}",
    )
    if allowed is None:
        raise ChatUploadRefusedError("the chat's folder is not writable by this person")
    ceilings = org_services.ceilings_resolver(
        org_id=uuid.UUID(str(drive.org_team_id)), user_id=ctx.effective_user_id, drive=drive
    )
    store = await store_factory(settings).for_domain(DomainId(drive.dedup_domain_id))
    service = ContentService(repo, ctx, SystemClock(), store, ceilings=ceilings)
    namespace = Namespace(repo, ctx, SystemClock(), ceilings=ceilings)

    async def make() -> FileNode:
        folder = await namespace.resolve_path(
            drive_id, UPLOADS_FOLDER, drive=drive, below=NodeId(working.id)
        )
        if folder is None:
            try:
                folder = await namespace.create(
                    drive_id, NodeId(working.id), "folder", UPLOADS_FOLDER.encode()
                )
            except Conflict:
                folder = await namespace.resolve_path(
                    drive_id, UPLOADS_FOLDER, drive=drive, below=NodeId(working.id)
                )
        if folder is None or folder.kind != "folder":
            raise ChatUploadRefusedError("the chat's uploads folder could not be made")
        return await namespace.create(
            drive_id, NodeId(folder.id), "file", name.encode("utf-8"), conflict="rename"
        )

    node, _ = await service.put_first_version(drive_id, make, content, mime_hint=mime_hint)
    landed = node.name.decode("utf-8", "replace")
    return ChatUpload(path=f"{UPLOADS_FOLDER}/{landed}", name=landed, node_id=node.id)


__all__ = [
    "UPLOADS_FOLDER",
    "ChatUpload",
    "ChatUploadRefusedError",
    "put_chat_upload",
]
