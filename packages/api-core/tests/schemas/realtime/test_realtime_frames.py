"""The top-level socket frames: every known tag parses to its model, an
unknown tag becomes a RawFrame, and anything that is not a frame is refused.
Pure — no database."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from alkera_core.schemas.realtime import (
    CLIENT_FRAME_TAGS,
    MAX_CHANNEL_LENGTH,
    SERVER_FRAME_TAGS,
    DocEnvelope,
    DocFrame,
    ErrorFrame,
    PingFrame,
    PongFrame,
    PresenceCursorFrame,
    PresenceFrame,
    PresenceHeartbeatFrame,
    PresenceJoinFrame,
    PresenceLeaveFrame,
    PresencePeer,
    PublisherFrame,
    RawFrame,
    ResetFrame,
    SubscribedFrame,
    SubscribeFrame,
    UnsubscribeFrame,
    WelcomeFrame,
    dump_frame,
    parse_client_frame,
    parse_server_frame,
)
from pydantic import ValidationError

CHANNEL = "doc:chat:sess-1"
ENVELOPE = {
    "doc_id": "sess-1",
    "doc_type": "chat",
    "epoch": 0,
    "peer_id": "p:1",
    "seq": 0,
    "kind": "hello",
    "payload": {},
}


def test_tag_sets_are_pinned() -> None:
    assert CLIENT_FRAME_TAGS == {
        "subscribe",
        "unsubscribe",
        "presence.join",
        "presence.leave",
        "presence.heartbeat",
        "presence.cursor",
        "ping",
        "doc",
    }
    assert SERVER_FRAME_TAGS == {
        "welcome",
        "subscribed",
        "presence",
        "reset",
        "error",
        "pong",
        "doc",
        "publisher",
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param({"t": "subscribe", "channel": CHANNEL}, SubscribeFrame, id="subscribe"),
        pytest.param({"t": "unsubscribe", "channel": CHANNEL}, UnsubscribeFrame, id="unsubscribe"),
        pytest.param({"t": "presence.join", "channel": CHANNEL}, PresenceJoinFrame, id="join"),
        pytest.param({"t": "presence.leave", "channel": CHANNEL}, PresenceLeaveFrame, id="leave"),
        pytest.param(
            {"t": "presence.heartbeat", "channel": CHANNEL}, PresenceHeartbeatFrame, id="heartbeat"
        ),
        pytest.param(
            {
                "t": "presence.cursor",
                "channel": CHANNEL,
                "cursor": {"offset": 4, "anchor": 4, "before": "what", "after": " now"},
            },
            PresenceCursorFrame,
            id="cursor",
        ),
        pytest.param({"t": "ping"}, PingFrame, id="ping"),
        pytest.param({"t": "doc", "envelope": ENVELOPE}, DocFrame, id="doc"),
    ],
)
def test_every_client_tag_parses_to_its_frame(raw: dict[str, Any], expected: type) -> None:
    frame = parse_client_frame(json.dumps(raw))
    assert type(frame) is expected
    assert frame.t == raw["t"]
    # Round trip: what the server would echo parses back identically.
    assert type(parse_client_frame(dump_frame(frame))) is expected


@pytest.mark.parametrize(
    ("frame", "tag"),
    [
        pytest.param(
            WelcomeFrame(
                peer_id="p:1", server_time=datetime(2026, 9, 5, tzinfo=UTC), instance="ab"
            ),
            "welcome",
            id="welcome",
        ),
        pytest.param(
            SubscribedFrame(channel=CHANNEL, can_write=True), "subscribed", id="subscribed"
        ),
        pytest.param(
            PresenceFrame(
                channel=CHANNEL,
                event="join",
                peers=[
                    PresencePeer(
                        peer_id="p:1", user_id="u", last_seen_at=datetime(2026, 9, 5, tzinfo=UTC)
                    )
                ],
            ),
            "presence",
            id="presence",
        ),
        pytest.param(ResetFrame(reason="overflow"), "reset", id="reset"),
        pytest.param(ErrorFrame(code="forbidden", channel=CHANNEL), "error", id="error"),
        pytest.param(PongFrame(), "pong", id="pong"),
        pytest.param(DocFrame(envelope=DocEnvelope.model_validate(ENVELOPE)), "doc", id="doc"),
        pytest.param(
            PublisherFrame(channel=CHANNEL, state="gone", at=datetime(2026, 10, 4, tzinfo=UTC)),
            "publisher",
            id="publisher",
        ),
    ],
)
def test_every_server_frame_dumps_with_its_tag_and_parses_back(frame: Any, tag: str) -> None:
    text = dump_frame(frame)
    body = json.loads(text)
    assert body["t"] == tag
    assert body["schema_version"] == type(frame).SCHEMA_VERSION
    parsed = parse_server_frame(text)
    assert type(parsed) is type(frame)
    assert parsed == frame


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param({"t": "typing", "channel": CHANNEL}, id="unknown-tag"),
        pytest.param({"t": "welcome", "peer_id": "p:1"}, id="server-only-tag-from-a-client"),
        pytest.param({"t": "SUBSCRIBE", "channel": CHANNEL}, id="wrong-case"),
    ],
)
def test_an_unknown_client_tag_is_a_raw_frame_that_keeps_its_fields(raw: dict[str, Any]) -> None:
    frame = parse_client_frame(json.dumps(raw))
    assert isinstance(frame, RawFrame)
    assert frame.t == raw["t"]
    dumped = frame.model_dump(mode="json")
    for key, value in raw.items():
        assert dumped[key] == value, "every field rides along on a raw frame"


def test_a_client_only_tag_from_the_server_is_raw_too() -> None:
    assert isinstance(
        parse_server_frame(json.dumps({"t": "subscribe", "channel": CHANNEL})), RawFrame
    )


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("not json", id="not-json"),
        pytest.param("[]", id="a-list"),
        pytest.param('"ping"', id="a-string"),
        pytest.param("{}", id="no-tag"),
        pytest.param('{"t": 5}', id="tag-not-a-string"),
        pytest.param('{"t": ""}', id="empty-tag"),
        pytest.param('{"t": "doc"}', id="doc-without-envelope"),
        pytest.param(
            json.dumps({"t": "doc", "envelope": {**ENVELOPE, "kind": "op"}}),
            id="doc-with-an-epoch-zero-op",
        ),
    ],
)
def test_anything_that_is_not_a_frame_is_refused_in_both_directions(raw: str) -> None:
    """Not an object, no usable tag, or a shared tag with a bad body: never a
    frame, and never quietly a RawFrame either."""
    with pytest.raises(ValidationError):
        parse_client_frame(raw)
    with pytest.raises(ValidationError):
        parse_server_frame(raw)


@pytest.mark.parametrize(
    ("raw", "parse"),
    [
        pytest.param('{"t": "subscribe"}', parse_client_frame, id="subscribe-without-channel"),
        pytest.param(
            '{"t": "subscribe", "channel": ""}', parse_client_frame, id="subscribe-empty-channel"
        ),
        pytest.param(
            json.dumps({"t": "subscribe", "channel": "c" * (MAX_CHANNEL_LENGTH + 1)}),
            parse_client_frame,
            id="subscribe-channel-too-long",
        ),
        pytest.param('{"t": "presence.join"}', parse_client_frame, id="join-without-channel"),
        pytest.param(
            '{"t": "presence", "channel": "c", "event": "typing"}',
            parse_server_frame,
            id="presence-unknown-event",
        ),
        pytest.param('{"t": "subscribed", "channel": "c"}', parse_server_frame, id="no-can-write"),
        pytest.param('{"t": "error"}', parse_server_frame, id="error-without-code"),
        pytest.param('{"t": "reset", "reason": ""}', parse_server_frame, id="reset-empty-reason"),
        pytest.param(
            '{"t": "publisher", "channel": "c", "state": "asleep", "at": "2026-10-04T00:00:00Z"}',
            parse_server_frame,
            id="publisher-unknown-state",
        ),
        pytest.param(
            '{"t": "publisher", "channel": "c", "state": "gone"}',
            parse_server_frame,
            id="publisher-without-time",
        ),
        pytest.param(
            '{"t": "welcome", "peer_id": "p:1", "instance": "ab"}',
            parse_server_frame,
            id="welcome-without-server-time",
        ),
    ],
)
def test_a_known_tag_with_a_bad_body_is_refused(raw: str, parse: Any) -> None:
    with pytest.raises(ValidationError):
        parse(raw)


def test_unknown_fields_on_a_known_frame_ride_along() -> None:
    frame = parse_client_frame(json.dumps({"t": "ping", "trace": "abc"}))
    assert isinstance(frame, PingFrame)
    assert frame.model_dump(mode="json")["trace"] == "abc"


def test_a_frame_dump_always_carries_the_current_schema_version() -> None:
    frame = SubscribeFrame(channel=CHANNEL)
    frame.schema_version = "0.9.0"  # a stale stamp is never written
    assert json.loads(dump_frame(frame))["schema_version"] == SubscribeFrame.SCHEMA_VERSION
