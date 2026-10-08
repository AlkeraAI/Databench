"""How a SQL result travels to the kernel.

Arrow written here is IPC metadata V5 with no body compression, and every
type the oldest supported reader may lack is cast to its classic form: view
types to their plain counterparts, run-end encoded arrays decoded, extension
types replaced by their storage (so no ``arrow.py_extension_type`` ever reaches
a kernel). Results up to the inline limit travel as an ``arrow.ipc.stream``
segment; larger ones are written as ``arrow.ipc.file`` into the kernel's
``data_dir``; a kernel that lists no Arrow codec gets ``rows.json``. The
engine only ever writes Arrow here; it never reads Arrow from a kernel.
"""

from __future__ import annotations

import hashlib
import io
import os
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

import pyarrow as pa
import pyarrow.compute as pc

from alkera_notebook.rpc import frames
from alkera_notebook.sql.errors import ResultTooLargeError
from alkera_notebook.tree_io import Tree

INLINE_LIMIT = 16 * 1024 * 1024

CODEC_STREAM = frames.CODEC_ARROW_STREAM
CODEC_FILE = frames.CODEC_ARROW_FILE
CODEC_ROWS = frames.CODEC_ROWS

TOO_LARGE_HINT = (
    "Results over 16 MiB travel as a file the kernel reads; this kernel cannot take one. "
    "Install polars or pyarrow in the notebook's environment, or add a LIMIT."
)
ROWS_TOO_LARGE_HINT = (
    "Install polars or pyarrow in the notebook's environment so large results can travel as Arrow, "
    "or add a LIMIT."
)

WRITE_OPTIONS = pa.ipc.IpcWriteOptions(metadata_version=pa.ipc.MetadataVersion.V5, compression=None)


# ---------------------------------------------------------------- classic types


def classic_type(t: pa.DataType) -> pa.DataType:
    """The type a reader without view, run-end or extension support can read."""
    if isinstance(t, pa.ExtensionType):
        return classic_type(t.storage_type)
    if t == pa.string_view():
        return pa.string()
    if t == pa.binary_view():
        return pa.binary()
    if pa.types.is_run_end_encoded(t):
        return classic_type(t.value_type)
    if hasattr(pa.types, "is_list_view") and (
        pa.types.is_list_view(t) or pa.types.is_large_list_view(t)
    ):
        value = t.value_field.with_type(classic_type(t.value_type))
        return pa.list_(value) if pa.types.is_list_view(t) else pa.large_list(value)
    if pa.types.is_list(t):
        return pa.list_(t.value_field.with_type(classic_type(t.value_type)))
    if pa.types.is_large_list(t):
        return pa.large_list(t.value_field.with_type(classic_type(t.value_type)))
    if pa.types.is_fixed_size_list(t):
        return pa.list_(t.value_field.with_type(classic_type(t.value_type)), t.list_size)
    if pa.types.is_struct(t):
        return pa.struct(
            [t.field(i).with_type(classic_type(t.field(i).type)) for i in range(t.num_fields)]
        )
    if pa.types.is_map(t):
        return pa.map_(classic_type(t.key_type), classic_type(t.item_type))
    if pa.types.is_dictionary(t):
        return pa.dictionary(t.index_type, classic_type(t.value_type))
    return t


def _classic_array(array: pa.Array, target: pa.DataType) -> pa.Array:
    if isinstance(array.type, pa.ExtensionType):
        array = array.storage
    if pa.types.is_run_end_encoded(array.type):
        array = pc.run_end_decode(array)
    if array.type == target:
        return array
    return array.cast(target)


def classic_schema(schema: pa.Schema) -> pa.Schema:
    fields = []
    for f in schema:
        metadata = {
            k: v for k, v in (f.metadata or {}).items() if not k.startswith(b"ARROW:extension")
        }
        fields.append(pa.field(f.name, classic_type(f.type), f.nullable, metadata or None))
    return pa.schema(fields, metadata=schema.metadata)


def classic_batch(batch: pa.RecordBatch, schema: pa.Schema) -> pa.RecordBatch:
    columns = [
        _classic_array(batch.column(i), schema.field(i).type) for i in range(batch.num_columns)
    ]
    return pa.RecordBatch.from_arrays(columns, schema=schema)


# ---------------------------------------------------------------- encoded result


@dataclass(frozen=True)
class EncodedTable:
    """What the response's ``table`` member names, plus the bytes behind it."""

    codec: str
    rows: int
    nbytes: int
    sha256: str
    inline: bytes | None = None
    file_name: str | None = None

    def value(self) -> frames.Segment | frames.FileRef:
        """The response's ``table``: a segment, or a file in ``data_dir``."""
        if self.file_name is not None:
            return frames.FileRef(self.file_name, self.codec, self.nbytes, self.sha256)
        assert self.inline is not None
        return frames.Segment(self.inline, self.codec, {"rows": self.rows})


class _HashingFile(io.RawIOBase):
    """A write-only file that hashes what it writes."""

    def __init__(self, f: BinaryIO) -> None:
        self._f = f
        self.digest = hashlib.sha256()
        self.size = 0

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        view = memoryview(data).cast("B")
        self._f.write(view)
        self.digest.update(view)
        self.size += len(view)
        return len(view)

    def close(self) -> None:
        if not self._f.closed:
            self._f.flush()
            os.fsync(self._f.fileno())
            self._f.close()
        super().close()


class ArrowSink:
    """Writes batches inline until the inline limit, then spills to a file in
    the kernel's data directory. There is no size cap: memory is bounded by
    the kernel sandbox's guard, as the whole result becomes a frame there.

    ``on_grow(n)`` is called with each increment of buffered bytes so the
    broker can account for results in flight.
    """

    def __init__(
        self,
        schema: pa.Schema,
        *,
        data_dir: Path | None,
        inline_limit: int = INLINE_LIMIT,
        file_stem: str | None = None,
    ) -> None:
        self.schema = classic_schema(schema)
        self._data_dir = data_dir
        self._inline_limit = inline_limit
        self._stem = file_stem or uuid.uuid4().hex
        self._buffer = io.BytesIO()
        self._stream: pa.ipc.RecordBatchStreamWriter | None = pa.ipc.new_stream(
            self._buffer, self.schema, options=WRITE_OPTIONS
        )
        self._held: list[pa.RecordBatch] = []
        self._file: _HashingFile | None = None
        self._file_writer: pa.ipc.RecordBatchFileWriter | None = None
        self._tmp: str | None = None
        self.rows = 0

    @property
    def spilled(self) -> bool:
        return self._file is not None

    @property
    def size(self) -> int:
        return self._file.size if self._file is not None else self._buffer.tell()

    def write(self, batch: pa.RecordBatch) -> None:
        batch = classic_batch(batch, self.schema)
        self.rows += batch.num_rows
        if self._file_writer is not None:
            self._file_writer.write_batch(batch)
        else:
            assert self._stream is not None
            self._stream.write_batch(batch)
            self._held.append(batch)
            if self._buffer.tell() > self._inline_limit:
                self._spill()

    def _spill(self) -> None:
        if self._data_dir is None:
            self.abort()
            raise ResultTooLargeError(self._inline_limit, TOO_LARGE_HINT)
        self._tmp = f".{self._stem}.arrow.part"
        self._file = _HashingFile(Tree(self._data_dir).create(self._tmp))
        self._file_writer = pa.ipc.new_file(self._file, self.schema, options=WRITE_OPTIONS)
        for held in self._held:
            self._file_writer.write_batch(held)
        self._held = []
        assert self._stream is not None
        self._stream.close()
        self._stream = None
        self._buffer = io.BytesIO()

    def finish(self) -> EncodedTable:
        if self._file_writer is not None:
            assert self._file is not None and self._tmp is not None and self._data_dir is not None
            self._file_writer.close()
            self._file.close()
            name = f"{self._stem}.arrow"
            Tree(self._data_dir).replace(self._tmp, name)
            self._tmp = None
            return EncodedTable(
                CODEC_FILE,
                self.rows,
                self._file.size,
                self._file.digest.hexdigest(),
                file_name=name,
            )
        assert self._stream is not None
        self._stream.close()
        data = self._buffer.getvalue()
        return EncodedTable(
            CODEC_STREAM, self.rows, len(data), hashlib.sha256(data).hexdigest(), inline=data
        )

    def abort(self) -> None:
        for closer in (self._stream, self._file_writer, self._file):
            try:
                if closer is not None:
                    closer.close()
            except Exception:  # noqa: S110 - best effort while discarding a refused result
                pass
        if self._tmp is not None and self._data_dir is not None:
            Tree(self._data_dir).unlink(self._tmp)
            self._tmp = None
        self._held = []


# ---------------------------------------------------------------- rows.json


def encode_rows_json(
    schema: pa.Schema, batches: Iterable[pa.RecordBatch], *, limit: int = INLINE_LIMIT
) -> EncodedTable:
    """``{"columns": [{name, type}], "rows": [...]}`` with tagged scalars (the
    RPC's own encoding), for a kernel that cannot decode Arrow."""
    target = classic_schema(schema)
    columns = [(f.name, str(f.type)) for f in target]
    rows: list[tuple[Any, ...]] = []
    approx = 0
    for batch in batches:
        batch = classic_batch(batch, target)
        approx += batch.nbytes
        if approx > limit:
            raise ResultTooLargeError(limit, ROWS_TOO_LARGE_HINT)
        # Column-wise, so two columns with one name both survive.
        rows.extend(zip(*(col.to_pylist() for col in batch.columns), strict=True))
    segment = frames.rows_segment(columns, rows)
    if len(segment.data) > limit:
        raise ResultTooLargeError(limit, ROWS_TOO_LARGE_HINT)
    data = segment.data
    return EncodedTable(
        CODEC_ROWS, len(rows), len(data), hashlib.sha256(data).hexdigest(), inline=data
    )
