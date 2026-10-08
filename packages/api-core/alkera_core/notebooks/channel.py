"""Frames of the realtime notebook channel ``nb:<item_id>``.

A person's socket subscribes to ``nb:<item_id>`` with the ordinary
``subscribe`` frame and is answered ``subscribed`` (``can_write`` says whether
it may send widget messages now). Every frame after that is
``{t: "nb", channel, event}``, ``event`` being the engine's ``NotebookEvent``:

* the first is ``{type: "snapshot", kernel_id, seq, view, frames}``: the
  notebook's view (as ``GET /api/v1/notebooks/{drive_id}/{item_id}`` answers
  it) and the kernel's state at ``(kernel_id, seq)`` as the replica last heard
  it (the engine's own snapshot fields ride beside ``view``);
* then every event of that kernel with a greater ``seq``, each once, in order,
  then the live ones (a kernel restart is a new ``kernel_id``);
* ``{type: "resync"}`` when the socket fell too far behind: the client
  subscribes again.

An event addressed to an output frame carries ``frame_id`` and reaches only
the sockets of the person who attached that frame (and, when the frame named
one, only that socket).

The client sends ``nb.comm``: one widget message from an attached frame,
authorized per message (Can edit on the notebook, decided afresh each time).

These frames are never persisted, so they are plain models. Everything inside
an event is the engine's, passed through as the box sent it.
"""

from __future__ import annotations

from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from alkera_core.notebooks.schemas import FRAME_ID_PATTERN

#: The ``t`` of every server frame on a notebook channel.
NB_FRAME: Final = "nb"
#: Every client frame's ``t`` on a notebook channel starts with this.
NB_FRAME_PREFIX: Final = "nb."
#: The largest widget message a client may send on the channel.
MAX_COMM_BUFFERS: Final = 64


class NbFrame(BaseModel):
    """Server -> client: one engine event on a notebook channel."""

    t: Literal["nb"] = "nb"
    channel: str
    event: dict[str, Any]


class NbCommFrame(BaseModel):
    """Client -> server: one widget message. ``buffers`` are standard base64."""

    model_config = ConfigDict(extra="forbid")

    t: Literal["nb.comm"] = "nb.comm"
    channel: str = Field(min_length=1, max_length=255)
    frame_id: str = Field(pattern=FRAME_ID_PATTERN)
    comm_id: str = Field(min_length=1, max_length=128)
    msg_id: str = Field(min_length=1, max_length=128)
    content: dict[str, Any]
    buffers: list[str] = Field(default_factory=list, max_length=MAX_COMM_BUFFERS)


__all__ = ["MAX_COMM_BUFFERS", "NB_FRAME", "NB_FRAME_PREFIX", "NbCommFrame", "NbFrame"]
