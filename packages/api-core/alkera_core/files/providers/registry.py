"""The one interface behind a node's bytes, and the registry that resolves it.

Three things live here and nothing else does.

:class:`ContentProvider` is the interface. Everything that reads or writes a
node's content — the content routes, ``pull``, WebDAV, search previews, the box
exporter — goes through it, so a new kind of content is a registration rather
than a branch added to five callers.

:class:`ProviderRegistry` resolves a node to its provider. The key is derived,
never stored: a ``file`` node is served by ``"bytes"``; an ``object`` node by
``"rows:<object type>"``, with the object type read back out of the node's
pointer extension (the same invertible mapping :mod:`alkera_core.files.objects_bridge`
writes the name with) so resolution costs no second query. A kind nobody
registered is refused **when the registration is made** — a provider missing a
method, or a second provider for a kind already taken, raises
:class:`ProviderRegistrationError` at import time rather than becoming a 500 the
first time someone opens that node.

:class:`RowRenderer` is the per-object-type half that the object services
register into :class:`RendererRegistry`. A renderer without ``parse`` is
read-only by construction: there is no flag to set and none to forget, so a
rendering that cannot be turned back into rows can only ever refuse a write.

The registries are plain objects, not module globals: a test builds its own and
the application builds one at startup, so registration order is never a hidden
dependency between test modules.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, runtime_checkable

from alkera_core.files.errors import InvalidRequest

if TYPE_CHECKING:  # pragma: no cover - typing only
    from alkera_core.files.content import VersionInfo
    from alkera_core.files.ids import VersionId
    from alkera_core.models.files.tree import FileNode

__all__ = [
    "BYTES_KIND",
    "CONTEXT_MEMBER_SEPARATOR",
    "DEFAULT_WEB_ROUTE",
    "OBJECT_WEB_ROUTES",
    "POINTER_EXTENSIONS",
    "POINTER_KIND",
    "PROVIDER_METHODS",
    "RENDERER_METHODS",
    "ROWS_PREFIX",
    "ContentInfo",
    "ContentProvider",
    "Materialization",
    "ProviderRegistrationError",
    "ProviderRegistry",
    "RendererRegistry",
    "RowRenderer",
    "WriteBody",
    "context_member_type",
    "object_type_of",
    "object_web_path",
    "rows_kind",
]

#: The store-backed provider: an ordinary file's bytes.
BYTES_KIND: Final = "bytes"
#: The provider that stands in for a row-backed object on a real filesystem.
POINTER_KIND: Final = "pointer"
#: Every rendering of Postgres rows is keyed ``rows:<object type>``.
ROWS_PREFIX: Final = "rows:"
#: What separates a replication context's object type from the member of its
#: folder being rendered. It may not appear in an object type, which is what
#: makes "does this subtype hold one?" the test for "is this node a member?".
CONTEXT_MEMBER_SEPARATOR: Final = ":"

#: What a registered provider must be able to do. Checked at registration so a
#: half-written provider cannot reach a reader.
PROVIDER_METHODS: Final[tuple[str, ...]] = ("head", "open", "versions", "write", "materialize")
#: What a renderer must be able to do. ``parse`` is deliberately absent: its
#: presence is what makes a rendering writable.
RENDERER_METHODS: Final[tuple[str, ...]] = ("render",)

#: How a node is written onto a real filesystem. ``bytes`` is an ordinary
#: file's content; ``pointer`` the small signed JSON stand-in a row-backed node
#: gets; ``context`` a DIRECTORY of derived members — what a replication
#: context (a ``.alkeraquery`` / ``.alkerareport``) is, since a folder is the
#: only shape that can hold the spec, its human-readable form and its last
#: output at once.
Materialization = Literal["bytes", "pointer", "context"]

#: ``object type -> pointer extension``. A new object kind registers here;
#: nothing branches on the set of kinds anywhere else. It lives beside the
#: provider registry rather than in the schemas layer because the library never
#: imports its own API shapes; ``alkera_core.schemas.files.pointer`` re-exports
#: it for the surfaces that already read it from there.
POINTER_EXTENSIONS: Final[dict[str, str]] = {
    "chat": ".alkerachat",
    "query": ".alkeraquery",
    "result": ".alkeraresult",
    "report": ".alkerareport",
    "board": ".alkeraboard",
    "app": ".alkeraapp",
    # A template is a chat a reader saved to start the next one from, so it is
    # spelled as what it makes: a chat, qualified. The extension holds a dot,
    # which is why the type is read off the LONGEST registered suffix and not
    # off the last one.
    "chat_template": ".alkerachat.template",
    # A workspace is the folder several chats share: its working tree and the
    # records of every chat in it.
    "workspace": ".alkeraworkspace",
}

#: ``extension -> object type``: the inverse of the map the bridge names nodes
#: with. Spelled once here so nothing else parses a pointer name by hand.
_TYPE_BY_EXTENSION: Final[dict[str, str]] = {
    extension: object_type for object_type, extension in POINTER_EXTENSIONS.items()
}

#: The same inverse, longest extension first. One extension may end with
#: another — ``.alkerachat.template`` ends with ``template`` and contains
#: ``.alkerachat`` — so a name is matched against the longest first and the
#: first hit wins, which makes the answer independent of dict order.
_EXTENSIONS_LONGEST_FIRST: Final[tuple[str, ...]] = tuple(
    sorted(_TYPE_BY_EXTENSION, key=len, reverse=True)
)

#: ``object type -> the route family it opens under in the web app``. A chat is
#: a conversation and opens at ``/chat/<id>``; everything else is an object page
#: at ``/objects/<id>``. It lives beside the extension registry because the two
#: answer the same question from the two sides — what a node is *called* and
#: where opening it *goes* — and a pointer written on disk carries the second,
#: so the on-disk document and the wire facet must not be able to disagree.
OBJECT_WEB_ROUTES: Final[dict[str, str]] = {
    "chat": "chat",
    # A template is not a conversation and not an object page: it is the thing
    # you start a chat FROM, and it opens on its own surface.
    "chat_template": "templates",
    # A workspace opens on its own page, where its chats and its files are.
    "workspace": "workspaces",
    "query": "objects",
    "result": "objects",
    "report": "objects",
    "board": "objects",
    "app": "objects",
}

#: Where an object opens when the route it belongs to is not registered. It is
#: the object page, which renders any object type it is handed, so an
#: unregistered type is a plain page rather than a dead link.
DEFAULT_WEB_ROUTE: Final = "objects"


def object_web_path(object_type: str, object_id: str) -> str:
    """Where an object opens in the web app, as a root-relative path.

    Root-relative rather than absolute: the app is served same-origin, and a
    path cannot carry a deployment's hostname into a tenant's wire payload. The
    pointer provider prefixes its own configured base to reach an absolute URL.
    """
    return f"/{OBJECT_WEB_ROUTES.get(object_type, DEFAULT_WEB_ROUTE)}/{object_id}"


def rows_kind(object_type: str) -> str:
    """The registry key for a rendering of ``object_type``'s rows."""
    return f"{ROWS_PREFIX}{object_type}"


def context_member_type(base_type: str, member: str) -> str:
    """The renderer key for one member of a replication-context folder.

    ``report`` + ``spec.json`` is ``report:spec.json``. It is a renderer key
    and a node ``subtype`` at once, which is what lets
    :func:`object_type_of` resolve a member from the row it already read.
    Spelled here, beside :func:`rows_kind`, so the bridge that stamps the
    subtype and the renderer that answers for it cannot disagree.
    """
    return f"{base_type}{CONTEXT_MEMBER_SEPARATOR}{member}"


def object_type_of(node: FileNode) -> str | None:
    """The object type behind an ``object`` node, or ``None`` if it has none.

    Read from the node's own name, whose extension the bridge assigns from
    :data:`POINTER_EXTENSIONS` — the mapping
    is invertible, so this costs no query and cannot disagree with the name a
    user sees.

    Matched against the LONGEST registered extension first, because an
    extension may itself hold a dot: ``Weekly.alkerachat.template`` is a chat
    template, not a chat with an odd tail, and reading only the segment after
    the last dot would serve every template the chat renderer.
    """
    if node.target_object_id is None:
        return None
    name = bytes(node.name)
    for extension in _EXTENSIONS_LONGEST_FIRST:
        if name.endswith(extension.encode("utf-8")):
            return _TYPE_BY_EXTENSION[extension]
    # A replication context's members (`spec.json`, `README.md`) are named for
    # a reader, so their extension resolves to nothing. They carry their
    # registry key on `subtype` — `report:spec.json` — which is the same field
    # an object pointer already stamps with its object type, so this is one
    # rule read twice rather than a second rule.
    return node.subtype or None


@dataclass(frozen=True, slots=True)
class ContentInfo:
    """What a reader learns about a node's bytes without reading them.

    The hashes are hex, as they are on the version row, so a caller can compare
    a provider's answer against a stored version with ``==`` and not a codec.
    """

    size: int
    content_hash: str
    block_hash: str
    mime: str


@dataclass(frozen=True, slots=True)
class WriteBody:
    """The bytes a caller wants a node to hold, and how many of them there are.

    The size travels with the stream because content is never buffered to
    measure it: the writer refuses at the declared boundary rather than
    discovering a 1 GB body after accepting a 1 MB one.
    """

    stream: AsyncIterator[bytes]
    size: int
    mime_hint: str | None = None


@runtime_checkable
class ContentProvider(Protocol):
    """Everything a caller may do with a node's content."""

    kind: str

    async def head(self, node: FileNode, version: VersionId | None) -> ContentInfo:
        """Size, hashes and MIME, from the same rendering ``open`` would yield."""

    async def open(
        self,
        node: FileNode,
        version: VersionId | None,
        *,
        range: tuple[int, int] | None = None,
    ) -> AsyncIterator[bytes]:
        """The bytes themselves, verified before any of them are yielded."""

    async def versions(self, node: FileNode) -> Sequence[VersionInfo]:
        """This node's version history, newest last. Empty for derived content."""

    async def write(self, node: FileNode, data: WriteBody, *, if_match: int) -> VersionInfo:
        """Replace the node's content. Raises ``ReadOnlyContent`` for a rendering
        with no parser — before any row is written."""

    def materialize(self, node: FileNode) -> Materialization:
        """Whether a filesystem gets the bytes or a pointer file."""


@runtime_checkable
class RowRenderer(Protocol):
    """One object type's canonical bytes: sorted keys, fixed numbers, ``\\n``, UTF-8."""

    object_type: str
    extension: str
    mime: str

    async def render(self, session: Any, obj: Any, version: Any) -> bytes:
        """The object's rows as bytes. Deterministic: two processes agree byte for byte."""


class ProviderRegistrationError(Exception):
    """A provider or renderer was registered that cannot serve a read.

    Deliberately not a :class:`~alkera_core.files.errors.FilesError`: this is a
    wiring mistake raised while the process starts, never an answer to a
    request, and giving it a status would invite someone to let it reach one.
    """


class ProviderRegistry:
    """``kind -> provider``, and the rule that turns a node into a kind."""

    def __init__(self) -> None:
        self._providers: dict[str, ContentProvider] = {}

    def register(self, kind: str, provider: ContentProvider) -> None:
        """Take ``kind`` for ``provider``, or refuse now and not at read time."""
        if not kind:
            raise ProviderRegistrationError("a provider kind may not be empty")
        if kind in self._providers:
            raise ProviderRegistrationError(f"kind {kind!r} is already registered")
        missing = [name for name in PROVIDER_METHODS if not callable(getattr(provider, name, None))]
        if missing:
            raise ProviderRegistrationError(
                f"provider for {kind!r} is missing {', '.join(sorted(missing))}"
            )
        self._providers[kind] = provider

    def kinds(self) -> tuple[str, ...]:
        """Every registered kind, in registration order."""
        return tuple(self._providers)

    def __iter__(self) -> Iterator[tuple[str, ContentProvider]]:
        return iter(tuple(self._providers.items()))

    def get(self, kind: str) -> ContentProvider:
        """The provider for ``kind``, or a typed refusal naming no row."""
        provider = self._providers.get(kind)
        if provider is None:
            raise InvalidRequest(message=f"no content provider for {kind!r}")
        return provider

    def kind_for(self, node: FileNode) -> str:
        """The registry key a node's content lives under."""
        if node.kind == "file":
            return BYTES_KIND
        if node.kind == "object":
            object_type = object_type_of(node)
            if object_type is None:
                raise InvalidRequest(message=f"node kind {node.kind!r} names no object type")
            return rows_kind(object_type)
        raise InvalidRequest(message=f"node kind {node.kind!r} has no content")

    def resolve(self, node: FileNode) -> ContentProvider:
        """The provider that serves this node's bytes."""
        return self.get(self.kind_for(node))


class RendererRegistry:
    """``object type -> renderer``, populated by the object services."""

    def __init__(self) -> None:
        self._renderers: dict[str, RowRenderer] = {}

    def register(self, renderer: RowRenderer) -> None:
        object_type = getattr(renderer, "object_type", "")
        if not object_type:
            raise ProviderRegistrationError("a renderer must name its object type")
        if object_type in self._renderers:
            raise ProviderRegistrationError(f"object type {object_type!r} already has a renderer")
        missing = [name for name in RENDERER_METHODS if not callable(getattr(renderer, name, None))]
        if missing:
            raise ProviderRegistrationError(
                f"renderer for {object_type!r} is missing {', '.join(sorted(missing))}"
            )
        if getattr(renderer, "mime", "") == "":
            raise ProviderRegistrationError(f"renderer for {object_type!r} declares no mime")
        self._renderers[object_type] = renderer

    def object_types(self) -> tuple[str, ...]:
        return tuple(self._renderers)

    def get(self, object_type: str) -> RowRenderer:
        renderer = self._renderers.get(object_type)
        if renderer is None:
            raise InvalidRequest(message=f"no renderer for object type {object_type!r}")
        return renderer

    def find(self, object_type: str | None) -> RowRenderer | None:
        """The renderer, or ``None`` — for callers that have a fallback."""
        return None if object_type is None else self._renderers.get(object_type)

    @staticmethod
    def is_writable(renderer: RowRenderer) -> bool:
        """Whether this rendering can be turned back into rows."""
        return callable(getattr(renderer, "parse", None))
