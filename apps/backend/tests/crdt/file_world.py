"""A file in a chat's working folder, for the tests of co-edited files: a
chat shared at every rung (``make_world``), a text file put in its working
folder the way a person hands a chat a file, and the drive read and written
the way any other Files caller does it."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from alkera_core.authz import ActingContext
from alkera_core.files import NodeId, VersionId
from alkera_core.files.content import ContentService
from alkera_core.models import WorkspaceObject
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion
from backend.services.crdt.registry import DocRef
from backend.services.files.chat_uploads import put_chat_upload
from backend.services.files.context import build_files_context
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin
from tests.crdt.crdt_world import Person, World, make_world

SOURCE = "def greet(name):\n    return 'hello ' + name\n"


@dataclass
class FileWorld:
    world: World
    ref: DocRef
    node_id: uuid.UUID


def acting(who: Person) -> ActingContext:
    return ActingContext.for_user(
        user_id=who.user.id, org_id=who.user.home_org_team_id, email=who.user.email
    )


async def file_world(
    db: AsyncSession,
    org: OrgWithAdmin,
    *,
    content: bytes = SOURCE.encode(),
    name: str = "greet.py",
    workspace: WorkspaceObject | None = None,
) -> FileWorld:
    """A chat shared at every rung, and a file in its working folder put
    there the way a person hands a chat a file. The chat is started in
    ``workspace`` when one is named."""
    world = await make_world(db, org, workspace=workspace)
    landed = await put_chat_upload(
        db,
        ctx=acting(world.owner),
        chat_id=uuid.UUID(world.ref.doc_id),
        filename=name,
        content=content,
    )
    await db.commit()
    return FileWorld(
        world=world,
        ref=DocRef(org_id=world.org_id, doc_type="file", doc_id=str(landed.node_id)),
        node_id=landed.node_id,
    )


async def node_of(db: AsyncSession, node_id: uuid.UUID) -> FileNode:
    node = await db.get(FileNode, node_id)
    assert node is not None
    await db.refresh(node)
    return node


async def drive_text(db: AsyncSession, fw: FileWorld) -> str:
    """The file's head version as the drive serves it."""
    node = await node_of(db, fw.node_id)
    assert node.head_version_id is not None
    ctx = acting(fw.world.owner)
    context = await build_files_context(db, ctx)
    service = ContentService(context.repo, context.ctx, context.clock, context.store)
    data = b"".join([c async for c in await service.open(VersionId(node.head_version_id))])
    return data.decode("utf-8")


async def head_version(db: AsyncSession, fw: FileWorld) -> FileVersion:
    node = await node_of(db, fw.node_id)
    version = await db.get(FileVersion, node.head_version_id)
    assert version is not None
    return version


async def outside_write(db: AsyncSession, fw: FileWorld, data: bytes) -> None:
    """A change to the file that is not the session's: an upload over it."""

    async def body() -> AsyncIterator[bytes]:
        yield data

    node = await node_of(db, fw.node_id)
    context = await build_files_context(db, acting(fw.world.owner))
    service = ContentService(context.repo, context.ctx, context.clock, context.store)
    await service.put_version(
        NodeId(fw.node_id), body(), size_declared=len(data), if_match=node.etag
    )
    await db.commit()


__all__ = [
    "SOURCE",
    "FileWorld",
    "acting",
    "drive_text",
    "file_world",
    "head_version",
    "node_of",
    "outside_write",
]
