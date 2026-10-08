"""The core document store: notebooks as files on disk.

One in-memory :class:`Document` per path. Every batch is applied to memory
and the file is rewritten atomically through the format writer. Changes made
to the file outside the store come in through :meth:`FileDocumentStore.reload`,
merged per cell three ways against the last text this store wrote.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import hashlib
from collections import OrderedDict
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from alkera_notebook.document.apply import apply_ops, check_file_size, normalize
from alkera_notebook.document.convert import document_from_ir, ir_from_document
from alkera_notebook.document.editing import FocusClaim, editing_now
from alkera_notebook.document.fmt import FormatApi, default_format
from alkera_notebook.document.model import DocCell, Document
from alkera_notebook.document.ops import (
    SUBMIT_ID_PATTERN,
    CellAfterOp,
    CellNotice,
    GraphSummary,
    InsertCell,
    NotebookOp,
    NotebookOpsResult,
    SetSetting,
)
from alkera_notebook.document.store import (
    CodeSnapshot,
    DocumentChange,
    EditingInfo,
    StoredNotebook,
)
from alkera_notebook.engine.config import Clock, SystemClock
from alkera_notebook.engine.errors import ForbiddenError, NotFoundError, ReadOnlyError
from alkera_notebook.engine.models import Actor
from alkera_notebook.tree_io import Tree

SUBMIT_MEMORY = 256
CHANGE_QUEUE_MAX = 256
NOTEBOOK_SUFFIX = ".alknb.py"


@dataclass
class _Entry:
    path: str
    file: Path
    document: Document
    base: Document  # the document as last written to (or read from) the file
    disk_text: str  # the text this store last wrote or accepted
    revision: int = 0
    token: str = ""
    file_invalid: bool = False
    file_deleted: bool = False
    submits: OrderedDict[str, NotebookOpsResult] = field(default_factory=OrderedDict)
    #: The last edit of each cell, by an actor that publishes no caret.
    edits: dict[str, tuple[Actor, datetime]] = field(default_factory=dict)
    #: Where each actor that publishes a caret last said it is (``None``: in
    #: no cell), and when.
    carets: dict[str, tuple[Actor, str | None, datetime]] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _content(c: DocCell) -> tuple[Any, ...]:
    return (c.kind, c.name, c.source, c.config, c.meta, c.extra)


def _settings_state(doc: Document) -> tuple[Any, ...]:
    return (dict(doc.settings), doc.unknown_settings, doc.meta.header, dict(doc.meta.app))


#: A notebook file as the store writes it: the engine's uid only.
_FILE_MODE = 0o600


def _is_valid_python(text: str, ir: Any) -> bool:
    if any(getattr(v, "code", "") == "syntax_error" for v in ir.violations):
        return False
    try:
        ast.parse(text)
    except SyntaxError:
        return False
    return True


class FileDocumentStore:
    """A :class:`~alkera_notebook.document.store.DocumentStore` over files under ``root``."""

    def __init__(
        self, root: str | Path, *, fmt: FormatApi | None = None, clock: Clock | None = None
    ) -> None:
        self.root = Path(root).resolve()
        #: Every notebook file is read and written through this: a link in
        #: the tree (the file itself or a folder on its way) is refused.
        self._tree = Tree(self.root)
        self.fmt = fmt if fmt is not None else default_format()
        self.clock = clock if clock is not None else SystemClock()
        self._entries: dict[str, _Entry] = {}
        self._subscribers: dict[str, list[asyncio.Queue[DocumentChange | None]]] = {}
        self._closed = False

    # paths -------------------------------------------------------------

    def _resolve(self, path: str) -> tuple[str, Path]:
        """``path``'s place under the root, worked out from its names alone
        (no link is resolved): ``..`` and a path outside the root are refused."""
        try:
            names = self._tree.parts(path)
        except ValueError:
            raise ValueError(f"{path} is outside the store's root") from None
        if not names:
            raise ValueError(f"{path} is not a notebook file")
        return PurePosixPath(*names).as_posix(), self.root.joinpath(*names)

    def _read(self, rel: str) -> str:
        """The file's text, line endings read as ``\\n`` (as ``read_text`` reads
        them); never through a link."""
        text = self._tree.read_text(rel)
        return text.replace("\r\n", "\n").replace("\r", "\n")

    def _write(self, rel: str, text: str) -> None:
        self._tree.write_text(rel, text, mode=_FILE_MODE)

    def _new_id(self, doc: Document) -> str:
        cid = self.fmt.new_cell_id()
        while cid in doc.cells:
            cid = self.fmt.new_cell_id()
        return cid

    def _render(self, doc: Document) -> str:
        return self.fmt.write(ir_from_document(doc, self.fmt))

    def _stamp(self, entry: _Entry) -> None:
        entry.revision += 1
        digest = hashlib.sha256(entry.disk_text.encode("utf-8")).hexdigest()[:12]
        entry.token = f"r{entry.revision}.{digest}"

    def _graph(self, doc: Document) -> GraphSummary:
        return GraphSummary.of_analysis(
            self.fmt.analyze_code([(c.id, c.code) for c in doc.live_cells()])
        )

    def _publish(self, change: DocumentChange) -> None:
        for q in self._subscribers.get(change.path, []):
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(change)

    def _conflict_path(self, file: Path) -> Path:
        name = file.name
        stem = name[: -len(NOTEBOOK_SUFFIX)] if name.endswith(NOTEBOOK_SUFFIX) else file.stem
        stamp = self.clock.now().strftime("%Y%m%dT%H%M%S")
        return file.with_name(f"{stem}.conflict-{stamp}{NOTEBOOK_SUFFIX}")

    def _save_conflict(self, file: Path, text: str) -> Path:
        target = self._conflict_path(file)
        self._write(target.relative_to(self.root).as_posix(), text)
        return target

    # protocol ------------------------------------------------------------

    async def load(self, path: str) -> StoredNotebook:
        rel, full = self._resolve(path)
        entry = self._entries.get(rel)
        notices: list[CellNotice] = []
        if entry is None:
            try:
                text = self._read(rel)
            except FileNotFoundError as exc:
                raise NotFoundError(f"no notebook at {rel}", path=rel) from exc
            ir = self.fmt.read(text)
            doc = normalize(document_from_ir(ir, self.fmt))
            entry = _Entry(path=rel, file=full, document=doc, base=doc.clone(), disk_text=text)
            if not _is_valid_python(text, ir):
                entry.file_invalid = True
                notices.append(
                    CellNotice(kind="invalid_file", message="the file is not valid Python")
                )
            self._stamp(entry)
            self._entries[rel] = entry
        return StoredNotebook(
            path=rel, token=entry.token, document=entry.document.clone(), notices=notices
        )

    async def create(
        self,
        path: str,
        cells: Document | Sequence[InsertCell],
        settings: dict[str, Any],
        actor: Actor,
    ) -> StoredNotebook:
        if not actor.can_edit:
            raise ForbiddenError("creating a notebook needs edit rights")
        rel, full = self._resolve(path)
        if self._tree.exists(rel) or rel in self._entries:
            raise FileExistsError(rel)
        if isinstance(cells, Document):
            doc = normalize(cells.clone())
            ops: list[NotebookOp] = []
        else:
            doc = Document()
            ops = list(cells)
        ops += [SetSetting(key=k, value=v) for k, v in settings.items()]
        doc.settings.setdefault("format", doc.meta.format)
        outcome = apply_ops(doc, ops, fmt=self.fmt, new_id=lambda: self._new_id(doc))
        doc = outcome.document
        text = self._render(doc)
        check_file_size(text, max(len(ops) - 1, 0))
        self._write(rel, text)
        entry = _Entry(path=rel, file=full, document=doc, base=doc.clone(), disk_text=text)
        self._stamp(entry)
        self._entries[rel] = entry
        return StoredNotebook(path=rel, token=entry.token, document=doc.clone())

    async def apply(
        self,
        path: str,
        ops: Sequence[NotebookOp],
        base_token: str | None,
        actor: Actor,
        submit_id: str | None,
    ) -> NotebookOpsResult:
        if submit_id is not None and not SUBMIT_ID_PATTERN.match(submit_id):
            raise ValueError("submit_id must match ^[A-Za-z0-9_-]{8,48}$")
        if not actor.can_edit:
            raise ForbiddenError("editing a notebook needs edit rights")
        await self.load(path)
        rel, _ = self._resolve(path)
        entry = self._entries[rel]
        async with entry.lock:
            if submit_id is not None and submit_id in entry.submits:
                return entry.submits[submit_id].model_copy(update={"repeat": True})
            doc = entry.document
            if doc.read_only_reason:
                # Nothing was read from the file (or it is a newer format), so
                # any write would replace its text with what this store holds.
                raise ReadOnlyError(f"{rel} is read only: {doc.read_only_reason}")
            outcome = apply_ops(doc, ops, fmt=self.fmt, new_id=lambda: self._new_id(doc))
            notices = list(outcome.notices)
            if base_token is not None and base_token != entry.token:
                notices.insert(
                    0,
                    CellNotice(
                        kind="stale_base",
                        message="the document changed since base_token; applied at head",
                        data={"base_token": base_token, "head": entry.token},
                    ),
                )
            new_doc = outcome.document
            # Refused before anything changes, merge or not: the file this
            # batch leads to must stay under the cap.
            rendered = self._render(new_doc)
            check_file_size(rendered, max(len(ops) - 1, 0))
            merged = False
            if not entry.file_invalid and self._disk_changed(entry):
                # The file changed outside since we last wrote it: merge it into
                # the batch's result instead of overwriting it.
                entry.document = new_doc
                merge_notices, _ = self._reload_locked(entry)
                notices += merge_notices
                new_doc = entry.document
                merged = not entry.file_invalid and not entry.file_deleted
            if not merged:
                text = rendered
                if entry.file_invalid and self._tree.exists(entry.path):
                    # Keep the person's unparsable file before replacing it.
                    previous = self._read(entry.path)
                    if previous != text:
                        saved = self._save_conflict(entry.file, previous)
                        notices.append(
                            CellNotice(
                                kind="external_conflict",
                                message="the invalid file was saved beside the notebook",
                                data={"saved_as": saved.name},
                            )
                        )
                self._write(entry.path, text)
                entry.document = new_doc
                entry.base = new_doc.clone()
                entry.disk_text = text
                entry.file_invalid = False
                entry.file_deleted = False
                self._stamp(entry)
            now = self.clock.now()
            if actor.id not in entry.carets:
                for cid in outcome.touched:
                    entry.edits[cid] = (actor, now)
            cells = [
                CellAfterOp(id=c.id, name=c.name, kind=c.kind, index=i)
                for i, c in enumerate(new_doc.live_cells())
            ]
            cells += [
                CellAfterOp(
                    id=cid,
                    name=new_doc.cells[cid].name,
                    kind=new_doc.cells[cid].kind,
                    index=None,
                    deleted=True,
                )
                for cid in outcome.touched
                if new_doc.cells[cid].deleted
            ]
            result = NotebookOpsResult(
                token=entry.token,
                repeat=False,
                cells=cells,
                created=list(outcome.created),
                notices=notices,
                graph=self._graph(new_doc),
            )
            if submit_id is not None:
                entry.submits[submit_id] = result
                while len(entry.submits) > SUBMIT_MEMORY:
                    entry.submits.popitem(last=False)
        self._publish(
            DocumentChange(
                path=rel,
                token=entry.token,
                actor_id=actor.id,
                cell_ids=tuple(outcome.touched),
                notices=tuple(notices),
                origin="ops",
            )
        )
        return result

    async def snapshot(self, path: str, cell_ids: Sequence[str] | None = None) -> CodeSnapshot:
        stored = await self.load(path)
        wanted = set(cell_ids) if cell_ids is not None else None
        cells = [
            (c.id, c.code) for c in stored.document.live_cells() if wanted is None or c.id in wanted
        ]
        return CodeSnapshot(token=stored.token, cells=cells)

    async def focus(self, path: str, actor: Actor, cell_id: str | None) -> None:
        """``actor``'s caret is in ``cell_id`` now (``None``: in no cell).
        Called again while it stands there, it confirms the caret. From its
        first call on the actor is wherever its caret is, so its edits stop
        counting as it being in the cells it edited."""
        await self.load(path)
        rel, _ = self._resolve(path)
        entry = self._entries[rel]
        entry.carets[actor.id] = (actor, cell_id, self.clock.now())
        for cid, (editor, _at) in list(entry.edits.items()):
            if editor.id == actor.id:
                del entry.edits[cid]

    async def editing(self, path: str) -> dict[str, list[EditingInfo]]:
        rel, _ = self._resolve(path)
        entry = self._entries.get(rel)
        if entry is None:
            return {}
        claims = [
            FocusClaim(actor.id, actor.display_name, actor.kind, cid, at, caret=False)
            for cid, (actor, at) in entry.edits.items()
        ] + [
            FocusClaim(actor.id, actor.display_name, actor.kind, cid, at, caret=True)
            for actor, cid, at in entry.carets.values()
            if cid is not None
        ]
        found = editing_now(claims, now=self.clock.now())
        for cid in [c for c in entry.edits if c not in found]:
            del entry.edits[cid]
        return found

    def changes(self, path: str) -> AsyncIterator[DocumentChange]:
        """Changes to ``path`` from now on. Subscribes at call time; bounded, oldest dropped."""
        rel, _ = self._resolve(path)
        q: asyncio.Queue[DocumentChange | None] = asyncio.Queue(maxsize=CHANGE_QUEUE_MAX)
        self._subscribers.setdefault(rel, []).append(q)

        async def stream() -> AsyncIterator[DocumentChange]:
            try:
                while not self._closed or not q.empty():
                    item = await q.get()
                    if item is None:
                        return
                    yield item
            finally:
                subs = self._subscribers.get(rel, [])
                if q in subs:
                    subs.remove(q)

        return stream()

    async def close(self) -> None:
        self._closed = True
        for subs in self._subscribers.values():
            for q in subs:
                if q.full():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        q.get_nowait()
                q.put_nowait(None)

    # outside changes -----------------------------------------------------

    def _disk_changed(self, entry: _Entry) -> bool:
        try:
            return self._read(entry.path) != entry.disk_text
        except FileNotFoundError:
            return False

    async def reload(self, path: str) -> list[CellNotice]:
        """Merge the file's current text into memory. Memory wins a conflict."""
        await self.load(path)
        rel, _ = self._resolve(path)
        entry = self._entries[rel]
        async with entry.lock:
            notices, changed = self._reload_locked(entry)
        if notices or changed:
            origin: Literal["external", "deleted"] = "deleted" if entry.file_deleted else "external"
            self._publish(
                DocumentChange(
                    path=rel,
                    token=entry.token,
                    actor_id=None,
                    cell_ids=tuple(changed),
                    notices=tuple(notices),
                    origin=origin,
                )
            )
        return notices

    def _reload_locked(self, entry: _Entry) -> tuple[list[CellNotice], list[str]]:
        try:
            text = self._read(entry.path)
        except FileNotFoundError:
            if entry.file_deleted:
                return [], []
            entry.file_deleted = True
            return [CellNotice(kind="file_deleted", message="the file was deleted")], []
        entry.file_deleted = False
        if text == entry.disk_text:
            return [], []
        mem = entry.document
        known = {c.id: c.code for c in entry.base.cells.values()}
        known.update({c.id: c.code for c in mem.cells.values()})
        ir = self.fmt.read(text, known=known)
        if not _is_valid_python(text, ir):
            entry.file_invalid = True
            return [CellNotice(kind="invalid_file", message="the file is not valid Python")], []
        if ir.read_only_reason and not mem.read_only_reason:
            # The file is no longer a notebook this reader can read. Merging
            # what was read (no cells) would delete every cell; the notebook
            # in memory is kept, and the file's text is saved beside it before
            # the next write replaces it.
            entry.file_invalid = True
            return [
                CellNotice(
                    kind="invalid_file",
                    message=f"the file is no longer a notebook ({ir.read_only_reason})",
                    data={"reason": ir.read_only_reason},
                )
            ], []
        entry.file_invalid = False
        theirs = document_from_ir(ir, self.fmt)
        if mem.read_only_reason:
            # Memory held nothing it could change; the file is the notebook now.
            entry.document = normalize(theirs)
            entry.base = entry.document.clone()
            entry.disk_text = text
            self._stamp(entry)
            return [], list(entry.document.order)
        base = entry.base
        merged = mem.clone()
        changed: list[str] = []
        conflicts: list[str] = []

        for cid in theirs.order:
            t = theirs.cells[cid]
            m = merged.cells.get(cid)
            b = base.cells.get(cid)
            b_live = b is not None and not b.deleted
            if m is None:
                merged.cells[cid] = DocCell(
                    id=cid,
                    kind=t.kind,
                    name=t.name,
                    source=t.source,
                    code=t.code,
                    config=dict(t.config),
                    meta=dict(t.meta),
                    extra=dict(t.extra),
                )
                changed.append(cid)
                continue
            file_changed = not b_live or b is None or _content(t) != _content(b)
            mem_changed = b is None or m.deleted != (not b_live) or _content(m) != _content(b)
            if not file_changed:
                continue
            if _content(m) == _content(t) and not m.deleted:
                continue
            if not mem_changed:
                m.kind, m.name, m.source, m.code = t.kind, t.name, t.source, t.code
                m.config, m.meta, m.extra = dict(t.config), dict(t.meta), dict(t.extra)
                m.deleted = False
                changed.append(cid)
            else:
                conflicts.append(cid)

        theirs_ids = set(theirs.order)
        for cid in base.order:
            if cid in theirs_ids:
                continue
            m = merged.cells.get(cid)
            b = base.cells[cid]
            if m is None or m.deleted:
                continue
            if _content(m) == _content(b):
                i = merged.order.index(cid)
                m.deleted_after = merged.order[i - 1] if i > 0 else None
                m.deleted = True
                merged.order.remove(cid)
                changed.append(cid)

        # Order: take the file's when memory kept the base order.
        live_mem = [i for i in merged.order if not merged.cells[i].deleted]
        if [i for i in mem.order] == [i for i in base.order]:
            new_order = [i for i in theirs.order if not merged.cells[i].deleted]
            new_order += [i for i in live_mem if i not in new_order]
        else:
            new_order = list(live_mem)
            for pos, cid in enumerate(theirs.order):
                if cid in new_order or merged.cells[cid].deleted:
                    continue
                prev = theirs.order[pos - 1] if pos > 0 else None
                at = new_order.index(prev) + 1 if prev in new_order else len(new_order)
                new_order.insert(at, cid)
        merged.order[:] = new_order

        # Settings and header, merged as one value.
        if _settings_state(theirs) != _settings_state(base):
            if _settings_state(mem) == _settings_state(base):
                merged.settings = dict(theirs.settings)
                merged.unknown_settings = theirs.unknown_settings
                merged.meta.header = theirs.meta.header
                merged.meta.app = dict(theirs.meta.app)
            elif _settings_state(mem) != _settings_state(theirs):
                conflicts.append("")

        notices: list[CellNotice] = []
        if conflicts:
            saved = self._save_conflict(entry.file, text)
            for cid in conflicts:
                notices.append(
                    CellNotice(
                        kind="external_conflict",
                        cell_id=cid or None,
                        message="the file changed here too; this version was kept",
                        data={"saved_as": saved.name},
                    )
                )
        normalize(merged)
        canonical = self._render(merged)
        if canonical != text:
            self._write(entry.path, canonical)
        entry.document = merged
        entry.base = merged.clone()
        entry.disk_text = canonical
        self._stamp(entry)
        return notices, changed
