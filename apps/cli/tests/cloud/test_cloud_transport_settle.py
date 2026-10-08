"""An ``error`` frame settles only the durable op it names.

The server answers a refused op with the op's own id; a chunk's refusal names
the chunk's id and a refused hello names none. The socket therefore never
settles a durable op on another lane's verdict — an unanswered op waits for
its ack timeout and its owner resends it — so a transcript entry cannot be
dropped because a token chunk beside it was too large."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from alkera_cli.cloud import CloudRestClient, CloudSocket
from alkera_cli.cloud.transport import DocOpError, _Outstanding
from alkera_core.schemas.realtime import DocEnvelope, DocFrame, ErrorPayload

CHAT_ID = "chat-settle"


def _socket() -> CloudSocket:
    rest = CloudRestClient(
        api_url="http://api.test",
        token="device-jwt",
        agent_id=CHAT_ID,
        transport=httpx.MockTransport(lambda r: httpx.Response(404)),
    )
    return CloudSocket(rest)


def _error_frame(doc_id: str, code: str, *, op_id: str | None) -> DocFrame:
    return DocFrame(
        envelope=DocEnvelope(
            doc_id=doc_id,
            doc_type="chat",
            epoch=1,
            peer_id="server",
            seq=0,
            kind="error",
            payload=ErrorPayload(code=code, message="refused", op_id=op_id).model_dump(mode="json"),
        )
    )


async def test_an_error_without_a_matching_op_id_settles_no_durable_op() -> None:
    socket = _socket()
    doc = socket.open_doc("chat", CHAT_ID)
    doc.epoch = 1
    doc.live.set()
    loop = asyncio.get_running_loop()
    first: asyncio.Future[dict[str, Any]] = loop.create_future()
    second: asyncio.Future[dict[str, Any]] = loop.create_future()
    doc.outstanding.append(_Outstanding(op_id="append-e1", future=first))
    doc.outstanding.append(_Outstanding(op_id="append-e2", future=second))

    # A chunk-lane verdict names the chunk's own id: the oldest append is untouched.
    await socket._on_envelope(_error_frame(CHAT_ID, "chunk_too_large", op_id="chunk-abcd"))
    assert not first.done() and not second.done()
    # A verdict that names no op at all (a refused hello) touches nothing either.
    await socket._on_envelope(_error_frame(CHAT_ID, "forbidden", op_id=None))
    assert not first.done() and not second.done()
    # A verdict naming the SECOND op settles exactly that one — not the head.
    await socket._on_envelope(_error_frame(CHAT_ID, "op_too_large", op_id="append-e2"))
    assert not first.done()
    with pytest.raises(DocOpError) as refused:
        second.result()
    assert refused.value.code == "op_too_large"
    assert [entry.op_id for entry in doc.outstanding] == ["append-e1"]
    first.cancel()


async def test_a_refused_hello_is_recorded_only_when_no_op_is_named() -> None:
    socket = _socket()
    doc = socket.open_doc("chat", CHAT_ID)
    doc.epoch = 1
    doc.hello_pending = True
    await socket._on_envelope(_error_frame(CHAT_ID, "not_found", op_id=None))
    assert doc.error == "not_found" and doc.hello_pending is False


async def test_a_refused_hello_does_not_take_a_queued_ops_place() -> None:
    """An op queued behind a pending hello is not the hello's failure: it
    keeps waiting for its own answer."""
    socket = _socket()
    doc = socket.open_doc("chat", CHAT_ID)
    doc.epoch = 1
    doc.hello_pending = True
    queued: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    doc.outstanding.append(_Outstanding(op_id="append-e1", future=queued))
    await socket._on_envelope(_error_frame(CHAT_ID, "not_found", op_id=None))
    assert doc.error == "not_found" and not queued.done()
    queued.cancel()


def test_the_error_payload_names_the_op_it_refuses() -> None:
    dumped = json.loads(ErrorPayload(code="op_too_large", op_id="append-e2").model_dump_json())
    assert dumped["op_id"] == "append-e2"
    # Stamped with a version that has ``op_id`` (added in 1.1.0); later
    # additive versions keep it.
    assert tuple(int(part) for part in dumped["schema_version"].split(".")) >= (1, 1, 0)
    # An older writer's payload still loads, with no op named.
    assert ErrorPayload.model_validate({"code": "forbidden", "message": ""}).op_id is None
