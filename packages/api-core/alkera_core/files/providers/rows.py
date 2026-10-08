"""The ``rows:<object type>`` provider: a workspace object, rendered as a file.

A chat, a saved query and a promoted result live in Postgres rows. They are
still files — ``alkera files cat`` prints one, an export writes one, a search
preview reads one — so each object type registers a ``RowRenderer`` that turns
its rows into canonical bytes, and one :class:`RowsProvider` serves every type
through the same content interface an ordinary file is served through.

Three rules shape the module.

**A rendering is a pure function of the rows.** Every renderer splits fetching
from formatting: :func:`render_chat`, :func:`render_query` and
:func:`render_result` take plain data and return bytes, and the renderer class
is the query that feeds them. That is what makes the rendering deterministic
across processes and machines — the goldens under
``packages/api-core/tests/fixtures/files/renderings/`` are produced by the same functions from a
committed input, so a formatting change fails a diff rather than a reader.

**Writability is structural.** A renderer is writable exactly when it defines
``parse``; there is no flag to set and none to forget. :class:`ChatRenderer`
and :class:`ResultRenderer` have none, so :meth:`RowsProvider.write` refuses
them with ``ReadOnlyContent`` before it has read a byte of the body.
:class:`QueryRenderer` has one, and it does not write rows itself: it hands the
parsed spec to the object service's own update function, injected as a
callable, so the file surface can never take a shortcut past the object
service's validation, versioning and ``etag`` bump.

**Types that cannot render yet are still registered.** ``board`` and ``app``
are named in :data:`~alkera_core.models.workspace_object.OBJECT_TYPES`, so the
registry lists them with a renderer that refuses at read time. A real renderer
replaces it without a new registration, and meanwhile a node of that type
resolves to a typed 422 instead of a 500 from an empty registry.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import select

from alkera_core.files.checkpoints import Checkpoints, NoopCheckpoints
from alkera_core.files.errors import (
    FilesError,
    InvalidRequest,
    PreconditionFailed,
    ReadOnlyContent,
)
from alkera_core.files.hashing import hash_bytes
from alkera_core.files.ids import VersionId
from alkera_core.files.providers.derived_members import DERIVED_MEMBERS, render_member
from alkera_core.files.providers.registry import (
    ContentInfo,
    Materialization,
    RendererRegistry,
    RowRenderer,
    WriteBody,
    context_member_type,
    object_type_of,
    rows_kind,
)
from alkera_core.models.workspace_object import (
    RETIRED_OBJECT_TYPES,
    ChatMessage,
    ObjectPayloadRow,
    WorkspaceObject,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession

    from alkera_core.files.content import VersionInfo
    from alkera_core.files.repo import FilesRepo
    from alkera_core.models.files.tree import FileNode

__all__ = [
    "CSV_MIME",
    "JSON_MIME",
    "RENDERING_SCHEMA_VERSION",
    "ROW_SEPARATOR",
    "UNRENDERED_TYPES",
    "ChatRenderer",
    "ChatTemplateRenderer",
    "ContextMemberRenderer",
    "NotImplementedRendering",
    "QuerySpecUpdate",
    "ResultRenderer",
    "RetiredRenderer",
    "RetiredRendering",
    "RowsProvider",
    "UnimplementedRenderer",
    "canonical_json",
    "chat_document",
    "chat_template_document",
    "context_member_renderers",
    "query_document",
    "register_rows_providers",
    "render_chat",
    "render_query",
    "render_result",
    "report_document",
    "standard_renderers",
]

#: What every JSON rendering is served as.
JSON_MIME: Final = "application/json"
#: What a promoted result is served as.
CSV_MIME: Final = "text/csv"
#: RFC 4180's record separator. A CSV rendering uses it; the JSON renderings
#: use ``\n``, which is what the canonical-JSON rule asks for.
ROW_SEPARATOR: Final = "\r\n"
#: Stamped into every JSON rendering, so a reader that meets an exported file
#: from an older server can tell which shape it is holding.
RENDERING_SCHEMA_VERSION: Final = "1.0.0"

#: The types whose rows do not render yet; named so the registry is
#: complete and a node of one of them fails typed rather than unregistered. A
#: workspace's node is always a folder, never an object whose rows are read.
UNRENDERED_TYPES: Final[tuple[str, ...]] = ("board", "app", "workspace")


class NotImplementedRendering(InvalidRequest):
    """This object type has no rendering yet.

    Its renderer is registered anyway: a registry missing an object type is a
    500 the first time someone opens such a node, while a registered refusal is
    a 422 that names the type and changes nothing when the real renderer lands.
    """

    code = "files.rendering_not_implemented"


# ---- canonical formatting --------------------------------------------------


def canonical_json(payload: Any) -> bytes:
    """``payload`` as the one JSON spelling every process agrees on.

    Keys sorted, two-space indent, ``\\n`` endings, UTF-8 without escapes, and a
    trailing newline so the file ends the way a text file ends. ``NaN`` and the
    infinities are refused rather than written as the JavaScript-only literals
    Python emits by default — they would not survive a round trip through any
    other reader.
    """
    text = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        separators=(",", ": "),
    )
    return f"{text}\n".encode()


def render_chat(document: Mapping[str, Any]) -> bytes:
    """A transcript's canonical bytes, from plain data."""
    return canonical_json(document)


def render_query(document: Mapping[str, Any]) -> bytes:
    """A saved query's canonical bytes, from plain data."""
    return canonical_json(document)


def render_result(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> bytes:
    """``rows`` as RFC 4180 CSV with ``columns`` as the header.

    The header order is the object's own column order, never the order a page of
    rows happened to arrive in, so two renderings of one result agree.
    """
    lines = [_csv_record(columns)]
    lines.extend(_csv_record([_cell(value) for value in row]) for row in rows)
    return (ROW_SEPARATOR.join(lines) + ROW_SEPARATOR).encode()


def _csv_record(fields: Sequence[str]) -> str:
    return ",".join(_csv_field(field) for field in fields)


def _csv_field(field: str) -> str:
    """One field, quoted when RFC 4180 requires it — and when it holds a control
    character, which no bare field may carry and which a NUL byte smuggled
    through a warehouse column is."""
    if _needs_quotes(field):
        return '"' + field.replace('"', '""') + '"'
    return field


def _needs_quotes(field: str) -> bool:
    return any(char in ',"\r\n' or ord(char) < 0x20 for char in field)


def _cell(value: Any) -> str:
    """One cell's text. Everything a warehouse can return has one spelling."""
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise InvalidRequest(message="a result cell may not hold NaN or an infinity")
        return repr(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _columns_of(obj: WorkspaceObject) -> tuple[str, ...]:
    """A result's header, read from its spec.

    Accepts both spellings the result envelope has used — a list of names and a
    list of ``{"name": ...}`` descriptors — because the header is the contract
    and a renderer that guessed it from the first row would reorder itself the
    day a page arrived empty.
    """
    declared = obj.spec.get("columns")
    if not isinstance(declared, list) or not declared:
        raise InvalidRequest(message=f"result {obj.id} declares no columns")
    names: list[str] = []
    for column in declared:
        if isinstance(column, str):
            names.append(column)
        elif isinstance(column, dict) and isinstance(column.get("name"), str):
            names.append(str(column["name"]))
        else:
            raise InvalidRequest(message=f"result {obj.id} has an unreadable column descriptor")
    return tuple(names)


# ---- the renderers ---------------------------------------------------------


def _object_header(obj: WorkspaceObject) -> dict[str, Any]:
    return {
        "id": str(obj.id),
        "logical_id": obj.logical_id,
        "namespace": obj.namespace,
        "title": obj.title,
        "version": obj.version,
    }


def chat_document(obj: WorkspaceObject, messages: Sequence[ChatMessage]) -> dict[str, Any]:
    """The transcript's plain-data shape, shared by the renderer and the golden."""
    return {
        "schema_version": RENDERING_SCHEMA_VERSION,
        "type": "chat",
        "object": _object_header(obj),
        "messages": [
            {
                "seq": message.seq,
                "role": message.role,
                "kind": message.kind,
                "event_id": message.event_id,
                "payload": message.payload,
            }
            for message in messages
        ],
    }


def query_document(obj: WorkspaceObject) -> dict[str, Any]:
    """A saved query's plain-data shape: its spec, under a stamped envelope."""
    return {
        "schema_version": RENDERING_SCHEMA_VERSION,
        "type": "query",
        "object": _object_header(obj),
        "spec": obj.spec,
    }


def chat_template_document(obj: WorkspaceObject) -> dict[str, Any]:
    """A chat template's plain-data shape: the envelope, then its spec.

    The same envelope a saved query and a report get, because the three answer
    the same question about a node — what object is this, and what does it
    say — and a reader that already parses one parses all three.
    """
    return {
        "schema_version": RENDERING_SCHEMA_VERSION,
        "type": "chat_template",
        "object": _object_header(obj),
        "spec": obj.spec,
    }


def report_document(obj: WorkspaceObject) -> dict[str, Any]:
    """A report's plain-data shape — the same envelope a saved query gets.

    One envelope for both, because both are replication contexts and the folder
    renderer reads them through the same keys: what differs is the spec inside,
    not the shape around it.
    """
    return {
        "schema_version": RENDERING_SCHEMA_VERSION,
        "type": "report",
        "object": _object_header(obj),
        "spec": obj.spec,
    }


class ChatRenderer:
    """A chat's transcript as canonical JSON.

    Read-only: a transcript is appended to by the harness that produced it, and
    a file write that replaced it would rewrite history nobody could audit.
    """

    object_type: str = "chat"
    extension: str = ".json"
    mime: str = JSON_MIME

    async def render(
        self, session: AsyncSession, obj: WorkspaceObject, version: Any = None
    ) -> bytes:
        rows = (
            (
                await session.execute(
                    select(ChatMessage)
                    .where(ChatMessage.chat_id == obj.id)
                    .order_by(ChatMessage.seq)
                )
            )
            .scalars()
            .all()
        )
        return render_chat(chat_document(obj, rows))


#: How a parsed spec would reach the object service. The file surface never
#: writes ``workspace_objects`` itself: it hands the spec to the service that
#: owns validation, the version counter and the node's ``etag`` bump. No
#: rendering parses one today — the type that did is retired — and the shape
#: stays because that is the contract a writable rendering signs.
QuerySpecUpdate = Callable[["AsyncSession", WorkspaceObject, dict[str, Any]], Awaitable[None]]


class RetiredRendering(FilesError):  # noqa: N818 - the route layer maps these names; the Error suffix is noise here
    """The object type is retired: nothing renders its bytes any more."""

    code = "files.rendering_retired"
    status = 410


class RetiredRenderer:
    """A registered object type whose rendering is gone for good.

    A saved query and a report were replication contexts; a chat template does
    that job now, and the conversion moves what their authors wrote into one.
    The rows the old types left behind still load — they are converted, listed
    and trashed like any other — but reading one as a file answers "this is
    retired" rather than serving bytes nothing writes any more. Registered
    rather than removed, so an old node gets that answer instead of the generic
    "no renderer" a missing registration would produce.
    """

    extension: str = ".json"
    mime: str = JSON_MIME

    def __init__(self, object_type: str) -> None:
        self.object_type = object_type

    async def render(
        self, session: AsyncSession, obj: WorkspaceObject, version: Any = None
    ) -> bytes:
        raise RetiredRendering(
            message=f"object type {self.object_type!r} is retired and no longer renders"
        )


class ChatTemplateRenderer:
    """A chat template's spec as canonical JSON.

    Read-only: a template's brief and title are edited on the template itself,
    and its files are ordinary files inside its folder. There is nothing in
    this rendering a write could change that is not already editable where it
    belongs, so the rendering has no parser and cannot be written back.
    """

    object_type: str = "chat_template"
    extension: str = ".json"
    mime: str = JSON_MIME

    async def render(
        self, session: AsyncSession, obj: WorkspaceObject, version: Any = None
    ) -> bytes:
        return canonical_json(chat_template_document(obj))


class ContextMemberRenderer:
    """One derived member of a row-backed folder, rendered from the object.

    A ``.alkerachat.template`` is a folder, and the ``README.md`` inside it is
    a real, listable, openable node. Its bytes are **derived** — rendered on
    every read by the one function
    :func:`~alkera_core.files.providers.derived_members.render_member` that a
    mount and a pull already use — so the folder cannot drift from the row, an
    edit needs no rewrite, and an edit that changes nothing produces no churn
    because nothing was ever written.

    Read-only by construction: the object is edited where it belongs, never
    through the prose derived from it.
    """

    extension: str = ""
    #: A member IS its bytes on a real filesystem — it is not a stand-in for
    #: an object the way a pointer node is.
    materialization: Materialization = "bytes"

    def __init__(self, base_type: str, member: str, *, mime: str) -> None:
        self.base_type = base_type
        self.member = member
        self.object_type = context_member_type(base_type, member)
        self.mime = mime

    async def render(
        self, session: AsyncSession, obj: WorkspaceObject, version: Any = None
    ) -> bytes:
        document = {
            "schema_version": RENDERING_SCHEMA_VERSION,
            "type": self.base_type,
            "object": _object_header(obj),
            "spec": obj.spec,
        }
        return render_member(self.base_type, self.member, document)


class ResultRenderer:
    """A promoted result's rows as RFC 4180 CSV.

    Read-only: a result is the receipted output of a query, and editing the CSV
    would not change what ran.
    """

    object_type: str = "result"
    extension: str = ".csv"
    mime: str = CSV_MIME

    async def render(
        self, session: AsyncSession, obj: WorkspaceObject, version: Any = None
    ) -> bytes:
        pages = (
            (
                await session.execute(
                    select(ObjectPayloadRow)
                    .where(ObjectPayloadRow.object_id == obj.id)
                    .order_by(ObjectPayloadRow.page)
                )
            )
            .scalars()
            .all()
        )
        rows: list[Sequence[Any]] = []
        for page in pages:
            rows.extend(page.page_rows)
        return render_result(_columns_of(obj), rows)


class UnimplementedRenderer:
    """A named object type whose rows nobody renders yet."""

    extension: str = ".json"
    mime: str = JSON_MIME

    def __init__(self, object_type: str) -> None:
        self.object_type = object_type

    async def render(
        self, session: AsyncSession, obj: WorkspaceObject, version: Any = None
    ) -> bytes:
        raise NotImplementedRendering(
            message=f"object type {self.object_type!r} has no rendering yet"
        )


def context_member_renderers() -> tuple[ContextMemberRenderer, ...]:
    """A renderer for every (object type, derived member) pair.

    Derived from the one registry rather than listed, so a new member of a
    folder — or a second object type that gains derived members — is served
    without a registration written by hand here.
    """
    return tuple(
        ContextMemberRenderer(base_type, member, mime=mime)
        for base_type, members in sorted(DERIVED_MEMBERS.items())
        for member, mime in members.items()
    )


def standard_renderers(*, update_query: QuerySpecUpdate) -> RendererRegistry:
    """Every object type, registered.

    The registry is complete by construction: a type in ``OBJECT_TYPES`` with no
    renderer would be an unregistered read the first time a user opened one.
    """
    renderers = RendererRegistry()
    renderers.register(ChatRenderer())
    for object_type in RETIRED_OBJECT_TYPES:
        renderers.register(RetiredRenderer(object_type))
    renderers.register(ResultRenderer())
    renderers.register(ChatTemplateRenderer())
    for renderer in context_member_renderers():
        renderers.register(renderer)
    for object_type in UNRENDERED_TYPES:
        renderers.register(UnimplementedRenderer(object_type))
    return renderers


# ---- the provider ----------------------------------------------------------


class RowsProvider:
    """One object type's rows, behind the content interface.

    One instance per renderer, so ``kind`` is fixed at construction and the
    registry's ``rows:<object type>`` key needs no lookup at read time.
    """

    def __init__(
        self,
        repo: FilesRepo,
        renderer: RowRenderer,
        *,
        checkpoints: Checkpoints | None = None,
    ) -> None:
        self._repo = repo
        self._renderer = renderer
        self._checkpoints = checkpoints or NoopCheckpoints()
        self.kind: str = rows_kind(renderer.object_type)

    async def head(self, node: FileNode, version: VersionId | None) -> ContentInfo:
        digests = hash_bytes(await self._render(node, version))
        return ContentInfo(
            size=digests.size,
            content_hash=digests.content_hash.hex(),
            block_hash=digests.block_hash.hex(),
            mime=self._renderer.mime,
        )

    async def open(
        self,
        node: FileNode,
        version: VersionId | None,
        *,
        range: tuple[int, int] | None = None,
    ) -> AsyncIterator[bytes]:
        payload = await self._render(node, version)
        start, end = (0, len(payload) - 1) if range is None else range
        return _one(payload[start : end + 1])

    async def versions(self, node: FileNode) -> Sequence[VersionInfo]:
        """None: a rendering is derived from the rows, never stored beside them."""
        return ()

    async def write(self, node: FileNode, data: WriteBody, *, if_match: int) -> VersionInfo:
        """Turn ``data`` back into rows, or refuse before reading any of it."""
        from alkera_core.files.content import VersionInfo as _VersionInfo

        parse = getattr(self._renderer, "parse", None)
        if not callable(parse):
            raise ReadOnlyContent(
                message=f"a {self._renderer.object_type} rendering cannot be written back"
            )
        if if_match != node.etag:
            raise PreconditionFailed(
                message=f"node {node.id} is at etag {node.etag}, not {if_match}"
            )
        obj = await self._object(node)
        body = b"".join([chunk async for chunk in data.stream])
        await self._checkpoints.reach("rows.before_parse")
        await parse(self._repo.session, obj, body)
        await self._checkpoints.reach("rows.after_parse")
        rendered = await self._renderer.render(self._repo.session, obj, None)
        digests = hash_bytes(rendered)
        return _VersionInfo(
            id=VersionId(node.id),
            seq=obj.version,
            size=digests.size,
            content_hash=digests.content_hash.hex(),
            block_hash=digests.block_hash.hex(),
            mime=self._renderer.mime,
            unchanged=False,
        )

    def materialize(self, node: FileNode) -> Materialization:
        """A pointer: a mount gets something that opens the object, and a copy of
        a rendering on disk would go stale the moment the object changed.

        A replication context's members are the exception and say so on their
        renderer: they ARE the bytes a mount should write, because the folder
        around them is what stands in for the object.
        """
        declared: Materialization = getattr(self._renderer, "materialization", "pointer")
        return declared

    async def _render(self, node: FileNode, version: VersionId | None) -> bytes:
        return await self._renderer.render(self._repo.session, await self._object(node), version)

    async def _object(self, node: FileNode) -> WorkspaceObject:
        """The row this node projects, in the caller's own org.

        The org predicate is applied here and not left to the caller: a node id
        is the only thing a reader supplies, and a node that somehow named an
        object outside its org must read as a missing object, not as that org's
        rows.
        """
        object_type = object_type_of(node)
        if node.target_object_id is None or object_type != self._renderer.object_type:
            raise InvalidRequest(message=f"node {node.id} is not a {self._renderer.object_type}")
        obj = (
            (
                await self._repo.session.execute(
                    select(WorkspaceObject).where(
                        WorkspaceObject.id == node.target_object_id,
                        WorkspaceObject.org_team_id == self._repo.scope.org_team_id,
                    )
                )
            )
            .scalars()
            .first()
        )
        if obj is None:
            raise InvalidRequest(message=f"node {node.id} names no readable object")
        return obj


def register_rows_providers(
    providers: Any,
    renderers: RendererRegistry,
    repo: FilesRepo,
    *,
    checkpoints: Checkpoints | None = None,
) -> None:
    """Give every registered object type its ``rows:<type>`` provider.

    Driven by the renderer registry rather than by a list, so a renderer added
    by an object service is served without editing this module.
    """
    for object_type in renderers.object_types():
        providers.register(
            rows_kind(object_type),
            RowsProvider(repo, renderers.get(object_type), checkpoints=checkpoints),
        )


async def _one(payload: bytes) -> AsyncIterator[bytes]:
    if payload:
        yield payload
