"""POSIX attributes: stored and round-tripped verbatim, never evaluated for access.

Two shapes live here and they are deliberately different classes.
:class:`NodeAttrs` is the persisted snapshot (JSONB on the history row, the
payload's ``attrs`` facet) and therefore a :class:`~alkera_core.versioning.VersionedModel`
with ``extra="allow"``. :class:`AttrsPatch` is the in-flight wire shape of a
``PATCH`` and is a plain ``BaseModel`` with ``extra="forbid"``, which is what
makes ``ctime`` refused rather than silently ignored: ``ctime`` is server-derived,
so a client that tries to set it is confused about who owns the value and gets
told, instead of writing a value that is discarded.

Extended attribute values are arbitrary bytes, so they travel as base64 in JSON
and as ``bytes`` in Python. The limits (255-byte names, 64 KiB per value and
64 KiB per node), the stored base64 spelling and the ``user.`` namespace rule
live in the library (``alkera_core.files.attrs``), because the library writes
that column and may never import its own API shapes; this module re-exports them
so a caller reads one name from one place.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, ClassVar

from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
)
from pydantic.alias_generators import to_camel

from alkera_core.files.attrs import (
    MAX_XATTR_NAME_BYTES,
    MAX_XATTR_TOTAL_BYTES,
    MAX_XATTR_VALUE_BYTES,
    USER_XATTR_PREFIX,
    stored_xattrs,
    validate_xattrs,
)
from alkera_core.versioning import VersionedModel


def _to_bytes(value: Any) -> Any:
    """Accept raw bytes (Python callers) or base64 text (a JSON document)."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return base64.b64decode(value.encode("ascii"), validate=True)
    return value


def _to_base64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


Base64Bytes = Annotated[
    bytes,
    BeforeValidator(_to_bytes),
    PlainSerializer(_to_base64, return_type=str, when_used="json"),
]
"""Opaque bytes that survive a JSON round trip without being mangled by an encoding."""


class NodeAttrs(VersionedModel):
    """The persisted attribute snapshot of one node.

    Every field is optional: a snapshot of a node that never carried POSIX
    metadata (an object node, a folder created through the web) stores nothing
    rather than a row of zeros that a materializer would then apply.

    ``ctime_ns`` is deliberately absent. It is derived by the server from the
    row's own mutation time, so persisting a client-supplied copy would create a
    second, disagreeing source of truth.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    mode: int | None = None
    uid: int | None = None
    gid: int | None = None
    #: The principal id that accompanies the numeric ``uid`` across machines.
    owner: str | None = None
    atime_ns: int | None = None
    mtime_ns: int | None = None
    birthtime_ns: int | None = None
    nlink: int | None = None
    rdev: int | None = None
    xattrs: dict[str, Base64Bytes] = Field(default_factory=dict)

    @field_validator("xattrs")
    @classmethod
    def _limits(cls, xattrs: dict[str, bytes]) -> dict[str, bytes]:
        return validate_xattrs(xattrs)


def _to_ns(value: Any) -> Any:
    """A wire timestamp as POSIX nanoseconds.

    The read side (``AttrsFacet``) renders ``mtime``/``atime``/``birthtime`` as
    RFC 3339 datetimes, so the patch takes the same spelling and the same value
    back: a client edits what it read instead of translating the field name and
    the unit itself. Raw nanoseconds still work for the library callers that
    have them.
    """
    if isinstance(value, datetime):
        return int(value.timestamp() * 1_000_000_000)
    if isinstance(value, str):
        return int(datetime.fromisoformat(value).timestamp() * 1_000_000_000)
    return value


Nanoseconds = Annotated[int, BeforeValidator(_to_ns)]
"""POSIX nanoseconds, accepting the datetime the read side rendered."""


class AttrsPatch(BaseModel):
    """The in-flight ``PATCH`` body. ``extra="forbid"`` is the refusal of ``ctime``.

    Every field is spelled as the ``attrs`` facet spells it on the way out
    (camelCase over the wire, snake_case for Python callers), so what a client
    reads is what it may send back. ``mtime``/``atime``/``birthtime`` are the
    datetimes the facet rendered; the ``…Ns`` spellings stay accepted for the
    callers that hold raw nanoseconds.
    """

    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, populate_by_name=True)

    mode: int | None = None
    uid: int | None = None
    gid: int | None = None
    owner: str | None = None
    atime_ns: Nanoseconds | None = Field(
        default=None, validation_alias=AliasChoices("atime", "atimeNs", "atime_ns")
    )
    mtime_ns: Nanoseconds | None = Field(
        default=None, validation_alias=AliasChoices("mtime", "mtimeNs", "mtime_ns")
    )
    birthtime_ns: Nanoseconds | None = Field(
        default=None, validation_alias=AliasChoices("birthtime", "birthtimeNs", "birthtime_ns")
    )
    nlink: int | None = None
    rdev: int | None = None
    xattrs: dict[str, Base64Bytes] | None = None

    @field_validator("xattrs")
    @classmethod
    def _limits(cls, xattrs: dict[str, bytes] | None) -> dict[str, bytes] | None:
        return None if xattrs is None else validate_xattrs(xattrs)


def _attrs_example() -> NodeAttrs:
    return NodeAttrs(
        mode=0o100644,
        uid=501,
        gid=20,
        owner="e7c0f4a4-1f8e-4a6e-9b06-1f3d1e1a0001",
        atime_ns=1_767_268_800_000_000_000,
        mtime_ns=1_767_268_800_123_456_789,
        birthtime_ns=1_767_182_400_000_000_000,
        nlink=1,
        rdev=0,
        xattrs={"user.com.apple.metadata:_kMDItemUserTags": b"Red\nImportant"},
    )


FIXTURE_EXAMPLES: list[tuple[str, Callable[[], VersionedModel]]] = [
    ("node_attrs", _attrs_example),
]


__all__ = [
    "FIXTURE_EXAMPLES",
    "MAX_XATTR_NAME_BYTES",
    "MAX_XATTR_TOTAL_BYTES",
    "MAX_XATTR_VALUE_BYTES",
    "USER_XATTR_PREFIX",
    "AttrsPatch",
    "Base64Bytes",
    "Nanoseconds",
    "NodeAttrs",
    "stored_xattrs",
    "validate_xattrs",
]
