"""What a row-backed object looks like when it lands on a real filesystem.

A pointer is derived and signed: the server can always tell its own bytes from
bytes a user edited, which is what makes "``push`` warns and skips" safe.
"""

from __future__ import annotations

import json
import uuid

import pytest
from alkera_core.files.errors import InvalidRequest
from alkera_core.files.providers.pointer import POINTER_MIME, PointerProvider
from alkera_core.files.providers.registry import RendererRegistry
from alkera_core.models.files.tree import FileNode
from alkera_core.schemas.files.pointer import (
    POINTER_EXTENSIONS,
    InvalidPointer,
    PointerFile,
    verify,
)
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import FilesFactory

KEY = "the-deployment-pointer-key"


@pytest.fixture
def provider() -> PointerProvider:
    renderers = RendererRegistry()
    return PointerProvider(key=KEY, web_base_url="https://app.example.test/", renderers=renderers)


@pytest.fixture
async def nodes(files_session: AsyncSession, files_factory: FilesFactory) -> dict[str, FileNode]:
    """One drive holding the two shapes every test here contrasts: a row-backed
    object node and an ordinary file."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("weekly.alkerachat notes.txt", drive=drive)
    node = tree["weekly.alkerachat"]
    node.kind = "object"
    node.target_object_id = uuid.uuid4()
    await files_session.commit()
    return tree


@pytest.fixture
def object_node(nodes: dict[str, FileNode]) -> FileNode:
    return nodes["weekly.alkerachat"]


@pytest.fixture
def plain_node(nodes: dict[str, FileNode]) -> FileNode:
    return nodes["notes.txt"]


async def read(provider: PointerProvider, node: FileNode) -> str:
    return b"".join([chunk async for chunk in await provider.open(node, None)]).decode()


# ---- the document ----------------------------------------------------------


async def test_a_pointer_carries_every_field_the_desktop_needs_and_verifies(
    provider: PointerProvider, object_node: FileNode
) -> None:
    text = await read(provider, object_node)
    pointer = verify(text, key=KEY)
    assert pointer.kind == "chat"
    assert pointer.node_id == object_node.id
    # The route the SPA actually registers (`/chat/:chatId`), not a pluralised
    # guess — the same path the wire facet carries, so the pointer on a laptop
    # and the item in the drive open the same page.
    assert pointer.web_url == f"https://app.example.test/chat/{object_node.target_object_id}"
    assert pointer.app_url == f"alkera://chat/{object_node.target_object_id}"
    assert pointer.rendered_mime == "application/json"
    assert pointer.schema_version == PointerFile.SCHEMA_VERSION
    assert json.loads(text)["schema_version"] == PointerFile.SCHEMA_VERSION


async def test_head_describes_the_pointer_as_a_pointer_not_as_the_object(
    provider: PointerProvider, object_node: FileNode
) -> None:
    info = await provider.head(object_node, None)
    assert info.mime == POINTER_MIME
    assert info.size == len(await read(provider, object_node))


async def test_rendered_mime_is_the_registered_renderer_s(object_node: FileNode) -> None:
    """The pointer says what the *object* renders as, so a client can preview it."""

    class _Renderer:
        object_type = "chat"
        extension = ".alkerachat"
        mime = "text/markdown"

        async def render(self, session: object, obj: object, version: object) -> bytes:
            return b"# chat\n"

    renderers = RendererRegistry()
    renderers.register(_Renderer())
    provider = PointerProvider(
        key=KEY, web_base_url="https://app.example.test", renderers=renderers
    )
    assert provider.pointer_for(object_node).rendered_mime == "text/markdown"


@pytest.mark.parametrize(
    ("kind", "extension"),
    [pytest.param(kind, ext, id=kind) for kind, ext in POINTER_EXTENSIONS.items()],
)
async def test_the_kind_follows_the_registered_extension(
    files_session: AsyncSession,
    files_factory: FilesFactory,
    provider: PointerProvider,
    kind: str,
    extension: str,
) -> None:
    drive = await files_factory.drive()
    tree = await files_factory.tree(f"item{extension}", drive=drive)
    node = tree[f"item{extension}"]
    node.kind = "object"
    node.target_object_id = uuid.uuid4()
    await files_session.commit()
    pointer = provider.pointer_for(node)
    assert pointer.kind == kind
    assert bytes(node.name).endswith(POINTER_EXTENSIONS[pointer.kind].encode())


# ---- an edited pointer is recognisable -------------------------------------


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(lambda doc: {**doc, "node_id": str(uuid.uuid4())}, id="repointed"),
        pytest.param(lambda doc: {**doc, "web_url": "https://evil.test/chats/1"}, id="url-swapped"),
        pytest.param(lambda doc: {**doc, "kind": "board"}, id="kind-swapped"),
        pytest.param(lambda doc: {k: v for k, v in doc.items() if k != "signature"}, id="unsigned"),
        pytest.param(lambda doc: {**doc, "signature": "AAAA"}, id="forged-signature"),
    ],
)
async def test_a_pointer_edited_on_disk_fails_verification(
    provider: PointerProvider,
    object_node: FileNode,
    tamper: object,
) -> None:
    text = await read(provider, object_node)
    edited = json.dumps(tamper(json.loads(text)))  # type: ignore[operator]
    with pytest.raises(InvalidPointer):
        verify(edited, key=KEY)


async def test_the_untouched_pointer_is_the_negative_twin(
    provider: PointerProvider, object_node: FileNode
) -> None:
    """The tampering test would pass on a stub that always raised."""
    assert verify(await read(provider, object_node), key=KEY).kind == "chat"


async def test_a_pointer_signed_by_another_deployment_does_not_verify(
    provider: PointerProvider, object_node: FileNode
) -> None:
    with pytest.raises(InvalidPointer):
        verify(await read(provider, object_node), key="a-different-deployment")


# ---- materialization -------------------------------------------------------


async def test_a_row_backed_node_materializes_as_a_pointer_and_a_plain_file_does_not(
    provider: PointerProvider, object_node: FileNode, plain_node: FileNode
) -> None:
    assert provider.materialize(object_node) == "pointer"
    assert provider.materialize(plain_node) == "bytes"


async def test_a_node_that_points_at_no_object_is_refused_not_rendered(
    provider: PointerProvider, plain_node: FileNode
) -> None:
    with pytest.raises(InvalidRequest):
        provider.pointer_for(plain_node)


async def test_a_pointer_keeps_no_version_history(
    provider: PointerProvider, object_node: FileNode
) -> None:
    """It is regenerated from the row, so there is nothing to roll back to."""
    assert await provider.versions(object_node) == ()
