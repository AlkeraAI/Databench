"""The event vocabulary the outbox carries.

Every ``event_outbox`` row names one :class:`EventType`. This registry is the
single list: the portal's invalidation stream, the WebSocket doc-sync lane and
the authorization audit all spell their types here, so a producer cannot
invent a type the consumers do not know, and a client-side key map can be
checked exhaustive against it.

Visibility is the row's audience: ``org`` (every member of the org), ``platform``
(platform staff only) or ``user:<uuid>`` (exactly one user). The grammar is
pinned once here and repeated as a CHECK constraint on the table, so a row a
reader would refuse can never be written.

Deliberately silent (no producer emits for these, by design): a
knowledge item ignore / unignore (a per-user optimistic toggle), a gate lease
heartbeat (one per runner every few seconds would flood the activity view
with frames that change nothing it shows), a lazy invitation expiry sweep,
and the schedule-driven balance rolls (the portal keeps its slow poll for
those until their cores announce themselves).
"""

from __future__ import annotations

import re
from enum import StrEnum
from uuid import UUID


class EventType(StrEnum):
    """Wire names of every event the outbox can carry."""

    GATE_RUN_INGESTED = "gate_run.ingested"
    # The /gate/activity surface: lease acquire + release only, never heartbeats.
    GATE_LEASE_CHANGED = "gate_lease.changed"
    TEAM_CONNECTION_UPDATED = "team_connection.updated"
    TEAM_CONNECTION_PROBED = "team_connection.probed"
    #: A verification record moved — queued, running, settled or abandoned.
    CONNECTION_VERIFICATION_CHANGED = "connection.verification_changed"
    #: A credential's own state changed, independent of any verification:
    #: a shared ciphertext Alkera cannot open, or a member's grant the
    #: provider definitively refused to renew.
    CONNECTION_CREDENTIAL_CHANGED = "connection.credential_changed"
    BILLING_SUMMARY_CHANGED = "billing.summary_changed"
    ORG_BILLING_CHANGED = "org_billing.changed"
    KB_ITEM_CHANGED = "kb_item.changed"
    USER_EMAIL_VERIFIED = "user.email_verified"
    MEMBERSHIP_CHANGED = "membership.changed"
    INVITATION_CHANGED = "invitation.changed"
    CHAT_UPDATED = "chat.updated"
    ARTIFACT_UPDATED = "artifact.updated"
    # A workspace machine registered, changed reachability, or refused a start.
    COMPUTE_MACHINE_CHANGED = "compute_machine.changed"
    #: An org machine changed state, step or stop reason. The payload is
    #: exactly ``{state, step, stop_reason}``: never a price.
    ORG_MACHINE_CHANGED = "org_machine.changed"
    #: A workspace's move to another machine advanced. The payload is
    #: ``{move_id, state, error_code}``.
    WORKSPACE_MACHINE_MOVE = "workspace.machine_move"
    #: A workspace object was created, edited, or changed status (a promoted
    #: result whose payload arrived). The payload names the type and the
    #: version so a client can decide whether it already holds this one.
    WORKSPACE_OBJECT_CHANGED = "workspace_object.changed"
    # The durable doc-sync lane: the payload is a doc-sync envelope.
    DOC_OP = "doc.op"
    # Written by the authorization layer for every allow / deny decision. Listed
    # here so the registry stays the one vocabulary; the value is also pinned
    # against ``alkera_core.authz.decision.AUTHZ_EVENT_TYPE``.
    AUTHZ_DECISION = "authz.decision"
    #: A file node was created, renamed, moved, trashed, restored or had its
    #: attributes or access changed. The payload carries ids only — never a
    #: name or a path — so outbox retention is off the erasure path.
    FILE_NODE_CHANGED = "file_node.changed"
    #: A bulk file operation (a large move, a trash sweep) advanced or finished.
    FILE_OPERATION_CHANGED = "file_operation.changed"
    #: The in-flight plane under a folder lease moved: the holder reported what
    #: it is writing, a write was admitted into the subtree for it to apply, or
    #: the lease was taken, handed back or reaped. The version is the lease's
    #: ``live_seq``, so a client that missed a frame knows it is behind without
    #: diffing anything.
    FILE_LEASE_CHANGED = "file_lease.changed"
    #: A batch of a notebook kernel's events (run and cell states, outputs,
    #: widget messages), delivered over the socket gateway to the notebook's
    #: ``nb:<item_id>`` channel and never as a bare invalidation.
    NOTEBOOK_EVENT = "notebook.event"


class RealtimeEventType(StrEnum):
    """The events a client may receive over the portal's event stream.

    Every :class:`EventType` except those that exist for the server's own
    use: ``doc.op`` (a doc-sync envelope, delivered over the socket gateway to
    a channel subscriber and never as a bare invalidation), ``notebook.event``
    (a notebook kernel's events, delivered the same way) and
    ``authz.decision`` (an audit record). This enum reaches the OpenAPI schema
    through the stream's response model, so a client-side handler map can be
    checked exhaustive against it; a test pins it equal to the registry minus
    those two.
    """

    GATE_RUN_INGESTED = "gate_run.ingested"
    GATE_LEASE_CHANGED = "gate_lease.changed"
    TEAM_CONNECTION_UPDATED = "team_connection.updated"
    TEAM_CONNECTION_PROBED = "team_connection.probed"
    CONNECTION_VERIFICATION_CHANGED = "connection.verification_changed"
    CONNECTION_CREDENTIAL_CHANGED = "connection.credential_changed"
    BILLING_SUMMARY_CHANGED = "billing.summary_changed"
    ORG_BILLING_CHANGED = "org_billing.changed"
    KB_ITEM_CHANGED = "kb_item.changed"
    USER_EMAIL_VERIFIED = "user.email_verified"
    MEMBERSHIP_CHANGED = "membership.changed"
    INVITATION_CHANGED = "invitation.changed"
    CHAT_UPDATED = "chat.updated"
    ARTIFACT_UPDATED = "artifact.updated"
    COMPUTE_MACHINE_CHANGED = "compute_machine.changed"
    ORG_MACHINE_CHANGED = "org_machine.changed"
    WORKSPACE_MACHINE_MOVE = "workspace.machine_move"
    WORKSPACE_OBJECT_CHANGED = "workspace_object.changed"
    FILE_NODE_CHANGED = "file_node.changed"
    FILE_OPERATION_CHANGED = "file_operation.changed"
    FILE_LEASE_CHANGED = "file_lease.changed"


#: The payload key a producer sets when what it is announcing can move WHO may
#: read the thing, rather than only what the thing says. One doorbell carries
#: both — a chat rings ``chat.updated`` for a new title and for its own deletion
#: — and a consumer that has already admitted a reader needs to tell them apart:
#: re-deciding on every event would put a database read on the streaming path,
#: and re-deciding on none of them is how a deleted chat kept streaming to
#: someone who was already watching. Spelled here, with the event vocabulary, so
#: producer and consumer cannot spell it differently.
ACCESS_CHANGED_KEY = "access_changed"

#: The payload key a chat's doorbell carries the chat's machine binding under —
#: the id of the box that runs it, ``None`` while nothing does. A box hears a
#: chat's traffic only while the chat is bound to it, and the announcement that
#: binds a chat is the first frame the box must hear about it: a predicate that
#: knew only the chats already bound would drop exactly that frame. Spelled
#: here, with the event vocabulary, so the producer and the consumers cannot
#: spell it differently. It never reaches a client: the event stream frames
#: only the narrowing keys it names.
BOUND_MACHINE_KEY = "machine_id"

#: The ``reason`` a chat's doorbell carries when the chat was deleted. The
#: box that served it reads it as the backend's word that the chat is gone,
#: which a not-found on the chat's folder cannot say on its own (a box that
#: lost its sight of a live chat is told the same not-found).
CHAT_DELETED_REASON = "deleted"

#: The registry members that never leave the server as an invalidation frame.
SERVER_ONLY_EVENT_TYPES: frozenset[EventType] = frozenset(
    {EventType.DOC_OP, EventType.AUTHZ_DECISION, EventType.NOTEBOOK_EVENT}
)
#: Wire values a client may receive; membership is the stream's delivery filter.
REALTIME_EVENT_TYPE_VALUES: frozenset[str] = frozenset(m.value for m in RealtimeEventType)


def is_realtime_event_type(value: str) -> bool:
    """Whether a row of type ``value`` may be framed onto the event stream."""
    return value in REALTIME_EVENT_TYPE_VALUES


class Entity(StrEnum):
    """The kinds of thing an event is about (``event_outbox.entity``)."""

    GATE_RUN = "gate_run"
    GATE_LEASE = "gate_lease"
    TEAM_CONNECTION = "team_connection"
    CONNECTION_VERIFICATION = "connection_verification"
    BILLING_ACCOUNT = "billing_account"
    ORG_BILLING = "org_billing"
    KB_ITEM = "kb_item"
    USER = "user"
    MEMBERSHIP = "membership"
    INVITATION = "invitation"
    CHAT = "chat"
    ARTIFACT = "artifact"
    COMPUTE_MACHINE = "compute_machine"
    ORG_MACHINE = "org_machine"
    WORKSPACE_OBJECT = "workspace_object"
    DOC = "doc"
    AUTHZ = "authz"
    NOTEBOOK = "notebook"


VISIBILITY_ORG = "org"
VISIBILITY_PLATFORM = "platform"
_USER_VISIBILITY_PREFIX = "user:"

#: The ``user:<uuid>`` grammar, as a pattern both ``re`` and Postgres ``~``
#: understand. The table's ``ck_event_outbox_visibility`` CHECK repeats it
#: verbatim (a model module cannot import this package without a cycle), and a
#: test pins the two spellings equal.
USER_VISIBILITY_PATTERN = r"^user:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_USER_VISIBILITY_RE = re.compile(USER_VISIBILITY_PATTERN)


def coerce_event_type(value: EventType | str) -> EventType:
    """The registry member for ``value``.

    Accepts a member or its wire value; anything else (an unknown name, the
    Python member name, a different case) raises ``ValueError`` so a producer
    cannot write a type no consumer knows.
    """
    if isinstance(value, EventType):
        return value
    try:
        return EventType(value)
    except ValueError as exc:
        raise ValueError(f"unknown event type {value!r}") from exc


def user_visibility(user_id: UUID) -> str:
    """The visibility string that addresses exactly ``user_id``."""
    return f"{_USER_VISIBILITY_PREFIX}{user_id}"


def validate_visibility(value: str) -> str:
    """``value`` unchanged when it is ``org``, ``platform`` or ``user:<uuid>``;
    ``ValueError`` otherwise."""
    if value in (VISIBILITY_ORG, VISIBILITY_PLATFORM) or _USER_VISIBILITY_RE.match(value):
        return value
    raise ValueError(f"invalid visibility {value!r}: expected 'org', 'platform' or 'user:<uuid>'")


def user_id_of_visibility(value: str) -> UUID | None:
    """The addressed user when ``value`` is ``user:<uuid>``, else ``None``."""
    if _USER_VISIBILITY_RE.match(value):
        return UUID(value.removeprefix(_USER_VISIBILITY_PREFIX))
    return None


__all__ = [
    "ACCESS_CHANGED_KEY",
    "BOUND_MACHINE_KEY",
    "REALTIME_EVENT_TYPE_VALUES",
    "SERVER_ONLY_EVENT_TYPES",
    "USER_VISIBILITY_PATTERN",
    "VISIBILITY_ORG",
    "VISIBILITY_PLATFORM",
    "Entity",
    "EventType",
    "RealtimeEventType",
    "coerce_event_type",
    "is_realtime_event_type",
    "user_id_of_visibility",
    "user_visibility",
    "validate_visibility",
]
