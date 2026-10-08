"""The ``notebook`` document type, as the sandbox sees it.

A notebook's live document is a tree, not one text::

    root
      meta: Map      format: str; generated_with: str; header: Text; app: Map
      settings: Map  reactivity; dataframe; env; outputs_in_git; autoreload; unknown: Text
      order: MovableList<str>
      cells: Map<id, Map>
        kind: str; name: str; source: Text; config: Map; meta: Map; extra: Map; deleted: bool
      apps: Map      reserved, empty

The file at rest is the ``.alknb.py`` text; turning one into the other is the
format API's (``alkera_notebook.format``: ``read`` and ``write``), which this
module calls and never reimplements. It runs only in the sandbox worker (it
imports Loro), on whatever interpreter the worker pool runs.

* **Validation** is by path. Every operation's container is resolved to the
  path it was created at (a container's creating operation never changes, so
  a source text keeps its path after ``set_kind`` swapped it out and late
  typing still lands there), checked against the schema above, and the cells
  the update touched are checked as they stand after it.
* **Seed** reads the file into a fresh document. **Render** writes the
  document's normal form back through the format writer.
* **Merge** of a text written outside the session (a box, an upload, a
  ``git pull``) reads it with the base's cells as known prior state, so a
  file whose ids were stripped keeps them, then diffs it into the base cell by
  cell: character edits into each source (concurrent typing survives), soft
  deletes (never with ``keep``), and reorders with as few moves as it can.
* **Normalization** brings any merged state back to normal form: live cells
  missing from ``order`` are appended, duplicate ids keep the first, deleted
  cells leave ``order``, a second ``setup`` becomes ``python`` and a
  ``setup`` that is not first moves first. It is idempotent.
* **Operations** (:func:`apply_ops`) are the agent and box peers' edits,
  applied on a fork at the version their token names, so they are concurrent
  with everything typed since, and merged into the head.
"""

from __future__ import annotations

import difflib
import functools
import hashlib
import json
import keyword
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

import loro
from loro import (
    CounterSpan,
    IdSpan,
    LoroDoc,
    LoroMap,
    LoroMovableList,
    LoroText,
)

from backend.services.crdt.sandbox import core
from backend.services.crdt.sandbox.core import Refused, SandboxError, guard, rewrite_text

CELL_ID_RE: Final = re.compile(r"^[0-9a-hjkmnp-tv-z]{10}$")
KINDS: Final = frozenset({"setup", "python", "function", "class", "sql", "markdown", "unparsable"})
#: marimo's cell configuration: key -> the type its value has.
CONFIG_TYPES: Final[Mapping[str, type]] = {
    "column": int,
    "disabled": bool,
    "hide_code": bool,
    "expand_output": bool,
}
#: The ``meta`` keys each kind may carry.
META_KEYS: Final[Mapping[str, frozenset[str]]] = {
    "sql": frozenset({"output_var", "connection", "engine", "show_output"}),
    "markdown": frozenset({"quote"}),
}
#: The text of a cell the format reader kept exactly as written (any kind).
VERBATIM: Final = "verbatim"
ALL_META_KEYS: Final = frozenset({VERBATIM}).union(*META_KEYS.values())
CONNECTION_RE: Final = re.compile(r"^[\w .-]{1,128}$")
EXTRA_KEY_RE: Final = re.compile(r"^alkera_[a-z][a-z0-9_]{0,63}$")
APP_KEY_RE: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


@functools.cache
def _setting_specs() -> Mapping[str, Any]:
    """The notebook's settings, as the format owns them."""
    return {spec.name: spec for spec in load_format().NOTEBOOK_SETTINGS}


def setting_names() -> tuple[str, ...]:
    """Every known header setting, in the order the format writes them."""
    return tuple(_setting_specs())


MAX_CELLS: Final = 2000
MAX_SOURCE_BYTES: Final = 1024 * 1024
#: The header outside the fence and the unknown settings' raw lines.
MAX_FREE_TEXT_BYTES: Final = 1024 * 1024
MAX_SCALAR_BYTES: Final = 4096
#: The largest file a session seeds from or merges.
MAX_FILE_BYTES: Final = 4 * 1024 * 1024
#: How much larger than its texts a rendered file can be: per byte of text
#: (string escapes, at worst ``\xNN`` for one byte, plus indentation), per
#: line (indentation inside a cell's function or call), per cell (its
#: decorator, signature and return) and per file (imports, the app, the run
#: guard). A file under this bound is under the cap without rendering it.
_RENDER_PER_BYTE: Final = 6
_RENDER_PER_LINE: Final = 16
_RENDER_PER_CELL: Final = 1024
_RENDER_FIXED: Final = 8192
#: The largest render bound for which a keystroke's projection renders the
#: document for its real hash (anything larger waits for the write back).
CHEAP_RENDER_BYTES: Final = 256 * 1024
#: The ``sha256`` a projection carries until the document is rendered: no
#: file's hash, so the session reads as unsaved until a pass settles it.
UNRENDERED: Final = "unrendered"
#: How long an agent's caret stays at the end of its last edit.
AGENT_CARET_MS: Final = 10_000

_CONTAINER_MARK: Final = "\U0001f99c:"
_ROOT_RE: Final = re.compile(r"^cid:root-(?P<name>[^:]+):(?P<type>\w+)$")
_NORMAL_RE: Final = re.compile(r"^cid:(?P<counter>\d+)@(?P<peer>\d+):(?P<type>\w+)$")
_ROOTS: Final[Mapping[str, str]] = {
    "meta": "Map",
    "settings": "Map",
    "order": "MovableList",
    "cells": "Map",
    "apps": "Map",
}
#: ``(parent shape, key) -> the container type a child there must be``.
_CHILD_TYPES: Final[Mapping[tuple[str, str], str]] = {
    ("meta", "header"): "Text",
    ("meta", "app"): "Map",
    ("settings", "unknown"): "Text",
    ("cell", "source"): "Text",
    ("cell", "config"): "Map",
    ("cell", "meta"): "Map",
    ("cell", "extra"): "Map",
}

Path = tuple[str, ...]


# ---------------------------------------------------------------------------
# The format API
# ---------------------------------------------------------------------------


def load_format() -> Any:
    """The format API module. Raises :class:`SandboxError`
    (``format_unavailable``) when the worker's interpreter cannot import it."""
    try:
        import alkera_notebook.format as notebook_format
    except ImportError as exc:
        raise SandboxError("format_unavailable", f"alkera_notebook.format: {exc}") from None
    return notebook_format


# ---------------------------------------------------------------------------
# Reading the document
# ---------------------------------------------------------------------------


def child(container: Any, key: str) -> Any:
    """``container[key]``: a container, a plain value, or ``None``."""
    got = container.get(key)
    if got is None:
        return None
    if isinstance(got, loro.ValueOrContainer.Container):
        return got.container
    return got.value


def text_of(container: Any, key: str) -> str:
    found = child(container, key)
    return str(found.to_string()) if isinstance(found, LoroText) else ""


def _plain(value: Any) -> bool:
    """A JSON scalar, or a list of them (marimo's ``auto_download``)."""
    if isinstance(value, list):
        return all(_plain(v) and not isinstance(v, list) for v in value)
    return value is None or isinstance(value, (str, int, float, bool))


def plain_map(container: Any, key: str) -> dict[str, Any]:
    found = child(container, key)
    if not isinstance(found, LoroMap):
        return {}
    value = found.get_value()
    return {k: v for k, v in (value.items() if isinstance(value, dict) else []) if _plain(v)}


def as_str(value: Any, default: str) -> str:
    return value if isinstance(value, str) else default


@dataclass(frozen=True, slots=True)
class Cell:
    """One cell as the document holds it (read leniently: a field that is
    missing or mistyped reads as its default, so reading never fails)."""

    id: str
    kind: str
    name: str
    source: str
    config: dict[str, Any]
    meta: dict[str, Any]
    extra: dict[str, Any]
    deleted: bool


def read_cell(doc: LoroDoc, cell_id: str) -> Cell | None:
    found = child(doc.get_map("cells"), cell_id)
    if not isinstance(found, LoroMap):
        return None
    kind = as_str(child(found, "kind"), "python")
    return Cell(
        id=cell_id,
        kind=kind if kind in KINDS else "python",
        name=as_str(child(found, "name"), "_"),
        source=text_of(found, "source"),
        config=plain_map(found, "config"),
        meta=plain_map(found, "meta"),
        extra=plain_map(found, "extra"),
        deleted=child(found, "deleted") is True,
    )


def all_cells(doc: LoroDoc) -> dict[str, Cell]:
    cells: dict[str, Cell] = {}
    for cell_id in doc.get_map("cells").keys():
        found = read_cell(doc, str(cell_id))
        if found is not None:
            cells[found.id] = found
    return cells


def raw_order(doc: LoroDoc) -> list[str]:
    value = doc.get_movable_list("order").get_value()
    return [str(item) for item in value] if isinstance(value, list) else []


def live_order(doc: LoroDoc, cells: Mapping[str, Cell] | None = None) -> list[str]:
    """The live cells in the order normalization would leave them in."""
    cells = all_cells(doc) if cells is None else cells
    seen: set[str] = set()
    order: list[str] = []
    for cell_id in raw_order(doc):
        cell = cells.get(cell_id)
        if cell is None or cell.deleted or cell_id in seen:
            continue
        seen.add(cell_id)
        order.append(cell_id)
    # A live cell nobody placed: at the end, in id order (deterministic).
    order.extend(sorted(c for c, cell in cells.items() if not cell.deleted and c not in seen))
    setups = [c for c in order if cells[c].kind == "setup"]
    if setups and order[0] != setups[0]:
        order.remove(setups[0])
        order.insert(0, setups[0])
    return order


def effective_kind(cell: Cell, first_setup: str | None) -> str:
    """A second ``setup`` reads as ``python``."""
    if cell.kind == "setup" and cell.id != first_setup:
        return "python"
    return cell.kind


def settings_of(doc: LoroDoc) -> dict[str, Any]:
    root = doc.get_map("settings")
    found: dict[str, Any] = {}
    for key in setting_names():
        value = child(root, key)
        if value is not None and setting_ok(key, value):
            found[key] = value
    return found


def setting_ok(key: str, value: Any) -> bool:
    spec = _setting_specs().get(key)
    return spec is not None and bool(spec.valid(value))


# ---------------------------------------------------------------------------
# The strategy
# ---------------------------------------------------------------------------


class _Paths:
    """Container id -> the path it was created at, from its creating
    operation (immutable, so memoized for the call)."""

    def __init__(self, doc: LoroDoc) -> None:
        self._doc = doc
        self._known: dict[str, Path | None] = {}

    def path(self, cid: str, depth: int = 0) -> Path | None:
        if cid in self._known:
            return self._known[cid]
        found = self._resolve(cid, depth)
        self._known[cid] = found
        return found

    def _resolve(self, cid: str, depth: int) -> Path | None:
        root = _ROOT_RE.match(cid)
        if root is not None:
            name = root["name"]
            return (name,) if _ROOTS.get(name) == root["type"] else None
        normal = _NORMAL_RE.match(cid)
        if normal is None or depth > 4:
            return None
        peer, counter = int(normal["peer"]), int(normal["counter"])
        try:
            raw = guard(
                functools.partial(
                    self._doc.export_json_in_id_span,
                    IdSpan(peer, CounterSpan(counter, counter + 1)),
                ),
                "reject",
            )
        except SandboxError:
            return None
        changes = json.loads(raw) if isinstance(raw, str) else raw
        for change in changes if isinstance(changes, list) else []:
            for op in change.get("ops") or []:
                if not isinstance(op, dict) or op.get("counter") != counter:
                    continue
                content = op.get("content")
                if (
                    not isinstance(content, dict)
                    or content.get("type") != "insert"
                    or content.get("value") != _CONTAINER_MARK + cid
                    or not isinstance(content.get("key"), str)
                ):
                    return None
                parent = self.path(str(op.get("container")), depth + 1)
                if parent is None:
                    return None
                child = (*parent, content["key"])
                return child if _child_type(child) == normal["type"] else None
        return None


def _shape(path: Path) -> str | None:
    """The schema position a path is: ``meta``, ``meta/header``, ...,
    ``cell``, ``cell/source``, ...; ``None`` for a path the schema lacks."""
    if len(path) == 1:
        return path[0] if path[0] in _ROOTS else None
    if path[0] == "cells":
        if not CELL_ID_RE.match(path[1]):
            return None
        if len(path) == 2:
            return "cell"
        if len(path) == 3 and ("cell", path[2]) in _CHILD_TYPES:
            return f"cell/{path[2]}"
        return None
    if len(path) == 2 and (path[0], path[1]) in _CHILD_TYPES:
        return f"{path[0]}/{path[1]}"
    return None


def _child_type(path: Path) -> str | None:
    """The container type the schema wants at ``path``."""
    if len(path) == 2 and path[0] == "cells":
        return "Map" if CELL_ID_RE.match(path[1]) else None
    parent = _shape(path[:-1])
    if parent is None:
        return None
    return _CHILD_TYPES.get((parent, path[-1]))


def _scalar(value: Any) -> bool:
    if isinstance(value, str):
        return not value.startswith(_CONTAINER_MARK) and len(value.encode()) <= MAX_SCALAR_BYTES
    return value is None or isinstance(value, (bool, int, float))


def _created(value: Any, op_id: str, kind: str) -> bool:
    """Whether a map value is the container this very operation created."""
    return bool(value == f"{_CONTAINER_MARK}cid:{op_id}:{kind}")


_TEXT_OPS: Final = frozenset({"insert", "delete"})
_MAP_OPS: Final = frozenset({"insert", "delete"})
_LIST_OPS: Final = frozenset({"insert", "delete", "move", "set"})
_META_SCALARS: Final = frozenset({"format", "generated_with"})
_CELL_SCALARS: Final[Mapping[str, type]] = {"kind": str, "name": str, "deleted": bool}


def _check_op(shape: str, content: Mapping[str, Any], op_id: str) -> str:
    """The rule one operation breaks at a container of ``shape``, or ``""``."""
    kind = content.get("type")
    if shape in ("meta/header", "settings/unknown", "cell/source"):
        return "" if kind in _TEXT_OPS else "op_type"
    if shape == "order":
        if kind not in _LIST_OPS:
            return "op_type"
        if kind == "insert":
            values = content.get("value")
            if not isinstance(values, list) or not all(
                isinstance(v, str) and CELL_ID_RE.match(v) for v in values
            ):
                return "order_value"
        if kind == "set":
            value = content.get("value")
            if not isinstance(value, str) or not CELL_ID_RE.match(value):
                return "order_value"
        return ""
    if shape == "apps":
        return "reserved"
    if kind not in _MAP_OPS:
        return "op_type"
    key = content.get("key")
    if not isinstance(key, str):
        return "key"
    value = content.get("value")
    container_child = ("cell" if shape == "cell" else shape, key)
    if shape in ("meta", "settings", "cell") and container_child in _CHILD_TYPES:
        # A structural slot: written once, as the container it must be.
        if kind == "delete":
            return "structure"
        return "" if _created(value, op_id, _CHILD_TYPES[container_child]) else "structure"
    if shape == "cells":
        if not CELL_ID_RE.match(key):
            return "cell_id"
        if kind == "delete":
            return "structure"
        return "" if _created(value, op_id, "Map") else "structure"
    if kind == "delete":
        if shape in ("meta", "cell"):
            return "structure"
        return ""
    if shape == "meta/app":
        if not APP_KEY_RE.match(key):
            return "key"
        if isinstance(value, list):
            return "" if len(value) <= 16 and all(_scalar(v) for v in value) else "value"
        return "" if _scalar(value) else "value"
    if not _scalar(value):
        return "value"
    allowed = {
        "meta": _META_SCALARS,
        "meta/app": None,
        "settings": frozenset(setting_names()),
        "cell": frozenset(_CELL_SCALARS),
        "cell/config": frozenset(CONFIG_TYPES),
        "cell/meta": ALL_META_KEYS,
        "cell/extra": None,
    }.get(shape, frozenset())
    if shape == "cell/extra":
        return "" if EXTRA_KEY_RE.match(key) and key != "alkera_id" else "key"
    if allowed is None or key not in allowed:
        return "key"
    return ""


def _replaces_structure(base: LoroDoc, path: Path, shape: str, content: Mapping[str, Any]) -> bool:
    """Whether an insert puts a new container where the document already
    holds one: a header, the unknown settings, a cell or its maps are written
    once. A cell's source is the exception (``set_kind`` swaps it)."""
    key = content.get("key")
    if content.get("type") != "insert" or not isinstance(key, str):
        return False
    if shape == "cells":
        parent: Any = base.get_map("cells")
    elif shape in ("meta", "settings"):
        parent = base.get_map(shape)
    elif shape == "cell":
        parent = child(base.get_map("cells"), path[1])
        if key == "source" or not isinstance(parent, LoroMap):
            return False
    else:
        return False
    slot = ("cell" if shape == "cell" else shape, key)
    if shape != "cells" and slot not in _CHILD_TYPES:
        return False
    return isinstance(child(parent, key), (LoroMap, LoroText))


def config_ok(config: Mapping[str, Any]) -> bool:
    for key, value in config.items():
        want = CONFIG_TYPES.get(key)
        if want is None:
            return False
        if want is int:
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return False
        elif not isinstance(value, want):
            return False
    return True


def _meta_value_ok(key: str, value: Any) -> bool:
    if key == "output_var":
        return isinstance(value, str) and value.isidentifier() and not keyword.iskeyword(value)
    if key == "connection":
        return value is None or (isinstance(value, str) and CONNECTION_RE.match(value) is not None)
    if key == "engine":
        return value is None or (isinstance(value, str) and 0 < len(value) <= 64)
    if key == VERBATIM:
        return isinstance(value, str) and utf8_len(value) <= MAX_SOURCE_BYTES
    if key == "show_output":
        return isinstance(value, bool)
    if key == "quote":
        return value in ("r", "rf")
    return False


def meta_ok(kind: str, meta: Mapping[str, Any], written: Iterable[str]) -> bool:
    """``meta`` as it stands, for the keys this update wrote: each one the
    kind admits, with a value its rule admits. A key written earlier that the
    kind no longer admits (a concurrent kind change) is left to the editors."""
    admitted = META_KEYS.get(kind, frozenset()) | {VERBATIM}
    for key in written:
        if key not in meta:
            continue
        if key not in admitted or not _meta_value_ok(key, meta[key]):
            return False
    return True


def name_ok(name: Any) -> bool:
    return isinstance(name, str) and (
        name == "_" or (name.isidentifier() and not keyword.iskeyword(name))
    )


def _texts(value: Any) -> list[str]:
    """Every string in a plain value (keys included), for size bounds."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [t for k, v in value.items() for t in (*_texts(k), *_texts(v))]
    if isinstance(value, list | tuple):
        return [t for item in value for t in _texts(item)]
    return [str(value)] if value is not None else []


def utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class NotebookStrategy:
    """The ``notebook`` type's :class:`~core.DocStrategy`."""

    name: str = "notebook"
    max_update_ops: int = 4 * 1024 * 1024
    max_text_bytes: int = MAX_FILE_BYTES
    load: Callable[[], Any] = load_format

    # -- reading ---------------------------------------------------------------

    def project(self, doc: LoroDoc) -> dict[str, Any]:
        """``project_cheap``: counts, and the rendered file's hash only where
        rendering is cheap (an update is judged on every keystroke).

        A document whose bound is under :data:`CHEAP_RENDER_BYTES` is
        rendered (the format memoizes each cell's code, so this is the
        writer's join) and carries its file's real hash: typing that returns
        the notebook to its saved text reads as saved again. A larger one
        carries :data:`UNRENDERED`, which no file's hash equals: the session
        reads as holding unsaved edits until a pass that renders the
        document settles the hash (the write back does), so the unsaved
        sweep and the dormant expiry never take an edited notebook for a
        saved one."""
        cells = all_cells(doc)
        order = live_order(doc, cells)
        kinds: dict[str, int] = {}
        for cell_id in order:
            kind = cells[cell_id].kind
            kinds[kind] = kinds.get(kind, 0) + 1
        projection: dict[str, Any] = {
            "cells": len(order),
            "kinds": kinds,
            "source_bytes": sum(utf8_len(cells[c].source) for c in order),
            "sha256": UNRENDERED,
        }
        if self.render_bound(doc) <= CHEAP_RENDER_BYTES:
            self._hash_into(projection, doc)
        return projection

    def project_at_rest(self, doc: LoroDoc) -> dict[str, Any]:
        """:meth:`project` with the rendered file's hash whatever the size:
        for a state the lane stores once (a seed), not for every update."""
        projection = self.project(doc)
        if projection["sha256"] == UNRENDERED:
            self._hash_into(projection, doc)
        return projection

    def _hash_into(self, projection: dict[str, Any], doc: LoroDoc) -> None:
        try:
            rendered = self.render(doc)
        except (SandboxError, Refused):
            return
        projection["sha256"] = hashlib.sha256(rendered.encode("utf-8")).hexdigest()

    def render_bound(self, doc: LoroDoc) -> int:
        """An upper bound of the rendered file's size, from the texts alone
        (no render): what decides cheaply that a document is under the cap."""
        cells = all_cells(doc)
        order = live_order(doc, cells)
        text = 0
        lines = 0
        for cell_id in order:
            cell = cells[cell_id]
            pieces = [cell.source, cell.name, *(_texts(cell.meta)), *(_texts(cell.extra))]
            pieces += _texts(cell.config)
            text += sum(utf8_len(piece) for piece in pieces)
            lines += sum(piece.count("\n") + 1 for piece in pieces)
        for root_name, key in (("meta", "header"), ("settings", "unknown")):
            free = text_of(doc.get_map(root_name), key)
            text += utf8_len(free)
            lines += free.count("\n") + 1
        text += sum(utf8_len(piece) for piece in _texts(plain_map(doc.get_map("meta"), "app")))
        return (
            _RENDER_PER_BYTE * text
            + _RENDER_PER_LINE * lines
            + _RENDER_PER_CELL * len(order)
            + _RENDER_FIXED
        )

    def over_file_cap(self, doc: LoroDoc) -> bool:
        """Whether the document's rendered file passes :data:`MAX_FILE_BYTES`.
        Renders only a document whose bound does not already answer."""
        return self._size_past_cap(doc) is not None

    def _size_past_cap(self, doc: LoroDoc) -> int | None:
        """The rendered file's size when it passes :data:`MAX_FILE_BYTES`;
        ``None`` when it does not, or cannot be rendered. Renders only a
        document whose bound does not already answer."""
        if self.render_bound(doc) <= MAX_FILE_BYTES:
            return None
        try:
            size = utf8_len(self.render(doc))
        except (SandboxError, Refused):
            return None
        return size if size > MAX_FILE_BYTES else None

    def grows_past_file_cap(self, base: LoroDoc, fork: LoroDoc) -> bool:
        """Whether ``fork`` carries the rendered file past the cap: its file
        passes it, and ``base``'s either did not or was smaller (an update
        that leaves an oversized file no larger stays allowed, so it can be
        cut back down). Each state is rendered at most once: on a file this
        size one render is most of a keystroke's budget."""
        grown = self._size_past_cap(fork)
        if grown is None:
            return False
        before = self._size_past_cap(base)
        return before is None or grown > before

    def ir(self, doc: LoroDoc) -> Any:
        """The document's normal form as the format API's ``NotebookIR``."""
        fmt = self.load()
        cells = all_cells(doc)
        order = live_order(doc, cells)
        first_setup = order[0] if order and cells[order[0]].kind == "setup" else None
        meta_root = doc.get_map("meta")
        settings_root = doc.get_map("settings")
        irs = []
        for cell_id in order:
            cell = cells[cell_id]
            kind = effective_kind(cell, first_setup)
            irs.append(
                fmt.CellIR(
                    id=cell.id,
                    kind=kind,
                    name=cell.name if name_ok(cell.name) else "_",
                    source=cell.source,
                    code=fmt.render_cell(kind, cell.source, cell.meta),
                    config={k: v for k, v in cell.config.items() if config_ok({k: v})},
                    meta=cell.meta,
                    extra=cell.extra,
                    resolution="keyword",
                )
            )
        return fmt.NotebookIR(
            format=as_str(child(meta_root, "format"), "1.0"),
            header_text=text_of(meta_root, "header"),
            settings=settings_of(doc),
            unknown_settings=text_of(settings_root, "unknown"),
            app_config=plain_map(meta_root, "app"),
            generated_with=as_str(child(meta_root, "generated_with"), ""),
            cells=tuple(irs),
            violations=(),
            read_only_reason=None,
        )

    def render(self, doc: LoroDoc) -> str:
        fmt = self.load()
        text = fmt.write(self.ir(doc))
        if not isinstance(text, str):
            raise SandboxError("internal", "the format writer returned no text")
        return text

    def known(self, doc: LoroDoc) -> dict[str, str]:
        """Every cell's normalized code, deleted ones too (an id comes back
        when its code does): the prior state the reader resolves ids by."""
        fmt = self.load()
        return {
            cell.id: fmt.normalize_code(fmt.render_cell(cell.kind, cell.source, cell.meta))
            for cell in all_cells(doc).values()
        }

    # -- judging ---------------------------------------------------------------

    def judge(self, base: LoroDoc, fork: LoroDoc, changes: list[dict[str, Any]]) -> str:
        paths = _Paths(fork)
        touched: set[str] = set()
        meta_written: dict[str, set[str]] = {}
        settings = False
        for change in changes:
            peer = str(change.get("id", "")).partition("@")[2]
            for op in change["ops"]:
                path = paths.path(str(op.get("container")))
                shape = None if path is None else _shape(path)
                if path is None or shape is None:
                    return "container"
                content = op.get("content")
                if not isinstance(content, dict):
                    return "op_type"
                reason = _check_op(shape, content, f"{op.get('counter')}@{peer}")
                if not reason and _replaces_structure(base, path, shape, content):
                    reason = "structure"
                if reason:
                    return reason
                if path[0] == "cells":
                    if len(path) >= 2:
                        touched.add(path[1])
                    elif isinstance(content.get("key"), str):
                        touched.add(content["key"])
                    if shape == "cell/meta" and isinstance(content.get("key"), str):
                        meta_written.setdefault(path[1], set()).add(content["key"])
                elif path[0] == "settings" and shape == "settings":
                    settings = True
        return self._post_state(base, fork, touched, meta_written, settings)

    def _post_state(
        self,
        base: LoroDoc,
        fork: LoroDoc,
        touched: set[str],
        meta_written: Mapping[str, set[str]],
        settings: bool,
    ) -> str:
        cells_root = fork.get_map("cells")
        before_count = len(base.get_map("cells").keys())
        if len(cells_root.keys()) > max(MAX_CELLS, before_count):
            return "too_many_cells"
        for cell_id in touched:
            found = child(cells_root, cell_id)
            if not isinstance(found, LoroMap):
                return "cell"
            kind = child(found, "kind")
            if kind not in KINDS:
                return "kind"
            if not name_ok(child(found, "name")):
                return "name"
            deleted = child(found, "deleted")
            if deleted is not None and not isinstance(deleted, bool):
                return "deleted"
            source = child(found, "source")
            if not isinstance(source, LoroText):
                return "cell"
            if int(source.len_utf8) > MAX_SOURCE_BYTES:
                earlier = read_cell(base, cell_id)
                if earlier is None or utf8_len(earlier.source) < int(source.len_utf8):
                    return "source_too_large"
            for key in ("config", "meta", "extra"):
                nested = child(found, key)
                if nested is not None and not isinstance(nested, LoroMap):
                    return "cell"
            if not config_ok(plain_map(found, "config")):
                return "config"
            if not meta_ok(kind, plain_map(found, "meta"), meta_written.get(cell_id, ())):
                return "meta"
        if settings:
            root = fork.get_map("settings")
            for key in setting_names():
                value = child(root, key)
                if value is not None and not setting_ok(key, value):
                    return "setting"
        for root_name, key in (("meta", "header"), ("settings", "unknown")):
            now = text_of(fork.get_map(root_name), key)
            if utf8_len(now) > MAX_FREE_TEXT_BYTES and utf8_len(now) > utf8_len(
                text_of(base.get_map(root_name), key)
            ):
                return "text_too_large"
        if self.grows_past_file_cap(base, fork):
            return "file_too_large"
        return ""

    def describe(
        self, base: LoroDoc, fork: LoroDoc, changes: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """The cells the update touched (for the record of who edited what),
        and whether the document is still in normal form."""
        paths = _Paths(fork)
        touched: set[str] = set()
        for change in changes:
            for op in change["ops"]:
                path = paths.path(str(op.get("container")))
                if path is None or path[0] != "cells":
                    continue
                content = op.get("content")
                if len(path) >= 2:
                    touched.add(path[1])
                elif isinstance(content, dict) and isinstance(content.get("key"), str):
                    touched.add(content["key"])
        return {"touched": sorted(touched), "normal": is_normal(fork)}

    # -- writing ---------------------------------------------------------------

    def write_seed(self, doc: LoroDoc, text: str, known: Mapping[str, str] | None) -> None:
        if utf8_len(text) > MAX_FILE_BYTES:
            raise Refused("too_large")
        fmt = self.load()
        ir = fmt.read(text, known=dict(known) if known else None)
        if ir.read_only_reason:
            raise Refused(str(ir.read_only_reason))
        _write_ir(doc, ir, keep=False)

    def anchor(self, doc: LoroDoc) -> None:
        """Set ``meta.format`` to the value it holds."""
        meta = doc.get_map("meta")
        meta.insert("format", as_str(child(meta, "format"), "1.0"))
        doc.commit()

    def rewrite(self, doc: LoroDoc, current: str, target: str, *, keep: bool) -> None:
        if utf8_len(target) > MAX_FILE_BYTES:
            raise Refused("too_large")
        fmt = self.load()
        ir = fmt.read(target, known=self.known(doc))
        if ir.read_only_reason:
            raise Refused(str(ir.read_only_reason))
        _write_ir(doc, ir, keep=keep)

    def normalize(self, doc: LoroDoc) -> bool:
        return normalize(doc)

    def warm_sample(self) -> str:
        """A notebook with a cell of every kind an editor inserts. Rendering
        a SQL cell is what costs most the first time (the format compiles it,
        and marimo's visitor loads DuckDB and sqlglot to read its SQL), so
        the sample carries one and the worker pays for it before it serves."""
        fmt = self.load()
        sql = {"output_var": "_df", "connection": None, "engine": None, "show_output": True}
        cells: tuple[tuple[str, str, str, dict[str, Any]], ...] = (
            ("setup", "setup", "import alkera", {}),
            ("python", "_", "x = 1", {}),
            ("sql", "_", "SELECT category, sum(amount) AS total FROM df GROUP BY 1", sql),
            ("markdown", "_", "# Notes", {}),
        )
        return str(
            fmt.write(
                fmt.NotebookIR(
                    cells=tuple(
                        fmt.CellIR(
                            id=fmt.new_cell_id(),
                            kind=kind,
                            name=name,
                            source=source,
                            code=fmt.render_cell(kind, source, meta),
                            meta=meta,
                        )
                        for kind, name, source, meta in cells
                    )
                )
            )
        )

    def caret(self, value: Mapping[str, Any]) -> dict[str, Any]:
        if not value or not set(value) <= {"anchor", "focus", "cell"}:
            raise SandboxError("ephemeral", "a caret is an anchor, a focus and a cell")
        facts: dict[str, Any] = {}
        for key, part in value.items():
            if key == "cell":
                if not isinstance(part, str) or not CELL_ID_RE.match(part):
                    raise SandboxError("ephemeral", "a caret's cell is a cell id")
                facts["cell"] = part
                continue
            container = core.cursor_container(part)
            if not isinstance(container, loro.ContainerID.Normal) or (
                str(container.container_type) != "Text"
            ):
                raise SandboxError("ephemeral", "a caret points into a cell's text")
        return facts


NOTEBOOK: Final = NotebookStrategy()
core.register(NOTEBOOK)


# ---------------------------------------------------------------------------
# Writing an IR into a document, and normal form
# ---------------------------------------------------------------------------


def _new_map() -> LoroMap:
    # Loro's stub declares the constructors without a return annotation.
    return LoroMap()  # type: ignore[no-untyped-call]


def _new_text() -> LoroText:
    return LoroText()  # type: ignore[no-untyped-call]


def insert_map(parent: Any, key: str) -> LoroMap:
    created = parent.insert_container(key, _new_map())
    if not isinstance(created, LoroMap):  # pragma: no cover - Loro returns what it was given
        raise SandboxError("internal", "a map container came back as another type")
    return created


def insert_text(parent: Any, key: str) -> LoroText:
    created = parent.insert_container(key, _new_text())
    if not isinstance(created, LoroText):  # pragma: no cover - Loro returns what it was given
        raise SandboxError("internal", "a text container came back as another type")
    return created


def ensure_map(parent: Any, key: str) -> LoroMap:
    found = child(parent, key)
    if isinstance(found, LoroMap):
        return found
    created: LoroMap = insert_map(parent, key)
    return created


def ensure_text(parent: Any, key: str) -> LoroText:
    found = child(parent, key)
    if isinstance(found, LoroText):
        return found
    created: LoroText = insert_text(parent, key)
    return created


def set_value(container: Any, key: str, value: Any) -> None:
    if child(container, key) != value or container.get(key) is None:
        container.insert(key, value)


def sync_map(container: LoroMap, wanted: Mapping[str, Any], *, keep: bool) -> None:
    for key, value in wanted.items():
        if _plain(value):
            set_value(container, key, value)
    if not keep:
        for key in list(container.keys()):
            if str(key) not in wanted:
                container.delete(str(key))


def sync_text(container: LoroText, target: str, *, keep: bool) -> None:
    current = str(container.to_string())
    if current != target:
        rewrite_text(container, current, target, keep=keep)


def _new_cell(cells_root: LoroMap, cell: Any) -> LoroMap:
    created: LoroMap = insert_map(cells_root, cell.id)
    fill_cell(created, kind=cell.kind, name=cell.name, source=cell.source)
    sync_map(ensure_map(created, "config"), dict(cell.config), keep=False)
    sync_map(ensure_map(created, "meta"), dict(cell.meta), keep=False)
    sync_map(ensure_map(created, "extra"), json_extra(cell.extra), keep=False)
    return created


def fill_cell(cell_map: LoroMap, *, kind: str, name: str, source: str) -> None:
    cell_map.insert("kind", kind)
    cell_map.insert("name", name)
    cell_map.insert("deleted", False)
    text = insert_text(cell_map, "source")
    if source:
        text.insert(0, source)


def json_extra(extra: Mapping[str, Any]) -> dict[str, Any]:
    """Other ``alkera_*`` keywords as JSON scalars (anything else dropped)."""
    return {
        k: v for k, v in extra.items() if EXTRA_KEY_RE.match(k) and k != "alkera_id" and _scalar(v)
    }


def _write_ir(doc: LoroDoc, ir: Any, *, keep: bool) -> None:
    """Edit ``doc`` into ``ir``: what is the same is left alone, each source
    is diffed character by character, a kind change swaps the source text,
    cells the text lacks are soft-deleted (unless ``keep``), and the order
    moves as little as it can."""
    cells_in = list(ir.cells)
    if len(cells_in) > MAX_CELLS:
        raise Refused("too_large")
    if any(utf8_len(c.source) > MAX_SOURCE_BYTES for c in cells_in):
        raise Refused("too_large")
    meta = doc.get_map("meta")
    set_value(meta, "format", str(ir.format))
    set_value(meta, "generated_with", str(ir.generated_with))
    sync_text(ensure_text(meta, "header"), str(ir.header_text), keep=keep)
    sync_map(ensure_map(meta, "app"), dict(ir.app_config), keep=keep)
    settings = doc.get_map("settings")
    # What the file sets, never the defaults the reader filled in: those
    # would read back as set by the file.
    wanted = {k: v for k, v in load_format().file_settings(ir).items() if setting_ok(k, v)}
    for key, value in wanted.items():
        set_value(settings, key, value)
    if not keep:
        for key in setting_names():
            if key not in wanted and settings.get(key) is not None:
                settings.delete(key)
    sync_text(ensure_text(settings, "unknown"), str(ir.unknown_settings), keep=keep)
    cells_root = doc.get_map("cells")
    existing = all_cells(doc)
    for cell in cells_in:
        here = existing.get(cell.id)
        if here is None:
            _new_cell(cells_root, cell)
            continue
        cell_map = child(cells_root, cell.id)
        if here.kind != cell.kind:
            cell_map.insert("kind", cell.kind)
            text = insert_text(cell_map, "source")
            if cell.source:
                text.insert(0, cell.source)
        else:
            sync_text(ensure_text(cell_map, "source"), cell.source, keep=keep)
        if here.name != cell.name:
            cell_map.insert("name", cell.name)
        if here.deleted:
            cell_map.insert("deleted", False)
        sync_map(ensure_map(cell_map, "config"), dict(cell.config), keep=keep)
        sync_map(ensure_map(cell_map, "meta"), dict(cell.meta), keep=keep)
        sync_map(ensure_map(cell_map, "extra"), json_extra(cell.extra), keep=keep)
    wanted_ids = [c.id for c in cells_in]
    if not keep:
        present = set(wanted_ids)
        for cell_id, here in existing.items():
            if cell_id not in present and not here.deleted:
                child(cells_root, cell_id).insert("deleted", True)
    reorder(doc.get_movable_list("order"), wanted_ids)


def reorder(order: LoroMovableList, target: list[str]) -> None:
    """Move ``order`` so the ids of ``target`` stand in its order, with as few
    moves as the longest common run allows; ids ``target`` lacks stay put."""
    value = order.get_value()
    current = [str(v) for v in value] if isinstance(value, list) else []
    wanted = set(target)
    seq = [c for i, c in enumerate(current) if c in wanted and c not in current[:i]]
    stable: set[str] = set()
    matcher = difflib.SequenceMatcher(None, seq, target, autojunk=False)
    for block in matcher.get_matching_blocks():
        stable.update(target[block.b : block.b + block.size])
    previous: str | None = None
    for cell_id in target:
        at = current.index(cell_id) if cell_id in current else None
        if cell_id in stable and at is not None:
            previous = cell_id
            continue
        if previous is None:
            firsts = [i for i, c in enumerate(current) if c in wanted and c != cell_id]
            dest = firsts[0] if firsts else (0 if at is None else at)
        else:
            dest = current.index(previous) + 1
        if at is None:
            order.insert(dest, cell_id)
            current.insert(dest, cell_id)
        else:
            if at < dest:
                dest -= 1
            if at != dest:
                order.mov(at, dest)
                current.insert(dest, current.pop(at))
        previous = cell_id


def is_normal(doc: LoroDoc) -> bool:
    cells = all_cells(doc)
    order = raw_order(doc)
    return order == live_order(doc, cells) and _setups_normal(cells, order)


def _setups_normal(cells: Mapping[str, Cell], order: list[str]) -> bool:
    setups = [c for c in order if cells[c].kind == "setup"]
    return len(setups) <= 1 and (not setups or order[0] == setups[0])


def normalize(doc: LoroDoc) -> bool:
    """Bring ``doc`` to normal form (see the module docstring); whether it
    wrote anything. Idempotent: a second call writes nothing."""
    wrote = False
    cells = all_cells(doc)
    order_list = doc.get_movable_list("order")
    current = raw_order(doc)
    seen: set[str] = set()
    # Back to front, so each delete leaves the indexes before it standing.
    keep_flags = []
    for cell_id in current:
        cell = cells.get(cell_id)
        keep_flags.append(cell is not None and not cell.deleted and cell_id not in seen)
        seen.add(cell_id)
    for index in range(len(current) - 1, -1, -1):
        if not keep_flags[index]:
            order_list.delete(index, 1)
            wrote = True
    kept = [c for c, flag in zip(current, keep_flags, strict=True) if flag]
    for cell_id in sorted(c for c, cell in cells.items() if not cell.deleted and c not in kept):
        order_list.push(cell_id)
        kept.append(cell_id)
        wrote = True
    setups = [c for c in kept if cells[c].kind == "setup"]
    cells_root = doc.get_map("cells")
    for extra_setup in setups[1:]:
        child(cells_root, extra_setup).insert("kind", "python")
        wrote = True
    if setups and kept[0] != setups[0]:
        order_list.mov(kept.index(setups[0]), 0)
        wrote = True
    return wrote


__all__ = [
    "AGENT_CARET_MS",
    "CELL_ID_RE",
    "CHEAP_RENDER_BYTES",
    "CONFIG_TYPES",
    "KINDS",
    "MAX_CELLS",
    "MAX_FREE_TEXT_BYTES",
    "MAX_SOURCE_BYTES",
    "META_KEYS",
    "NOTEBOOK",
    "UNRENDERED",
    "Cell",
    "NotebookStrategy",
    "all_cells",
    "as_str",
    "child",
    "config_ok",
    "effective_kind",
    "ensure_map",
    "ensure_text",
    "fill_cell",
    "insert_map",
    "insert_text",
    "is_normal",
    "live_order",
    "load_format",
    "meta_ok",
    "name_ok",
    "normalize",
    "plain_map",
    "raw_order",
    "read_cell",
    "reorder",
    "set_value",
    "setting_names",
    "setting_ok",
    "settings_of",
    "sync_map",
    "sync_text",
    "text_of",
    "utf8_len",
]
