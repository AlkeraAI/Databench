"""The ``bytes`` provider: an ordinary file, served out of the object store.

It owns no logic. Every rule about writing content — one pass over the stream,
the declared-size boundary, the single publish call, the compare-and-swap head
swap — lives in :class:`~alkera_core.files.content.ContentService`, and this
module is the thin shape that lets a caller reach it through the same interface
that serves a rendered chat. The only work done here is picking which version a
call means when the caller did not name one: the node's head.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING

from alkera_core.files.content import ContentService, VersionInfo
from alkera_core.files.errors import NotFound
from alkera_core.files.ids import NodeId, VersionId
from alkera_core.files.providers.registry import (
    BYTES_KIND,
    ContentInfo,
    Materialization,
    WriteBody,
)
from alkera_core.files.repo import FilesRepo

if TYPE_CHECKING:  # pragma: no cover - typing only
    from alkera_core.models.files.tree import FileNode

__all__ = ["BytesProvider"]


class BytesProvider:
    """Store-backed content, behind the provider interface."""

    kind: str = BYTES_KIND

    def __init__(self, repo: FilesRepo, content: ContentService) -> None:
        self._repo = repo
        self._content = content

    async def head(self, node: FileNode, version: VersionId | None) -> ContentInfo:
        info = await self._content.version_info(self._version(node, version))
        return ContentInfo(
            size=info.size,
            content_hash=info.content_hash,
            block_hash=info.block_hash,
            mime=info.mime,
        )

    async def open(
        self,
        node: FileNode,
        version: VersionId | None,
        *,
        range: tuple[int, int] | None = None,
    ) -> AsyncIterator[bytes]:
        return await self._content.open(self._version(node, version), range=range)

    async def versions(self, node: FileNode) -> Sequence[VersionInfo]:
        async with self._repo.transaction():
            rows = await self._repo.versions_of(NodeId(node.id))
        return [
            VersionInfo(
                id=VersionId(row.id),
                seq=row.seq,
                size=row.size_bytes,
                content_hash=row.content_hash,
                block_hash=row.block_hash,
                mime=row.mime_sniffed,
                unchanged=False,
            )
            for row in rows
        ]

    async def write(self, node: FileNode, data: WriteBody, *, if_match: int) -> VersionInfo:
        return await self._content.put_version(
            NodeId(node.id),
            data.stream,
            size_declared=data.size,
            if_match=if_match,
            mime_hint=data.mime_hint,
        )

    def materialize(self, node: FileNode) -> Materialization:
        return "bytes"

    @staticmethod
    def _version(node: FileNode, version: VersionId | None) -> VersionId:
        """The version a call means: the one it named, else the node's head."""
        if version is not None:
            return version
        if node.head_version_id is None:
            raise NotFound(message=f"node {node.id} has no content")
        return VersionId(node.head_version_id)
