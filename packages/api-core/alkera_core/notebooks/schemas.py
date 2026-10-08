"""Wire shapes of the notebook routes (``/api/v1/notebooks/...``).

The document operations are the engine's (``NotebookOp``), spelled here as the
HTTP body the agent and box peers send; their semantics are the same whichever
store applies them. Every shape is in flight only (nothing here is persisted),
so they are plain Pydantic models.

What the routes answer about a notebook (``NotebookView``, ``NotebookOpsResult``,
the kernel and the environments) is the engine's own models, which the backend
imports from ``alkera_notebook`` itself: this package never depends on the
notebook engine, so the worker and the gateway, which load the ORM through it,
do not carry the engine and its kernel stack in their images.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

#: A cell id: 10 characters of lower-case Crockford base32 (50 bits).
CELL_ID_PATTERN: Final = r"^[0-9a-hjkmnp-tv-z]{10}$"
CELL_ID_RE: Final = re.compile(CELL_ID_PATTERN)
#: A submit id, which makes a batch of operations idempotent.
SUBMIT_ID_PATTERN: Final = r"^[A-Za-z0-9_-]{8,48}$"
#: A chat's id as a box names it on an operation batch (a chat id is a UUID).
AGENT_CHAT_ID_PATTERN: Final = r"^[0-9a-fA-F-]{32,36}$"
#: A client's own id for a run request (idempotent, like a submit id).
CLIENT_RUN_ID_PATTERN: Final = SUBMIT_ID_PATTERN
#: A blob or widget asset hash: lower-case hex SHA-256.
SHA256_PATTERN: Final = r"^[0-9a-f]{64}$"

CellKind = Literal["setup", "python", "function", "class", "sql", "markdown", "unparsable"]
CELL_KINDS: Final[tuple[str, ...]] = (
    "setup",
    "python",
    "function",
    "class",
    "sql",
    "markdown",
    "unparsable",
)

#: Every error an operation batch can be refused with, naming the op index.
OpErrorCode = Literal[
    "cell_not_found",
    "edit_not_found",
    "edit_ambiguous",
    "invalid_name",
    "unknown_kind",
    "invalid_config",
    "setup_must_be_first",
    "cap_exceeded",
    "not_representable",
]
OP_ERROR_CODES: Final[tuple[str, ...]] = (
    "cell_not_found",
    "edit_not_found",
    "edit_ambiguous",
    "invalid_name",
    "unknown_kind",
    "invalid_config",
    "setup_must_be_first",
    "cap_exceeded",
    "not_representable",
)

#: The most operations one batch carries, and edits one ``edit`` carries.
MAX_OPS_PER_BATCH: Final = 500
MAX_EDITS_PER_OP: Final = 100


class _Op(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextEdit(BaseModel):
    """Replace ``old`` with ``new`` in a cell's text. ``occurrence`` (1-based)
    picks one of several matches; without it, ``old`` must match once."""

    model_config = ConfigDict(extra="forbid")

    old: str
    new: str
    occurrence: int | None = Field(default=None, ge=1)


class InsertCell(_Op):
    op: Literal["insert"]
    kind: str = "python"
    source: str = ""
    name: str = "_"
    after: str | None = None
    before: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)


class EditCell(_Op):
    op: Literal["edit"]
    cell_id: str
    edits: list[TextEdit] = Field(min_length=1, max_length=MAX_EDITS_PER_OP)


class ReplaceCell(_Op):
    """Set a cell's whole text; applied as a diff, so concurrent typing in the
    parts it leaves alone survives."""

    op: Literal["replace"]
    cell_id: str
    source: str


class DeleteCell(_Op):
    op: Literal["delete"]
    cell_id: str


class RestoreCell(_Op):
    op: Literal["restore"]
    cell_id: str
    after: str | None = None


class MoveCell(_Op):
    op: Literal["move"]
    cell_id: str
    after: str | None = None
    before: str | None = None


class SetCellName(_Op):
    op: Literal["rename"]
    cell_id: str
    name: str


class SetCellKind(_Op):
    op: Literal["set_kind"]
    cell_id: str
    kind: str


class SetCellConfig(_Op):
    op: Literal["set_config"]
    cell_id: str
    # Its own title, so the SDK generator does not collide it with the
    # insert's plain "Config".
    config: dict[str, Any] = Field(title="Cell config changes")


class SetCellMeta(_Op):
    """Change a SQL or Markdown cell's settings; a key set to ``None`` goes
    back to its default (a SQL cell with no ``connection`` runs in DuckDB)."""

    op: Literal["set_meta"]
    cell_id: str
    # Its own title: the SDK generator names an inline object by its title, and
    # a second plain "Meta" would collide with the insert's.
    meta: dict[str, Any] = Field(title="Cell meta changes")


class SetSetting(_Op):
    op: Literal["set_setting"]
    key: str
    value: Any


NotebookOp = Annotated[
    InsertCell
    | EditCell
    | ReplaceCell
    | DeleteCell
    | RestoreCell
    | MoveCell
    | SetCellName
    | SetCellKind
    | SetCellConfig
    | SetCellMeta
    | SetSetting,
    Field(discriminator="op"),
]


class NotebookOpsRequest(BaseModel):
    """``POST /api/v1/notebooks/{drive_id}/{item_id}/ops``: a batch applied
    atomically on a Loro peer at ``base_token`` (the head when absent)."""

    model_config = ConfigDict(extra="forbid")

    ops: list[NotebookOp] = Field(min_length=1, max_length=MAX_OPS_PER_BATCH)
    base_token: str | None = Field(default=None, max_length=8192)
    submit_id: str | None = Field(default=None, pattern=SUBMIT_ID_PATTERN)
    #: The chat whose agent made the batch, when the box holding the folder
    #: applies it for that agent: the batch is then written as the agent,
    #: for the chat's person. Only the holder may name one, and only a chat
    #: bound to it in the notebook's workspace.
    agent_chat_id: str | None = Field(default=None, pattern=AGENT_CHAT_ID_PATTERN)


#: The largest unsent update an editor may hand over after a restart (the
#: largest update the lane takes, base64-encoded).
MAX_REBASE_UPDATE_CHARS: Final = 1_400_000


class RebaseRequest(BaseModel):
    """``POST .../rebase``: an editor's update written in ``epoch`` that never
    reached it (the document's history restarted first), standard base64 of
    the Loro update bytes. The server carries it into the current epoch cell
    by cell."""

    model_config = ConfigDict(extra="forbid")

    epoch: int = Field(ge=1)
    update: str = Field(min_length=1, max_length=MAX_REBASE_UPDATE_CHARS)


class RebaseResult(BaseModel):
    """The token of the document's state once the update is in."""

    token: str


class OpErrorBody(BaseModel):
    """A refused batch (422): which operation, and why."""

    code: OpErrorCode
    message: str
    op_index: int


# -- reading ------------------------------------------------------------------


# -- runs, kernel, comm, environment --------------------------------------------


class CellsTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["cells"]
    ids: list[str] = Field(min_length=1, max_length=2000)


class AllTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["all"]


class StaleTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["stale"]


class AboveTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["above"]
    id: str


class BelowTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["below"]
    id: str


RunTarget = Annotated[
    CellsTarget | AllTarget | StaleTarget | AboveTarget | BelowTarget,
    Field(discriminator="kind"),
]

#: A run's status, in the engine's words: ``queued`` and ``running`` while it
#: lasts; ``ok``, ``error``, ``interrupted`` (``reason`` says by what),
#: ``kernel_restarted`` (the kernel went away under it) or ``refused`` (the
#: store or a gate refused it) once it ended; ``needs_confirmation`` (the cost
#: guard asks first), ``coalesced`` (merged into an identical queued run) and
#: ``planned`` (a plan-only request) for a request that never ran itself.
RunStatus = Literal[
    "queued",
    "running",
    "ok",
    "error",
    "interrupted",
    "kernel_restarted",
    "refused",
    "needs_confirmation",
    "coalesced",
    "planned",
]
RUN_STATUSES: Final[tuple[str, ...]] = (
    "queued",
    "running",
    "ok",
    "error",
    "interrupted",
    "kernel_restarted",
    "refused",
    "needs_confirmation",
    "coalesced",
    "planned",
)
#: The statuses a run ends in: none of them moves on.
RUN_FINAL_STATUSES: Final[frozenset[str]] = frozenset(
    {"ok", "error", "interrupted", "kernel_restarted", "refused", "coalesced", "planned"}
)
RunTrigger = Literal["run", "run_all", "run_stale", "widget", "autorun"]
RUN_TRIGGERS: Final[tuple[str, ...]] = ("run", "run_all", "run_stale", "widget", "autorun")


class RunRequest(BaseModel):
    """``POST .../runs``. ``frontier`` is the document token the requester
    holds: the run waits (up to 2 s) for the document to include it, then
    takes its targets' text from there."""

    model_config = ConfigDict(extra="forbid")

    target: RunTarget
    frontier: str | None = Field(default=None, max_length=8192)
    confirm_expensive: bool = False
    client_run_id: str | None = Field(default=None, pattern=CLIENT_RUN_ID_PATTERN)


class RunAccepted(BaseModel):
    run_id: str
    status: RunStatus
    #: Whether the requester's frontier was in the document when the targets'
    #: text was taken (false: it was taken at the head after the wait).
    frontier_included: bool
    #: ``cell id -> the text submitted for it``, as taken.
    submitted: dict[str, str] = Field(default_factory=dict)
    repeat: bool = False


KernelAction = Literal["status", "interrupt", "interrupt_all", "restart", "shutdown"]


class KernelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: KernelAction


class OutputsClearRequest(BaseModel):
    """Clear these cells' outputs for everyone, or every cell's when
    ``cell_ids`` is ``None``."""

    model_config = ConfigDict(extra="forbid")
    cell_ids: list[Annotated[str, Field(pattern=CELL_ID_PATTERN, max_length=10)]] | None = Field(
        default=None, min_length=1, max_length=2000
    )


#: A frame id, minted and signed by the backend for one person, one notebook
#: and (optionally) one socket: ``<owner>.<socket tag>.<nonce>.<signature>``.
FRAME_ID_PATTERN: Final = r"^[0-9a-f]{32}\.(any|[0-9a-f]{12})\.[0-9a-f]{16}\.[0-9a-f]{32}$"


class CommRequest(BaseModel):
    """A person's widget message from an output frame. The frame must be
    attached (``POST .../frames``), the sender's, and own ``comm_id``.
    ``buffers`` are standard base64."""

    model_config = ConfigDict(extra="forbid")

    frame_id: str = Field(pattern=FRAME_ID_PATTERN)
    comm_id: str = Field(min_length=1, max_length=128)
    msg_id: str = Field(min_length=1, max_length=128)
    content: dict[str, Any]
    buffers: list[str] = Field(default_factory=list, max_length=64)


class EnvInstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    packages: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        min_length=1, max_length=50
    )


#: What ``POST .../env/{action}`` does: build the environment from its spec
#: now, remove packages from it, or cancel the build under way.
EnvChangeAction = Literal["build", "remove", "cancel"]


class EnvChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: The packages to remove (by name); empty for a build or a cancel.
    packages: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=50
    )


class Accepted(BaseModel):
    """A request handed to the notebook's machine."""

    accepted: bool
    request_id: str


# -- output frames, tables, environments, widget assets --------------------------


class FrameAttachRequest(BaseModel):
    """``POST .../frames``: attach an output frame at the engine's widget hub.
    ``peer_id`` (the socket's id from its ``welcome``) narrows the frame's
    events to that socket; without it they reach every socket of the person."""

    model_config = ConfigDict(extra="forbid")

    output_id: str = Field(min_length=1, max_length=128)
    model_ids: list[Annotated[str, Field(min_length=1, max_length=128)]] | None = Field(
        default=None, max_length=256
    )
    peer_id: str | None = Field(default=None, min_length=1, max_length=64)


class FrameAttached(BaseModel):
    """The frame's id and the comm-open replays of its model closure."""

    frame_id: str
    opens: list[dict[str, Any]]


class TableColumn(BaseModel):
    """One column of a table page: its name and the frame library's own name
    for its type (``Date``, ``Decimal(precision=4, scale=2)``, ``Int64``)."""

    model_config = ConfigDict(extra="ignore")

    name: str
    type: str = ""


class TablePage(BaseModel):
    """``GET .../cells/{cell_id}/table``: one page of a table output.

    The table page the kernel makes (``_alkera_kernel.tables``), unchanged:
    the same ``schema``, ``rows``, ``total_rows`` and ``offset`` a cell's
    table output carries for its first page, so a reader takes every page
    the same way. ``rows`` is one list per row in column order, each cell
    already plain JSON (a date as ISO text, a decimal as its text)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    #: ``schema`` on the wire (the name shadows a pydantic attribute).
    columns: list[TableColumn] = Field(alias="schema")
    rows: list[list[Any]]
    total_rows: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1)


class WidgetAssetResolved(BaseModel):
    """``GET .../widget-assets/resolve``: the hash of a widget module's code."""

    sha256: str


# -- activity -------------------------------------------------------------------


class ActingForRef(BaseModel):
    """The person an agent acts for: their id and their name."""

    id: str
    display_name: str


class ActorRef(BaseModel):
    """Who did something, named: a person by their name, an agent as
    "<the brand's agent name> for <the person's name>" with that person in
    ``acting_for``, the platform itself by the product's name."""

    kind: Literal["person", "agent", "system"]
    id: str
    display_name: str
    acting_for: ActingForRef | None = None


class ActivityItem(BaseModel):
    at: datetime
    actor: ActorRef
    kind: Literal["edit", "run"]
    cell_ids: list[str] = Field(default_factory=list)
    run_id: str | None = None
    status: str | None = None


class NotebookConnection(BaseModel):
    """One connection a SQL cell of the notebook may name.

    ``name`` is the connection's name, what a cell stores
    (``alkera.sql(..., connection="<name>")``) and what the kernel resolves
    on the box; it is also how the connection is shown. ``can_use`` is the
    server's answer for this reader and this connection, with ``reason`` one
    sentence when it is no."""

    id: str
    name: str
    engine: str
    """The connector, as the platform names it (``snowflake``, ``postgres``)."""
    engine_title: str
    """The connector as people read it (``Snowflake``, ``PostgreSQL``)."""
    kind: Literal["team", "personal", "per_user"]
    """A team's shared connection, its owner's own one, or a team's
    connection each person signs in to (used with the workspace owner's
    sign-in)."""
    team_name: str = ""
    credential_owner: str = ""
    """The person whose credentials a statement on this connection runs on:
    the owner of the workspace holding the notebook, by name. Everyone the
    workspace is shared with uses the owner's connections, never their own."""
    can_use: bool
    reason: str = ""


class NotebookConnections(BaseModel):
    """``GET .../connections``: what the notebook's SQL cells may name. Empty
    for a notebook in no workspace, whose kernel resolves no connection."""

    connections: list[NotebookConnection] = Field(default_factory=list)


class NotebookEditor(BaseModel):
    """``GET .../editor``: where the notebook editor opens a notebook, or a
    new notebook in a folder. ``chat_id`` is the chat whose workspace pane
    runs it: the chat whose own folder holds it, or, in a workspace's folder,
    the chat a wake of that workspace goes through. ``None`` where no kernel
    can run (outside every workspace and chat folder, or in an ended one) or
    where the caller may send in no chat that would run it."""

    chat_id: str | None = None


class Activity(BaseModel):
    """``GET .../activity``: what happened since ``since``, oldest first."""

    since: datetime
    items: list[ActivityItem]


__all__ = [
    "CELL_ID_PATTERN",
    "CELL_ID_RE",
    "CELL_KINDS",
    "CLIENT_RUN_ID_PATTERN",
    "FRAME_ID_PATTERN",
    "MAX_EDITS_PER_OP",
    "MAX_OPS_PER_BATCH",
    "MAX_REBASE_UPDATE_CHARS",
    "OP_ERROR_CODES",
    "RUN_FINAL_STATUSES",
    "RUN_STATUSES",
    "RUN_TRIGGERS",
    "SHA256_PATTERN",
    "SUBMIT_ID_PATTERN",
    "AboveTarget",
    "Accepted",
    "ActingForRef",
    "Activity",
    "ActivityItem",
    "ActorRef",
    "AllTarget",
    "BelowTarget",
    "CellKind",
    "CellsTarget",
    "CommRequest",
    "DeleteCell",
    "EditCell",
    "EnvInstallRequest",
    "FrameAttachRequest",
    "FrameAttached",
    "InsertCell",
    "KernelAction",
    "KernelRequest",
    "MoveCell",
    "NotebookConnection",
    "NotebookConnections",
    "NotebookEditor",
    "NotebookOp",
    "NotebookOpsRequest",
    "OpErrorBody",
    "OpErrorCode",
    "OutputsClearRequest",
    "RebaseRequest",
    "RebaseResult",
    "ReplaceCell",
    "RestoreCell",
    "RunAccepted",
    "RunRequest",
    "RunStatus",
    "RunTarget",
    "RunTrigger",
    "SetCellConfig",
    "SetCellKind",
    "SetCellMeta",
    "SetCellName",
    "SetSetting",
    "StaleTarget",
    "TableColumn",
    "TablePage",
    "TextEdit",
    "WidgetAssetResolved",
]
