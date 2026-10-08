"""Replicas on two builds exchange renamed document types during a roll.

The composer draft is ``chat_draft`` on this build and ``chat_workspace`` on the
previous one. An event this build writes (an outbox row, a NOTIFY) carries the
old spelling so a replica still on the previous build can read it, and every
event this build reads, whichever build wrote it, is read under the current
name, so a draft keeps crossing replicas through a roll.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.doc_type_names import respell_channel, respell_payload
from alkera_core.events.hub import HubEvent
from alkera_core.events.listener import ephemeral_payload, parse_ephemeral
from alkera_core.events.outbox import emit
from alkera_core.models.event_outbox import EventOutbox

DOC = "1b9e"


def _envelope(doc_type: str) -> dict[str, Any]:
    return {"schema_version": "2.0.0", "doc_type": doc_type, "doc_id": DOC, "kind": "crdt"}


@pytest.mark.parametrize(
    ("channel", "to_replicas", "from_replicas"),
    [
        pytest.param(
            f"doc:chat_draft:{DOC}",
            f"doc:chat_workspace:{DOC}",
            f"doc:chat_draft:{DOC}",
            id="the-draft",
        ),
        pytest.param(
            f"doc:chat_workspace:{DOC}",
            f"doc:chat_workspace:{DOC}",
            f"doc:chat_draft:{DOC}",
            id="the-old-spelling",
        ),
        pytest.param(f"doc:chat:{DOC}", f"doc:chat:{DOC}", f"doc:chat:{DOC}", id="a-chat"),
        pytest.param(f"doc:file:{DOC}", f"doc:file:{DOC}", f"doc:file:{DOC}", id="a-file"),
        pytest.param(None, None, None, id="no-channel"),
    ],
)
def test_a_channel_is_written_old_and_read_current(
    channel: str | None, to_replicas: str | None, from_replicas: str | None
) -> None:
    assert respell_channel(channel, "to_replicas") == to_replicas
    assert respell_channel(channel, "from_replicas") == from_replicas


def test_an_envelope_is_respelled_and_the_rest_of_the_payload_kept() -> None:
    payload = {"envelope": _envelope("chat_draft"), "team_id": "t", "relay": False}

    written = respell_payload(payload, "to_replicas")

    assert written["envelope"]["doc_type"] == "chat_workspace"
    assert {k: v for k, v in written.items() if k != "envelope"} == {"team_id": "t", "relay": False}
    assert respell_payload(written, "from_replicas")["envelope"]["doc_type"] == "chat_draft"
    assert payload["envelope"]["doc_type"] == "chat_draft", "the caller's payload is not mutated"


@pytest.mark.parametrize("written_as", ["chat_draft", "chat_workspace"], ids=["new", "old"])
def test_an_outbox_row_from_either_build_is_read_under_the_current_name(written_as: str) -> None:
    row = EventOutbox(
        id=7,
        org_id=uuid4(),
        type="doc.op",
        entity="doc",
        entity_id=f"doc:{written_as}:{DOC}",
        version=1,
        visibility="org",
        payload={"envelope": _envelope(written_as)},
    )

    event = HubEvent.from_outbox(row)

    assert event.channel == event.entity_id == f"doc:chat_draft:{DOC}"
    assert event.payload["envelope"]["doc_type"] == "chat_draft"


def test_a_notify_is_sent_old_and_read_back_current() -> None:
    event = HubEvent(
        lane="ephemeral",
        org_id=uuid4(),
        type="presence",
        entity="ephemeral",
        entity_id=f"doc:chat_draft:{DOC}",
        version=0,
        visibility="org",
        payload={"envelope": _envelope("chat_draft")},
        channel=f"doc:chat_draft:{DOC}",
    )

    wire = ephemeral_payload(event)
    back = parse_ephemeral(wire)

    assert '"doc:chat_workspace:' in wire and '"chat_draft"' not in wire
    assert back.channel == back.entity_id == f"doc:chat_draft:{DOC}"
    assert back.payload["envelope"]["doc_type"] == "chat_draft"


async def test_an_outbox_row_is_written_in_the_previous_builds_spelling() -> None:
    async with AsyncSessionLocal() as db:
        row = await emit(
            db,
            org_id=uuid4(),
            type="doc.op",
            entity="doc",
            entity_id=f"doc:chat_draft:{DOC}",
            payload={"envelope": _envelope("chat_draft")},
            flush=False,
        )
        await db.rollback()

    assert row.entity_id == f"doc:chat_workspace:{DOC}"
    assert row.payload["envelope"]["doc_type"] == "chat_workspace"
