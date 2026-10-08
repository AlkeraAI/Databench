"""The one filter object every Files listing takes, and the cursor it pages by.

`children`, `search`, `recent` and `sharedWithMe` all take the same
`ListFilters`, so a chip the UI offers on one surface is a SQL predicate on all
of them: `predicates()` is the single translation from the query object to the
`WHERE` clause, and nothing else spells a filter column.

The `Marker` is an opaque, signed keyset cursor over `(order value, id)`. It is
signed because it is handed to a client: an unsigned cursor is a position a
caller can forge into a different folder's page, and keyset pagination keeps no
server-side state to check it against.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final, Self
from uuid import UUID

from sqlalchemy import and_, exists, false, func, or_, select
from sqlalchemy.sql.expression import ColumnElement

from alkera_core.config import settings
from alkera_core.files.authz.ladder import ROLE_OWNER
from alkera_core.files.errors import InvalidRequest
from alkera_core.models.files.acl import FileShare
from alkera_core.models.files.history import FileStar
from alkera_core.models.files.leases import FileLease
from alkera_core.models.files.tree import MIME_CLASSES, NODE_KINDS, FileNode

#: `flags` is a bitfield of per-node booleans too small to earn a column. The
#: starred bit is spelled here and nowhere else.
FLAG_STARRED: Final = 1 << 0

#: The name flags computed at create; a listing may filter on any of them
#: ("show me what will not survive a Windows mount").
NAME_FLAGS: Final = ("windows_safe", "macos_safe", "display_warning")


def starred_by(node_table: Any, user_id: UUID | None) -> ColumnElement[bool]:
    """Whether ``user_id`` starred each row of ``node_table``.

    A correlated `EXISTS` rather than an outer join: it is the shape the
    `(org_team_id, user_id, node_id)` primary key answers with an index seek per
    row, and it cannot multiply a listing's rows the way a join against a table
    with a different grain can. A caller with no user behind it stars nothing,
    which is a constant the planner folds away rather than a subquery.

    It lives beside `FLAG_STARRED` because both spellings of "starred" belong to
    one module: the node bit summarises everybody, this one answers for one
    person, and a surface picking the wrong one is the bug they are kept
    together to prevent.
    """
    if user_id is None:
        return false()
    return exists(
        select(FileStar.node_id).where(
            FileStar.node_id == node_table.id,
            FileStar.user_id == user_id,
            FileStar.org_team_id == node_table.org_team_id,
        )
    )


#: Nanoseconds since the epoch is what `mtime_ns` holds.
_NS: Final = 1_000_000_000


class OrderField(StrEnum):
    """What a listing may be ordered by. The tie-break is always the id."""

    NAME = "name"
    SIZE = "size"
    MTIME = "mtime"
    KIND = "kind"


class Direction(StrEnum):
    ASC = "asc"
    DESC = "desc"


_ORDER_COLUMNS: Final[dict[OrderField, str]] = {
    OrderField.NAME: "name_key",
    OrderField.SIZE: "size",
    OrderField.MTIME: "mtime_ns",
    OrderField.KIND: "kind",
}


@dataclass(frozen=True, slots=True)
class OrderBy:
    """One ordering: a column and a direction."""

    field: OrderField = OrderField.NAME
    direction: Direction = Direction.ASC

    @property
    def column(self) -> Any:
        """The `FileNode` column this order sorts on."""
        return getattr(FileNode, _ORDER_COLUMNS[self.field])

    @property
    def descending(self) -> bool:
        return self.direction is Direction.DESC


@dataclass(frozen=True, slots=True)
class ListFilters:
    """Every chip a listing surface offers, as one value. All fields optional.

    `owner` arrives on the wire as a principal id or the literal `"me"`;
    `from_query` resolves `"me"` so nothing downstream knows about the caller.

    `caller` is the exception: a star is one person's bookmark, so the
    `starred` chip is only meaningful against somebody. It is carried on the
    filters rather than passed to `predicates()` so every surface that already
    threads `ListFilters` narrows by the *caller's* stars without a new
    argument, and a filter object built without a caller stars nothing.
    """

    #: Whose stars the `starred` chip means. `None` is a principal with no user
    #: behind it — an agent has no bookmarks, so `starred=true` matches nothing.
    caller: UUID | None = None

    kind: str | None = None
    object_type: str | None = None
    mime_class: str | None = None
    owner: UUID | None = None
    modified_after: datetime | None = None
    modified_before: datetime | None = None
    size_min: int | None = None
    size_max: int | None = None
    name_flag: str | None = None
    starred: bool | None = None
    shared: bool | None = None
    leased: bool | None = None
    trashed: bool | None = None

    @classmethod
    def from_query(cls, params: Mapping[str, str], *, me: UUID) -> Self:
        """Parse the query string, refusing anything it cannot represent.

        An unknown key is refused rather than ignored: a client that misspells
        `mimeClass` would otherwise silently receive an unfiltered page and
        show the user the wrong thing.
        """
        unknown = set(params) - set(_QUERY_KEYS)
        if unknown:
            raise InvalidRequest(
                "files.unknown_filter", f"unknown filter(s): {', '.join(sorted(unknown))}"
            )
        made = cls(caller=me)
        for key, raw in params.items():
            field, parse = _QUERY_KEYS[key]
            made = replace(made, **{field: parse(raw, me)})
        if made.size_min is not None and made.size_max is not None:
            if made.size_min > made.size_max:
                raise InvalidRequest("files.bad_filter", "sizeMin is greater than sizeMax")
        if made.modified_after is not None and made.modified_before is not None:
            if made.modified_after > made.modified_before:
                raise InvalidRequest("files.bad_filter", "modifiedAfter is after modifiedBefore")
        return made

    def predicates(self, node_table: Any = FileNode) -> list[ColumnElement[bool]]:
        """Every set filter as a SQL predicate against `node_table`.

        A filter is never applied in Python: the page is cut after filtering,
        so an excluded sibling can never change a page's size.
        """
        made: list[ColumnElement[bool]] = []
        if self.kind is not None:
            made.append(node_table.kind == self.kind)
        if self.object_type is not None:
            made.append(node_table.subtype == self.object_type)
        if self.mime_class is not None:
            made.append(node_table.mime_class == self.mime_class)
        if self.owner is not None:
            made.append(node_table.created_by == self.owner)
        if self.modified_after is not None:
            made.append(node_table.mtime_ns >= _to_ns(self.modified_after))
        if self.modified_before is not None:
            made.append(node_table.mtime_ns <= _to_ns(self.modified_before))
        if self.size_min is not None:
            made.append(node_table.size >= self.size_min)
        if self.size_max is not None:
            made.append(node_table.size <= self.size_max)
        if self.name_flag is not None:
            made.append(node_table.flags_names[self.name_flag].astext == "true")
        if self.starred is not None:
            # The caller's own rows, never the node bit: the bit says somebody
            # starred it, so filtering by it would put a colleague's bookmark
            # under this caller's "Starred".
            starred = starred_by(node_table, self.caller)
            made.append(starred if self.starred else ~starred)
        if self.shared is not None:
            shared = exists(
                select(FileShare.id).where(
                    FileShare.node_id == node_table.id,
                    FileShare.revoked_at.is_(None),
                )
            )
            made.append(shared if self.shared else ~shared)
        if self.leased is not None:
            # Live, not merely un-reaped: a lease whose TTL ran out fences its
            # own holder's writes the moment it lapses, so a folder whose
            # holder died an hour ago is not leased however long the reaper
            # takes to notice. Without the expiry the filter would hide it
            # from `leased=false` and offer it under `leased=true` until the
            # sweep, which is the one window a person would try to mount it in.
            leased = exists(
                select(FileLease.node_id).where(
                    FileLease.node_id == node_table.id,
                    FileLease.released_at.is_(None),
                    FileLease.reaped_at.is_(None),
                    FileLease.expires_at > func.now(),
                )
            )
            made.append(leased if self.leased else ~leased)
        return made


def _to_ns(when: datetime) -> int:
    moment = when if when.tzinfo is not None else when.replace(tzinfo=UTC)
    return int(moment.timestamp() * _NS)


def _parse_bool(raw: str, _me: UUID) -> bool:
    lowered = raw.strip().lower()
    if lowered in {"true", "1", "yes"}:
        return True
    if lowered in {"false", "0", "no"}:
        return False
    raise InvalidRequest("files.bad_filter", f"not a boolean: {raw!r}")


def _parse_size(raw: str, _me: UUID) -> int:
    try:
        size = int(raw)
    except ValueError:
        raise InvalidRequest("files.bad_filter", f"not a size: {raw!r}") from None
    if size < 0:
        raise InvalidRequest("files.bad_filter", f"negative size: {raw!r}")
    return size


def _parse_time(raw: str, _me: UUID) -> datetime:
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        raise InvalidRequest("files.bad_filter", f"not a timestamp: {raw!r}") from None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _parse_owner(raw: str, me: UUID) -> UUID:
    if raw == "me":
        return me
    try:
        return UUID(raw)
    except ValueError:
        raise InvalidRequest("files.bad_filter", f"not a principal id: {raw!r}") from None


def _one_of(name: str, allowed: tuple[str, ...]) -> Callable[[str, UUID], str]:
    def parse(raw: str, _me: UUID) -> str:
        if raw not in allowed:
            raise InvalidRequest("files.bad_filter", f"not a {name}: {raw!r}")
        return raw

    return parse


def _free_text(raw: str, _me: UUID) -> str:
    if not raw:
        raise InvalidRequest("files.bad_filter", "empty filter value")
    return raw


#: query key → (field name, parser). The only place a wire filter name is spelled.
#: The owner chip is the ladder's top rung, not a name of its own: it selects the
#: nodes the principal owns, so the spelling comes from the ladder rather than
#: being repeated here where a rename would miss it.
_QUERY_KEYS: Final[dict[str, tuple[str, Callable[[str, UUID], Any]]]] = {
    "kind": ("kind", _one_of("kind", NODE_KINDS)),
    "objectType": ("object_type", _free_text),
    "mimeClass": ("mime_class", _one_of("mime class", MIME_CLASSES)),
    ROLE_OWNER: (ROLE_OWNER, _parse_owner),
    "modifiedAfter": ("modified_after", _parse_time),
    "modifiedBefore": ("modified_before", _parse_time),
    "sizeMin": ("size_min", _parse_size),
    "sizeMax": ("size_max", _parse_size),
    "nameFlag": ("name_flag", _one_of("name flag", NAME_FLAGS)),
    "starred": ("starred", _parse_bool),
    "shared": ("shared", _parse_bool),
    "leased": ("leased", _parse_bool),
    "trashed": ("trashed", _parse_bool),
}

#: Every filter name a listing accepts *from the query string*, and therefore
#: exactly the query parameters a listing route declares. `ListFilters` also
#: carries fields the server derives from the principal (`caller`, whose stars
#: the `starred` chip means); those are never wire keys, because a client that
#: could name one would be choosing whose bookmarks it sees.
WIRE_KEYS: Final[frozenset[str]] = frozenset(_QUERY_KEYS)


class MarkerInvalid(InvalidRequest):
    """A marker was forged, truncated, or belongs to another query."""

    def __init__(self, message: str = "marker is not valid") -> None:
        super().__init__("files.invalid_marker", message)


@dataclass(frozen=True, slots=True)
class Marker:
    """The keyset position a page resumes from: the last row's order value and id.

    Opaque and signed on the wire. The parent and the ordering ride inside so a
    marker cut from one folder cannot be replayed against another, or against
    the same folder ordered differently — where it would silently skip rows.
    """

    parent_id: UUID
    order: OrderBy
    value: str | int
    last_id: UUID

    def encode(self) -> str:
        body = json.dumps(
            {
                "p": str(self.parent_id),
                "f": self.order.field.value,
                "d": self.order.direction.value,
                "v": self.value,
                "i": str(self.last_id),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return f"{_b64(body)}.{_b64(_sign(body))}"

    @classmethod
    def decode(cls, raw: str, *, parent_id: UUID, order: OrderBy) -> Self:
        head, _, tail = raw.partition(".")
        if not tail:
            raise MarkerInvalid()
        try:
            body = _unb64(head)
            signature = _unb64(tail)
        except (ValueError, TypeError):
            raise MarkerInvalid() from None
        # Constant-time: a forged marker must not be distinguishable by how
        # long the comparison took.
        if not hmac.compare_digest(signature, _sign(body)):
            raise MarkerInvalid()
        try:
            loaded = json.loads(body)
            last_id = UUID(loaded["i"])
        except (ValueError, KeyError, TypeError):
            raise MarkerInvalid() from None
        if loaded["p"] != str(parent_id):
            raise MarkerInvalid("marker belongs to another folder")
        if loaded["f"] != order.field.value or loaded["d"] != order.direction.value:
            raise MarkerInvalid("marker belongs to another ordering")
        return cls(parent_id=parent_id, order=order, value=loaded["v"], last_id=last_id)

    def predicate(self) -> ColumnElement[bool]:
        """The `WHERE` clause that resumes strictly after this position.

        The id tie-break is ascending in both directions: it exists so a page
        boundary landing inside a run of equal order values resumes exactly
        once per row, not to order anything a caller asked for.
        """
        column = self.order.column
        past = column < self.value if self.order.descending else column > self.value
        resumed: ColumnElement[bool] = or_(
            past, and_(column == self.value, FileNode.id > self.last_id)
        )
        return resumed


def _sign(body: bytes) -> bytes:
    key = settings.effective_files_content_signing_key.encode()
    return hmac.new(key, b"files.marker.v1:" + body, hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


__all__ = [
    "FLAG_STARRED",
    "NAME_FLAGS",
    "WIRE_KEYS",
    "Direction",
    "ListFilters",
    "Marker",
    "MarkerInvalid",
    "OrderBy",
    "OrderField",
]
