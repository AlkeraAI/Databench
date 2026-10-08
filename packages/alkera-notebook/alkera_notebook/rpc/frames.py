"""The Alkera client RPC, protocol 1: framing, the JSON-RPC envelope, tagged
values, the ``rows.json`` codec and the protocol's names.

This module is the reference implementation shared by both sides of the
connection. The kernel runtime carries a byte-for-byte copy of it
(``_alkera_kernel/_frames.py``, written by ``make gen-kernel-rpc``), so it
imports only the standard library and keeps to Python 3.10 syntax. Nothing in
it does I/O: it turns bytes into frames and values and back.

A frame on the wire::

    u32 total_length (big-endian) | u32 header_length | header (UTF-8 JSON) | segments

``total_length = 4 + header_length + sum(segment lengths)``. The header is a
JSON-RPC 2.0 object plus ``seg`` (the segment lengths, in order) and ``ctx``
(the run a request belongs to).
"""

from __future__ import annotations

import base64
import binascii
import datetime as _dt
import decimal
import json
import math
import re
import struct
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeGuard

PROTOCOL = 1

#: Frames from the client (the kernel) to the service (the engine).
FRAME_LIMIT_CLIENT = 16 * 1024 * 1024
#: Frames from the service (the engine) to the client (the kernel).
FRAME_LIMIT_SERVICE = 64 * 1024 * 1024
#: Requests one side keeps in flight; the rest wait in a local queue.
MAX_INFLIGHT = 32
#: A connection that has not sent ``hello`` by then is closed.
HELLO_TIMEOUT_S = 2.0
#: Raw token size before base64url.
TOKEN_BYTES = 32

CANCEL_METHOD = "$/cancelRequest"
HELLO_METHOD = "hello"

# Engine to kernel requests (the kernel hosts them).
RUN_EXECUTE = "run.execute"
RUN_INTERRUPT = "run.interrupt"
NAMES_DELETE = "names.delete"
COMM_DELIVER = "comm.deliver"
INSPECT_FRAME = "inspect.frame"
INSPECT_VALUE = "inspect.value"
COMPLETE = "complete"
KERNEL_SHUTDOWN = "kernel.shutdown"
KERNEL_METHODS = (
    RUN_EXECUTE,
    RUN_INTERRUPT,
    NAMES_DELETE,
    COMM_DELIVER,
    INSPECT_FRAME,
    INSPECT_VALUE,
    COMPLETE,
    KERNEL_SHUTDOWN,
)

# Kernel to engine requests (the service hosts them); run-scoped.
SQL_EXECUTE = "sql.execute"

# Kernel to engine notifications.
EVENT_RUN_STARTED = "run.started"
EVENT_RUN_FINISHED = "run.finished"
EVENT_CELL_STARTED = "cell.started"
EVENT_CELL_OUTPUT = "cell.output"
EVENT_CELL_STREAM = "cell.stream"
EVENT_CELL_FINISHED = "cell.finished"
EVENT_CELL_VARIABLES = "cell.variables"
EVENT_COMM_OPEN = "comm.open"
EVENT_COMM_MSG = "comm.msg"
EVENT_COMM_CLOSE = "comm.close"
EVENT_COMM_IDLE = "comm.idle"
EVENT_UI_BINDINGS = "ui.bindings"
EVENT_MODULE_MISSING = "module.missing"
EVENT_THREAD_OUTPUT = "thread.output"
EVENT_WIDGET_ASSET = "widget.asset"
KERNEL_EVENTS = (
    EVENT_RUN_STARTED,
    EVENT_RUN_FINISHED,
    EVENT_CELL_STARTED,
    EVENT_CELL_OUTPUT,
    EVENT_CELL_STREAM,
    EVENT_CELL_FINISHED,
    EVENT_CELL_VARIABLES,
    EVENT_COMM_OPEN,
    EVENT_COMM_MSG,
    EVENT_COMM_CLOSE,
    EVENT_COMM_IDLE,
    EVENT_UI_BINDINGS,
    EVENT_MODULE_MISSING,
    EVENT_THREAD_OUTPUT,
    EVENT_WIDGET_ASSET,
)

# What the kernel reports in ``cell.finished`` and ``run.finished``.
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_INTERRUPTED = "interrupted"
STATUS_STOPPED = "stopped"
RESULT_STATUSES = (STATUS_OK, STATUS_ERROR, STATUS_INTERRUPTED, STATUS_STOPPED)

CODEC_JSON = "json"
CODEC_BYTES = "bytes"
CODEC_ROWS = "rows.json"
CODEC_ARROW_STREAM = "arrow.ipc.stream"
CODEC_ARROW_FILE = "arrow.ipc.file"
CODECS = (CODEC_JSON, CODEC_BYTES, CODEC_ROWS, CODEC_ARROW_STREAM, CODEC_ARROW_FILE)
#: The only codec that travels as a file in ``data_dir``.
FILE_CODECS = (CODEC_ARROW_FILE,)

_PREFIX = struct.Struct(">II")
_U32 = struct.Struct(">I")
_MAX_SAFE_INT = 2**53


# --------------------------------------------------------------------------- errors


class ErrorCode:
    """JSON-RPC error codes, standard and Alkera's own."""

    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    UNAUTHORIZED = -32001
    FORBIDDEN = -32002
    TOO_LARGE = -32003
    CANCELLED = -32004
    UNAVAILABLE = -32005
    KERNEL_BUSY = -32006
    #: Method-specific errors start here (``sql.unknown_connection`` and so on).
    METHOD_SPECIFIC = -32010


ERROR_NAMES: dict[int, str] = {
    ErrorCode.PARSE_ERROR: "parse_error",
    ErrorCode.INVALID_REQUEST: "invalid_request",
    ErrorCode.METHOD_NOT_FOUND: "method_not_found",
    ErrorCode.INVALID_PARAMS: "invalid_params",
    ErrorCode.INTERNAL_ERROR: "internal_error",
    ErrorCode.UNAUTHORIZED: "unauthorized",
    ErrorCode.FORBIDDEN: "forbidden",
    ErrorCode.TOO_LARGE: "too_large",
    ErrorCode.CANCELLED: "cancelled",
    ErrorCode.UNAVAILABLE: "unavailable",
    ErrorCode.KERNEL_BUSY: "kernel_busy",
}
RETRYABLE_CODES = frozenset({ErrorCode.UNAVAILABLE, ErrorCode.KERNEL_BUSY})


class RpcError(Exception):
    """An error answer to one request. Raised by a handler to answer with it;
    raised by a caller's ``request`` when the peer answered with it."""

    def __init__(self, code: int, message: str, data: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        payload = dict(data or {})
        name = ERROR_NAMES.get(code)
        if name is not None:
            payload.setdefault("name", name)
        self.data: dict[str, Any] = payload

    @property
    def name(self) -> str:
        value = self.data.get("name")
        return value if isinstance(value, str) else ERROR_NAMES.get(self.code, "error")

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data:
            out["data"] = self.data
        return out

    @classmethod
    def from_json(cls, obj: object) -> RpcError:
        if not isinstance(obj, dict):
            raise ProtocolError("invalid_error", "error must be an object")
        code = obj.get("code")
        message = obj.get("message")
        data = obj.get("data")
        if not _is_int(code) or not isinstance(message, str):
            raise ProtocolError("invalid_error", "error needs an integer code and a message")
        if data is not None and not isinstance(data, dict):
            raise ProtocolError("invalid_error", "error data must be an object")
        return cls(code, message, data)

    def __repr__(self) -> str:
        return f"RpcError({self.code}, {self.message!r}, {self.data!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RpcError):
            return NotImplemented
        return (self.code, self.message, self.data) == (other.code, other.message, other.data)

    __hash__ = Exception.__hash__

    # Named constructors for the Alkera codes.
    @classmethod
    def unauthorized(cls, message: str = "unauthorized") -> RpcError:
        return cls(ErrorCode.UNAUTHORIZED, message)

    @classmethod
    def forbidden(cls, reason: str, message: str = "forbidden") -> RpcError:
        return cls(ErrorCode.FORBIDDEN, message, {"reason": reason})

    @classmethod
    def too_large(cls, limit: int, hint: str = "") -> RpcError:
        return cls(ErrorCode.TOO_LARGE, "message too large", {"limit": limit, "hint": hint})

    @classmethod
    def cancelled(cls) -> RpcError:
        return cls(ErrorCode.CANCELLED, "cancelled")

    @classmethod
    def unavailable(cls, message: str = "unavailable") -> RpcError:
        return cls(ErrorCode.UNAVAILABLE, message)

    @classmethod
    def kernel_busy(cls, message: str = "the kernel is busy") -> RpcError:
        return cls(ErrorCode.KERNEL_BUSY, message)

    @classmethod
    def method_not_found(cls, method: str) -> RpcError:
        return cls(ErrorCode.METHOD_NOT_FOUND, f"method not found: {method}")

    @classmethod
    def invalid_params(cls, message: str) -> RpcError:
        return cls(ErrorCode.INVALID_PARAMS, message)


class ProtocolError(Exception):
    """A violation the receiver answers by closing the connection: a frame
    that cannot be read, or a header that is not a JSON-RPC message. ``reason``
    is a stable name the conformance vectors check."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason


class FrameTooLargeError(ProtocolError):
    def __init__(self, size: int, limit: int) -> None:
        super().__init__("too_large", f"frame of {size} bytes exceeds the limit of {limit}")
        self.size = size
        self.limit = limit


# --------------------------------------------------------------------------- framing


@dataclass(frozen=True)
class Frame:
    """One decoded frame: the header object and its segments."""

    header: dict[str, Any]
    segments: tuple[bytes, ...] = ()


def _dump_json(obj: object) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode(
        "utf-8"
    )


def encode_frame(header: Mapping[str, Any], segments: Sequence[bytes] = (), *, limit: int) -> bytes:
    """The bytes of one frame. ``header["seg"]`` is set from ``segments``.

    Raises ``FrameTooLargeError`` when the frame would exceed ``limit`` (measured as
    ``total_length``), and ``ValueError`` when the header is not JSON."""
    body = dict(header)
    body["seg"] = [len(s) for s in segments]
    try:
        raw = _dump_json(body)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"header is not JSON: {exc}") from exc
    total = 4 + len(raw) + sum(body["seg"])
    if total > limit:
        raise FrameTooLargeError(total, limit)
    return b"".join([_PREFIX.pack(total, len(raw)), raw, *(bytes(s) for s in segments)])


def _parse_header(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError("bad_header", "header is not UTF-8") from exc
    try:
        header = json.loads(text, parse_constant=_refuse_constant)
    except ValueError as exc:
        raise ProtocolError("bad_header", f"header is not JSON: {exc}") from exc
    if not isinstance(header, dict):
        raise ProtocolError("header_not_object", "header must be a JSON object")
    return header


def _refuse_constant(name: str) -> object:
    raise ValueError(f"{name} is not JSON")


def _segment_lengths(header: Mapping[str, Any]) -> list[int]:
    seg = header.get("seg", [])
    if not isinstance(seg, list) or not all(_is_int(n) and n >= 0 for n in seg):
        raise ProtocolError("bad_segments", "seg must be a list of non-negative integers")
    return list(seg)


def _split(total: int, body: bytes) -> Frame:
    """``body`` is everything after the total-length prefix (``total`` bytes)."""
    if total < 4:
        raise ProtocolError("length_mismatch", f"total_length {total} is below 4")
    (header_length,) = _U32.unpack_from(body, 0)
    if header_length > total - 4:
        raise ProtocolError("length_mismatch", "header_length runs past the frame")
    header = _parse_header(body[4 : 4 + header_length])
    lengths = _segment_lengths(header)
    if 4 + header_length + sum(lengths) != total:
        raise ProtocolError("length_mismatch", "segment lengths do not add up to total_length")
    segments: list[bytes] = []
    offset = 4 + header_length
    for n in lengths:
        segments.append(bytes(body[offset : offset + n]))
        offset += n
    return Frame(header, tuple(segments))


def decode_frame(data: bytes, *, limit: int) -> Frame:
    """Exactly one frame from ``data``; anything short, long or malformed is a
    ``ProtocolError``."""
    if len(data) < 4:
        raise ProtocolError("truncated", "fewer than 4 bytes")
    (total,) = _U32.unpack_from(data, 0)
    if total > limit:
        raise FrameTooLargeError(total, limit)
    if len(data) < 4 + total:
        raise ProtocolError("truncated", f"frame needs {4 + total} bytes, got {len(data)}")
    if len(data) > 4 + total:
        raise ProtocolError("length_mismatch", "bytes after the end of the frame")
    if total < 4:
        raise ProtocolError("length_mismatch", f"total_length {total} is below 4")
    return _split(total, data[4:])


class FrameReader:
    """Incremental decoder over a byte stream: ``feed`` what the socket gave
    and get every complete frame. A frame above ``limit`` is refused as soon
    as its length prefix arrives, before its body is buffered."""

    def __init__(self, *, limit: int) -> None:
        self.limit = limit
        self._buf = bytearray()

    @property
    def buffered(self) -> int:
        return len(self._buf)

    def feed(self, data: bytes) -> list[Frame]:
        self._buf += data
        frames: list[Frame] = []
        while len(self._buf) >= 4:
            (total,) = _U32.unpack_from(self._buf, 0)
            if total > self.limit:
                raise FrameTooLargeError(total, self.limit)
            if total < 4:
                raise ProtocolError("length_mismatch", f"total_length {total} is below 4")
            if len(self._buf) < 4 + total:
                break
            body = bytes(self._buf[4 : 4 + total])
            del self._buf[: 4 + total]
            frames.append(_split(total, body))
        return frames

    def at_frame_boundary(self) -> bool:
        """True when no partial frame is buffered (a clean end of stream)."""
        return not self._buf


# --------------------------------------------------------------------------- envelope


@dataclass(frozen=True)
class Request:
    id: int
    method: str
    params: dict[str, Any] = field(default_factory=dict)
    ctx: dict[str, Any] | None = None
    segments: tuple[bytes, ...] = ()


@dataclass(frozen=True)
class Response:
    id: int
    result: Any = None
    error: RpcError | None = None
    segments: tuple[bytes, ...] = ()

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class Notification:
    method: str
    params: dict[str, Any] = field(default_factory=dict)
    segments: tuple[bytes, ...] = ()


Message = Request | Response | Notification


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def message_header(message: Message) -> dict[str, Any]:
    """The JSON-RPC header for ``message`` (without ``seg``)."""
    if isinstance(message, Request):
        out: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": message.id,
            "method": message.method,
            "params": message.params,
        }
        if message.ctx is not None:
            out["ctx"] = message.ctx
        return out
    if isinstance(message, Response):
        out = {"jsonrpc": "2.0", "id": message.id}
        if message.error is not None:
            out["error"] = message.error.to_json()
        else:
            out["result"] = message.result
        return out
    return {"jsonrpc": "2.0", "method": message.method, "params": message.params}


def encode_message(message: Message, *, limit: int) -> bytes:
    return encode_frame(message_header(message), message.segments, limit=limit)


def parse_message(frame: Frame) -> Message:
    """The message a frame carries. Members the receiver does not know are
    ignored. Raises ``ProtocolError`` for anything that is not a JSON-RPC 2.0
    request, response or notification."""
    h = frame.header
    if h.get("jsonrpc") != "2.0":
        raise ProtocolError("invalid_message", 'jsonrpc must be "2.0"')
    has_id = "id" in h
    if "method" in h:
        method = h["method"]
        if not isinstance(method, str) or not method:
            raise ProtocolError("invalid_message", "method must be a non-empty string")
        params = h.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ProtocolError("invalid_message", "params must be an object")
        if not has_id:
            return Notification(method, params, frame.segments)
        rid = h["id"]
        if not _is_int(rid):
            raise ProtocolError("invalid_message", "id must be an integer")
        ctx = h.get("ctx")
        if ctx is not None and not isinstance(ctx, dict):
            raise ProtocolError("invalid_message", "ctx must be an object")
        return Request(rid, method, params, ctx, frame.segments)
    if not has_id or not _is_int(h["id"]):
        raise ProtocolError("invalid_message", "a response needs an integer id")
    if ("result" in h) == ("error" in h):
        raise ProtocolError("invalid_message", "a response has exactly one of result and error")
    if "error" in h:
        try:
            error = RpcError.from_json(h["error"])
        except ProtocolError as exc:
            raise ProtocolError("invalid_message", str(exc)) from exc
        return Response(h["id"], None, error, frame.segments)
    return Response(h["id"], h["result"], None, frame.segments)


# --------------------------------------------------------------------------- tagged values


@dataclass(frozen=True)
class Segment:
    """A binary payload carried beside the header: ``{"$seg": i, "codec", "meta"}``."""

    data: bytes
    codec: str = CODEC_BYTES
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FileRef:
    """A file the service wrote into the client's ``data_dir``."""

    name: str
    codec: str
    bytes: int
    sha256: str

    def path_in(self, data_dir: str) -> str:
        """The file's path inside ``data_dir``; a name that would leave the
        directory is refused."""
        if not _FILE_NAME.fullmatch(self.name):
            raise ValueError(f"file name {self.name!r} is not a plain name")
        return f"{data_dir.rstrip('/')}/{self.name}"


_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class TagError(ValueError):
    """A tagged value that cannot be decoded (answered as invalid params)."""


def encode_value(value: Any, segments: list[bytes]) -> Any:
    """``value`` as JSON with tagged values; a ``Segment`` is appended to
    ``segments`` and referenced by index. Raises ``TypeError`` for a value the
    protocol cannot carry (no pickle-family fallback exists)."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_INT:
            return {"$t": "int", "v": str(value)}
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return {"$t": "float", "v": "nan"}
        if math.isinf(value):
            return {"$t": "float", "v": "inf" if value > 0 else "-inf"}
        return value
    if isinstance(value, Segment):
        segments.append(bytes(value.data))
        return {"$seg": len(segments) - 1, "codec": value.codec, "meta": dict(value.meta)}
    if isinstance(value, FileRef):
        return {
            "$file": value.name,
            "codec": value.codec,
            "bytes": value.bytes,
            "sha256": value.sha256,
        }
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        escaped = False
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"object keys must be strings, got {type(key).__name__}")
            escaped = escaped or key.startswith("$")
            out[key] = encode_value(item, segments)
        return {"$t": "object", "v": out} if escaped else out
    if isinstance(value, (list, tuple)):
        return [encode_value(item, segments) for item in value]
    if isinstance(value, decimal.Decimal):
        return {"$t": "decimal", "v": str(value)}
    if isinstance(value, _dt.datetime):
        return {"$t": "datetime", "v": value.isoformat()}
    if isinstance(value, _dt.date):
        return {"$t": "date", "v": value.isoformat()}
    if isinstance(value, _dt.time):
        return {"$t": "time", "v": value.isoformat()}
    if isinstance(value, _dt.timedelta):
        return {"$t": "timedelta", "v": format_duration(value)}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$t": "bytes", "v": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, uuid.UUID):
        return {"$t": "uuid", "v": str(value)}
    raise TypeError(f"{type(value).__name__} cannot cross the RPC")


def decode_value(obj: Any, segments: Sequence[bytes], *, allow_files: bool = False) -> Any:
    """The Python value of a tagged JSON value. ``$file`` is accepted only
    where the service may send it (``allow_files``)."""
    if isinstance(obj, list):
        return [decode_value(item, segments, allow_files=allow_files) for item in obj]
    if not isinstance(obj, dict):
        return obj
    if "$seg" in obj:
        index = obj["$seg"]
        if not _is_int(index) or not 0 <= index < len(segments):
            raise TagError(f"segment {index!r} does not exist")
        codec = obj.get("codec", CODEC_BYTES)
        meta = obj.get("meta", {})
        if not isinstance(codec, str) or not isinstance(meta, dict):
            raise TagError("a segment needs a string codec and an object meta")
        return Segment(segments[index], codec, meta)
    if "$file" in obj:
        if not allow_files:
            raise TagError("files are sent only by the service")
        name, codec, size, digest = (
            obj["$file"],
            obj.get("codec"),
            obj.get("bytes"),
            obj.get("sha256"),
        )
        if not isinstance(name, str) or not _FILE_NAME.fullmatch(name):
            raise TagError(f"file name {name!r} is not a plain name")
        if codec not in FILE_CODECS:
            raise TagError(f"codec {codec!r} does not travel as a file")
        if (
            not _is_int(size)
            or size < 0
            or not isinstance(digest, str)
            or not _SHA256.fullmatch(digest)
        ):
            raise TagError("a file needs bytes and a sha256")
        return FileRef(name, codec, size, digest)
    if "$t" in obj:
        return _decode_typed(obj, segments, allow_files)
    out = {}
    for key, item in obj.items():
        if key.startswith("$"):
            raise TagError(f"unknown tag {key!r}")
        out[key] = decode_value(item, segments, allow_files=allow_files)
    return out


def _decode_typed(obj: dict[str, Any], segments: Sequence[bytes], allow_files: bool) -> Any:
    tag, v = obj["$t"], obj.get("v")
    if tag == "object":
        if not isinstance(v, dict):
            raise TagError("an escaped object needs an object value")
        return {k: decode_value(item, segments, allow_files=allow_files) for k, item in v.items()}
    if not isinstance(v, str):
        raise TagError(f"tag {tag!r} needs a string value")
    try:
        if tag == "decimal":
            return decimal.Decimal(v)
        if tag == "datetime":
            return _dt.datetime.fromisoformat(v)
        if tag == "date":
            return _dt.date.fromisoformat(v)
        if tag == "time":
            return _dt.time.fromisoformat(v)
        if tag == "timedelta":
            return parse_duration(v)
        if tag == "bytes":
            return base64.b64decode(v, validate=True)
        if tag == "uuid":
            return uuid.UUID(v)
        if tag == "int":
            if not re.fullmatch(r"-?[0-9]+", v):
                raise TagError(f"{v!r} is not an integer")
            return int(v)
        if tag == "float":
            special = {"nan": math.nan, "inf": math.inf, "-inf": -math.inf}
            if v in special:
                return special[v]
            return float(v)
    except (ValueError, decimal.InvalidOperation, binascii.Error) as exc:
        if isinstance(exc, TagError):
            raise
        raise TagError(f"{tag} value {v!r} is malformed") from exc
    raise TagError(f"unknown tag {tag!r}")


_DURATION = re.compile(
    r"(?P<sign>-)?P(?:(?P<days>[0-9]+)D)?"
    r"(?:T(?:(?P<hours>[0-9]+)H)?(?:(?P<minutes>[0-9]+)M)?(?:(?P<seconds>[0-9]+(?:\.[0-9]{1,6})?)S)?)?"
)


def format_duration(delta: _dt.timedelta) -> str:
    """ISO 8601 duration, exact to the microsecond: ``-P1DT2H3M4.5S``."""
    total_us = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    sign = "-" if total_us < 0 else ""
    total_us = abs(total_us)
    days, rest = divmod(total_us, 86_400_000_000)
    hours, rest = divmod(rest, 3_600_000_000)
    minutes, rest = divmod(rest, 60_000_000)
    seconds, micros = divmod(rest, 1_000_000)
    sec = f"{seconds}.{micros:06d}".rstrip("0").rstrip(".") if micros else str(seconds)
    return f"{sign}P{days}DT{hours}H{minutes}M{sec}S"


def parse_duration(text: str) -> _dt.timedelta:
    m = _DURATION.fullmatch(text)
    if m is None or text in {"P", "-P", "PT", "-PT"} or text.endswith("T"):
        raise TagError(f"{text!r} is not an ISO 8601 duration")
    seconds = decimal.Decimal(m["seconds"] or "0")
    delta = _dt.timedelta(
        days=int(m["days"] or 0),
        hours=int(m["hours"] or 0),
        minutes=int(m["minutes"] or 0),
        microseconds=int(seconds * 1_000_000),
    )
    return -delta if m["sign"] else delta


# --------------------------------------------------------------------------- rows.json


@dataclass(frozen=True)
class RowsTable:
    """A decoded ``rows.json`` table: ``columns`` are ``(name, type)`` pairs and
    every row has one value per column."""

    columns: tuple[tuple[str, str], ...]
    rows: tuple[tuple[Any, ...], ...]

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.columns]

    def records(self) -> list[dict[str, Any]]:
        return [dict(zip(self.names, row, strict=True)) for row in self.rows]


def encode_rows(
    columns: Iterable[tuple[str, str]], rows: Iterable[Sequence[Any]]
) -> dict[str, Any]:
    """The ``rows.json`` object for a table, scalars tagged. Segments are not
    allowed inside a table."""
    cols = [(str(name), str(kind)) for name, kind in columns]
    out_rows: list[list[Any]] = []
    for row in rows:
        if len(row) != len(cols):
            raise ValueError(f"row has {len(row)} values for {len(cols)} columns")
        nested: list[bytes] = []
        out_rows.append([encode_value(v, nested) for v in row])
        if nested:
            raise TypeError("a table cell cannot carry a segment")
    return {"columns": [{"name": n, "type": t} for n, t in cols], "rows": out_rows}


def decode_rows(obj: Any) -> RowsTable:
    if not isinstance(obj, dict):
        raise TagError("rows.json must be an object")
    columns, rows = obj.get("columns"), obj.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise TagError("rows.json needs columns and rows lists")
    cols: list[tuple[str, str]] = []
    for col in columns:
        if (
            not isinstance(col, dict)
            or not isinstance(col.get("name"), str)
            or not isinstance(col.get("type"), str)
        ):
            raise TagError("each column needs a string name and type")
        cols.append((col["name"], col["type"]))
    out: list[tuple[Any, ...]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(cols):
            raise TagError("each row is a list with one value per column")
        out.append(tuple(decode_value(v, ()) for v in row))
    return RowsTable(tuple(cols), tuple(out))


def rows_segment(
    columns: Iterable[tuple[str, str]],
    rows: Iterable[Sequence[Any]],
    *,
    fallback: str | None = None,
) -> Segment:
    """A table as a ``rows.json`` segment; ``fallback`` names why Arrow was not
    used, when the sender would otherwise have used it."""
    meta: dict[str, Any] = {"fallback": fallback} if fallback else {}
    return Segment(_dump_json(encode_rows(columns, rows)), CODEC_ROWS, meta)


def read_rows_segment(segment: Segment) -> RowsTable:
    if segment.codec != CODEC_ROWS:
        raise TagError(f"segment codec is {segment.codec!r}, not rows.json")
    try:
        obj = json.loads(segment.data.decode("utf-8"), parse_constant=_refuse_constant)
    except ValueError as exc:
        raise TagError(f"rows.json segment is not JSON: {exc}") from exc
    return decode_rows(obj)


# --------------------------------------------------------------------------- message helpers


def encode_params(params: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[bytes, ...]]:
    """Tagged params plus the segments they reference."""
    segments: list[bytes] = []
    encoded = encode_value(dict(params), segments)
    if not isinstance(encoded, dict) or "$t" in encoded:
        raise TypeError("param names cannot start with $")
    return encoded, tuple(segments)


def decode_params(
    params: Mapping[str, Any], segments: Sequence[bytes], *, allow_files: bool = False
) -> dict[str, Any]:
    value = decode_value(dict(params), segments, allow_files=allow_files)
    assert isinstance(value, dict)
    return value


def request(
    rid: int,
    method: str,
    params: Mapping[str, Any] | None = None,
    ctx: Mapping[str, Any] | None = None,
) -> Request:
    encoded, segments = encode_params(params or {})
    return Request(rid, method, encoded, dict(ctx) if ctx is not None else None, segments)


def notification(method: str, params: Mapping[str, Any] | None = None) -> Notification:
    encoded, segments = encode_params(params or {})
    return Notification(method, encoded, segments)


def result_response(rid: int, result: Any) -> Response:
    segments: list[bytes] = []
    encoded = encode_value(result, segments)
    return Response(rid, encoded, None, tuple(segments))


def error_response(rid: int, error: RpcError) -> Response:
    return Response(rid, None, error)


def run_context(run_id: str, cell_id: str | None) -> dict[str, Any]:
    return {"run_id": run_id, "cell_id": cell_id}
