"""Writes ``vectors.json``: the RPC conformance vectors.

Frames are assembled here with ``struct`` and ``json`` directly, never with the
implementation under test, and every expected decoded value is spelled out by
hand, so replaying the vectors checks the implementation against the protocol
text rather than against itself. Re-run only to add vectors:

    uv run python packages/alkera-notebook/tests/rpc_vectors/make_vectors.py
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
MiB = 1024 * 1024


def frame(
    header: dict[str, Any] | bytes,
    segments: list[bytes] = (),
    *,
    seg: Any = "auto",
    total: int | None = None,
    header_length: int | None = None,
) -> str:  # type: ignore[assignment]
    if isinstance(header, dict):
        h = dict(header)
        if seg == "auto":
            h["seg"] = [len(s) for s in segments]
        elif seg is not None:
            h["seg"] = seg
        raw = json.dumps(h, separators=(",", ":"), ensure_ascii=False).encode()
    else:
        raw = header
    hl = len(raw) if header_length is None else header_length
    t = 4 + len(raw) + sum(len(s) for s in segments) if total is None else total
    return (struct.pack(">II", t, hl) + raw + b"".join(segments)).hex()


def py(kind: str, v: Any, **extra: Any) -> dict[str, Any]:
    return {"py": kind, "v": v, **extra}


V: list[dict[str, Any]] = []


def valid(
    name: str, hex_: str, *, kind: str, expect: dict[str, Any], direction: str = "service_to_client"
) -> None:
    V.append(
        {
            "name": name,
            "direction": direction,
            "hex": hex_,
            "valid": True,
            "kind": kind,
            "expect": expect,
        }
    )


def invalid(name: str, hex_: str, *, reason: str, direction: str = "service_to_client") -> None:
    V.append({"name": name, "direction": direction, "hex": hex_, "valid": False, "reason": reason})


def value_error(name: str, hex_: str, *, direction: str = "service_to_client") -> None:
    """A frame whose envelope is valid but whose params do not decode: the
    receiver answers ``invalid_params`` (no disconnect)."""
    V.append(
        {
            "name": name,
            "direction": direction,
            "hex": hex_,
            "valid": True,
            "kind": "request",
            "params_error": True,
        }
    )


# --- envelope ---------------------------------------------------------------

valid(
    "request.basic",
    frame(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "run.execute",
            "params": {"run_id": "r1"},
            "ctx": {"run_id": "r1", "cell_id": "c1"},
        }
    ),
    kind="request",
    expect={
        "id": 7,
        "method": "run.execute",
        "params": {"run_id": "r1"},
        "ctx": {"run_id": "r1", "cell_id": "c1"},
    },
)
valid(
    "request.no_params",
    frame({"jsonrpc": "2.0", "id": 1, "method": "kernel.shutdown"}),
    kind="request",
    expect={"id": 1, "method": "kernel.shutdown", "params": {}, "ctx": None},
)
valid(
    "request.unknown_members_ignored",
    frame(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "complete",
            "params": {"code": "x."},
            "trace": {"a": 1},
            "x-future": [1, 2],
        }
    ),
    kind="request",
    expect={"id": 2, "method": "complete", "params": {"code": "x."}, "ctx": None},
)
valid(
    "request.missing_seg_means_none",
    frame(
        {"jsonrpc": "2.0", "id": 3, "method": "names.delete", "params": {"names": ["a"]}}, seg=None
    ),
    kind="request",
    expect={"id": 3, "method": "names.delete", "params": {"names": ["a"]}, "ctx": None},
)
valid(
    "notification.basic",
    frame(
        {
            "jsonrpc": "2.0",
            "method": "cell.stream",
            "params": {"run_id": "r", "cell_id": "c", "name": "stdout", "text": "héllo\n"},
        }
    ),
    kind="notification",
    expect={
        "method": "cell.stream",
        "params": {"run_id": "r", "cell_id": "c", "name": "stdout", "text": "héllo\n"},
    },
    direction="client_to_service",
)
valid(
    "notification.cancel",
    frame({"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": 12}}),
    kind="notification",
    expect={"method": "$/cancelRequest", "params": {"id": 12}},
)
valid(
    "response.result",
    frame({"jsonrpc": "2.0", "id": 7, "result": {"accepted": True}}),
    kind="response",
    expect={"id": 7, "result": {"accepted": True}},
    direction="client_to_service",
)
valid(
    "response.null_result",
    frame({"jsonrpc": "2.0", "id": 8, "result": None}),
    kind="response",
    expect={"id": 8, "result": None},
)
valid(
    "response.error_forbidden",
    frame(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "error": {
                "code": -32002,
                "message": "forbidden",
                "data": {"name": "forbidden", "reason": "outside_run"},
            },
        }
    ),
    kind="response",
    expect={
        "id": 9,
        "error": {
            "code": -32002,
            "name": "forbidden",
            "data": {"name": "forbidden", "reason": "outside_run"},
        },
    },
)
valid(
    "response.error_cancelled",
    frame(
        {
            "jsonrpc": "2.0",
            "id": 12,
            "error": {"code": -32004, "message": "cancelled", "data": {"name": "cancelled"}},
        }
    ),
    kind="response",
    expect={
        "id": 12,
        "error": {"code": -32004, "name": "cancelled", "data": {"name": "cancelled"}},
    },
)
valid(
    "response.error_without_data",
    frame(
        {"jsonrpc": "2.0", "id": 10, "error": {"code": -32601, "message": "method not found: x"}}
    ),
    kind="response",
    expect={
        "id": 10,
        "error": {"code": -32601, "name": "method_not_found", "data": {"name": "method_not_found"}},
    },
)

# --- framing errors ---------------------------------------------------------

good = bytes.fromhex(frame({"jsonrpc": "2.0", "id": 1, "method": "complete", "params": {}}))
invalid("frame.empty", "", reason="truncated")
invalid("frame.three_bytes", "000000", reason="truncated")
invalid("frame.body_truncated", good[:-3].hex(), reason="truncated")
invalid("frame.trailing_bytes", (good + b"\x00").hex(), reason="length_mismatch")
invalid("frame.total_below_four", struct.pack(">II", 3, 0).hex(), reason="length_mismatch")
invalid(
    "frame.header_length_past_end",
    frame({"jsonrpc": "2.0", "id": 1, "method": "m"}, header_length=500),
    reason="length_mismatch",
)
invalid(
    "frame.segments_do_not_add_up",
    frame({"jsonrpc": "2.0", "id": 1, "method": "m"}, [b"abc"], seg=[5]),
    reason="length_mismatch",
)
invalid(
    "frame.oversized_client_to_service",
    struct.pack(">II", 16 * MiB + 1, 2).hex() + "7b7d",
    reason="too_large",
    direction="client_to_service",
)
invalid(
    "frame.oversized_service_to_client",
    struct.pack(">II", 64 * MiB + 1, 2).hex() + "7b7d",
    reason="too_large",
    direction="service_to_client",
)
invalid("frame.header_not_json", frame(b"{nope"), reason="bad_header")
invalid("frame.header_not_utf8", frame(b'{"a":"\xff"}'), reason="bad_header")
invalid("frame.header_nan_constant", frame(b'{"x":NaN,"seg":[]}'), reason="bad_header")
invalid("frame.header_is_array", frame(b"[1,2]"), reason="header_not_object")
invalid(
    "frame.seg_not_a_list",
    frame({"jsonrpc": "2.0", "id": 1, "method": "m"}, seg="3"),
    reason="bad_segments",
)
invalid(
    "frame.seg_negative",
    frame({"jsonrpc": "2.0", "id": 1, "method": "m"}, seg=[-1]),
    reason="bad_segments",
)
invalid("message.no_jsonrpc", frame({"id": 1, "method": "m"}), reason="invalid_message")
invalid(
    "message.string_id",
    frame({"jsonrpc": "2.0", "id": "1", "method": "m"}),
    reason="invalid_message",
)
invalid(
    "message.bool_id",
    frame({"jsonrpc": "2.0", "id": True, "method": "m"}),
    reason="invalid_message",
)
invalid(
    "message.params_array",
    frame({"jsonrpc": "2.0", "id": 1, "method": "m", "params": [1]}),
    reason="invalid_message",
)
invalid(
    "message.ctx_string",
    frame({"jsonrpc": "2.0", "id": 1, "method": "m", "ctx": "r1"}),
    reason="invalid_message",
)
invalid(
    "message.result_and_error",
    frame({"jsonrpc": "2.0", "id": 1, "result": 1, "error": {"code": 1, "message": "x"}}),
    reason="invalid_message",
)
invalid(
    "message.response_without_id", frame({"jsonrpc": "2.0", "result": 1}), reason="invalid_message"
)
invalid(
    "message.error_without_code",
    frame({"jsonrpc": "2.0", "id": 1, "error": {"message": "x"}}),
    reason="invalid_message",
)

# --- tagged values ----------------------------------------------------------


def req(params: dict[str, Any], segments: list[bytes] = ()) -> str:  # type: ignore[assignment]
    return frame({"jsonrpc": "2.0", "id": 5, "method": "t", "params": params}, list(segments))


def valued(
    name: str,
    params: dict[str, Any],
    expect: dict[str, Any],
    segments: list[bytes] = (),
    *,
    direction: str = "service_to_client",
) -> None:  # type: ignore[assignment]
    valid(
        name,
        req(params, list(segments)),
        kind="request",
        expect={"id": 5, "method": "t", "params": expect, "ctx": None},
        direction=direction,
    )


valued(
    "value.plain_json",
    {"a": [1, 2.5, "x", True, None, {"b": []}]},
    {"a": [1, 2.5, "x", True, None, {"b": []}]},
)
valued("value.decimal", {"v": {"$t": "decimal", "v": "12.3400"}}, {"v": py("decimal", "12.3400")})
valued(
    "value.datetime_tz",
    {"v": {"$t": "datetime", "v": "2026-10-05T12:30:00.250000+00:00"}},
    {"v": py("datetime", "2026-10-05T12:30:00.250000+00:00")},
)
valued(
    "value.datetime_naive",
    {"v": {"$t": "datetime", "v": "2026-10-05T12:30:00"}},
    {"v": py("datetime", "2026-10-05T12:30:00")},
)
valued("value.date", {"v": {"$t": "date", "v": "2026-10-05"}}, {"v": py("date", "2026-10-05")})
valued(
    "value.time",
    {"v": {"$t": "time", "v": "23:59:59.000001"}},
    {"v": py("time", "23:59:59.000001")},
)
valued(
    "value.timedelta",
    {"v": {"$t": "timedelta", "v": "P1DT2H3M4.5S"}},
    {"v": py("timedelta_us", 93784500000)},
)
valued(
    "value.timedelta_negative",
    {"v": {"$t": "timedelta", "v": "-P0DT0H0M0.000001S"}},
    {"v": py("timedelta_us", -1)},
)
valued(
    "value.timedelta_short_form",
    {"v": {"$t": "timedelta", "v": "PT90S"}},
    {"v": py("timedelta_us", 90000000)},
)
valued("value.bytes", {"v": {"$t": "bytes", "v": "AAEC/w=="}}, {"v": py("bytes", "000102ff")})
valued("value.float_nan", {"v": {"$t": "float", "v": "nan"}}, {"v": py("float", "nan")})
valued(
    "value.float_inf",
    {"v": {"$t": "float", "v": "inf"}, "w": {"$t": "float", "v": "-inf"}},
    {"v": py("float", "inf"), "w": py("float", "-inf")},
)
valued(
    "value.uuid",
    {"v": {"$t": "uuid", "v": "12345678-1234-5678-1234-567812345678"}},
    {"v": py("uuid", "12345678-1234-5678-1234-567812345678")},
)
valued(
    "value.big_int",
    {"v": {"$t": "int", "v": "-123456789012345678901234567890"}},
    {"v": py("int", "-123456789012345678901234567890")},
)
valued(
    "value.escaped_object",
    {"v": {"$t": "object", "v": {"$seg": 1, "$t": "x", "k": {"$t": "date", "v": "2020-01-02"}}}},
    {"v": py("dict", {"$seg": 1, "$t": "x", "k": py("date", "2020-01-02")})},
)
valued(
    "value.segments",
    {
        "a": {"$seg": 0, "codec": "bytes", "meta": {}},
        "b": {"$seg": 1, "codec": "arrow.ipc.stream", "meta": {"rows": 3}},
    },
    {
        "a": py("segment", "00ff", codec="bytes", meta={}),
        "b": py("segment", "414243", codec="arrow.ipc.stream", meta={"rows": 3}),
    },
    [b"\x00\xff", b"ABC"],
)
valued(
    "value.segment_defaults",
    {"a": {"$seg": 0}},
    {"a": py("segment", "", codec="bytes", meta={})},
    [b""],
)
rows_json = json.dumps(
    {
        "columns": [{"name": "id", "type": "int64"}, {"name": "at", "type": "date"}],
        "rows": [[1, {"$t": "date", "v": "2026-01-01"}], [2, None]],
    },
    separators=(",", ":"),
).encode()
valued(
    "value.rows_json_fallback",
    {"table": {"$seg": 0, "codec": "rows.json", "meta": {"fallback": "pyarrow_missing"}}},
    {
        "table": py(
            "rows",
            {
                "columns": [["id", "int64"], ["at", "date"]],
                "rows": [[1, py("date", "2026-01-01")], [2, None]],
            },
            meta={"fallback": "pyarrow_missing"},
        )
    },
    [rows_json],
)
valued(
    "value.file_from_service",
    {"table": {"$file": "q-1.arrow", "codec": "arrow.ipc.file", "bytes": 1024, "sha256": "a" * 64}},
    {"table": py("file", "q-1.arrow", codec="arrow.ipc.file", bytes=1024, sha256="a" * 64)},
)
value_error(
    "value.file_from_client_refused",
    req({"t": {"$file": "q.arrow", "codec": "arrow.ipc.file", "bytes": 1, "sha256": "a" * 64}}),
    direction="client_to_service",
)
value_error(
    "value.file_path_escape",
    req(
        {"t": {"$file": "../etc/passwd", "codec": "arrow.ipc.file", "bytes": 1, "sha256": "a" * 64}}
    ),
)
value_error(
    "value.file_wrong_codec",
    req({"t": {"$file": "q.json", "codec": "rows.json", "bytes": 1, "sha256": "a" * 64}}),
)
value_error("value.segment_out_of_range", req({"a": {"$seg": 1}}, [b"x"]))
value_error("value.unknown_tag", req({"a": {"$t": "pickle", "v": "gASV"}}))
value_error("value.unknown_dollar_key", req({"a": {"$ref": "x"}}))
value_error("value.bad_base64", req({"a": {"$t": "bytes", "v": "@@@"}}))
value_error("value.bad_int", req({"a": {"$t": "int", "v": "12.5"}}))
value_error("value.bad_duration", req({"a": {"$t": "timedelta", "v": "P"}}))
value_error("value.tag_without_string", req({"a": {"$t": "decimal", "v": 1.5}}))


def main() -> None:
    names = [v["name"] for v in V]
    assert len(names) == len(set(names)), "duplicate vector names"
    out = HERE / "vectors.json"
    out.write_text(
        json.dumps({"protocol": 1, "vectors": V}, indent=1, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(V)} vectors to {out}")


if __name__ == "__main__":
    main()
