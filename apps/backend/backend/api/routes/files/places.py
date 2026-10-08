"""Where the client finds the folders a member's own things are filed in.

A chat, a template — anything whose node is a folder — is filed in a named
folder directly under the caller's home. The name is the server's
(``Chats``, ``Chat Templates``), the node id is the only thing a client may
hold, and the client must never derive one from a name: a member may rename or
move the folder, and a stranger may have put a folder of that name under the
container. So the ids are answered here.

Two shapes, one route. The bare read is what a page draws with: it answers
``null`` for a place nobody has needed yet, so opening Files never creates a
folder the member did not ask for. ``?ensure=`` is what a surface about to file
something there sends: it makes the named places on the way and answers their
ids, which is one round trip instead of "read, notice the null, create, read
again" — and it decides as the write it is.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Final

from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.authorize import authorize
from alkera_core.files.errors import InvalidRequest, NotFound
from alkera_core.files.ids import NodeId
from alkera_core.files.namespace import Namespace
from alkera_core.files.objects_bridge import (
    CHAT_TEMPLATE_TYPE,
    CHAT_TYPE,
    FOLDER_OBJECT_KINDS,
    chat_templates_folder,
    chats_folder,
)
from alkera_core.files.quota import CeilingsResolver
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.tree import FileNode
from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from backend.api.deps.files import (
    as_platform,
    files_enforcer,
    platform_wrap,
    ratelimited,
)
from backend.api.deps.files_context import FilesCtx
from backend.api.deps.files_facts import facts_for
from backend.api.params import PathId
from backend.services import org as org_services
from backend.services.files.context import FilesContext
from backend.services.files.home import caller_home

router = APIRouter()

#: The wire name of each place, and the object type whose folder is filed
#: there. The *name* of the folder is never spelled here — it is read from the
#: type's registration, so a place cannot drift from the folder a newly created
#: object of that type actually lands in. A third kind is a line here plus its
#: field on :class:`PlacesRead`.
PLACE_TYPES: Final[dict[str, str]] = {
    "chats": CHAT_TYPE,
    "chatTemplates": CHAT_TEMPLATE_TYPE,
}

#: Make one place under a home, idempotently: the shape of the library call
#: that creates it and, having lost the race for the name to a concurrent first
#: object of the same type, reads back the winner's folder instead of failing.
_PlaceMaker = Callable[[FilesRepo, Namespace, FileNode], Awaitable[FileNode]]

#: How each place is *made*. The registration above says where a folder of that
#: type is filed; these are the calls that put it there.
_PLACE_MAKERS: Final[dict[str, _PlaceMaker]] = {
    "chats": chats_folder,
    "chatTemplates": chat_templates_folder,
}


class PlacesRead(BaseModel):
    """The caller's own home and the named folders inside it.

    Every field is nullable because every one of them is answered by a *read*
    when ``ensure`` did not name it: a member who has never saved a template
    has no ``Chat Templates`` folder, and inventing one on a page load would
    put a folder in their drive that they did not make.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    home_id: str | None = None
    chats_id: str | None = None
    chat_templates_id: str | None = None


@router.get(
    "/drives/{drive_id}/places",
    response_model=PlacesRead,
    dependencies=[Depends(ratelimited("items"))],
)
async def get_places(
    request: Request,
    files: FilesCtx,
    background: BackgroundTasks,
    drive_id: PathId,
    ensure: Annotated[str | None, Query()] = None,
) -> PlacesRead:
    """The caller's places, optionally made on the way.

    ``ensure`` is a comma-separated list of the names in :data:`PLACE_TYPES`;
    a name that is not one of them is refused rather than ignored, so a client
    that misspells a place learns it instead of quietly getting a ``null``
    forever.

    The drive is checked before anything is read or made, and by raising the
    same absence a missing home does: naming another org's drive must not be a
    way to learn that the drive is real.

    One decision covers the whole answer, on the caller's own home, because
    that is the node every place is a child of — ``read`` for the bare read,
    ``write`` for an ``ensure``, which is what putting a folder in somebody's
    home is. A principal with no home (a CI or a proxy token) has nowhere for a
    place to be, so it is the same absence rather than an empty document that
    would read as "you have none yet".
    """
    wanted = _wanted(ensure)
    if _uuid_or_none(drive_id) != files.drive.id:
        raise NotFound()
    home = await caller_home(files, background=background)
    if home is None:
        raise NotFound()

    async with files.repo.transaction():
        async with as_platform(files.repo.session):
            facts = await facts_for(request, files.repo.session, files.ctx, files.drive)
        session = files.repo.session
        decided = await authorize(
            files.ctx,
            files.repo,
            NodeId(home.id),
            FilesAction.WRITE if wanted else FilesAction.READ,
            facts=facts,
            enforce=files_enforcer(request, session, wrap=platform_wrap(session)),
        )
        home = decided.node
        namespace = Namespace(
            files.repo, files.ctx, files.clock, files.store, ceilings=_ceilings(files, wanted)
        )
        found: dict[str, FileNode | None] = {}
        for place, object_type in PLACE_TYPES.items():
            existing = await _place(files.repo, home, object_type)
            if existing is None and place in wanted:
                existing = await _PLACE_MAKERS[place](files.repo, namespace, home)
            found[place] = existing

    return PlacesRead(
        home_id=str(home.id),
        chats_id=_id(found["chats"]),
        chat_templates_id=_id(found["chatTemplates"]),
    )


def _wanted(ensure: str | None) -> frozenset[str]:
    """The places ``ensure`` names, refusing anything that is not one."""
    if ensure is None:
        return frozenset()
    names = [part.strip() for part in ensure.split(",") if part.strip()]
    unknown = sorted(set(names) - set(PLACE_TYPES))
    if unknown:
        raise InvalidRequest(
            "files.unknown_place",
            f"There is no such place: {', '.join(unknown)}. "
            f"The places are {', '.join(PLACE_TYPES)}.",
        )
    return frozenset(names)


def _uuid_or_none(raw: str) -> uuid.UUID | None:
    """``raw`` as a drive id, or ``None`` for anything that cannot be one.

    Unparseable and simply-not-yours both come back as the one absence: a
    caller who learns that a malformed id is refused *differently* has learned
    that the well-formed one they sent got further.
    """
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def _ceilings(files: FilesContext, wanted: frozenset[str]) -> CeilingsResolver | None:
    """The limits a place-making write is checked against.

    Resolved here rather than taken from the context because this is a ``GET``,
    and the context only resolves ceilings for a method that is a write by its
    verb. Without it a member sitting at their own node ceiling could still put
    folders in their home by asking for them a place at a time.
    """
    user_id = files.ctx.effective_user_id
    if not wanted or user_id is None:
        return None
    return org_services.ceilings_resolver(
        org_id=files.drive.org_team_id, user_id=user_id, drive=files.drive
    )


async def _place(repo: FilesRepo, home: FileNode, object_type: str) -> FileNode | None:
    """The live folder a ``object_type`` object is filed in, without making it.

    Byte-exact and folders only, the same identity test the library applies
    when it files a new object: a *file* called ``Chats`` is not a place a chat
    can go, and ``chats`` is a different name the member chose. Found only
    directly under the home, so a place the owner moved elsewhere is not
    followed — reporting it would make the move look undone.
    """
    name = FOLDER_OBJECT_KINDS[object_type].home_folder_name
    if name is None:  # pragma: no cover - every place-having kind names a folder
        return None
    for child in await repo.siblings(NodeId(home.id)):
        if child.kind == "folder" and bytes(child.name) == name:
            return child
    return None


def _id(node: FileNode | None) -> str | None:
    return str(node.id) if node is not None else None


__all__ = ["PLACE_TYPES", "PlacesRead", "router"]
