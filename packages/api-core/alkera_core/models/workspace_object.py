"""Workspace objects: the durable nouns a workspace accumulates.

One table, one type discriminator. A ``chat`` is a conversation; a ``query`` is
saved parameterised SQL; a ``result`` is a receipted table promoted out of a
chat so it survives the machine that produced it; a ``workspace`` holds chats
that share one file tree, and every chat names the workspace it is in. ``board``
and ``app`` are named by the CHECK so a later wave adds rows, not a migration.

The shape follows the one ``kb_items`` proved, with its corrections applied:

* a row ``id`` AND a client-minted ``logical_id`` inside a ``namespace``, so a
  client that retries a create lands on the row it already made rather than a
  second one — the uniqueness is ``(org_team_id, namespace, logical_id)``;
* ``version`` starts at 1 and counts accepted writes. ``expected_version`` is a
  REQUEST field, never a column, and ``0`` is not "no precondition": a write
  naming 0 is refused against a live row, which is what stops a client that
  forgot to read from silently overwriting one that did;
* ``content_updated_at`` is the writer's own clock, distinct from the server's
  ``updated_at`` row cursor, so two writers can compare edits without trusting
  each other's server;
* ``deleted_at`` is a tombstone epoch (0 = live), so a delete propagates
  instead of leaving a reader guessing why a row vanished.

``chat_messages`` is the transcript's system of record — the durable half of
the realtime chat document, which keeps only a bounded live window. ``seq`` is
assigned by the server under the chat row's lock, so it is dense and ordered
per chat; ``event_id`` is the producer's idempotency key, unique within the
chat, so a republished harness event lands once.

``object_payload_rows`` is the result store's spill: a promoted table larger
than the inline cap is paged into rows keyed ``(object_id, page)``. The handle
columns (``sha256``, ``size``, ``media_type``) are the daemon's blob handle,
carried unchanged so a later S3 backend swaps the storage without changing a
single shape a client sees.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, TypeAlias, get_args

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    FetchedValue,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base

#: The type discriminator's closed vocabulary, spelled ONCE. The CHECK below
#: repeats the tuple derived from it and the API's read shape annotates its
#: ``type`` with the alias itself, so the column, the constraint and the wire
#: cannot drift: a row the database admits is a row the route can hand back.
#: (A read shape that kept its own copy of this list answered 500 for every
#: chat template the moment that type was added here and not there.)
ObjectType: TypeAlias = Literal[
    "chat", "query", "result", "board", "app", "report", "chat_template", "workspace"
]
OBJECT_TYPES: tuple[str, ...] = get_args(ObjectType)
#: The types nothing may create any more. They still LOAD — an existing row is
#: read, rendered and listed exactly as it was — but a create naming one is
#: refused, because a chat template is the one shape a reader saves a chat as
#: now, and two ways to save the same thing is a question nobody should be
#: asked. Kept in ``OBJECT_TYPES`` deliberately: the CHECK still has to admit
#: the rows that exist.
RETIRED_OBJECT_TYPES: tuple[str, ...] = ("query", "report")
#: An object's lifecycle. ``pending_upload`` is a promoted result whose payload
#: the producing daemon has not delivered yet; a reader must not show its rows.
OBJECT_STATUSES: tuple[str, ...] = ("draft", "pending_upload", "ready", "failed")
#: Who may author a chat message.
MESSAGE_ROLES: tuple[str, ...] = ("user", "assistant", "tool", "system")
#: The per-message payload cap, matching the event outbox's — a transcript
#: entry is a published event, and the two must agree or one of them lies.
MAX_MESSAGE_PAYLOAD_BYTES = 2 * 1024 * 1024
#: The namespace every Wave-1 object is minted into. Kept a column, not a
#: constant, so a later per-project or per-connection namespace is a value.
DEFAULT_NAMESPACE = "workspace"


def _in_list(column: str, values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({joined})"


class WorkspaceObject(Base):
    __tablename__ = "workspace_objects"
    __table_args__ = (
        UniqueConstraint(
            "org_team_id",
            "namespace",
            "logical_id",
            name="uq_workspace_objects_org_namespace_logical",
        ),
        CheckConstraint(_in_list("type", OBJECT_TYPES), name="ck_workspace_objects_type"),
        CheckConstraint(_in_list("status", OBJECT_STATUSES), name="ck_workspace_objects_status"),
        CheckConstraint("version >= 1", name="ck_workspace_objects_version_positive"),
        Index("ix_workspace_objects_org_type_created", "org_team_id", "type", "created_at"),
        Index("ix_workspace_objects_owner_user_id", "owner_user_id"),
        # One warmed-ahead chat per person in each org: the rule is the
        # database's, so two warm calls racing each other leave one spare, and
        # "the owner's spare here" is one indexed read rather than a scan of
        # their chats.
        Index(
            "uq_workspace_objects_one_spare_per_owner_org",
            "owner_user_id",
            "org_team_id",
            unique=True,
            postgresql_where=text("type = 'chat' AND deleted_at = 0 AND (spec->>'spare') = 'true'"),
        ),
        # The chats a machine holds: what a box lists when its socket opens,
        # what a release moves off it, what placement offers the next box. An
        # index on the bound machine id over the live chat rows only, so each
        # of those is an indexed read rather than a scan of every object.
        Index(
            "ix_workspace_objects_chat_machine",
            text("(spec->>'machine_id')"),
            postgresql_where=text("type = 'chat' AND deleted_at = 0"),
        ),
        # The workspaces a box reports holding (its sandbox, where notebook
        # kernels run): with the chats above, the orgs a pool box's request
        # may reach, read on every request it makes.
        Index(
            "ix_workspace_objects_workspace_machine",
            text("(spec->>'machine_id')"),
            postgresql_where=text(
                "type = 'workspace' AND deleted_at = 0 "
                "AND (spec->>'binding_authority') = 'workspace'"
            ),
        ),
        # The chats a workspace holds: what its page lists, what deleting it
        # ends, and how a chat finds the owner whose connections it uses.
        Index(
            "ix_workspace_objects_chat_workspace",
            text("(spec->>'workspace_id')"),
            postgresql_where=text(
                "type = 'chat' AND deleted_at = 0 AND (spec->>'workspace_id') IS NOT NULL"
            ),
        ),
        # The live chats in no workspace yet: what the reconcile pass adopts,
        # oldest id first. Partial on exactly its predicate, so a pass over a
        # table where every chat is adopted reads an empty index, not the table.
        Index(
            "ix_workspace_objects_chat_unadopted",
            "id",
            postgresql_where=text(
                "type = 'chat' AND deleted_at = 0 AND (spec->>'workspace_id') IS NULL"
            ),
        ),
        # The rows a workspace being deleted still has to finish: its chats,
        # unreachable since the request, and the workspace itself until its
        # folder goes to the trash. Empty whenever no deletion is in flight.
        Index(
            "ix_workspace_objects_ending",
            "id",
            postgresql_where=text("(spec->>'ending_workspace_id') IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_workspace_objects_org_team_id_teams"),
        nullable=False,
    )
    #: The client's own id for this object, unique within its namespace and org.
    logical_id: Mapped[str] = mapped_column(String(255), nullable=False)
    namespace: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=DEFAULT_NAMESPACE
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    #: Accepted writes, starting at 1; the optimistic-concurrency token.
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="ready")
    #: The type-specific body: one ``VersionedModel`` per type, dumped.
    spec: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE", name="fk_workspace_objects_owner_user_id_users"),
        nullable=False,
    )
    #: The team the object belongs to; null means the whole org may read it.
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="SET NULL", name="fk_workspace_objects_team_id_teams"),
        nullable=True,
    )
    #: ``org`` | ``team:<uuid>`` | ``private`` — the audience, in the scope
    #: grammar of ``alkera_core.authz.chat_scope`` that both the REST policies
    #: and the socket decide from (``scope_for_team`` spells it).
    visibility_scope: Mapped[str] = mapped_column(String(128), nullable=False)
    #: The WRITER's content clock (epoch seconds), distinct from ``updated_at``.
    content_updated_at: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0")
    )
    #: Tombstone epoch; 0 is live.
    deleted_at: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        # (chat_id, seq) is the paging key AND the ordering invariant, so the
        # unique constraint's own btree index is the index the reads use; a
        # second one on the same columns would only cost writes.
        UniqueConstraint("chat_id", "seq", name="uq_chat_messages_chat_seq"),
        UniqueConstraint("chat_id", "event_id", name="uq_chat_messages_chat_event"),
        CheckConstraint(_in_list("role", MESSAGE_ROLES), name="ck_chat_messages_role"),
        CheckConstraint("seq >= 1", name="ck_chat_messages_seq_positive"),
        CheckConstraint(
            f"octet_length(payload::text) <= {MAX_MESSAGE_PAYLOAD_BYTES}",
            name="ck_chat_messages_payload_size",
        ),
        Index("ix_chat_messages_org_team_id", "org_team_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "workspace_objects.id",
            ondelete="CASCADE",
            name="fk_chat_messages_chat_id_workspace_objects",
        ),
        nullable=False,
    )
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_chat_messages_org_team_id_teams"),
        nullable=False,
    )
    #: Dense, server-assigned, ordered within the chat. The paging cursor.
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The producer's event kind (``prompt``, a harness ``event_type``, …).
    kind: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    #: The producer's idempotency key; a republished event lands once.
    event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ObjectPayloadRow(Base):
    __tablename__ = "object_payload_rows"
    __table_args__ = (
        CheckConstraint("page >= 0", name="ck_object_payload_rows_page_nonnegative"),
        CheckConstraint("size >= 0", name="ck_object_payload_rows_size_nonnegative"),
        Index("ix_object_payload_rows_org_team_id", "org_team_id"),
    )

    object_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "workspace_objects.id",
            ondelete="CASCADE",
            name="fk_object_payload_rows_object_id_workspace_objects",
        ),
        primary_key=True,
    )
    page: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: The org the row belongs to: the key the ``tenant_isolation`` row-level
    #: policy filters on. No Python default: the ``trg_object_payload_rows_org`` trigger fills it
    #: from its object's org when a writer leaves it unset, refuses a value that
    #: disagrees, and refuses any change of it; the insert reads it back.
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_object_payload_rows_org_team_id_teams"),
        nullable=False,
        server_default=FetchedValue(),
    )
    #: The producing daemon's blob handle, carried unchanged.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    media_type: Mapped[str] = mapped_column(
        String(128), nullable=False, server_default="application/json"
    )
    #: One page of the envelope's rows, as a JSON array of arrays.
    page_rows: Mapped[list[list[Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = [
    "DEFAULT_NAMESPACE",
    "MAX_MESSAGE_PAYLOAD_BYTES",
    "MESSAGE_ROLES",
    "OBJECT_STATUSES",
    "OBJECT_TYPES",
    "ChatMessage",
    "ObjectPayloadRow",
    "ObjectType",
    "WorkspaceObject",
]
