"""The ``ContentProvider`` seam, driven over a second concrete implementation.

Future caller this stands for: a **remote** provider — a Google Drive, OneDrive
or NFS subtree read through a connector, and the document renderings beside it.
Its bytes never touch the object store, it has no version history of its own,
and the connector it reads is read-only, so it is exactly the shape that would
force a signature change if the seam were really "the store, plus a special
case".

The claim is that registering it is the whole change: the hash-equals-bytes
contract below is parametrized over the registry's own kinds, so the remote
provider comes under the same invariant the store-backed ``bytes`` provider is
held to — and the ``bytes`` half of that comparison is the real
:class:`~alkera_core.files.content.ContentService` writing to a real store, not
a second stub agreeing with the first.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from alkera_core.files.clock import FakeClock
from alkera_core.files.content import ContentService, VersionInfo
from alkera_core.files.errors import NotFound, ReadOnlyContent
from alkera_core.files.hashing import hash_bytes
from alkera_core.files.ids import DomainId, NodeId, OrgScope, VersionId
from alkera_core.files.providers.bytes import BytesProvider
from alkera_core.files.providers.registry import (
    BYTES_KIND,
    ContentInfo,
    ContentProvider,
    Materialization,
    ProviderRegistrationError,
    ProviderRegistry,
    WriteBody,
)
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.files.store.scoped import _RootedDomainStore
from alkera_core.models.files.tree import FileNode
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg
from tests.files.content.conftest import content_ctx, stream

pytestmark = pytest.mark.asyncio

#: The kind the connector registers itself under. Deliberately not one of the
#: constants in ``registry.py``: the library has never heard of it.
REMOTE_KIND = "remote:gdrive"

#: Both bodies span several 4 MiB-hashing chunks' worth of reads and differ, so
#: "head agrees with open" cannot pass by both sides being empty or equal.
STORE_BYTES = b"bytes the object store holds, block after block, " * 900
REMOTE_BYTES = "a document the connector owns — éè中文\n".encode() * 700

#: What the remote provider yields per read.
CHUNK = 8192


# ---- the second implementation ---------------------------------------------


@dataclass
class FakeConnector:
    """A remote namespace: documents by remote id, and whether it may be written."""

    documents: dict[str, bytes] = field(default_factory=dict)
    writable: bool = False
    #: Every id the connector was asked for, so a test can prove a refused write
    #: never reached the remote system.
    fetched: list[str] = field(default_factory=list)

    def fetch(self, remote_id: str) -> bytes:
        if remote_id not in self.documents:
            raise NotFound(message=f"remote document {remote_id}")
        self.fetched.append(remote_id)
        return self.documents[remote_id]


def _slices(size: int) -> list[tuple[int, int]]:
    """``size`` bytes cut into the spans one read yields."""
    return [(start, start + CHUNK) for start in range(0, size, CHUNK)]


class RemoteProvider:
    """Content served by a connector, behind the same interface as a file."""

    kind: str = REMOTE_KIND

    def __init__(self, connector: FakeConnector) -> None:
        self._connector = connector

    @staticmethod
    def _remote_id(node: FileNode) -> str:
        """The connector's own id for this node, read off the node's name.

        A remote item's identity lives in the connector, so it is derived from
        what the node already stores rather than from a column the tree layer
        would have to learn about.
        """
        return bytes(node.name).decode("utf-8")

    async def head(self, node: FileNode, version: VersionId | None) -> ContentInfo:
        digests = hash_bytes(self._connector.fetch(self._remote_id(node)))
        return ContentInfo(
            size=digests.size,
            content_hash=digests.content_hash.hex(),
            block_hash=digests.block_hash.hex(),
            mime="text/plain",
        )

    async def open(
        self,
        node: FileNode,
        version: VersionId | None,
        *,
        range: tuple[int, int] | None = None,
    ) -> AsyncIterator[bytes]:
        body = self._connector.fetch(self._remote_id(node))
        if range is not None:
            body = body[range[0] : range[1] + 1]

        async def chunks() -> AsyncIterator[bytes]:
            for start, stop in _slices(len(body)):
                yield body[start:stop]

        return chunks()

    async def versions(self, node: FileNode) -> Sequence[VersionInfo]:
        # A remote item has no history here: the connector owns it.
        return ()

    async def write(self, node: FileNode, data: WriteBody, *, if_match: int) -> VersionInfo:
        if not self._connector.writable:
            raise ReadOnlyContent(message=f"node {node.id} is served read-only")
        raise AssertionError("a writable connector is a later phase")

    def materialize(self, node: FileNode) -> Materialization:
        return "bytes"


# ---- the rig ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeamRig:
    registry: ProviderRegistry
    connector: FakeConnector
    nodes: dict[str, FileNode]
    session: AsyncSession


@pytest.fixture
async def rig(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    tmp_path: Path,
) -> SeamRig:
    drive = await files_factory.drive()
    tree = await files_factory.tree("papers/ papers/a.bin papers/remote-42", drive=drive)

    domain_id = DomainId(drive.dedup_domain_id)
    store = _RootedDomainStore(
        FilesystemStore(tmp_path / "domains" / str(domain_id), clock=clock.now), domain_id
    )
    repo = FilesRepo(files_session, OrgScope(org_team_id=files_org.org_team_id))
    content = ContentService(repo, content_ctx(), clock, store)

    stored = tree["papers/a.bin"]
    await content.put_version(
        NodeId(stored.id),
        stream(STORE_BYTES),
        size_declared=len(STORE_BYTES),
        if_match=stored.etag,
    )
    await files_session.refresh(stored)

    connector = FakeConnector(documents={"remote-42": REMOTE_BYTES})
    registry = ProviderRegistry()
    registry.register(BYTES_KIND, BytesProvider(repo, content))
    registry.register(REMOTE_KIND, RemoteProvider(connector))
    return SeamRig(
        registry=registry,
        connector=connector,
        nodes={BYTES_KIND: stored, REMOTE_KIND: tree["papers/remote-42"]},
        session=files_session,
    )


async def drain(chunks: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in chunks])


# ---- the seam's claims -----------------------------------------------------


async def test_the_remote_provider_satisfies_the_protocol_at_runtime() -> None:
    """Registration alone: a provider is one only if it has all five members."""
    assert isinstance(RemoteProvider(FakeConnector()), ContentProvider)


async def test_a_provider_missing_a_method_is_refused_at_registration() -> None:
    """The negative twin: the interface is a gate, not a label."""

    class HalfProvider:
        kind = "remote:half"

        async def head(self, node: FileNode, version: VersionId | None) -> ContentInfo:
            raise AssertionError

    with pytest.raises(ProviderRegistrationError) as raised:
        ProviderRegistry().register("remote:half", HalfProvider())  # type: ignore[arg-type]
    assert "materialize" in str(raised.value)


@pytest.mark.parametrize(
    "kind", [pytest.param(BYTES_KIND, id=BYTES_KIND), pytest.param(REMOTE_KIND, id=REMOTE_KIND)]
)
async def test_head_answers_with_the_hash_of_what_open_yields(rig: SeamRig, kind: str) -> None:
    """The one contract every provider owes, driven over the registry's kinds."""
    provider = rig.registry.get(kind)
    node = rig.nodes[kind]
    info = await provider.head(node, None)
    body = await drain(await provider.open(node, None))
    digests = hash_bytes(body)
    assert info.content_hash == digests.content_hash.hex()
    assert info.block_hash == digests.block_hash.hex()
    assert info.size == len(body) == digests.size


async def test_every_registered_kind_is_under_the_hash_invariant(rig: SeamRig) -> None:
    """A provider registered without a case above fails here, not in review."""
    assert set(rig.registry.kinds()) == {BYTES_KIND, REMOTE_KIND}


async def test_the_two_providers_serve_different_bytes(rig: SeamRig) -> None:
    """The invariant above would hold trivially if both served one body."""
    stored = await drain(await rig.registry.get(BYTES_KIND).open(rig.nodes[BYTES_KIND], None))
    remote = await drain(await rig.registry.get(REMOTE_KIND).open(rig.nodes[REMOTE_KIND], None))
    assert stored == STORE_BYTES
    assert remote == REMOTE_BYTES


@pytest.mark.parametrize(
    ("start", "end"),
    [
        pytest.param(0, 0, id="first-byte"),
        pytest.param(0, CHUNK - 1, id="exactly-one-chunk"),
        pytest.param(0, CHUNK, id="one-chunk-plus-one"),
        pytest.param(CHUNK, 3 * CHUNK - 1, id="two-whole-chunks"),
        pytest.param(len(REMOTE_BYTES) - 1, len(REMOTE_BYTES) - 1, id="last-byte"),
        pytest.param(0, len(REMOTE_BYTES) - 1, id="whole-body"),
    ],
)
async def test_a_remote_range_read_returns_that_span(rig: SeamRig, start: int, end: int) -> None:
    """A range is the seam's, not the store's: the connector honours it too."""
    provider = rig.registry.get(REMOTE_KIND)
    part = await drain(await provider.open(rig.nodes[REMOTE_KIND], None, range=(start, end)))
    assert part == REMOTE_BYTES[start : end + 1]


async def test_a_read_only_connector_refuses_write_before_reading_the_body(rig: SeamRig) -> None:
    """``write`` is the seam's only mutating member, and read-only is a property
    of the implementation — not a flag a caller could pass to turn off."""
    sent: list[bytes] = []

    async def body() -> AsyncIterator[bytes]:
        sent.append(b"x")
        yield b"x"

    node = rig.nodes[REMOTE_KIND]
    with pytest.raises(ReadOnlyContent) as raised:
        await rig.registry.get(REMOTE_KIND).write(node, WriteBody(body(), 1), if_match=node.etag)
    assert raised.value.status == 409
    # The body was never pulled, and nothing reached the remote system.
    assert sent == []
    assert rig.connector.fetched == []


async def test_the_store_backed_provider_still_writes_a_real_version(rig: SeamRig) -> None:
    """The refusal above is the remote provider's, not the seam going read-only."""
    node = rig.nodes[BYTES_KIND]
    payload = b"a second upload" * 10
    info = await rig.registry.get(BYTES_KIND).write(
        node, WriteBody(stream(payload), len(payload)), if_match=node.etag
    )
    await rig.session.refresh(node)
    assert node.head_version_id == uuid.UUID(str(info.id))
    assert await drain(await rig.registry.get(BYTES_KIND).open(node, None)) == payload


async def test_a_remote_node_has_no_local_version_history(rig: SeamRig) -> None:
    """The seam admits content whose history lives somewhere else entirely."""
    assert list(await rig.registry.get(REMOTE_KIND).versions(rig.nodes[REMOTE_KIND])) == []
    assert len(await rig.registry.get(BYTES_KIND).versions(rig.nodes[BYTES_KIND])) == 1
