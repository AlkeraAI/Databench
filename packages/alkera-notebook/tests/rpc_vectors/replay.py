"""Replays ``vectors.json`` against an implementation of the RPC framing.

Standard library only and Python 3.10 syntax, so the same replay runs in the
test process (against ``alkera_notebook.rpc.frames`` and the kernel's copy) and
under every interpreter the kernel supports (``python replay.py <frames.py>``).
Exit status 0 when every vector behaves as specified.
"""

from __future__ import annotations

import datetime as dt
import decimal
import importlib.util
import json
import math
import sys
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

HERE = Path(__file__).resolve().parent


def load_frames(path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location("_frames_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_vectors() -> list[dict[str, Any]]:
    data = json.loads((HERE / "vectors.json").read_text(encoding="utf-8"))
    assert data["protocol"] == 1
    return list(data["vectors"])


def expected(obj: Any, fm: ModuleType) -> Any:
    """The Python value an expectation names (independent of the decoder)."""
    if isinstance(obj, list):
        return [expected(v, fm) for v in obj]
    if not isinstance(obj, dict):
        return obj
    if "py" not in obj:
        return {k: expected(v, fm) for k, v in obj.items()}
    kind, v = obj["py"], obj["v"]
    if kind == "decimal":
        return decimal.Decimal(v)
    if kind == "datetime":
        return dt.datetime.fromisoformat(v)
    if kind == "date":
        return dt.date.fromisoformat(v)
    if kind == "time":
        return dt.time.fromisoformat(v)
    if kind == "timedelta_us":
        return dt.timedelta(microseconds=v)
    if kind == "bytes":
        return bytes.fromhex(v)
    if kind == "float":
        return float(v)
    if kind == "uuid":
        return uuid.UUID(v)
    if kind == "int":
        return int(v)
    if kind == "dict":
        return {k: expected(x, fm) for k, x in v.items()}
    if kind == "segment":
        return ("segment", bytes.fromhex(v), obj["codec"], obj["meta"])
    if kind == "rows":
        cols = tuple(tuple(c) for c in v["columns"])
        rows = tuple(tuple(expected(c, fm) for c in r) for r in v["rows"])
        return ("rows", cols, rows, obj["meta"])
    if kind == "file":
        return ("file", v, obj["codec"], obj["bytes"], obj["sha256"])
    raise ValueError(f"unknown expectation {kind}")


def normalize(value: Any, fm: ModuleType) -> Any:
    """Decoded values in the shape ``expected`` builds."""
    if isinstance(value, list):
        return [normalize(v, fm) for v in value]
    if isinstance(value, dict):
        return {k: normalize(v, fm) for k, v in value.items()}
    if isinstance(value, fm.Segment):
        if value.codec == fm.CODEC_ROWS:
            table = fm.read_rows_segment(value)
            return ("rows", table.columns, table.rows, value.meta)
        return ("segment", value.data, value.codec, value.meta)
    if isinstance(value, fm.FileRef):
        return ("file", value.name, value.codec, value.bytes, value.sha256)
    return value


def same(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    if type(a) is not type(b):
        return False
    if isinstance(a, (list, tuple)):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, dt.datetime):
        return a == b and a.tzinfo == b.tzinfo
    return bool(a == b)


def limit_for(fm: ModuleType, direction: str) -> int:
    return fm.FRAME_LIMIT_CLIENT if direction == "client_to_service" else fm.FRAME_LIMIT_SERVICE


def check(vector: dict[str, Any], fm: ModuleType) -> str | None:
    """None when the vector behaves as specified, else what went wrong."""
    data = bytes.fromhex(vector["hex"])
    limit = limit_for(fm, vector["direction"])
    allow_files = vector["direction"] == "service_to_client"
    if not vector["valid"]:
        try:
            msg = fm.parse_message(fm.decode_frame(data, limit=limit))
        except fm.ProtocolError as exc:
            if exc.reason != vector["reason"]:
                return f"rejected with {exc.reason}, expected {vector['reason']}"
        else:
            return f"accepted {msg!r}, expected rejection {vector['reason']}"
        # The incremental reader must agree (it may simply wait on a short frame).
        reader = fm.FrameReader(limit=limit)
        try:
            frames = reader.feed(data)
            if vector["reason"] == "truncated":
                return None if not frames else "reader produced a frame from a truncated input"
            for frame in frames:
                fm.parse_message(frame)
            if vector["reason"] == "length_mismatch" and frames and reader.buffered:
                return None  # trailing bytes stay buffered as the start of a frame
            return "reader accepted an invalid frame"
        except fm.ProtocolError as exc:
            return None if exc.reason == vector["reason"] else f"reader: {exc.reason}"
    try:
        frame = fm.decode_frame(data, limit=limit)
        message = fm.parse_message(frame)
        (again,) = fm.FrameReader(limit=limit).feed(data)
        if again != frame:
            return "the incremental reader decoded a different frame"
    except fm.ProtocolError as exc:
        return f"rejected a valid frame: {exc}"
    kind = vector["kind"]
    expect = vector.get("expect", {})
    if kind == "request":
        if not isinstance(message, fm.Request):
            return f"decoded {type(message).__name__}, expected Request"
        try:
            params = fm.decode_params(message.params, message.segments, allow_files=allow_files)
        except fm.TagError:
            return None if vector.get("params_error") else "params did not decode"
        if vector.get("params_error"):
            return f"params decoded to {params!r}, expected a refusal"
        got = {
            "id": message.id,
            "method": message.method,
            "params": normalize(params, fm),
            "ctx": message.ctx,
        }
    elif kind == "notification":
        if not isinstance(message, fm.Notification):
            return f"decoded {type(message).__name__}, expected Notification"
        got = {
            "method": message.method,
            "params": normalize(fm.decode_params(message.params, message.segments), fm),
        }
    else:
        if not isinstance(message, fm.Response):
            return f"decoded {type(message).__name__}, expected Response"
        got = {"id": message.id}
        if message.error is not None:
            got["error"] = {
                "code": message.error.code,
                "name": message.error.name,
                "data": message.error.data,
            }
        else:
            got["result"] = message.result
    want = expected(expect, fm)
    if not same(got, want):
        return f"decoded {got!r}, expected {want!r}"
    # Re-encoding the decoded message reproduces an equivalent frame.
    if kind != "request" or not vector.get("params_error"):
        again_frame = fm.decode_frame(fm.encode_message(message, limit=limit), limit=limit)
        if fm.parse_message(again_frame) != message:
            return "re-encoding changed the message"
    return None


def run(frames_path: str) -> list[str]:
    fm = load_frames(frames_path)
    failures = []
    for vector in load_vectors():
        problem = check(vector, fm)
        if problem is not None:
            failures.append(f"{vector['name']}: {problem}")
    return failures


if __name__ == "__main__":
    problems = run(sys.argv[1])
    for p in problems:
        print(p)
    print(f"{len(load_vectors()) - len(problems)} passed, {len(problems)} failed")
    sys.exit(1 if problems else 0)
