"""The socketpair protocol: a closed set of frames, nothing else."""

from __future__ import annotations

import asyncio
import json

import pytest
from alkera_cli.org_worker_protocol import (
    MAX_FRAME_BYTES,
    Credential,
    Hello,
    ProtocolError,
    Ready,
    Refused,
    Route,
    Status,
    Stop,
    decode_to_supervisor,
    decode_to_worker,
    encode,
    read_line,
)

ORG = "8ef423bc-531e-4d2a-baa5-8bfb6e9e8863"
SECRET = "alkm_secret_value"


@pytest.mark.parametrize(
    "frame",
    [
        Hello(org_id=ORG, slot=2, machine_id="m-1", credential=SECRET),
        Credential(credential=SECRET),
        Route(chat_ids=("c-1", "c-2")),
        Route(),
        Stop(),
    ],
)
def test_a_frame_to_a_worker_round_trips(frame: object) -> None:
    assert decode_to_worker(encode(frame)) == frame  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "frame",
    [Ready(), Status(chats_served=2, chats_busy=1, rss_bytes=10), Refused(chat_id="c", reason="r")],
)
def test_a_frame_to_the_supervisor_round_trips(frame: object) -> None:
    assert decode_to_supervisor(encode(frame)) == frame  # type: ignore[arg-type]


def test_the_credential_is_never_in_a_repr() -> None:
    assert SECRET not in repr(Hello(org_id=ORG, slot=0, machine_id="m", credential=SECRET))
    assert SECRET not in repr(Credential(credential=SECRET))


def test_an_org_is_keyed_in_one_spelling() -> None:
    hello = Hello(org_id=ORG.upper(), slot=0, machine_id="m", credential="c")
    assert hello.org_id == ORG


def _line(payload: object) -> bytes:
    return json.dumps(payload).encode() + b"\n"


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(b"not json\n", id="not-json"),
        pytest.param(_line({"type": "launch", "argv": ["sh"]}), id="unknown-type"),
        pytest.param(_line({"type": "route", "chat_ids": ["a"], "org_id": ORG}), id="extra-field"),
        pytest.param(_line({"type": "route", "chat_ids": ["../x"]}), id="path-shaped-id"),
        pytest.param(
            _line(
                {"type": "hello", "org_id": "acme", "slot": 0, "machine_id": "m", "credential": "c"}
            ),
            id="not-an-org",
        ),
        pytest.param(
            _line(
                {"type": "hello", "org_id": ORG, "slot": -1, "machine_id": "m", "credential": "c"}
            ),
            id="negative-slot",
        ),
        pytest.param(_line({"type": "ready"}), id="a-supervisor-frame"),
        pytest.param(b"x" * (MAX_FRAME_BYTES + 1), id="past-the-limit"),
    ],
)
def test_anything_else_to_a_worker_is_a_protocol_error(line: bytes) -> None:
    with pytest.raises(ProtocolError):
        decode_to_worker(line)


def test_a_worker_cannot_speak_a_supervisor_frame() -> None:
    with pytest.raises(ProtocolError):
        decode_to_supervisor(encode(Stop()))
    with pytest.raises(ProtocolError):
        decode_to_supervisor(
            _line({"type": "status", "chats_served": -1, "chats_busy": 0, "rss_bytes": 0})
        )


async def test_a_stream_cut_inside_a_frame_is_an_error_and_a_clean_end_is_none() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"type": "st')
    reader.feed_eof()
    with pytest.raises(ProtocolError):
        await read_line(reader)
    clean = asyncio.StreamReader()
    clean.feed_eof()
    assert await read_line(clean) is None
