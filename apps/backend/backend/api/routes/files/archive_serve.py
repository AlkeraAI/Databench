"""Serving a download operation's archive, on the content domain.

This module exposes ``content_router`` rather than ``router``: the discovery
walk in this package mounts it on the content app, which carries no session
cookie and its own header recipe. That is the whole reason the token
is self-contained — there is no cookie here to identify anybody with.

A token is a claim, never a capability. Redemption rebuilds the caller's
context from the user the token names and **re-runs the authorization** before
a single byte is produced, so a grant revoked in the minute between minting and
clicking is refused. Everything that can go wrong — a forged signature, an
expired deadline, an operation from another org, a subtree the user may no
longer export — answers the same opaque 404.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from alkera_core.db.session import get_db
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.authorize import Authorized, authorize
from alkera_core.files.clock import SystemClock
from alkera_core.files.errors import FilesError, NotFound
from alkera_core.files.ids import NodeId
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps.files import require_files_enabled
from backend.api.routes.files.operations import access_facts, enforcer
from backend.content_app import disposition_header
from backend.services.files.archive import (
    actor_for,
    stream_zip64,
    subtree_archive,
    verify_token,
)
from backend.services.files.context import build_files_context
from backend.services.files.guards import refuse_a_copy_over_hidden_chats

content_router = APIRouter(tags=["files-content"])

#: The archive's media type, and the type its disposition is decided from: ZIP
#: is not on the inline allowlist, so the one helper answers ``attachment``.
ARCHIVE_MEDIA_TYPE: str = "application/zip"


# The path is relative to the content mount (``CONTENT_MOUNT_PATH``, ``/c``),
# which this app is already mounted at: spelling the mount again here would
# serve the archive at ``/c/c/archive/…`` while ``operations._archive_link``
# mints ``/c/archive/…``, and every link handed to a client would be a 404.
@content_router.get("/archive/{token}")
async def serve_archive(
    request: Request,
    token: str,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _enabled: Annotated[None, Depends(require_files_enabled)],
) -> StreamingResponse:
    """Stream the subtree the token names as one ZIP64 archive."""
    from alkera_core.config import settings

    clock = SystemClock()
    claim = verify_token(
        token, settings.effective_files_content_signing_key.encode(), now=clock.now()
    )
    if claim is None:
        raise NotFound()
    files = await build_files_context(db, actor_for(claim))
    enforce = enforcer(request, db)
    facts = await access_facts(request, db, files)
    try:
        async with files.repo.transaction():
            root = await authorize(
                files.ctx,
                files.repo,
                NodeId(claim.node_id),
                FilesAction.EXPORT,
                facts=facts,
                enforce=enforce,
            )

            async def decide(node_id: uuid.UUID, action: FilesAction) -> Authorized[Any]:
                return await authorize(
                    files.ctx, files.repo, NodeId(node_id), action, facts=facts, enforce=enforce
                )

            # Decided again at redemption, as the mint decided it: a
            # conversation the redeemer cannot open that appeared in the folder
            # since would otherwise be pruned and named in ``skipped.txt``.
            await refuse_a_copy_over_hidden_chats(
                files.repo,
                files.ctx,
                root.node,
                facts=facts,
                decide=decide,
                action=FilesAction.EXPORT,
            )
    except FilesError:
        raise NotFound() from None
    # The subtree is decided while it is written, a page at a time, so a
    # hundred-thousand-item folder costs one page of rows and not the tree —
    # and every member is decided against the access this redemption just
    # resolved, so a grant withdrawn since the link was minted is refused here
    # and not merely at the root.
    archive = subtree_archive(files, root, facts)
    # The rest of the A5 recipe is stamped by the content app's own middleware.
    # The disposition is the one header that depends on the response, and it
    # comes from the single helper so an archive is escaped exactly as a file's
    # own name is — one place to get RFC 6266 right, and one place to change.
    filename = f"{claim.operation_id}.zip".encode()
    return StreamingResponse(
        stream_zip64(archive),
        media_type=ARCHIVE_MEDIA_TYPE,
        headers={"Content-Disposition": disposition_header(filename, ARCHIVE_MEDIA_TYPE)},
    )


__all__ = ["ARCHIVE_MEDIA_TYPE", "content_router", "serve_archive"]
