"""Which of a box's channels a publisher frame is about, and what a reader is sent.

A box publishes chats. A socket also holds channels of other documents (a
workspace's live tree, ``doc:workspace:*``, on servers that have them), and
announcing a publisher for those would tell a workspace's readers a chat box
came or went. The frame itself is a reader's news: a box is never sent it.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

import pytest
from alkera_core.events import HubEvent
from alkera_core.schemas.realtime import PublisherFrame
from backend.services.realtime import presence
from backend.services.realtime.channels import Channel, ChannelGrant
from backend.services.realtime.publisher import chat_grants, publisher_frame


def _grant(doc_type: str) -> ChannelGrant:
    return ChannelGrant(
        channel=Channel(doc_type=cast(Any, doc_type), doc_id=str(uuid4())),
        org_id=uuid4(),
        can_write=True,
        owner_user_id=None,
        team_id=None,
    )


def test_only_chat_channels_have_a_publisher_to_report() -> None:
    chat = _grant("chat")
    others = [_grant("workspace"), _grant("artifact"), _grant("chat_workspace")]
    assert chat_grants([*others, chat, object()]) == [chat]


def _event(state: object, at: object = "2026-10-04T16:00:00+00:00") -> HubEvent:
    return HubEvent(
        lane="ephemeral",
        org_id=uuid4(),
        type=presence.PUBLISHER_EVENT_TYPE,
        entity=presence.EPHEMERAL_ENTITY,
        entity_id="doc:chat:c1",
        version=0,
        visibility="org",
        payload={"state": state, "at": at},
        id=None,
        channel="doc:chat:c1",
    )


def test_a_reader_is_sent_the_frame() -> None:
    frame = publisher_frame(_event("gone"), machine_id=None)
    assert isinstance(frame, PublisherFrame)
    assert (frame.channel, frame.state) == ("doc:chat:c1", "gone")


def test_a_box_is_never_sent_its_own_news() -> None:
    assert publisher_frame(_event("gone"), machine_id="m1") is None


@pytest.mark.parametrize(
    ("state", "at"),
    [
        pytest.param("asleep", "2026-10-04T16:00:00+00:00", id="unknown-state"),
        pytest.param("gone", None, id="no-time"),
    ],
)
def test_a_malformed_event_becomes_no_frame(state: object, at: object) -> None:
    assert publisher_frame(_event(state, at), machine_id=None) is None
