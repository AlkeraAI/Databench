"""The top-level frames on a realtime socket, discriminated on ``t``.

A socket carries JSON text frames. Each is one of the models here, told apart
by its ``t`` tag. Client → server: ``subscribe`` / ``unsubscribe`` a channel,
``presence.join`` / ``presence.leave`` / ``presence.heartbeat`` on a channel,
``presence.cursor`` (where the peer's caret is in the shared draft — fanned
out as a ``presence`` delta and never stored), ``ping``, and ``doc`` carrying
a :class:`DocEnvelope`. Server → client:
``welcome`` (the peer id the server minted for this socket), ``subscribed``
(with whether the peer may write), ``presence`` rosters and deltas, ``reset``
(the peer fell behind; re-``hello`` every channel), ``error``, ``pong``,
``doc``, and ``publisher`` (the box that publishes a chat's document came onto
the channel or went from it).

An unknown tag routes to :class:`RawFrame` instead of failing: an older
reader must not crash on a frame a newer writer added. The gateway answers a
raw client frame with ``error{unknown_frame}`` and keeps the socket open.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, ClassVar, Final, Literal

from pydantic import BaseModel, Discriminator, Field, Tag, TypeAdapter

from alkera_core.schemas.realtime.envelope import DocEnvelope, PresenceCursor, PresencePeer
from alkera_core.versioning import VersionedModel, make_unknown_tag_discriminator

FRAME_SCHEMA_VERSION: Final = "1.0.0"
UNKNOWN_TAG: Final = "__unknown__"
MAX_CHANNEL_LENGTH: Final = 320

#: The realtime client generation this server speaks. A web client compiles in
#: its own generation (``REALTIME_CLIENT_GENERATION`` in
#: ``apps/web/src/api/realtime/clientGeneration.ts``); a tab whose generation is
#: below the one ``welcome`` names was built before a protocol change it cannot
#: follow, so it reloads into the current build. Raise it only with a change an
#: older tab would get wrong; the two constants move together and a test pins
#: that they agree.
REALTIME_CLIENT_GENERATION: Final = 2

PresenceEvent = Literal["join", "leave", "heartbeat", "roster", "cursor"]

#: Where the box publishing a chat's document stands on the channel: ``here``
#: once it holds the channel, ``gone`` the moment its socket went away.
PublisherState = Literal["here", "gone"]


class _Frame(VersionedModel):
    __abstract__: ClassVar[bool] = True


# --- client → server ---------------------------------------------------------


class SubscribeFrame(_Frame):
    SCHEMA_VERSION: ClassVar[str] = FRAME_SCHEMA_VERSION
    t: Literal["subscribe"] = "subscribe"
    channel: str = Field(min_length=1, max_length=MAX_CHANNEL_LENGTH)


class UnsubscribeFrame(_Frame):
    SCHEMA_VERSION: ClassVar[str] = FRAME_SCHEMA_VERSION
    t: Literal["unsubscribe"] = "unsubscribe"
    channel: str = Field(min_length=1, max_length=MAX_CHANNEL_LENGTH)


class PresenceJoinFrame(_Frame):
    SCHEMA_VERSION: ClassVar[str] = FRAME_SCHEMA_VERSION
    t: Literal["presence.join"] = "presence.join"
    channel: str = Field(min_length=1, max_length=MAX_CHANNEL_LENGTH)


class PresenceLeaveFrame(_Frame):
    SCHEMA_VERSION: ClassVar[str] = FRAME_SCHEMA_VERSION
    t: Literal["presence.leave"] = "presence.leave"
    channel: str = Field(min_length=1, max_length=MAX_CHANNEL_LENGTH)


class PresenceHeartbeatFrame(_Frame):
    SCHEMA_VERSION: ClassVar[str] = FRAME_SCHEMA_VERSION
    t: Literal["presence.heartbeat"] = "presence.heartbeat"
    channel: str = Field(min_length=1, max_length=MAX_CHANNEL_LENGTH)


class PresenceCursorFrame(_Frame):
    """Where this peer's caret is in the channel's shared draft. Fanned out to
    the channel as ``presence{event: cursor}`` with the cursor on the peer;
    nothing is written, so a peer may send one as often as it likes within
    the frame budget."""

    SCHEMA_VERSION: ClassVar[str] = FRAME_SCHEMA_VERSION
    t: Literal["presence.cursor"] = "presence.cursor"
    channel: str = Field(min_length=1, max_length=MAX_CHANNEL_LENGTH)
    cursor: PresenceCursor


class PingFrame(_Frame):
    SCHEMA_VERSION: ClassVar[str] = FRAME_SCHEMA_VERSION
    t: Literal["ping"] = "ping"


class DocFrame(_Frame):
    """Both directions: a doc-sync envelope."""

    SCHEMA_VERSION: ClassVar[str] = FRAME_SCHEMA_VERSION
    t: Literal["doc"] = "doc"
    envelope: DocEnvelope


# --- server → client ---------------------------------------------------------


class SocketLimits(VersionedModel):
    """The inbound budget this socket is held to: at most ``frames_per_window``
    frames and ``bytes_per_window`` bytes in any ``window_seconds``, and no
    single frame over ``max_frame_bytes``. A client paces itself under it
    rather than discovering it as a close.

    It also names two server sizes a publisher sizes its traffic by:
    ``ephemeral_max_bytes``, the largest notification the ephemeral lane
    carries (a streamed chunk with everything the server wraps around it), and
    ``doc_max_bytes``, the largest a document may grow, so the largest
    snapshot a hello is answered with. Both are absent from a server before
    1.1.0, and a client falls back to its own defaults.

    ``presence_ttl_seconds`` is how long a peer stays on a roster unheard
    from: a client drops a peer silent for that long even when no ``leave``
    reached it (absent before 1.2.0; a client then expires nobody)."""

    # 1.1.0 adds ``ephemeral_max_bytes`` and ``doc_max_bytes`` (additive).
    # 1.2.0 adds ``presence_ttl_seconds`` (additive).
    SCHEMA_VERSION: ClassVar[str] = "1.2.0"

    frames_per_window: int = Field(ge=1)
    bytes_per_window: int = Field(ge=1)
    window_seconds: float = Field(gt=0)
    max_frame_bytes: int = Field(ge=1)
    ephemeral_max_bytes: int | None = Field(default=None, ge=1)
    doc_max_bytes: int | None = Field(default=None, ge=1)
    presence_ttl_seconds: float | None = Field(default=None, gt=0)


class WelcomeFrame(_Frame):
    # 1.4.0 adds ``min_client_generation``; 1.5.0 adds ``limits``; 1.6.0
    # adds the two sizes on ``limits``; 1.7.0 the presence TTL on ``limits``
    # (all additive; an older reader ignores them).
    SCHEMA_VERSION: ClassVar[str] = "1.7.0"
    t: Literal["welcome"] = "welcome"
    peer_id: str = Field(min_length=1, max_length=64)
    server_time: datetime
    instance: str = Field(min_length=1, max_length=32)
    #: The oldest client generation this server serves; an older tab reloads.
    min_client_generation: int = Field(default=0, ge=0)
    #: This socket's inbound budget; absent from a server that predates it.
    limits: SocketLimits | None = None


class SubscribedFrame(_Frame):
    SCHEMA_VERSION: ClassVar[str] = FRAME_SCHEMA_VERSION
    t: Literal["subscribed"] = "subscribed"
    channel: str = Field(min_length=1, max_length=MAX_CHANNEL_LENGTH)
    can_write: bool


class PresenceFrame(_Frame):
    SCHEMA_VERSION: ClassVar[str] = FRAME_SCHEMA_VERSION
    t: Literal["presence"] = "presence"
    channel: str = Field(min_length=1, max_length=MAX_CHANNEL_LENGTH)
    event: PresenceEvent
    peers: list[PresencePeer] = Field(default_factory=list)


class PublisherFrame(_Frame):
    """The box that publishes this chat's document came onto the channel, or
    its socket went away.

    A reader learns a box died from the machine's heartbeat only once the
    ready window has passed, which is most of a minute in which the page says
    nothing. The box's socket closing is the first sign there is, so it is
    told at once: ``gone`` is a hint the page shows as reconnecting, not a
    verdict, and ``here`` (the box back on the channel) withdraws it. ``at``
    is the server's clock, so a ``gone`` that one replica noticed late never
    outranks the ``here`` another replica sent after it."""

    SCHEMA_VERSION: ClassVar[str] = FRAME_SCHEMA_VERSION
    t: Literal["publisher"] = "publisher"
    channel: str = Field(min_length=1, max_length=MAX_CHANNEL_LENGTH)
    state: PublisherState
    at: datetime


class ResetFrame(_Frame):
    SCHEMA_VERSION: ClassVar[str] = FRAME_SCHEMA_VERSION
    t: Literal["reset"] = "reset"
    reason: str = Field(min_length=1, max_length=64)


class ErrorFrame(_Frame):
    SCHEMA_VERSION: ClassVar[str] = FRAME_SCHEMA_VERSION
    t: Literal["error"] = "error"
    code: str = Field(min_length=1, max_length=64)
    message: str = ""
    channel: str | None = Field(default=None, max_length=MAX_CHANNEL_LENGTH)


class PongFrame(_Frame):
    SCHEMA_VERSION: ClassVar[str] = FRAME_SCHEMA_VERSION
    t: Literal["pong"] = "pong"


class RawFrame(_Frame):
    """A frame whose tag this reader does not know; every other field rides
    along as an extra so nothing is lost on round-trip."""

    SCHEMA_VERSION: ClassVar[str] = FRAME_SCHEMA_VERSION
    t: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)


CLIENT_FRAME_TAGS: Final[frozenset[str]] = frozenset(
    {
        "subscribe",
        "unsubscribe",
        "presence.join",
        "presence.leave",
        "presence.heartbeat",
        "presence.cursor",
        "ping",
        "doc",
    }
)
SERVER_FRAME_TAGS: Final[frozenset[str]] = frozenset(
    {"welcome", "subscribed", "presence", "reset", "error", "pong", "doc", "publisher"}
)

_client_tag = make_unknown_tag_discriminator(set(CLIENT_FRAME_TAGS), field="t")
_server_tag = make_unknown_tag_discriminator(set(SERVER_FRAME_TAGS), field="t")

ClientFrame = Annotated[
    (
        Annotated[SubscribeFrame, Tag("subscribe")]
        | Annotated[UnsubscribeFrame, Tag("unsubscribe")]
        | Annotated[PresenceJoinFrame, Tag("presence.join")]
        | Annotated[PresenceLeaveFrame, Tag("presence.leave")]
        | Annotated[PresenceHeartbeatFrame, Tag("presence.heartbeat")]
        | Annotated[PresenceCursorFrame, Tag("presence.cursor")]
        | Annotated[PingFrame, Tag("ping")]
        | Annotated[DocFrame, Tag("doc")]
        | Annotated[RawFrame, Tag(UNKNOWN_TAG)]
    ),
    Discriminator(_client_tag),
]

ServerFrame = Annotated[
    (
        Annotated[WelcomeFrame, Tag("welcome")]
        | Annotated[SubscribedFrame, Tag("subscribed")]
        | Annotated[PresenceFrame, Tag("presence")]
        | Annotated[ResetFrame, Tag("reset")]
        | Annotated[ErrorFrame, Tag("error")]
        | Annotated[PongFrame, Tag("pong")]
        | Annotated[DocFrame, Tag("doc")]
        | Annotated[PublisherFrame, Tag("publisher")]
        | Annotated[RawFrame, Tag(UNKNOWN_TAG)]
    ),
    Discriminator(_server_tag),
]

CLIENT_FRAME_ADAPTER: TypeAdapter[ClientFrame] = TypeAdapter(ClientFrame)
SERVER_FRAME_ADAPTER: TypeAdapter[ServerFrame] = TypeAdapter(ServerFrame)


def parse_client_frame(raw: str | bytes) -> ClientFrame:
    """The client frame in ``raw`` JSON; an unknown tag yields a
    :class:`RawFrame`; anything that is not a frame at all (not an object, no
    ``t``, a known tag with a bad body) raises ``pydantic.ValidationError``."""
    return CLIENT_FRAME_ADAPTER.validate_json(raw)


def parse_server_frame(raw: str | bytes) -> ServerFrame:
    return SERVER_FRAME_ADAPTER.validate_json(raw)


def dump_frame(frame: BaseModel) -> str:
    """The wire text of a frame: a versioned protocol frame, or a machine
    channel frame (:mod:`alkera_core.schemas.realtime.machine`), which is not."""
    return frame.model_dump_json()


__all__ = [
    "CLIENT_FRAME_ADAPTER",
    "CLIENT_FRAME_TAGS",
    "FRAME_SCHEMA_VERSION",
    "MAX_CHANNEL_LENGTH",
    "SERVER_FRAME_ADAPTER",
    "SERVER_FRAME_TAGS",
    "UNKNOWN_TAG",
    "ClientFrame",
    "DocFrame",
    "ErrorFrame",
    "PingFrame",
    "PongFrame",
    "PresenceCursorFrame",
    "PresenceEvent",
    "PresenceFrame",
    "PresenceHeartbeatFrame",
    "PresenceJoinFrame",
    "PresenceLeaveFrame",
    "RawFrame",
    "ResetFrame",
    "ServerFrame",
    "SocketLimits",
    "SubscribeFrame",
    "SubscribedFrame",
    "UnsubscribeFrame",
    "WelcomeFrame",
    "dump_frame",
    "parse_client_frame",
    "parse_server_frame",
]
