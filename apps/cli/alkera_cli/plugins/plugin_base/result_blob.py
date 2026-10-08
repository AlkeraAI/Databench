"""Canonical result-blob envelope + pagination.

Big tool results don't go inline — they spill to the content-addressed
``BlobStore`` and the model gets a *handle*, not a dump. For
that handle to be useful the model must be able to **page through it**, so both
producers (``sql.query`` and the generic ``ToolRegistry.dispatch`` spill) write
ONE canonical envelope and the hot ``fetch_result`` tool + the ``blob.fetch``
daemon RPC both page it through :func:`paginate`.

The envelope is a ``VersionedModel`` because it is persisted to the blob store —
a newer writer's blob must still be readable by an older reader (the seven
commandments). ``kind="rows"`` pages by row; ``kind="text"`` pages by character.
A blob that isn't a recognizable envelope (a legacy or opaque blob) still pages
as a raw character window, so EVERY handle is fetchable.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from alkera_core.json_safe import json_safe
from alkera_core.versioning import VersionedModel
from pydantic import Field
from pydantic_core import to_jsonable_python

if TYPE_CHECKING:
    from alkera_core.project.chats.blobs import BlobStore

logger = logging.getLogger(__name__)

#: Page-size defaults + ceilings, per ``kind``. Rows page by ROW; text pages by
#: CHARACTER, so its page must be far larger to be usable (a JSON dump read back
#: 50 chars at a time is unusable). The ceilings keep one fetch from re-flooding
#: context. ``limit=None`` (the tool/RPC default) picks the per-kind default.
DEFAULT_ROW_PAGE = 50
MAX_ROW_PAGE = 1000
DEFAULT_TEXT_PAGE = 4000
MAX_TEXT_PAGE = 50_000


class BlobHandle(VersionedModel):
    """A pointer to a spilled result in the content-addressed blob store. Handed
    to the model / editor in place of an oversized inline payload; dereferenced
    via the ``fetch_result`` tool or the ``blob.fetch`` RPC.

    Persisted ⇒ ``VersionedModel``: the handle rides a promoted result's spec
    into the cloud, so a field a newer daemon adds beside the digest has to
    survive an older reader instead of being dropped on the way through."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    sha256: str
    size: int
    media_type: str = "application/json"


class ResultBlobEnvelope(VersionedModel):
    """The canonical on-blob shape both result producers write. Persisted ⇒
    ``VersionedModel`` (fixture + lineage test). Exactly one of ``rows`` /
    ``text`` is populated per ``kind``."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    kind: Literal["rows", "text"] = "text"
    columns: list[str] = Field(default_factory=list)
    """Column names for ``kind="rows"`` (empty otherwise)."""
    rows: list[list[Any]] = Field(default_factory=list)
    """All rows for ``kind="rows"`` (empty otherwise)."""
    text: str = ""
    """The full text for ``kind="text"`` (empty otherwise)."""
    total: int = 0
    """Total rows (``kind="rows"``) or characters (``kind="text"``) — so the
    model knows the size before paging."""


def write_rows_blob(blobs: BlobStore, *, columns: list[str], rows: list[list[Any]]) -> BlobHandle:
    """Spill a tabular result. Pages by row via :func:`paginate`."""
    env = ResultBlobEnvelope(kind="rows", columns=list(columns), rows=rows, total=len(rows))
    return _write(blobs, env)


def write_text_blob(blobs: BlobStore, *, text: str) -> BlobHandle:
    """Spill an opaque/text result (e.g. a JSON-encoded tool result). Pages by
    character via :func:`paginate`."""
    env = ResultBlobEnvelope(kind="text", text=text, total=len(text))
    return _write(blobs, env)


def _write(blobs: BlobStore, env: ResultBlobEnvelope) -> BlobHandle:
    # Same mode="python" → to_jsonable_python → json_safe pipeline as Tool.invoke
    # (NOT mode="json", which nulls a non-finite float in the Any-typed rows before
    # we can wrap it). sql.query writes its rows-blob here BEFORE invoke's serialize
    # runs, so this is the only thing keeping invalid NaN/Infinity tokens out of the
    # stored bytes (a landmine for fetch_result / any strict re-reader).
    coerced = to_jsonable_python(env.model_dump(mode="python"), bytes_mode="base64")
    data = json.dumps(json_safe(coerced)).encode()
    sha, size = blobs.write(data)
    return BlobHandle(sha256=sha, size=size)


def _clamp_limit(limit: int | None, *, default: int, maximum: int) -> int:
    if limit is None or limit <= 0:
        return default
    return min(limit, maximum)


def paginate(
    blobs: BlobStore, handle: str, *, offset: int = 0, limit: int | None = None
) -> dict[str, Any]:
    """Read the blob at ``handle`` and return one fixed-size page — a pure
    reader, no byte bound.

    Returns ``{kind, columns?, rows?|text?, offset, limit, total, returned,
    has_more, next_offset}``. ``limit=None`` (the default) picks the per-kind
    default page size (rows page by row, text by character). A blob that isn't a
    :class:`ResultBlobEnvelope` (legacy / opaque) is paged as a raw UTF-8
    character window with ``kind="text"`` — so no handle is ever a dead end.
    ``has_more``/``next_offset`` follow the standard offset-pagination contract.

    Fixed-size pages are the editor RPC's contract (its numbered pager computes
    offsets from ``limit``, and no inline cap applies on that wire). The
    model-facing ``fetch_result`` tool fits the page through the delivery door
    before returning it, where the one inline bound lives.
    """
    raw = blobs.read(handle)  # FileNotFoundError surfaces to the caller as a clean tool error
    offset = max(offset, 0)
    env = _parse_envelope(raw)
    if env is None:
        # Opaque/legacy blob → character window over the decoded bytes.
        return _text_page(raw.decode("utf-8", errors="replace"), offset, limit)
    if env.kind == "rows":
        eff = _clamp_limit(limit, default=DEFAULT_ROW_PAGE, maximum=MAX_ROW_PAGE)
        page = env.rows[offset : offset + eff]
        # Cursor keys first: FetchResultOutput's declaration order carries the
        # cursor-survives-the-preview contract; the RPC consumer reads by key.
        return {
            **page_meta(offset=offset, limit=eff, total=env.total, returned=len(page)),
            "kind": "rows",
            "columns": env.columns,
            "rows": page,
        }
    return _text_page(env.text, offset, limit)


def read_envelope(blobs: BlobStore, handle: str) -> ResultBlobEnvelope | None:
    """Read the blob at ``handle`` and parse it as a :class:`ResultBlobEnvelope`,
    or return ``None`` for an opaque/legacy blob. Propagates ``FileNotFoundError``
    (missing / GC'd) and ``ValueError`` (``handle`` isn't a valid sha)."""
    return _parse_envelope(blobs.read(handle))


def load_rows_envelope(blobs: BlobStore, handle: str) -> ResultBlobEnvelope:
    """Read a blob and require it be a tabular (``kind="rows"``) envelope — the
    shape the compute tools (``blob.profile`` / ``blob.query`` / ``blob.derive``)
    operate over. The single reader they share so they can't drift on the
    envelope shape. Raises ``FileNotFoundError`` (missing / GC'd) and
    ``ValueError`` (bad sha, or the blob isn't a tabular result)."""
    env = read_envelope(blobs, handle)
    if env is None or env.kind != "rows":
        raise ValueError("not a tabular result (expected a rows blob)")
    return env


def _text_page(text: str, offset: int, limit: int | None) -> dict[str, Any]:
    eff = _clamp_limit(limit, default=DEFAULT_TEXT_PAGE, maximum=MAX_TEXT_PAGE)
    chunk = text[offset : offset + eff]
    return {
        **page_meta(offset=offset, limit=eff, total=len(text), returned=len(chunk)),
        "kind": "text",
        "text": chunk,
    }


def page_meta(*, offset: int, limit: int, total: int, returned: int) -> dict[str, Any]:
    """The one pagination law: ``has_more`` is progress-dependent (a page that
    returned nothing must not claim more, or a corrupt envelope whose ``total``
    outruns its rows pages forever), and ``next_offset`` follows what was
    actually returned. Shared with the delivery door's page fit so shrinking a
    page cannot drift from reading one."""
    has_more = returned > 0 and offset + returned < total
    return {
        "offset": offset,
        "limit": limit,
        "total": total,
        "returned": returned,
        "has_more": has_more,
        "next_offset": offset + returned if has_more else None,
    }


def _parse_envelope(raw: bytes) -> ResultBlobEnvelope | None:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("kind") not in ("rows", "text"):
        return None
    try:
        return ResultBlobEnvelope.model_validate(payload)
    except Exception:
        # It LOOKED like an envelope (right `kind`) but didn't validate — a
        # genuine envelope-schema regression, not just an opaque blob. Degrade to
        # the raw-text window (no handle is a dead end) but leave a trail so the
        # regression is observable rather than silent.
        logger.debug("blob looked like a ResultBlobEnvelope but failed validation", exc_info=True)
        return None


__all__ = [
    "DEFAULT_ROW_PAGE",
    "DEFAULT_TEXT_PAGE",
    "MAX_ROW_PAGE",
    "MAX_TEXT_PAGE",
    "BlobHandle",
    "ResultBlobEnvelope",
    "load_rows_envelope",
    "paginate",
    "read_envelope",
    "write_rows_blob",
    "write_text_blob",
]
