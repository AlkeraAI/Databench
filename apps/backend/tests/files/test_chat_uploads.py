"""A file handed to a chat on the server lands where the web composer puts one.

Real chats through the real route, real Files underneath; what is asserted is
where the bytes are afterwards, under which name, and who was refused.
"""

from __future__ import annotations

import uuid

import pytest
from _files_kit import FilesOrgFixture
from alkera_core.authz.principal import ActingContext
from alkera_core.files import VersionId
from alkera_core.files.content import ContentService
from alkera_core.models.files.tree import FileNode
from backend.services.files.chat_paths import chat_path, resolve_chat_file
from backend.services.files.chat_uploads import (
    ChatUploadRefusedError,
    put_chat_upload,
)
from backend.services.files.context import build_files_context
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.usefixtures("files_on")

PNG = b"\x89PNG\r\n\x1a\n" + b"pasted" * 10


async def _chat(client: AsyncClient) -> uuid.UUID:
    response = await client.post("/api/v1/chats", json={"title": "Hand-over"})
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


def _as(user_id: uuid.UUID, org: FilesOrgFixture) -> ActingContext:
    return ActingContext.for_user(user_id=user_id, org_id=org.org.org_id, email="")


async def _read_back(
    session: AsyncSession, org: FilesOrgFixture, chat_id: uuid.UUID, path: str
) -> bytes | None:
    ctx = _as(org.org.admin_id, org)
    parsed = chat_path(path, chat_id=str(chat_id))
    assert parsed is not None
    found = await resolve_chat_file(
        session, ctx=ctx, org_team_id=org.org.org_id, chat_id=chat_id, path=parsed
    )
    if found is None:
        return None
    node = await session.get(FileNode, found.node_id)
    assert node is not None and node.head_version_id is not None
    files = await build_files_context(session, ctx)
    service = ContentService(files.repo, files.ctx, files.clock, files.store)
    return b"".join([c async for c in await service.open(VersionId(node.head_version_id))])


async def test_the_file_lands_in_the_working_folders_uploads_and_reads_back(
    files_client: AsyncClient, real_session: AsyncSession, files_org: FilesOrgFixture
) -> None:
    chat_id = await _chat(files_client)
    landed = await put_chat_upload(
        real_session,
        ctx=_as(files_org.org.admin_id, files_org),
        chat_id=chat_id,
        filename="q3 chart.png",
        content=PNG,
        mime_hint="image/png",
    )
    await real_session.commit()
    assert landed.path == "uploads/q3 chart.png"
    assert await _read_back(real_session, files_org, chat_id, "uploads/q3 chart.png") == PNG


async def test_a_second_file_of_the_same_name_never_overwrites_the_first(
    files_client: AsyncClient, real_session: AsyncSession, files_org: FilesOrgFixture
) -> None:
    chat_id = await _chat(files_client)
    ctx = _as(files_org.org.admin_id, files_org)
    first = await put_chat_upload(
        real_session, ctx=ctx, chat_id=chat_id, filename="data.csv", content=b"a\n1\n"
    )
    second = await put_chat_upload(
        real_session, ctx=ctx, chat_id=chat_id, filename="data.csv", content=b"a\n2\n"
    )
    await real_session.commit()
    assert first.path != second.path
    assert await _read_back(real_session, files_org, chat_id, first.path) == b"a\n1\n"
    assert await _read_back(real_session, files_org, chat_id, second.path) == b"a\n2\n"


async def test_a_name_with_a_slash_cannot_climb_out_of_uploads(
    files_client: AsyncClient, real_session: AsyncSession, files_org: FilesOrgFixture
) -> None:
    chat_id = await _chat(files_client)
    landed = await put_chat_upload(
        real_session,
        ctx=_as(files_org.org.admin_id, files_org),
        chat_id=chat_id,
        filename="../../secrets.txt",
        content=b"x",
    )
    await real_session.commit()
    assert landed.path.startswith("uploads/")
    assert "/" not in landed.name


async def test_someone_the_chat_is_not_shared_with_is_refused(
    files_client: AsyncClient, real_session: AsyncSession, files_org: FilesOrgFixture
) -> None:
    """The admin's chat is theirs: an org member who holds no grant on it is
    refused the write, and nothing lands."""
    chat_id = await _chat(files_client)
    with pytest.raises(ChatUploadRefusedError):
        await put_chat_upload(
            real_session,
            ctx=_as(files_org.member.id, files_org),
            chat_id=chat_id,
            filename="intruder.txt",
            content=b"x",
        )
    await real_session.commit()
    assert await _read_back(real_session, files_org, chat_id, "uploads/intruder.txt") is None


async def test_a_chat_with_no_folder_is_refused(
    real_session: AsyncSession, files_org: FilesOrgFixture
) -> None:
    with pytest.raises(ChatUploadRefusedError):
        await put_chat_upload(
            real_session,
            ctx=_as(files_org.org.admin_id, files_org),
            chat_id=uuid.uuid4(),
            filename="x.txt",
            content=b"x",
        )
