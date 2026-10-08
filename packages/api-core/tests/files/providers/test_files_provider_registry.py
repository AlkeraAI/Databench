"""The registry is the only thing that knows where a node's bytes come from.

Every test here runs against real Postgres on the lane database and a real
filesystem store: "the bytes came back" is read off the disk the store wrote,
and "nothing happened" is read off the row counts in ``file_versions`` and
``file_history``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from alkera_core.files.clock import FakeClock
from alkera_core.files.content import ContentService
from alkera_core.files.errors import InvalidRequest, ReadOnlyContent
from alkera_core.files.hashing import hash_bytes
from alkera_core.files.ids import DomainId, NodeId, OrgScope
from alkera_core.files.providers.bytes import BytesProvider
from alkera_core.files.providers.pointer import PointerProvider
from alkera_core.files.providers.registry import (
    BYTES_KIND,
    POINTER_KIND,
    PROVIDER_METHODS,
    ContentProvider,
    ProviderRegistrationError,
    ProviderRegistry,
    RendererRegistry,
    WriteBody,
    rows_kind,
)
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.files.store.scoped import _RootedDomainStore
from alkera_core.models.files.history import FileHistory
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg
from tests.files.content.conftest import content_ctx, stream

PAYLOAD = b"the bytes a reader must get back, unchanged" * 40


@dataclass(frozen=True, slots=True)
class ProvidersRig:
    registry: ProviderRegistry
    renderers: RendererRegistry
    pointer: PointerProvider
    repo: FilesRepo
    session: AsyncSession
    file_node: FileNode
    object_node: FileNode
    folder_node: FileNode

    def node_for(self, kind: str) -> FileNode:
        return self.file_node if kind == BYTES_KIND else self.object_node


@pytest.fixture
async def rig(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    tmp_path: Path,
) -> ProvidersRig:
    drive = await files_factory.drive()
    tree = await files_factory.tree("papers/ papers/a.bin papers/weekly.alkerachat", drive=drive)
    object_node = tree["papers/weekly.alkerachat"]
    object_node.kind = "object"
    object_node.target_object_id = uuid.uuid4()
    await files_session.commit()

    domain_id = DomainId(drive.dedup_domain_id)
    inner = FilesystemStore(tmp_path / "domains" / str(domain_id), clock=clock.now)
    store = _RootedDomainStore(inner, domain_id)
    repo = FilesRepo(files_session, OrgScope(org_team_id=files_org.org_team_id))
    content = ContentService(repo, content_ctx(), clock, store)

    file_node = tree["papers/a.bin"]
    await content.put_version(
        NodeId(file_node.id), stream(PAYLOAD), size_declared=len(PAYLOAD), if_match=file_node.etag
    )
    await files_session.refresh(file_node)

    renderers = RendererRegistry()
    pointer = PointerProvider(
        key="a-test-signing-key", web_base_url="https://app.example.test", renderers=renderers
    )
    registry = ProviderRegistry()
    registry.register(BYTES_KIND, BytesProvider(repo, content))
    registry.register(rows_kind("chat"), pointer)
    registry.register(POINTER_KIND, pointer)
    return ProvidersRig(
        registry=registry,
        renderers=renderers,
        pointer=pointer,
        repo=repo,
        session=files_session,
        file_node=file_node,
        object_node=object_node,
        folder_node=tree["papers"],
    )


async def drain(chunks: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in chunks])


# ---- the invariant every provider owes -------------------------------------

#: The kinds the hash invariant below is driven over. A provider added to the
#: registry without a case here fails ``test_every_registered_provider_is_covered``.
COVERED_KINDS = (BYTES_KIND, rows_kind("chat"), POINTER_KIND)


@pytest.mark.parametrize("kind", [pytest.param(kind, id=kind) for kind in COVERED_KINDS])
async def test_head_answers_with_the_hash_of_what_open_yields(rig: ProvidersRig, kind: str) -> None:
    """A caller that trusts ``head`` and then reads must not find a different file."""
    provider = rig.registry.get(kind)
    node = rig.node_for(kind)
    info = await provider.head(node, None)
    body = await drain(await provider.open(node, None))
    digests = hash_bytes(body)
    assert info.content_hash == digests.content_hash.hex()
    assert info.block_hash == digests.block_hash.hex()
    assert info.size == len(body) == digests.size


async def test_every_registered_provider_is_covered_by_the_hash_invariant(
    rig: ProvidersRig,
) -> None:
    """Registration alone is what puts a provider under the invariant above."""
    assert set(rig.registry.kinds()) == set(COVERED_KINDS)


async def test_a_range_read_returns_that_span_of_what_head_described(rig: ProvidersRig) -> None:
    provider = rig.registry.get(BYTES_KIND)
    whole = await drain(await provider.open(rig.file_node, None))
    part = await drain(await provider.open(rig.file_node, None, range=(10, 19)))
    assert part == whole[10:20]


# ---- resolution ------------------------------------------------------------


async def test_a_file_resolves_to_bytes_and_an_object_to_its_rows_rendering(
    rig: ProvidersRig,
) -> None:
    assert isinstance(rig.registry.resolve(rig.file_node), BytesProvider)
    assert rig.registry.resolve(rig.object_node) is rig.pointer


@pytest.mark.parametrize(
    ("node_kind", "name"),
    [
        pytest.param("folder", b"papers", id="folder-has-no-content"),
        pytest.param("object", b"plans.alkera-unknown", id="object-type-nobody-registered"),
        pytest.param("object", b"plans", id="object-with-no-extension"),
    ],
)
async def test_a_node_no_provider_serves_is_a_typed_refusal_not_a_crash(
    rig: ProvidersRig, node_kind: str, name: bytes
) -> None:
    node = rig.object_node if node_kind == "object" else rig.folder_node
    node.kind = node_kind
    node.name = name
    with pytest.raises(InvalidRequest):
        rig.registry.resolve(node)


# ---- registration is where a mistake is caught -----------------------------


class _Stub:
    """A provider that answers every call, so removing one method is the variable."""

    kind = "stub"

    async def head(self, node: FileNode, version: object) -> None: ...
    async def open(self, node: FileNode, version: object) -> None: ...
    async def versions(self, node: FileNode) -> None: ...
    async def write(self, node: FileNode, data: object, *, if_match: int) -> None: ...
    def materialize(self, node: FileNode) -> str:
        return "bytes"


def test_a_complete_provider_registers(rig: ProvidersRig) -> None:
    """The negative twin of the case below: nothing is refused by accident."""
    registry = ProviderRegistry()
    registry.register("stub", _Stub())
    assert registry.kinds() == ("stub",)


@pytest.mark.parametrize("missing", [pytest.param(name, id=name) for name in PROVIDER_METHODS])
def test_a_provider_missing_a_method_is_refused_at_registration(missing: str) -> None:
    """The failure lands at import time, never as a 500 the first time it is read."""
    provider = _Stub()
    setattr(provider, missing, None)
    with pytest.raises(ProviderRegistrationError, match=missing):
        ProviderRegistry().register("stub", provider)


def test_registering_a_kind_twice_is_refused(rig: ProvidersRig) -> None:
    with pytest.raises(ProviderRegistrationError, match="already registered"):
        rig.registry.register(BYTES_KIND, _Stub())


def test_a_kind_with_no_name_is_refused() -> None:
    with pytest.raises(ProviderRegistrationError):
        ProviderRegistry().register("", _Stub())


def test_asking_for_a_kind_nobody_registered_is_a_typed_refusal() -> None:
    with pytest.raises(InvalidRequest):
        ProviderRegistry().get("rows:chat")


def test_the_stub_satisfies_the_provider_protocol() -> None:
    """Registration checks the same surface the type system does."""
    assert isinstance(_Stub(), ContentProvider)


# ---- a read-only rendering refuses before it does anything -----------------


async def test_write_on_a_read_only_rendering_raises_before_any_effect(
    rig: ProvidersRig,
) -> None:
    """A pointer edited on a laptop can never become a version of the object."""

    async def body() -> AsyncIterator[bytes]:
        yield b"someone edited the pointer"
        pytest.fail("the stream was read before the write was refused")

    versions = select(func.count()).select_from(FileVersion)
    history = select(func.count()).select_from(FileHistory)
    before = (await rig.session.scalar(versions), await rig.session.scalar(history))
    provider = rig.registry.resolve(rig.object_node)
    with pytest.raises(ReadOnlyContent):
        await provider.write(rig.object_node, WriteBody(stream=body(), size=26), if_match=1)
    after = (await rig.session.scalar(versions), await rig.session.scalar(history))
    assert after == before


# ---- renderers -------------------------------------------------------------


class _Renderer:
    object_type = "chat"
    extension = ".alkerachat"
    mime = "text/markdown"

    async def render(self, session: object, obj: object, version: object) -> bytes:
        return b"# chat\n"


class _WritableRenderer(_Renderer):
    async def parse(self, session: object, obj: object, data: bytes) -> None: ...


def test_a_renderer_is_read_only_exactly_when_it_has_no_parser() -> None:
    """There is no flag to forget: the parser's presence is the whole rule."""
    assert RendererRegistry.is_writable(_Renderer()) is False
    assert RendererRegistry.is_writable(_WritableRenderer()) is True


def test_registering_two_renderers_for_one_object_type_is_refused() -> None:
    renderers = RendererRegistry()
    renderers.register(_Renderer())
    with pytest.raises(ProviderRegistrationError, match="already has a renderer"):
        renderers.register(_WritableRenderer())
    assert renderers.object_types() == ("chat",)


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        pytest.param("object_type", "", id="no-object-type"),
        pytest.param("render", None, id="no-render"),
        pytest.param("mime", "", id="no-mime"),
    ],
)
def test_an_incomplete_renderer_is_refused_at_registration(attribute: str, value: object) -> None:
    renderer = _Renderer()
    setattr(renderer, attribute, value)
    with pytest.raises(ProviderRegistrationError):
        RendererRegistry().register(renderer)


def test_asking_for_an_object_type_nobody_rendered_is_a_typed_refusal() -> None:
    renderers = RendererRegistry()
    with pytest.raises(InvalidRequest):
        renderers.get("chat")
    assert renderers.find("chat") is None
