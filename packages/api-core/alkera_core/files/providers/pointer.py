"""The ``pointer`` provider: what a row-backed object looks like on a real disk.

A chat is a row, not a file, so a mount cannot hand an editor its bytes and call
that the truth. What it hands over instead is a small signed JSON document —
``kind``, ``node_id``, ``web_url``, ``app_url``, ``rendered_mime`` — named
``<name>.alkera<kind>``, the way Drive materializes a ``.gdoc``. Opening it
launches the app that actually owns the object.

Two properties make the rest of the system simple.

**It is derived, never authoritative.** :meth:`PointerProvider.write` always
raises :class:`~alkera_core.files.errors.ReadOnlyContent`, before touching the
database, so a pointer edited on a laptop can never become a version of the
object it points at. ``push`` skips pointers and warns; ``pull`` rewrites them.

**It is deterministic and signed.** The bytes are a function of the node alone,
so ``head()`` can answer with the hash of exactly what ``open()`` will yield
without rendering twice, and a pointer that has been edited on disk fails
``verify`` rather than being acted on as if the server had written it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import AsyncIterator, Callable, Sequence
from typing import TYPE_CHECKING, Any, ClassVar, Final

from alkera_core.files.content import VersionInfo
from alkera_core.files.errors import InvalidRequest, ReadOnlyContent
from alkera_core.files.hashing import hash_bytes
from alkera_core.files.ids import NodeId, VersionId
from alkera_core.files.providers.derived_members import DERIVED_MEMBERS
from alkera_core.files.providers.registry import (
    POINTER_EXTENSIONS,
    POINTER_KIND,
    ContentInfo,
    Materialization,
    RendererRegistry,
    WriteBody,
    object_type_of,
    object_web_path,
)
from alkera_core.versioning import VersionedModel

if TYPE_CHECKING:  # pragma: no cover - typing only
    from alkera_core.models.files.tree import FileNode

__all__ = [
    "POINTER_EXTENSIONS",
    "POINTER_MIME",
    "SIGNATURE_KEY",
    "InvalidPointer",
    "PointerFile",
    "PointerProvider",
    "sign",
    "verify",
]

#: The JSON key the detached signature is written under.
SIGNATURE_KEY: Final = "signature"


class InvalidPointer(ValueError):  # noqa: N818 - the name the Files spec pins
    """The bytes are not a pointer this deployment wrote."""


class PointerFile(VersionedModel):
    """The small JSON document written as `<name>.alkera<kind>`."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    kind: str = ""
    node_id: NodeId | None = None
    web_url: str | None = None
    app_url: str | None = None
    rendered_mime: str = "application/json"


def _payload(pointer: PointerFile) -> bytes:
    body = pointer.model_dump(mode="json")
    body.pop(SIGNATURE_KEY, None)
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _mac(payload: bytes, key: str) -> str:
    digest = hmac.new(key.encode("utf-8"), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def sign(pointer: PointerFile, *, key: str) -> str:
    """The exact bytes written to disk for this pointer."""
    body = json.loads(_payload(pointer))
    body[SIGNATURE_KEY] = _mac(_payload(pointer), key)
    return json.dumps(body, indent=2, sort_keys=True) + "\n"


def verify(text: str, *, key: str) -> PointerFile:
    """Parse a pointer file, or raise `InvalidPointer` if it was touched."""
    try:
        decoded: Any = json.loads(text)
    except ValueError as exc:
        raise InvalidPointer("pointer is not JSON") from exc
    if not isinstance(decoded, dict):
        raise InvalidPointer("pointer is not an object")
    claimed = decoded.pop(SIGNATURE_KEY, None)
    if not isinstance(claimed, str):
        raise InvalidPointer("pointer carries no signature")
    try:
        pointer = PointerFile.model_validate(decoded)
    except ValueError as exc:
        raise InvalidPointer("pointer is not a pointer file") from exc
    if not hmac.compare_digest(claimed, _mac(_payload(pointer), key)):
        raise InvalidPointer("signature does not verify")
    return pointer


FIXTURE_EXAMPLES: list[tuple[str, Callable[[], VersionedModel]]] = [
    (
        "pointer_file",
        lambda: PointerFile(
            kind="chat",
            web_url="https://app.example.test/chats/1",
            app_url="alkera://chats/1",
            rendered_mime="text/markdown",
        ),
    ),
]


#: A pointer is JSON on the wire whatever it points at; ``rendered_mime`` inside
#: it is what the *object* renders as.
POINTER_MIME: Final = "application/alkera-pointer+json"

#: What a pointer claims the object renders as when no renderer is registered.
#: The object services register the real ones.
_DEFAULT_RENDERED_MIME: Final = "application/json"


class PointerProvider:
    """The signed stand-in a filesystem gets for a row-backed node."""

    kind: str = POINTER_KIND

    def __init__(
        self,
        *,
        key: str,
        web_base_url: str,
        app_scheme: str = "alkera",
        renderers: RendererRegistry | None = None,
    ) -> None:
        self._key = key
        self._web_base = web_base_url.rstrip("/")
        self._app_scheme = app_scheme
        self._renderers = renderers

    # ---- the pointer itself ---------------------------------------------

    def pointer_for(self, node: FileNode) -> PointerFile:
        """The document this node materializes as."""
        object_type = object_type_of(node)
        if object_type is None or node.target_object_id is None:
            raise InvalidRequest(message=f"node {node.id} is not row-backed")
        renderer = None if self._renderers is None else self._renderers.find(object_type)
        # The same route the wire facet carries, so opening the pointer on a
        # laptop and opening the item in the drive land on one page.
        target = object_web_path(object_type, str(node.target_object_id))
        return PointerFile(
            kind=object_type,
            node_id=NodeId(node.id),
            web_url=f"{self._web_base}{target}",
            app_url=f"{self._app_scheme}:/{target}",
            rendered_mime=_DEFAULT_RENDERED_MIME if renderer is None else renderer.mime,
        )

    def render(self, node: FileNode) -> bytes:
        """The exact bytes written to disk, signature and all."""
        return sign(self.pointer_for(node), key=self._key).encode("utf-8")

    # ---- the provider interface -----------------------------------------

    async def head(self, node: FileNode, version: VersionId | None) -> ContentInfo:
        digests = hash_bytes(self.render(node))
        return ContentInfo(
            size=digests.size,
            content_hash=digests.content_hash.hex(),
            block_hash=digests.block_hash.hex(),
            mime=POINTER_MIME,
        )

    async def open(
        self,
        node: FileNode,
        version: VersionId | None,
        *,
        range: tuple[int, int] | None = None,
    ) -> AsyncIterator[bytes]:
        payload = self.render(node)
        start, end = (0, len(payload) - 1) if range is None else range
        return _one(payload[start : end + 1])

    async def versions(self, node: FileNode) -> Sequence[VersionInfo]:
        """None: a pointer is regenerated, never kept."""
        return ()

    async def write(self, node: FileNode, data: WriteBody, *, if_match: int) -> VersionInfo:
        raise ReadOnlyContent(message=f"node {node.id} materializes as a pointer")

    def materialize(self, node: FileNode) -> Materialization:
        """What a filesystem gets for this node.

        A chat template is a DIRECTORY, not a stand-in file: it is meant to be
        handed to a fresh chat, and what that chat needs — the files it starts
        with, and the brief written out for whoever reads the folder — does not
        fit in one pointer. The derived members come from
        :func:`alkera_core.files.providers.derived_members.render_member`, fed
        by the same document the rows renderer serves, so the folder and the
        single-file rendering can never describe different rows.
        """
        if node.target_object_id is None:
            return "bytes"
        if object_type_of(node) in DERIVED_MEMBERS:
            return "context"
        return "pointer"


async def _one(payload: bytes) -> AsyncIterator[bytes]:
    if payload:
        yield payload
