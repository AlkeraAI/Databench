"""The machine channel's frames: what a box may answer, and the channel grammar."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from alkera_core.schemas.realtime import (
    MACHINE_ACK_OUTCOMES,
    MachineRequest,
    machine_channel,
    machine_of_channel,
    parse_machine_frame,
)
from pydantic import ValidationError

REQUEST_ID = str(uuid.uuid4())


def _ack(**fields: Any) -> str:
    return json.dumps({"t": "machine.ack", "request_id": REQUEST_ID, **fields})


@pytest.mark.parametrize("outcome", [o for o in MACHINE_ACK_OUTCOMES if o != "changed"])
def test_every_plain_outcome_parses(outcome: str) -> None:
    ack = parse_machine_frame(_ack(outcome=outcome))
    assert (str(ack.request_id), ack.outcome, ack.observed) == (REQUEST_ID, outcome, None)


def test_a_changed_ack_carries_what_the_box_observed() -> None:
    ack = parse_machine_frame(_ack(outcome="changed", observed={"size": 9, "mtime_ns": 7}))
    assert ack.observed is not None
    assert (ack.observed.size, ack.observed.mtime_ns) == (9, 7)


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(_ack(outcome="changed"), id="changed-without-observed"),
        pytest.param(_ack(outcome="done"), id="an-outcome-nobody-defined"),
        pytest.param(_ack(outcome="busy", request_id="not-a-uuid"), id="request-id-not-a-uuid"),
        pytest.param(
            json.dumps({"t": "machine.hello", "request_id": REQUEST_ID, "outcome": "busy"}),
            id="a-machine-tag-this-server-does-not-know",
        ),
        pytest.param(_ack(), id="no-outcome"),
        pytest.param("[]", id="not-an-object"),
    ],
)
def test_anything_but_a_well_formed_ack_is_refused(raw: str) -> None:
    with pytest.raises(ValidationError):
        parse_machine_frame(raw)


def test_a_request_serialises_as_the_wire_frame() -> None:
    request = MachineRequest(
        request_id=uuid.UUID(REQUEST_ID),
        kind="promote",
        lease_node_id=uuid.UUID(int=1),
        epoch=7,
        node_id=uuid.UUID(int=2),
        path="sub/dir/file.csv",
        expected={"size": 1234, "mtime_ns": 1_758_625_000_123_456_789},
        deadline_ms=8000,
    )
    assert json.loads(request.model_dump_json()) == {
        "t": "machine.request",
        "request_id": REQUEST_ID,
        "kind": "promote",
        "lease_node_id": str(uuid.UUID(int=1)),
        "epoch": 7,
        "node_id": str(uuid.UUID(int=2)),
        "path": "sub/dir/file.csv",
        "expected": {"size": 1234, "mtime_ns": 1_758_625_000_123_456_789},
        "deadline_ms": 8000,
    }


@pytest.mark.parametrize(
    ("raw", "machine"),
    [
        pytest.param(
            "machine:5f0c9a1e-0000-4000-8000-000000000001",
            "5f0c9a1e-0000-4000-8000-000000000001",
            id="an-allocation-id",
        ),
        pytest.param("machine:", None, id="empty-id"),
        pytest.param("machine:a/b", None, id="a-slash"),
        pytest.param("machine:abc\n", None, id="a-trailing-newline"),
        pytest.param("doc:chat:abc", None, id="a-document-channel"),
        pytest.param(f"machine:{'a' * 256}", None, id="too-long"),
    ],
)
def test_the_machine_channel_grammar(raw: str, machine: str | None) -> None:
    assert machine_of_channel(raw) == machine
    if machine is not None:
        assert machine_channel(machine) == raw


def test_a_flush_names_the_folder_and_no_file() -> None:
    request = MachineRequest.model_validate(
        {
            "request_id": REQUEST_ID,
            "kind": "flush",
            "lease_node_id": str(uuid.UUID(int=1)),
            "epoch": 3,
            "deadline_ms": 20000,
        }
    )
    assert (request.kind, request.node_id, request.path, request.expected) == (
        "flush",
        None,
        None,
        None,
    )


@pytest.mark.parametrize("missing", ["node_id", "path", "expected"])
def test_a_promote_without_its_file_is_refused(missing: str) -> None:
    frame: dict[str, Any] = {
        "request_id": REQUEST_ID,
        "kind": "promote",
        "lease_node_id": str(uuid.UUID(int=1)),
        "epoch": 3,
        "node_id": str(uuid.UUID(int=2)),
        "path": "a.txt",
        "expected": {"size": 1, "mtime_ns": 2},
        "deadline_ms": 8000,
    }
    del frame[missing]
    with pytest.raises(ValidationError):
        MachineRequest.model_validate(frame)
