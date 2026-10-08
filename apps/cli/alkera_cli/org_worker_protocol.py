"""What the supervisor and an org worker say to each other.

They share one socketpair the supervisor made at spawn: no path on disk, so no
other process can connect. Each frame is one line of JSON naming its ``type``,
validated against the closed set below; anything else (an unknown type, a
field that does not fit, a line past :data:`MAX_FRAME_BYTES`) is a protocol
error that ends the worker, never a guess.

The supervisor speaks ids and states only. The one secret that travels is the
worker's credential, in :class:`Hello` and :class:`Credential`, kept out of
every repr.
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from alkera_cli.org_slot import MAX_SLOTS, SlotError, canonical_org

#: The longest frame either side reads; a routing frame for a full box fits
#: well inside it.
MAX_FRAME_BYTES: Final = 1 << 20
#: The most chats one route frame names.
MAX_ROUTED_CHATS: Final = 4_096

_Id = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]


class ProtocolError(RuntimeError):
    """A frame that is not one of the closed set."""


class _Frame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Hello(_Frame):
    """The first frame to a worker: whom it serves and as what."""

    type: Literal["hello"] = "hello"
    org_id: str
    slot: int = Field(ge=0, lt=MAX_SLOTS)
    machine_id: _Id
    credential: str = Field(min_length=1, max_length=4096, repr=False)

    @field_validator("org_id")
    @classmethod
    def _org(cls, value: str) -> str:
        try:
            return canonical_org(value)
        except SlotError as exc:
            raise ValueError(str(exc)) from None


class Credential(_Frame):
    """A fresher credential for the worker, numbered for its status to name."""

    type: Literal["credential"] = "credential"
    credential: str = Field(min_length=1, max_length=4096, repr=False)
    seq: int = Field(default=0, ge=0)


class Route(_Frame):
    """Every chat of the worker's org this box serves now. A worker opens only
    chats named here, and only when the chat's own row names its org."""

    type: Literal["route"] = "route"
    chat_ids: tuple[_Id, ...] = Field(default=(), max_length=MAX_ROUTED_CHATS)


class Stop(_Frame):
    """Drain and exit. A ``final`` stop hands every chat back (its folder
    pushed, its lease released) within the drain ceiling; a restart in place
    (``final`` false) keeps every lease, lets in-flight work settle for a short
    while and hands nothing back, because the next worker takes the chats up
    again on this box."""

    type: Literal["stop"] = "stop"
    final: bool = True


class Ready(_Frame):
    """The worker is up and serving its org."""

    type: Literal["ready"] = "ready"


class Status(_Frame):
    """What the worker holds now, for the box's heartbeat and placement."""

    type: Literal["status"] = "status"
    chats_served: int = Field(ge=0)
    chats_busy: int = Field(ge=0)
    #: What a drain would wait for; ``None`` from a worker that does not count it.
    chats_working: int | None = Field(default=None, ge=0)
    rss_bytes: int = Field(ge=0)
    #: The number of the credential it holds, and whether that one was refused
    #: as expired: the supervisor re-sends a newer one or mints one at once.
    credential_seq: int = Field(default=0, ge=0)
    credential_refused: bool = False


class Refused(_Frame):
    """A chat the worker would not serve, and why (ids and a reason only)."""

    type: Literal["refused"] = "refused"
    chat_id: _Id
    reason: str = Field(max_length=256)


ToWorker = Annotated[Hello | Credential | Route | Stop, Field(discriminator="type")]
ToSupervisor = Annotated[Ready | Status | Refused, Field(discriminator="type")]
_TO_WORKER: TypeAdapter[Hello | Credential | Route | Stop] = TypeAdapter(ToWorker)
_TO_SUPERVISOR: TypeAdapter[Ready | Status | Refused] = TypeAdapter(ToSupervisor)


def encode(frame: _Frame) -> bytes:
    line = frame.model_dump_json().encode("utf-8") + b"\n"
    if len(line) > MAX_FRAME_BYTES:
        raise ProtocolError(f"a {frame.__class__.__name__} frame is past the frame limit")
    return line


def _json(line: bytes) -> object:
    if len(line) > MAX_FRAME_BYTES:
        raise ProtocolError("a frame past the frame limit")
    try:
        return json.loads(line)
    except ValueError:
        raise ProtocolError("not a frame: not JSON") from None


def decode_to_worker(line: bytes) -> Hello | Credential | Route | Stop:
    try:
        return _TO_WORKER.validate_python(_json(line))
    except ValidationError as exc:
        raise ProtocolError(f"not a frame for a worker: {exc.error_count()} errors") from None


def decode_to_supervisor(line: bytes) -> Ready | Status | Refused:
    try:
        return _TO_SUPERVISOR.validate_python(_json(line))
    except ValidationError as exc:
        raise ProtocolError(f"not a frame for the supervisor: {exc.error_count()} errors") from None


async def read_line(reader: asyncio.StreamReader) -> bytes | None:
    """The next frame's line, or ``None`` at the end of the stream."""
    try:
        line = await reader.readuntil(b"\n")
    except asyncio.IncompleteReadError as exc:
        if exc.partial:
            raise ProtocolError("the stream ended inside a frame") from None
        return None
    except asyncio.LimitOverrunError:
        raise ProtocolError("a frame past the frame limit") from None
    return line


__all__ = [
    "MAX_FRAME_BYTES",
    "MAX_ROUTED_CHATS",
    "Credential",
    "Hello",
    "ProtocolError",
    "Ready",
    "Refused",
    "Route",
    "Status",
    "Stop",
    "ToSupervisor",
    "ToWorker",
    "decode_to_supervisor",
    "decode_to_worker",
    "encode",
    "read_line",
]
