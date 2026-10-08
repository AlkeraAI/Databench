"""The machine channel: the drive asking the box holding a folder for one file.

A box's socket may subscribe to ``machine:<allocation_id>`` — its own machine,
proven at admission, and no other. On it the server sends ``machine.request``
frames and the box answers each with one ``machine.ack``. Nothing on this
channel carries a byte of a file: a request names a file, the box puts it at the
front of its upload queue, and the bytes land through the ordinary fenced
upload, which is the only way bytes reach the drive.

A box that is a text peer of a co-edited file is also told on this channel that
the file's live document moved: a ``machine.request`` of kind ``live_text``
naming the node and the document's version token, never its text. It is not
answered; the box reads the text over REST under its lease's fence. A box from
before it knows no such kind and ignores it, as the rule below says.

These frames live outside the document protocol — no epoch, no sequence, no
``realtime_docs`` row — and are never persisted, so they are plain
``BaseModel``s rather than versioned ones. A frame from a newer server with a
``kind`` this box does not know is not answered (the server's deadline covers
it); a frame from the box with a ``machine.*`` tag the server does not know is a
protocol error and closes the socket.
"""

from __future__ import annotations

import re
import uuid
from typing import Annotated, Any, Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

#: Every ``t`` on this channel starts with this.
MACHINE_FRAME_PREFIX: Final = "machine."
MACHINE_REQUEST_TAG: Final = "machine.request"
MACHINE_ACK_TAG: Final = "machine.ack"

#: The channel grammar. The id is the workspace machine's allocation id; the
#: character class is the document channels' own, so one id spelling fits both.
MACHINE_CHANNEL_PREFIX: Final = "machine:"
MACHINE_CHANNEL_PATTERN: Final = r"^machine:([A-Za-z0-9._:-]{1,255})$"
MACHINE_CHANNEL_RE: Final = re.compile(MACHINE_CHANNEL_PATTERN)

#: What the server may ask a box to do, or tell it: bring one file's bytes now
#: (``promote``), push everything it holds of the folder before the folder is
#: trashed (``flush``), so nothing the box wrote goes into the trash unsaved, or
#: hear that a live file's document moved (``live_text``, never answered).
MachineRequestKind = Literal["promote", "flush", "live_text", "notebook"]
MACHINE_REQUEST_KINDS: Final[tuple[str, ...]] = get_args(MachineRequestKind)

#: What a box answers a request with. ``accepted`` means the file is at the
#: front of the queue and its bytes are on their way (for a flush: the push has
#: started, and a second ack follows); ``flushed`` means a flush's push has
#: ended with everything sent; every other outcome means nothing more is coming
#: for this request.
MachineAckOutcome = Literal["accepted", "flushed", "not_holder", "missing", "changed", "busy"]
MACHINE_ACK_OUTCOMES: Final[tuple[str, ...]] = get_args(MachineAckOutcome)

#: A relative path under the lease root is bounded like a tree report's path.
MAX_MACHINE_PATH_LENGTH: Final = 4096


class FileStamp(BaseModel):
    """A file's size and modified time: what the drive expects the box to hold,
    or what the box observed instead.

    A modified time may be before the epoch (an archive extracted as it was
    stamped): it is what the box's disk says, and an ack carrying it must
    parse, because an ack the gateway cannot parse closes the box's socket."""

    model_config = ConfigDict(extra="ignore")

    size: int = Field(ge=0)
    mtime_ns: int


class MachineRequest(BaseModel):
    """Server → box: bring one file's bytes to the drive now, or (``flush``)
    push everything the box holds of the leased folder.

    ``path`` is relative to the lease root, spelled as the box's own tree report
    spelled it. ``epoch`` is the lease's current epoch: a box whose held folder
    is at another epoch (a lease handed over since) answers ``not_holder``.
    ``expected`` is what the box last reported for the file; a box whose copy
    no longer matches answers ``changed`` rather than uploading something the
    reader did not ask for. ``deadline_ms`` is how long the drive will wait, so
    a box that cannot make it may answer ``busy`` at once. A flush names no
    file: ``node_id``, ``path`` and ``expected`` are a promote's alone.
    """

    model_config = ConfigDict(extra="ignore")

    t: Literal["machine.request"] = "machine.request"
    request_id: uuid.UUID
    kind: Literal["promote", "flush"]
    lease_node_id: uuid.UUID
    epoch: int = Field(ge=0)
    node_id: uuid.UUID | None = None
    path: str | None = Field(default=None, min_length=1, max_length=MAX_MACHINE_PATH_LENGTH)
    expected: FileStamp | None = None
    deadline_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _a_promote_names_its_file(self) -> MachineRequest:
        if self.kind == "promote" and (
            self.node_id is None or self.path is None or self.expected is None
        ):
            raise ValueError("a promote names the node, its path and what the box last reported")
        return self


#: The longest version token a ``live_text`` notice carries: an epoch and a
#: version vector, base64, which a document's peer cap keeps far below this.
MAX_LIVE_TOKEN_LENGTH: Final = 16 * 1024


class LiveTextRequest(BaseModel):
    """Server → box: the live document of a file under this box's lease moved.

    ``token`` names the document's version now (opaque to the box: it compares
    it with the one it holds and reads the text when they differ). ``epoch``
    is the lease's, as on a promote. Never answered."""

    model_config = ConfigDict(extra="ignore")

    t: Literal["machine.request"] = "machine.request"
    request_id: uuid.UUID
    kind: Literal["live_text"] = "live_text"
    lease_node_id: uuid.UUID
    epoch: int = Field(ge=0)
    node_id: uuid.UUID
    token: str = Field(min_length=1, max_length=MAX_LIVE_TOKEN_LENGTH)


#: What a notebook request asks of the engine on the box holding the notebook.
NotebookRequestOp = Literal[
    "run",
    "kernel",
    "outputs_clear",
    "comm",
    "env_install",
    "env_action",
    "snapshot",
    "frame_attach",
    "frame_detach",
    "table",
    "envs",
    "env_packages",
]
NOTEBOOK_REQUEST_OPS: Final[tuple[str, ...]] = get_args(NotebookRequestOp)
#: The code a box answers a notebook request with when it does not hold the
#: notebook's folder the request's lease names: it has not taken the folder
#: yet (a box just started or woken takes its folders seconds after its
#: worker is ready), or it put the folder's chat to sleep. The platform
#: decides which: it wakes a sleeping chat, and otherwise sends the same
#: request again under the same id, which the box serves once it holds the
#: folder. A box still not holding it past the platform's wait ends a run
#: with this as its reason.
NOTEBOOK_FOLDER_NOT_HELD: Final = "folder_not_held"


class NotebookMachineRequest(BaseModel):
    """Server -> box: a request for the notebook engine serving ``item_id``
    (a run, a kernel action, a person's widget message, an install, or a
    fresh snapshot of the kernel's state). Never acked on this channel: what
    it causes comes back as the kernel's events, posted by the box to the
    notebook's events route under the box's own credential. A box from before
    notebooks knows no such kind and ignores it."""

    model_config = ConfigDict(extra="ignore")

    t: Literal["machine.request"] = "machine.request"
    request_id: uuid.UUID
    kind: Literal["notebook"] = "notebook"
    drive_id: uuid.UUID
    item_id: uuid.UUID
    op: NotebookRequestOp
    body: dict[str, Any] = Field(default_factory=dict)
    #: The leased folder the notebook is in and its path under it, as a
    #: promote names a file: how the holder finds the notebook on its disk.
    #: ``None`` from a server that did not name them.
    lease_node_id: uuid.UUID | None = None
    path: str | None = Field(default=None, min_length=1, max_length=MAX_MACHINE_PATH_LENGTH)


#: A request of any kind.
AnyMachineRequest = MachineRequest | LiveTextRequest | NotebookMachineRequest

#: A request of any kind, told apart by ``kind``.
_MACHINE_REQUEST_ADAPTER: TypeAdapter[AnyMachineRequest] = TypeAdapter(
    Annotated[
        MachineRequest | LiveTextRequest | NotebookMachineRequest, Field(discriminator="kind")
    ]
)


def parse_machine_request(payload: object) -> AnyMachineRequest:
    """A server's ``machine.request`` of a kind this build knows. Raises
    ``pydantic.ValidationError`` for anything else."""
    return _MACHINE_REQUEST_ADAPTER.validate_python(payload)


class MachineAck(BaseModel):
    """Box → server: what became of one request. ``changed`` carries what the
    box found on disk instead."""

    model_config = ConfigDict(extra="ignore")

    t: Literal["machine.ack"] = "machine.ack"
    request_id: uuid.UUID
    outcome: MachineAckOutcome
    observed: FileStamp | None = None

    @model_validator(mode="after")
    def _observed_only_on_changed(self) -> MachineAck:
        if self.outcome == "changed" and self.observed is None:
            raise ValueError("a changed ack carries what the box observed")
        return self


#: What a box may send on the machine channel: one frame today.
_MACHINE_CLIENT_ADAPTER: TypeAdapter[MachineAck] = TypeAdapter(MachineAck)


def parse_machine_frame(raw: str | bytes) -> MachineAck:
    """The box's frame in ``raw`` JSON. Raises ``pydantic.ValidationError`` for
    anything that is not a well-formed ``machine.ack`` — including a
    ``machine.*`` tag this server does not know, which the gateway treats as a
    protocol error."""
    return _MACHINE_CLIENT_ADAPTER.validate_json(raw)


def machine_channel(machine_id: str) -> str:
    """The channel a machine's socket subscribes to."""
    return f"{MACHINE_CHANNEL_PREFIX}{machine_id}"


def machine_of_channel(raw: str) -> str | None:
    """The machine id a ``machine:<id>`` channel names, or ``None`` when ``raw``
    is not one."""
    match = MACHINE_CHANNEL_RE.fullmatch(raw)
    return None if match is None else match.group(1)


__all__ = [
    "MACHINE_ACK_OUTCOMES",
    "MACHINE_ACK_TAG",
    "MACHINE_CHANNEL_PATTERN",
    "MACHINE_CHANNEL_PREFIX",
    "MACHINE_CHANNEL_RE",
    "MACHINE_FRAME_PREFIX",
    "MACHINE_REQUEST_KINDS",
    "MACHINE_REQUEST_TAG",
    "MAX_LIVE_TOKEN_LENGTH",
    "MAX_MACHINE_PATH_LENGTH",
    "NOTEBOOK_FOLDER_NOT_HELD",
    "NOTEBOOK_REQUEST_OPS",
    "AnyMachineRequest",
    "FileStamp",
    "LiveTextRequest",
    "MachineAck",
    "MachineAckOutcome",
    "MachineRequest",
    "MachineRequestKind",
    "NotebookMachineRequest",
    "NotebookRequestOp",
    "machine_channel",
    "machine_of_channel",
    "parse_machine_frame",
    "parse_machine_request",
]
