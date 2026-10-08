"""Outputs at rest: ``__marimo__/session/<file name>.json`` beside the notebook.

The file is marimo's ``NotebookSessionV1`` shape, so stock marimo opens it,
with an ``alkera`` extension at the top level (kernel and environment) and on
each cell (run and provenance). Values above the blob threshold live in
``<file name>.d/<sha256>.<ext>`` and are referenced by
``application/vnd.alkera.ref+json``; a reader finds a blob only from its hash
and an extension derived from its MIME type, verifies the hash, and ignores
any path a file claims. Blobs nothing references are deleted at each write.

Provenance in this file is display-only; current attribution comes from the
engine's run records.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from alkera_notebook.engine.config import Clock
from alkera_notebook.engine.models import ErrorInfo, RunActor
from alkera_notebook.outputs.provenance import code_hash
from alkera_notebook.outputs.state import (
    CHART_MIMES,
    CellOutputs,
    MimeBundle,
    RunMeta,
    StreamItem,
    value_bytes,
    with_plain,
)
from alkera_notebook.tree_io import Tree

logger = logging.getLogger(__name__)

SESSION_VERSION = "1"
ALKERA_SCHEMA_VERSION = "1.1.0"
REF_MIME = "application/vnd.alkera.ref+json"
DEFAULT_MARIMO_VERSION = "0.25.1"
BLOB_THRESHOLD = 256 * 1024

# MIME type -> blob file extension. Anything else is ``bin``.
_EXT: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "application/json": "json",
    "text/html": "html",
    "text/plain": "txt",
    "text/markdown": "md",
}
# Raster images are stored decoded; their in-memory value is base64 text.
_BINARY_EXT = frozenset({"png", "jpg", "gif", "webp"})
_HEX64 = frozenset("0123456789abcdef")


def blob_extension(mime: str) -> str:
    if mime in _EXT:
        return _EXT[mime]
    if mime.endswith("+json"):
        return "json"
    return "bin"


def _encode_blob(mime: str, value: Any) -> bytes:
    ext = blob_extension(mime)
    if ext in _BINARY_EXT and isinstance(value, str):
        try:
            return base64.b64decode(value, validate=True)
        except ValueError:
            pass
    if ext == "json":
        return json.dumps(value, separators=(",", ":")).encode("utf-8")
    if isinstance(value, str):
        return value.encode("utf-8")
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _decode_blob(mime: str, data: bytes) -> Any:
    ext = blob_extension(mime)
    if ext in _BINARY_EXT:
        return base64.b64encode(data).decode("ascii")
    if ext == "json":
        return json.loads(data.decode("utf-8"))
    return data.decode("utf-8")


def session_dir(notebook_path: Path) -> Path:
    return notebook_path.parent / "__marimo__" / "session"


def snapshot_path(notebook_path: Path) -> Path:
    return session_dir(notebook_path) / f"{notebook_path.name}.json"


def blob_dir(notebook_path: Path) -> Path:
    return session_dir(notebook_path) / f"{notebook_path.name}.d"


#: What the snapshot files are created with (they may hold anything a cell
#: showed): the engine's uid only.
_FILE_MODE = 0o600


@dataclass(frozen=True)
class NotebookPlace:
    """A notebook in the tree its outputs are written into.

    ``tree`` is the trusted root (the workspace's folder, named by the host);
    ``rel`` is the notebook's path in it. Every read and write of the outputs
    folder goes through ``tree``, so a link anywhere below the root (the
    notebook's folder, ``__marimo__``, ``session``, the blob folder or a file
    in it) is refused, never followed."""

    tree: Tree
    rel: PurePosixPath

    @classmethod
    def beside(cls, notebook_path: str | Path) -> NotebookPlace:
        """A notebook with no wider root: its own folder is the tree."""
        path = Path(notebook_path)
        return cls(Tree(path.parent), PurePosixPath(path.name))

    @property
    def path(self) -> Path:
        return self.tree.root.joinpath(*self.rel.parts)

    @property
    def session(self) -> PurePosixPath:
        return self.rel.parent / "__marimo__" / "session"

    @property
    def snapshot(self) -> PurePosixPath:
        return self.session / f"{self.rel.name}.json"

    @property
    def blobs(self) -> PurePosixPath:
        return self.session / f"{self.rel.name}.d"


def _place(notebook: NotebookPlace | str | Path) -> NotebookPlace:
    return notebook if isinstance(notebook, NotebookPlace) else NotebookPlace.beside(notebook)


#: A chart spec weighing more than this, as stored JSON, also gets a file of
#: its own beside the notebook, named by its hash like any stored output,
#: while the snapshot keeps it inline. A reader that cannot carry the spec
#: (a chat transcript, a Slack thread) names that file instead.
CHART_COPY_BYTES = 16 * 1024


@dataclass(frozen=True)
class StoredOutput:
    """An output value as the notebook's output folder holds it."""

    name: str
    """``<sha256>.<ext>``: the file it is stored under."""
    sha256: str
    data: bytes


def stored_output(mime: str, value: Any) -> StoredOutput:
    """``value`` as it is stored beside the notebook: its bytes and the name
    they go under. The one place a stored output is named, so the snapshot,
    a live output and a reference to either agree on the hash."""
    data = _encode_blob(mime, value)
    digest = hashlib.sha256(data).hexdigest()
    return StoredOutput(name=f"{digest}.{blob_extension(mime)}", sha256=digest, data=data)


#: The image types an output carried inline is read back as by its hash:
#: rasters only, since an SVG is markup a page could run.
RASTER_MIMES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


@dataclass(frozen=True)
class InlineImage:
    """A raster image a cell shows, as the bytes its hash names."""

    mime: str
    data: bytes


def inline_image(bundles: Iterable[Mapping[str, Any]], sha256: str) -> InlineImage | None:
    """The raster image among ``bundles`` whose stored bytes hash to
    ``sha256``, or ``None``. An image under the blob threshold is carried
    inline and never gets a file of its own; this is how one is still found
    by the hash a reference to it names (``stored_output`` hashes both)."""
    for bundle in bundles:
        for mime, value in bundle.items():
            if mime not in RASTER_MIMES or not isinstance(value, str):
                continue
            stored = stored_output(mime, value)
            if stored.sha256 == sha256:
                return InlineImage(mime=mime, data=stored.data)
    return None


def chart_copy(mime: str, value: Any) -> StoredOutput | None:
    """The file a chart's spec is also stored under, or ``None`` for a chart
    small enough to carry inline (or a value that is not a chart)."""
    if mime not in CHART_MIMES or not isinstance(value, dict) or REF_MIME in value:
        return None
    stored = stored_output(mime, value)
    return stored if len(stored.data) > CHART_COPY_BYTES else None


def store_blob(notebook: NotebookPlace | str | Path, mime: str, value: Any) -> dict[str, Any]:
    """``value`` stored out of line beside the notebook (written once, by its
    hash) and the reference that stands for it in a bundle. The snapshot's
    blobs and a live output's are one store: a snapshot that shows the same
    output keeps the same file."""
    place = _place(notebook)
    stored = stored_output(mime, value)
    target = place.blobs / stored.name
    if not place.tree.exists(target):
        place.tree.write_atomic(target, stored.data, mode=_FILE_MODE)
    return {REF_MIME: {"sha256": stored.sha256, "mime": mime, "bytes": len(stored.data)}}


# Writing ---------------------------------------------------------------------


@dataclass(frozen=True)
class KernelMeta:
    kernel_id: str
    env_id: str
    env_fingerprint: str


@dataclass(frozen=True)
class SnapshotCell:
    """One live cell in document order; ``outputs`` None when it has none."""

    cell_id: str
    code: str
    outputs: CellOutputs | None


@dataclass(frozen=True)
class SnapshotContent:
    cells: Sequence[SnapshotCell]
    kernel: KernelMeta | None = None
    outputs_in_git: bool = False
    marimo_version: str = DEFAULT_MARIMO_VERSION
    script_metadata_hash: str | None = None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


class SnapshotWriter:
    """Writes one notebook's snapshot, its blobs and its ``.gitignore`` lines."""

    def __init__(
        self, notebook: NotebookPlace | str | Path, *, blob_threshold: int = BLOB_THRESHOLD
    ) -> None:
        self.place = _place(notebook)
        self.notebook_path = self.place.path
        self.blob_threshold = blob_threshold

    @property
    def path(self) -> Path:
        return snapshot_path(self.notebook_path)

    def _externalize(self, bundle: MimeBundle, blobs: dict[str, bytes]) -> MimeBundle:
        out: MimeBundle = {}
        for mime, value in bundle.items():
            if mime != "text/plain" and value_bytes(value) > self.blob_threshold:
                stored = stored_output(mime, value)
                blobs[stored.name] = stored.data
                out[mime] = {
                    REF_MIME: {"sha256": stored.sha256, "mime": mime, "bytes": len(stored.data)}
                }
                continue
            out[mime] = value
            copy = chart_copy(mime, value)
            if copy is not None:
                blobs[copy.name] = copy.data
        return out

    def render(self, content: SnapshotContent) -> tuple[dict[str, Any], dict[str, bytes]]:
        blobs: dict[str, bytes] = {}
        cells: list[dict[str, Any]] = []
        for cell in content.cells:
            o = cell.outputs
            entry: dict[str, Any] = {
                "id": cell.cell_id,
                "code_hash": (
                    o.code_hash if o is not None and o.code_hash else code_hash(cell.code)
                ),
                "outputs": [],
                "console": [],
            }
            if o is not None:
                for bundle in o.bundles:
                    entry["outputs"].append(
                        {"type": "data", "data": self._externalize(with_plain(bundle), blobs)}
                    )
                if o.error is not None:
                    entry["outputs"].append(
                        {
                            "type": "error",
                            "ename": o.error.ename,
                            "evalue": o.error.evalue,
                            "traceback": list(o.error.traceback),
                        }
                    )
                entry["console"] = [
                    {"type": "stream", "name": i.name, "text": i.text, "mimetype": "text/plain"}
                    for i in o.console
                ]
                ext: dict[str, Any] = {
                    "provenance": {
                        "code_hash": entry["code_hash"],
                        "lineage_hash": o.lineage_hash,
                        "env_fingerprint": o.env_fingerprint,
                    },
                    # Where each console item stood among the rich outputs
                    # (how many came before it), so a reload shows them in
                    # the order they were made.
                    "console_after": [i.after for i in o.console],
                }
                if o.run is not None:
                    ext["run"] = {
                        "run_id": o.run.run_id,
                        "trigger": o.run.trigger,
                        "by": o.run.by.model_dump(mode="json"),
                        "started_at": _iso(o.run.started_at),
                        "finished_at": _iso(o.run.finished_at),
                        "status": o.run.status,
                    }
                entry["alkera"] = ext
            cells.append(entry)
        alkera: dict[str, Any] = {"schema_version": ALKERA_SCHEMA_VERSION}
        if content.kernel is not None:
            alkera["kernel"] = {
                "kernel_id": content.kernel.kernel_id,
                "env_id": content.kernel.env_id,
                "env_fingerprint": content.kernel.env_fingerprint,
            }
        doc = {
            "version": SESSION_VERSION,
            "metadata": {
                "marimo_version": content.marimo_version,
                "script_metadata_hash": content.script_metadata_hash,
            },
            "cells": cells,
            "alkera": alkera,
        }
        return doc, blobs

    def write(self, content: SnapshotContent) -> Path:
        """Write the snapshot. A link anywhere on the way raises
        :class:`~alkera_notebook.tree_io.LinkRefusedError` before anything is
        written or removed through it."""
        doc, blobs = self.render(content)
        tree, bdir = self.place.tree, self.place.blobs
        for name, data in blobs.items():
            if not tree.exists(bdir / name):
                tree.write_atomic(bdir / name, data, mode=_FILE_MODE)
        tree.write_atomic(
            self.place.snapshot, (json.dumps(doc, indent=1) + "\n").encode("utf-8"), mode=_FILE_MODE
        )
        self._collect(tree, bdir, set(blobs))
        update_gitignore(self.place, content.outputs_in_git)
        return self.path

    @staticmethod
    def _collect(tree: Tree, bdir: PurePosixPath, keep: set[str]) -> None:
        for name in tree.files(bdir):
            if name not in keep:
                tree.unlink(bdir / name)
        if not keep:
            with contextlib.suppress(OSError):
                tree.rmdir(bdir)


def _ignore_lines(notebook_path: Path) -> list[str]:
    return [f"/{notebook_path.name}.json", f"/{notebook_path.name}.d/"]


def update_gitignore(notebook: NotebookPlace | str | Path, outputs_in_git: bool) -> None:
    """Ignore this notebook's outputs unless ``outputs_in_git``; other lines stay."""
    place = _place(notebook)
    tree, path = place.tree, place.session / ".gitignore"
    ours = _ignore_lines(place.path)
    try:
        current: str | None = tree.read_text(path)
    except FileNotFoundError:
        current = None
    existing = current.splitlines() if current is not None else []
    kept = [line for line in existing if line not in ours]
    lines = kept if outputs_in_git else kept + ours
    if not lines:
        tree.unlink(path)
        return
    text = "\n".join(lines) + "\n"
    if current != text:
        tree.write_atomic(path, text.encode("utf-8"), mode=_FILE_MODE)


# Reading ---------------------------------------------------------------------


@dataclass
class SavedCell:
    id: str
    code_hash: str | None
    outputs: CellOutputs


@dataclass
class SnapshotRead:
    cells: list[SavedCell] = field(default_factory=list)
    alkera: bool = False
    kernel: KernelMeta | None = None
    notices: list[str] = field(default_factory=list)


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


#: Resolves a stored-output reference to its value: ``(True, value)``, or
#: ``(False, None)`` when the blob cannot be read.
BlobResolver = Callable[[Any], tuple[bool, Any]]


def valid_ref(ref: Any) -> tuple[str, str] | None:
    """``(sha256, mime)`` of a well-formed stored-output reference, or ``None``."""
    if not isinstance(ref, dict):
        return None
    digest, mime = ref.get("sha256"), ref.get("mime")
    if not (isinstance(digest, str) and isinstance(mime, str)):
        return None
    if len(digest) != 64 or not set(digest) <= _HEX64:
        return None
    return digest, mime


def _resolve_ref(ref: Any, tree: Tree, bdir: PurePosixPath) -> tuple[bool, Any]:
    """A blob beside the notebook, read through ``tree`` (never a link)."""
    found = valid_ref(ref)
    if found is None:
        return False, None
    digest, mime = found
    try:
        data = tree.read_bytes(bdir / f"{digest}.{blob_extension(mime)}")
    except OSError:
        return False, None
    if hashlib.sha256(data).hexdigest() != digest:
        return False, None
    try:
        return True, _decode_blob(mime, data)
    except (ValueError, UnicodeDecodeError):
        return False, None


def _read_bundle(
    raw: dict[str, Any], resolve: BlobResolver, notices: list[str], cell_id: str
) -> MimeBundle:
    out: MimeBundle = {}
    for mime, value in raw.items():
        if isinstance(value, dict) and REF_MIME in value:
            ok, resolved = resolve(value[REF_MIME])
            if ok:
                out[mime] = resolved
            else:
                notices.append(f"missing_blob: cell {cell_id} {mime}")
        else:
            out[mime] = value
    return out


def read_snapshot(notebook: NotebookPlace | str | Path) -> SnapshotRead:
    """The snapshot beside the notebook; never raises. A link on the way to
    it, or to a blob, is not followed."""
    place = _place(notebook)
    try:
        text = place.tree.read_text(place.snapshot)
    except FileNotFoundError:
        return SnapshotRead()
    except (OSError, UnicodeDecodeError):
        return SnapshotRead(notices=["corrupt_snapshot: not valid JSON"])
    return parse_snapshot(text, lambda ref: _resolve_ref(ref, place.tree, place.blobs))


def parse_snapshot(text: str | bytes, resolve: BlobResolver) -> SnapshotRead:
    """A snapshot's text, read; a stored output's reference is resolved by
    ``resolve``. Pure apart from ``resolve``; never raises."""
    try:
        doc = json.loads(text)
    except (UnicodeDecodeError, ValueError):
        return SnapshotRead(notices=["corrupt_snapshot: not valid JSON"])
    if not isinstance(doc, dict) or not isinstance(doc.get("cells"), list):
        return SnapshotRead(notices=["corrupt_snapshot: not a session snapshot"])
    result = SnapshotRead()
    alk = doc.get("alkera")
    if isinstance(alk, dict):
        result.alkera = True
        k = alk.get("kernel")
        if isinstance(k, dict) and all(
            isinstance(k.get(f), str) for f in ("kernel_id", "env_id", "env_fingerprint")
        ):
            result.kernel = KernelMeta(k["kernel_id"], k["env_id"], k["env_fingerprint"])
    for raw in doc["cells"]:
        try:
            result.cells.append(_read_cell(raw, resolve, result))
        except (TypeError, ValueError, KeyError, AttributeError):
            result.notices.append("corrupt_snapshot: a cell could not be read")
    return result


def _run_actor(raw: Any) -> RunActor | None:
    if not isinstance(raw, dict):
        return None
    try:
        return RunActor.model_validate(raw)
    except ValueError:
        return None


def _read_cell(raw: Any, resolve: BlobResolver, result: SnapshotRead) -> SavedCell:
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
        raise ValueError("cell")
    cid = raw["id"]
    ch = raw.get("code_hash")
    outputs = CellOutputs(code_hash=ch if isinstance(ch, str) else "")
    outputs.origin = "saved" if result.alkera else "unknown"
    for item in raw.get("outputs") or []:
        if item.get("type") == "data" and isinstance(item.get("data"), dict):
            bundle = _read_bundle(item["data"], resolve, result.notices, cid)
            if bundle:
                outputs.bundles.append(with_plain(bundle))
        elif item.get("type") == "error":
            tb = item.get("traceback")
            outputs.error = ErrorInfo(
                ename=str(item.get("ename", "Error")),
                evalue=str(item.get("evalue", "")),
                traceback=[str(t) for t in tb] if isinstance(tb, list) else [],
            )
    shown = len(outputs.bundles)
    raw_console = raw.get("console") or []
    ext = raw.get("alkera")
    places = ext.get("console_after") if result.alkera and isinstance(ext, dict) else None
    if not (
        isinstance(places, list)
        and len(places) == len(raw_console)
        and all(isinstance(p, int) and not isinstance(p, bool) and p >= 0 for p in places)
    ):
        # No order recorded (stock marimo, an earlier writer): the console
        # follows the rich outputs, as those readers showed it.
        places = [shown] * len(raw_console)
    for item, place in zip(raw_console, places, strict=True):
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if item.get("type") == "stream" and name in ("stdout", "stderr"):
            outputs.console.append(StreamItem(name, str(item.get("text", "")), min(place, shown)))
    if result.alkera and isinstance(ext, dict):
        prov = ext.get("provenance")
        if isinstance(prov, dict):
            lh, ef = prov.get("lineage_hash"), prov.get("env_fingerprint")
            outputs.lineage_hash = lh if isinstance(lh, str) else None
            outputs.env_fingerprint = ef if isinstance(ef, str) else None
        run = ext.get("run")
        by = _run_actor(run.get("by")) if isinstance(run, dict) else None
        # A run whose requester cannot be read (an earlier writer stored a
        # bare id) keeps its outputs and loses only its attribution.
        if isinstance(run, dict) and isinstance(run.get("run_id"), str) and by is not None:
            outputs.run = RunMeta(
                run_id=run["run_id"],
                trigger=str(run.get("trigger", "run")),
                by=by,
                status=str(run.get("status", "")),
                started_at=_parse_dt(run.get("started_at")),
                finished_at=_parse_dt(run.get("finished_at")),
            )
    return SavedCell(id=cid, code_hash=ch if isinstance(ch, str) else None, outputs=outputs)


def reattach(snapshot: SnapshotRead, cells: Sequence[tuple[str, str]]) -> dict[str, CellOutputs]:
    """Saved outputs for the current cells, matched by ``code_hash``.

    An Alkera snapshot prefers the saved cell with the same id; any snapshot
    falls back to the first unused saved cell with the same code hash (stock
    marimo writes its own cell ids, so only the hash can match).
    """
    by_id = {c.id: c for c in snapshot.cells}
    used: set[int] = set()
    out: dict[str, CellOutputs] = {}
    for cid, code in cells:
        h = code_hash(code)
        match: SavedCell | None = None
        same = by_id.get(cid)
        if snapshot.alkera and same is not None and same.code_hash == h and id(same) not in used:
            match = same
        else:
            for saved in snapshot.cells:
                if saved.code_hash == h and id(saved) not in used:
                    match = saved
                    break
        if match is not None:
            used.add(id(match))
            out[cid] = match.outputs
    return out


def outdated(saved: CellOutputs, current_lineage: str, current_env_fingerprint: str | None) -> bool:
    """Whether a saved output no longer matches its upstream code or environment.

    Outputs with no provenance (written by stock marimo) are never outdated
    here; they show as origin unknown instead.
    """
    if saved.origin == "unknown":
        return False
    if saved.lineage_hash is not None and saved.lineage_hash != current_lineage:
        return True
    return (
        saved.env_fingerprint is not None
        and current_env_fingerprint is not None
        and saved.env_fingerprint != current_env_fingerprint
    )


# Scheduling ------------------------------------------------------------------


class SnapshotScheduler:
    """Debounced snapshot writes: one write ``debounce_s`` after the first
    ``schedule()`` of a burst, with content produced at write time."""

    def __init__(
        self,
        writer: SnapshotWriter,
        produce: Callable[[], SnapshotContent],
        clock: Clock,
        debounce_s: float = 1.0,
        on_written: Callable[[Path, int], None] | None = None,
    ) -> None:
        self._writer = writer
        self._produce = produce
        self._clock = clock
        self._debounce = debounce_s
        self._on_written = on_written
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._writing = False
        self.writes = 0

    def schedule(self) -> None:
        if self._closed or (self._task is not None and not self._task.done()):
            return
        self._task = asyncio.get_running_loop().create_task(self._later())

    async def _later(self) -> None:
        await self._clock.sleep(self._debounce)
        self._writing = True
        try:
            await self._write()
        finally:
            self._writing = False

    async def _write(self) -> None:
        async with self._lock:
            content = self._produce()
            try:
                path = await asyncio.to_thread(self._writer.write, content)
            except OSError as exc:
                # A link placed in the outputs folder (or a full disk): this
                # snapshot is not written, and nothing else stops for it.
                logger.warning("notebook %s: outputs not saved: %s", self._writer.path, exc)
                return
            self.writes += 1
            if self._on_written is not None:
                self._on_written(path, len(content.cells))

    async def flush(self) -> None:
        """Write now; a pending debounced write is replaced by this one."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            if not self._writing:
                task.cancel()
            # A write already under way finishes; this one follows it.
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._write()

    async def close(self) -> None:
        pending = self._task is not None and not self._task.done()
        self._closed = True
        if pending:
            await self.flush()
