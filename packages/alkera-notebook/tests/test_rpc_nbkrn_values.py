"""Encoding side of the RPC framing: every value the protocol carries
round-trips, and everything it cannot carry is refused."""

from __future__ import annotations

import datetime as dt
import decimal
import json
import math
import uuid
from typing import Any

import pytest
from alkera_notebook.rpc import frames as f

UTC = dt.UTC
ROUND_TRIP = [
    pytest.param(None, id="none"),
    pytest.param(True, id="bool"),
    pytest.param(2**53, id="max-safe-int"),
    pytest.param(2**53 + 1, id="big-int"),
    pytest.param(-(2**70), id="big-negative-int"),
    pytest.param(1.5, id="float"),
    pytest.param(math.inf, id="inf"),
    pytest.param(-math.inf, id="minus-inf"),
    pytest.param(decimal.Decimal("-0.000100"), id="decimal"),
    pytest.param(dt.datetime(2026, 10, 5, 1, 2, 3, 4, tzinfo=UTC), id="datetime-tz"),
    pytest.param(dt.datetime(2026, 10, 5, 1, 2, 3), id="datetime-naive"),
    pytest.param(dt.date(1999, 12, 31), id="date"),
    pytest.param(dt.time(23, 59, 59, 999999), id="time"),
    pytest.param(dt.timedelta(days=-3, seconds=5, microseconds=7), id="negative-timedelta"),
    pytest.param(dt.timedelta(days=400, hours=5, minutes=6, seconds=7), id="long-timedelta"),
    pytest.param(dt.timedelta(0), id="zero-timedelta"),
    pytest.param(b"\x00\xffbytes", id="bytes"),
    pytest.param(uuid.UUID(int=12345), id="uuid"),
    pytest.param({"$ref": 1, "plain": [1, {"$t": "x"}]}, id="dollar-keys"),
    pytest.param({"nested": {"list": [dt.date(2020, 1, 1), None]}}, id="nested"),
    pytest.param(f.Segment(b"\x01\x02", "arrow.ipc.stream", {"rows": 2}), id="segment"),
]


@pytest.mark.parametrize("value", ROUND_TRIP)
def test_rpc_value_round_trips_through_a_frame(value: Any) -> None:
    message = f.request(3, "m", {"v": value})
    data = f.encode_message(message, limit=f.FRAME_LIMIT_SERVICE)
    decoded = f.parse_message(f.decode_frame(data, limit=f.FRAME_LIMIT_SERVICE))
    assert isinstance(decoded, f.Request)
    params = f.decode_params(decoded.params, decoded.segments)
    assert params["v"] == value
    assert type(params["v"]) is type(value)
    if isinstance(value, dt.datetime):
        assert params["v"].tzinfo == value.tzinfo


def test_rpc_nan_round_trips() -> None:
    message = f.request(1, "m", {"v": math.nan})
    out = f.decode_params(message.params, message.segments)
    assert math.isnan(out["v"])


def test_rpc_tuples_and_bytearrays_travel_as_their_json_forms() -> None:
    message = f.request(1, "m", {"t": (1, 2), "b": bytearray(b"ab"), "m": memoryview(b"cd")})
    out = f.decode_params(message.params, message.segments)
    assert out == {"t": [1, 2], "b": b"ab", "m": b"cd"}


def test_rpc_files_are_encoded_and_resolve_only_inside_data_dir() -> None:
    ref = f.FileRef("q-1.arrow", "arrow.ipc.file", 10, "a" * 64)
    message = f.result_response(4, {"table": ref})
    assert message.result == {
        "table": {"$file": "q-1.arrow", "codec": "arrow.ipc.file", "bytes": 10, "sha256": "a" * 64}
    }
    assert f.decode_value(message.result, (), allow_files=True)["table"] == ref
    assert ref.path_in("/data/k1/") == "/data/k1/q-1.arrow"
    with pytest.raises(ValueError, match="plain name"):
        f.FileRef("../x", "arrow.ipc.file", 1, "a" * 64).path_in("/data")


@pytest.mark.parametrize(
    ("value", "error"),
    [
        pytest.param(object(), TypeError, id="object"),
        pytest.param({1: "x"}, TypeError, id="int-key"),
        pytest.param({1, 2}, TypeError, id="set"),
        pytest.param(lambda: 1, TypeError, id="function"),
    ],
)
def test_rpc_values_the_protocol_cannot_carry_are_refused(
    value: Any, error: type[Exception]
) -> None:
    with pytest.raises(error):
        f.request(1, "m", {"v": value})


def test_rpc_param_names_cannot_start_with_dollar() -> None:
    with pytest.raises(TypeError, match=r"\$"):
        f.encode_params({"$seg": 1})


def test_rpc_rows_codec_round_trips_with_fallback() -> None:
    columns = [("id", "int64"), ("at", "timestamp[us, tz=UTC]"), ("amount", "decimal")]
    rows = [[1, dt.datetime(2026, 1, 1, tzinfo=UTC), decimal.Decimal("1.10")], [2, None, None]]
    segment = f.rows_segment(columns, rows, fallback="pyarrow_missing")
    assert segment.codec == "rows.json"
    assert segment.meta == {"fallback": "pyarrow_missing"}
    table = f.read_rows_segment(segment)
    assert table.columns == tuple(columns)
    assert [list(r) for r in table.rows] == rows
    assert table.records()[1] == {"id": 2, "at": None, "amount": None}
    assert f.rows_segment(columns, []).meta == {}


@pytest.mark.parametrize(
    ("columns", "rows", "error"),
    [
        pytest.param([("a", "int")], [[1, 2]], ValueError, id="row-too-wide"),
        pytest.param([("a", "bytes")], [[f.Segment(b"x")]], TypeError, id="segment-in-cell"),
    ],
)
def test_rpc_rows_codec_refuses_bad_tables(columns: Any, rows: Any, error: type[Exception]) -> None:
    with pytest.raises(error):
        f.encode_rows(columns, rows)


@pytest.mark.parametrize(
    "obj",
    [
        pytest.param([], id="not-object"),
        pytest.param({"columns": [{"name": 1, "type": "x"}], "rows": []}, id="bad-column"),
        pytest.param({"columns": [{"name": "a", "type": "x"}], "rows": [[1, 2]]}, id="row-width"),
        pytest.param({"columns": "a", "rows": []}, id="columns-not-list"),
    ],
)
def test_rpc_rows_decoder_refuses_malformed_tables(obj: Any) -> None:
    with pytest.raises(f.TagError):
        f.decode_rows(obj)


def test_rpc_rows_segment_must_be_rows_json() -> None:
    with pytest.raises(f.TagError, match=r"rows\.json"):
        f.read_rows_segment(f.Segment(b"{}", "bytes"))
    with pytest.raises(f.TagError, match="not JSON"):
        f.read_rows_segment(f.Segment(b"{nope", "rows.json"))


@pytest.mark.parametrize(
    ("delta", "text"),
    [
        pytest.param(
            dt.timedelta(days=1, hours=2, minutes=3, seconds=4.5), "P1DT2H3M4.5S", id="mixed"
        ),
        pytest.param(dt.timedelta(microseconds=-1), "-P0DT0H0M0.000001S", id="tiny-negative"),
        pytest.param(dt.timedelta(0), "P0DT0H0M0S", id="zero"),
    ],
)
def test_rpc_durations_are_exact_iso_8601(delta: dt.timedelta, text: str) -> None:
    assert f.format_duration(delta) == text
    assert f.parse_duration(text) == delta


@pytest.mark.parametrize("text", ["P", "PT", "-P", "P1H", "1D", "PT1.1234567S", "P1DT"])
def test_rpc_malformed_durations_are_refused(text: str) -> None:
    with pytest.raises(f.TagError):
        f.parse_duration(text)


def test_rpc_frame_reader_handles_a_stream_split_anywhere() -> None:
    messages = [
        f.request(1, "a", {"x": 1}),
        f.notification("cell.stream", {"text": "héllo"}),
        f.result_response(1, {"blob": f.Segment(b"\x00" * 300)}),
    ]
    stream = b"".join(f.encode_message(m, limit=f.FRAME_LIMIT_SERVICE) for m in messages)
    for chunk in (1, 7, 64, len(stream)):
        reader = f.FrameReader(limit=f.FRAME_LIMIT_SERVICE)
        got = []
        for i in range(0, len(stream), chunk):
            got += [f.parse_message(fr) for fr in reader.feed(stream[i : i + chunk])]
        assert got == messages
        assert reader.at_frame_boundary()


def test_rpc_encode_frame_limits_and_header_checks() -> None:
    with pytest.raises(f.FrameTooLargeError) as info:
        f.encode_frame({"a": 1}, [b"x" * 100], limit=50)
    assert info.value.limit == 50 and info.value.size > 50
    with pytest.raises(ValueError, match="not JSON"):
        f.encode_frame({"a": math.nan}, limit=1000)
    raw = f.encode_frame({"jsonrpc": "2.0"}, [b"ab", b"c"], limit=1000)
    header_len = int.from_bytes(raw[4:8], "big")
    assert json.loads(raw[8 : 8 + header_len]) == {"jsonrpc": "2.0", "seg": [2, 1]}
    assert int.from_bytes(raw[:4], "big") == len(raw) - 4


def test_rpc_error_objects() -> None:
    assert f.RpcError.kernel_busy().retryable and f.RpcError.unavailable().retryable
    assert not f.RpcError.forbidden("outside_run").retryable
    err = f.RpcError.too_large(16, "use a file")
    assert err.to_json() == {
        "code": -32003,
        "message": "message too large",
        "data": {"limit": 16, "hint": "use a file", "name": "too_large"},
    }
    assert f.RpcError.from_json(err.to_json()) == err
    assert (
        f.RpcError(-32050, "x", {"name": "sql.statement_refused"}).name == "sql.statement_refused"
    )
    assert f.RpcError(-32099, "x").name == "error"
    assert f.RpcError.cancelled().code == f.ErrorCode.CANCELLED
    assert f.RpcError.method_not_found("x").data == {"name": "method_not_found"}
    assert f.RpcError.unauthorized().data == {"name": "unauthorized"}
    for bad in ("x", {"code": "1", "message": "m"}, {"code": 1, "message": "m", "data": []}):
        with pytest.raises(f.ProtocolError):
            f.RpcError.from_json(bad)


def test_rpc_message_headers() -> None:
    req = f.request(1, "m", {"a": 1}, {"run_id": "r", "cell_id": None})
    assert f.message_header(req) == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "m",
        "params": {"a": 1},
        "ctx": {"run_id": "r", "cell_id": None},
    }
    assert "ctx" not in f.message_header(f.request(2, "m"))
    assert f.message_header(f.error_response(3, f.RpcError.cancelled()))["error"]["code"] == -32004
    assert f.run_context("r", "c") == {"run_id": "r", "cell_id": "c"}
    assert f.Response(1, None).ok and not f.error_response(1, f.RpcError.cancelled()).ok


def test_rpc_method_and_event_names_are_unique() -> None:
    names = [*f.KERNEL_METHODS, f.SQL_EXECUTE, *f.KERNEL_EVENTS]
    assert len(names) == len(set(names))
    assert f.HELLO_METHOD not in names and f.CANCEL_METHOD not in names
