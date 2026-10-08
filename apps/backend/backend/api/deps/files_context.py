"""The route dependency that builds a ``FilesContext``, cached on the request."""

from __future__ import annotations

import uuid
from typing import Annotated

from alkera_core.db.cross_tenant import cross_tenant_write
from alkera_core.db.session import get_db
from alkera_core.files.ids import NodeId
from alkera_core.files.repo import FilesRepo
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.dependencies import CurrentPrincipal
from backend.services.files.context import FilesContext, build_files_context


async def files_context(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    principal: CurrentPrincipal,
) -> FilesContext:
    """The route dependency. Cached on ``request.state`` so two dependencies on
    one route (an item route that also takes the lease context) do not ensure
    the drive twice."""
    cached: FilesContext | None = getattr(request.state, "files_context", None)
    if cached is not None:
        return cached
    drive_id: uuid.UUID | None = None
    if principal.is_machine:
        # Which org the box's request is decided in is read across every org
        # the drive could be in; whether the box serves it is decided next.
        async with cross_tenant_write(db, reason="files.machine.drive_named"):
            drive_id = await _drive_named(request, db)
    built = await build_files_context(
        db,
        principal,
        resolve_ceilings=request.method not in ("GET", "HEAD", "OPTIONS"),
        drive_id=drive_id,
    )
    request.state.files_context = built
    return built


#: The one Files family whose routes name no drive in their path: an upload
#: session is opened against a parent NODE and then addressed by its own id.
UPLOADS_PATH = "/api/v1/files/uploads"


#: The drive route itself names no drive either: a person's is their org's,
#: and a box — which has none — names the CHAT whose drive it asks for.
DRIVES_PATH = "/api/v1/files/drives"


#: The query parameter a box names that chat with on the drive route.
CHAT_QUERY = "chatId"


async def _drive_named(request: Request, db: AsyncSession) -> uuid.UUID | None:
    """The drive a box's request addresses, or ``None`` when it names none.

    A box has no drive of its own, so its scope is whatever drive the request
    NAMES: the ``drive_id`` in the path for every family that carries one; on
    the drive route, the drive of the folder of the chat it names; for an
    upload, the drive of the parent node the session is opened against, or
    of the session it continues. Read platform-wide and unverified — this only
    chooses which org the request is decided in; whether the box may act
    there is the context's ``serves`` check and the policy's, both still to
    come. A malformed id is no drive, and a box that named one is refused as
    a box that named none. Read for a machine principal only; a person's
    drive is their credential's.
    """
    named = _uuid_or_none(request.path_params.get("drive_id"))
    if named is not None:
        return named
    path = request.url.path.rstrip("/")
    if path == DRIVES_PATH:
        chat = _uuid_or_none(request.query_params.get(CHAT_QUERY))
        if chat is None:
            return None
        folder = await FilesRepo.chat_folder_anywhere(db, chat)
        return None if folder is None else uuid.UUID(str(folder.drive_id))
    if not path.startswith(UPLOADS_PATH):
        return None
    session_id = _uuid_or_none(request.path_params.get("session_id"))
    if session_id is not None:
        found = await FilesRepo.drive_of_upload_anywhere(db, session_id)
        return None if found is None else uuid.UUID(str(found))
    if request.method != "POST" or path != UPLOADS_PATH:
        return None
    try:
        body = await request.json()
    except ValueError:
        return None
    parent = _uuid_or_none(body.get("parentId") if isinstance(body, dict) else None)
    if parent is None:
        return None
    found = await FilesRepo.drive_of_node_anywhere(db, NodeId(parent))
    return None if found is None else uuid.UUID(str(found))


def _uuid_or_none(raw: object) -> uuid.UUID | None:
    if not isinstance(raw, str):
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


FilesCtx = Annotated[FilesContext, Depends(files_context)]
