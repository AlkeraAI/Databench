"""The frame codec between the backend and a sandbox worker, and the worker's
dispatch of requests it must refuse rather than crash on."""

from __future__ import annotations

import asyncio
import io
import struct

import pytest
from backend.services.crdt.sandbox import core, worker
from backend.services.crdt.sandbox.protocol import (
    MAX_BLOB_BYTES,
    MAX_BLOBS,
    MAX_HEADER_BYTES,
    Frame,
    FrameError,
    decode_frame,
    encode_frame,
    read_frame,
)

U32 = struct.Struct(">I")


@pytest.mark.parametrize(
    "frame",
    [
        Frame({"op": "ping"}),
        Frame({"op": "x", "n": 1, "nested": {"a": [1, 2]}}, (b"",)),
        Frame({"op": "x"}, (b"\x00\xff" * 100, b"", bytes(range(256)))),
    ],
)
def test_a_frame_round_trips_through_bytes_and_a_stream(frame: Frame) -> None:
    data = encode_frame(frame)
    assert decode_frame(data) == frame

    async def streamed() -> Frame:
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()
        return await read_frame(reader)

    assert asyncio.run(streamed()) == frame
    assert worker.read_request(io.BytesIO(data)) == frame


def _raw(header: bytes, *blobs: bytes, count: int | None = None) -> bytes:
    parts = [U32.pack(len(header)), header, U32.pack(len(blobs) if count is None else count)]
    for blob in blobs:
        parts += [U32.pack(len(blob)), blob]
    return b"".join(parts)


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(U32.pack(MAX_HEADER_BYTES + 1), id="header-over-cap"),
        pytest.param(_raw(b"[1,2]"), id="header-not-an-object"),
        pytest.param(_raw(b"\xff\xfe"), id="header-not-utf8"),
        pytest.param(_raw(b"{not json"), id="header-not-json"),
        pytest.param(_raw(b"{}", count=MAX_BLOBS + 1), id="too-many-blobs"),
        pytest.param(_raw(b"{}", count=1) + U32.pack(MAX_BLOB_BYTES + 1), id="blob-over-cap"),
        pytest.param(_raw(b"{}", b"abc")[:-1], id="truncated-blob"),
        pytest.param(_raw(b"{}")[:-2], id="truncated-count"),
    ],
)
def test_a_frame_that_breaks_the_format_is_refused(data: bytes) -> None:
    with pytest.raises(FrameError):
        decode_frame(data)
    with pytest.raises(FrameError):
        worker.read_request(io.BytesIO(data))


def test_trailing_bytes_are_refused() -> None:
    with pytest.raises(FrameError, match="trailing"):
        decode_frame(encode_frame(Frame({"op": "ping"})) + b"x")


def test_an_empty_stream_is_a_clean_end_for_the_worker() -> None:
    assert worker.read_request(io.BytesIO(b"")) is None


def test_encoding_refuses_what_decoding_would() -> None:
    with pytest.raises(FrameError):
        encode_frame(Frame({"pad": "x" * MAX_HEADER_BYTES}))
    with pytest.raises(FrameError):
        encode_frame(Frame({}, (b"",) * (MAX_BLOBS + 1)))
    with pytest.raises(ValueError):
        encode_frame(Frame({"n": float("nan")}))


@pytest.mark.parametrize(
    ("header", "blobs", "code"),
    [
        pytest.param(
            {"op": "nope", "key": "k", "epoch": 1, "log_seq": 0}, (), "bad_request", id="unknown-op"
        ),
        pytest.param(
            {"op": "validate", "epoch": 1, "log_seq": 0}, (b"",), "bad_request", id="no-key"
        ),
        pytest.param(
            {"op": "validate", "key": "k", "epoch": True, "log_seq": 0},
            (b"",),
            "bad_request",
            id="bool-epoch",
        ),
        pytest.param(
            {"op": "validate", "key": "k", "epoch": 0, "log_seq": 0},
            (b"",),
            "bad_request",
            id="epoch-zero",
        ),
        pytest.param(
            {"op": "validate", "key": "k", "epoch": 1, "log_seq": -1},
            (b"",),
            "bad_request",
            id="negative-log",
        ),
        pytest.param(
            {
                "op": "validate",
                "key": "k",
                "epoch": 1,
                "log_seq": 0,
                "rules": "chat_draft",
                "peers": [5],
            },
            (b"",),
            "bad_request",
            id="server-peer",
        ),
        pytest.param(
            {
                "op": "validate",
                "key": "k",
                "epoch": 1,
                "log_seq": 0,
                "rules": "chat_draft",
                "peers": [],
            },
            (b"",),
            "bad_request",
            id="no-peers",
        ),
        pytest.param(
            {
                "op": "validate",
                "key": "k",
                "epoch": 1,
                "log_seq": 0,
                "rules": "chat_draft",
                "peers": [5000, True],
            },
            (b"",),
            "bad_request",
            id="bool-peer",
        ),
        pytest.param(
            {
                "op": "validate",
                "key": "k",
                "epoch": 1,
                "log_seq": 0,
                "rules": "chat_draft",
                "peers": 5000,
            },
            (b"",),
            "bad_request",
            id="peers-not-a-list",
        ),
        pytest.param(
            {
                "op": "validate",
                "key": "k",
                "epoch": 1,
                "log_seq": 0,
                "rules": "chat_draft",
                "peer": 5000,
            },
            (b"",),
            "bad_request",
            id="old-single-peer-field",
        ),
        pytest.param(
            {
                "op": "validate",
                "key": "k",
                "epoch": 1,
                "log_seq": 0,
                "rules": "chat_draft",
                "peers": list(range(2000, 6097)),
            },
            (b"",),
            "bad_request",
            id="too-many-peers",
        ),
        pytest.param(
            {"op": "validate", "key": "k", "epoch": 1, "log_seq": 0},
            (),
            "bad_request",
            id="no-update",
        ),
        pytest.param(
            {"op": "load", "key": "k", "epoch": 1, "log_seq": 0, "rules": "chat_draft"},
            (b"",),
            "bad_request",
            id="load-without-vector",
        ),
        pytest.param(
            {"op": "seed", "key": "k", "epoch": 1, "rules": "chat_draft", "peer": 4000},
            (b"\xff\xfe",),
            "bad_request",
            id="seed-text-not-utf8",
        ),
        pytest.param(
            {"op": "seed", "key": "k", "epoch": 1, "rules": "chat_draft", "peer": 4000},
            (),
            "bad_request",
            id="seed-without-its-text",
        ),
        pytest.param(
            {"op": "seed", "key": "k", "epoch": 1, "rules": "chat_draft", "peer": 4000},
            (b"a", b"b", b"c"),
            "bad_request",
            id="seed-with-too-many-blobs",
        ),
        pytest.param(
            {"op": "seed", "key": "k", "epoch": 1, "rules": "other", "peer": 4000},
            (b"",),
            "unknown_rules",
            id="unknown-rules",
        ),
        pytest.param(
            {"op": "merge", "key": "k", "epoch": 1, "log_seq": 0, "rules": "file", "peer": 4000},
            (b"",),
            "bad_request",
            id="merge-without-its-text",
        ),
        pytest.param(
            {"op": "content", "key": "k", "epoch": 1, "log_seq": 0, "rules": "file"},
            (b"", b""),
            "bad_request",
            id="content-with-two-versions",
        ),
        pytest.param(
            {"op": "ephemeral", "rules": "chat_draft", "peer": 5000},
            (),
            "bad_request",
            id="ephemeral-without-data",
        ),
    ],
)
def test_the_worker_refuses_a_malformed_request_in_band(
    header: dict[str, object], blobs: tuple[bytes, ...], code: str
) -> None:
    reply = worker.handle(Frame(header, blobs), core.DocCache())
    assert reply.header["ok"] is False
    assert reply.header["code"] == code


def test_the_worker_serves_requests_in_order_until_its_input_ends() -> None:
    requests = encode_frame(Frame({"op": "ping"})) + encode_frame(Frame({"op": "stats"}))
    out = io.BytesIO()
    worker.serve(io.BytesIO(requests), out, core.DocCache())

    async def replies() -> list[Frame]:
        reader = asyncio.StreamReader()
        reader.feed_data(out.getvalue())
        reader.feed_eof()
        frames = [await read_frame(reader), await read_frame(reader)]
        assert reader.at_eof()
        return frames

    pong, stats = asyncio.run(replies())
    assert pong.header["loro"] == core.LORO_VERSION
    assert stats.header == {"ok": True, "docs": 0, "bytes": 0}
