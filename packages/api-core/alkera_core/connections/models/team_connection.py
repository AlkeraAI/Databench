"""Team/org Preconfigured Connections — admin-defined data connections
distributed to every member's workspace.

A row is one connection definition with one of two owners. With
``owner_user_id`` NULL it belongs to a team (the org root team for org-wide
ones) and is visible to members of that team and every descendant team
(membership materialization makes that a single ``team_id IN (member's teams)``
query — no tree walk). With ``owner_user_id`` set it is one person's own
connection: it still hangs off their org root team so every tenancy check and
event stream keyed by team keeps working, but exactly one person sees it.

Secrets are encrypted at rest via ``alkera_core.auth.secret_box`` and are
NEVER returned by any read API:

- ``shared_secret_encrypted`` — the admin-entered service credential for
  ``auth_mode="shared"`` connections; distributed to members over TLS at sync
  time via a dedicated, audited credential-fetch endpoint.
- ``named_secrets_encrypted``: independent role-to-ciphertext entries for a
  connector that authenticates several endpoints.
- ``oauth_client_secret_encrypted`` — the OAuth *client* secret for
  confidential per-user OAuth providers (e.g. a Google Desktop-app client).
  It never leaves the backend: the token exchange AND refresh run server-side.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    FetchedValue,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.account import dispositions
from alkera_core.account.dispositions import Disposition, Kind
from alkera_core.connections import CredentialState, Outcome
from alkera_core.db.base import Base
from alkera_core.db.row_security import register_content_table

_OUTCOMES = "(" + ",".join(f"'{o.value}'" for o in Outcome) + ")"
_IN_FLIGHT = "('queued','running')"
#: This slice writes only these two; the rest of ``CredentialState`` is reserved.
_CREDENTIAL_STATES = f"('{CredentialState.present.value}','{CredentialState.unreadable.value}')"


#: The stand-in owner of a team row inside the identity key. Postgres treats
#: NULLs as distinct, so a plain nullable fourth column would stop constraining
#: team rows entirely — which is every row that has no personal owner.
NIL_OWNER = "00000000-0000-0000-0000-000000000000"


class TeamConnection(Base):
    __tablename__ = "team_connections"
    __table_args__ = (
        # One (team, plugin, handle) per team as before, and one more per person
        # who adds their own connection under the same names.
        Index(
            "uq_team_connections_team_plugin_handle_owner",
            "team_id",
            "plugin",
            "handle",
            text(f"COALESCE(owner_user_id, '{NIL_OWNER}'::uuid)"),
            unique=True,
        ),
        CheckConstraint(
            f"last_outcome IS NULL OR last_outcome IN {_OUTCOMES}", name="ck_tc_last_outcome"
        ),
        CheckConstraint(
            f"verification_state IS NULL OR verification_state IN {_IN_FLIGHT}",
            name="ck_tc_verification_state",
        ),
        CheckConstraint(f"credential_state IN {_CREDENTIAL_STATES}", name="ck_tc_credential_state"),
        Index("ix_team_connections_org_team_id", "org_team_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: The org the row belongs to: the key the ``tenant_isolation`` row-level
    #: policy filters on. No Python default: the ``trg_team_connections_org`` trigger fills it
    #: from the root of ``team_id`` when a writer leaves it unset, refuses a value that
    #: disagrees, and refuses any change of it; the insert reads it back.
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_team_connections_org_team_id_teams"),
        nullable=False,
        server_default=FetchedValue(),
    )
    #: Set on a PERSONAL connection: the one member it belongs to. ``team_id``
    #: is then their org root team, which is what keys every tenancy check and
    #: event stream on this table — a personal row is scoped to the org like any
    #: other, it is simply visible to exactly one person inside it. NULL means
    #: the row is the team's, configured by an admin for everyone below it.
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE", name="fk_tc_owner_user_id"),
        nullable=True,
        index=True,
    )
    plugin: Mapped[str] = mapped_column(String(64), nullable=False)
    handle: Mapped[str] = mapped_column(String(128), nullable=False)
    # The RAW form values the admin chose to distribute — the connector's own
    # input names, exactly as typed, never a built connection. The member's
    # machine merges these with what its member typed and calls the connector's
    # build once, on a complete set, which is the only way a connector that
    # renames its inputs can produce the right shape. Secrets NEVER land here
    # (the anti-leak invariant every tier tests).
    shared_values: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    # "shared" (admin's service credential, distributable) | "per_user" (each
    # member completes with their own credential / OAuth authorize). Derived at
    # write time from which secret inputs the admin distributed, never asked.
    auth_mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default="shared")
    # Which of the plugin's auth methods this connection uses (must be a
    # team-capable method per the connector catalog; validated at write).
    auth_method: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    # The form fields each MEMBER supplies locally — every input whose distribute
    # switch the admin turned off, secrets included. The complement of
    # ``shared_values`` within the form, stored explicitly so a member's client
    # never has to re-derive its own job from a connector schema that may be a
    # version behind the one the admin filled in.
    member_fields: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    # Who put a shared credential in front of the whole team, when, and over
    # which inputs. NULL on a row that distributes no secret; cleared when the
    # stored secret is.
    shared_consent: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Admin opt-in: materialize as live in member workspaces without a click.
    # Only valid for fully-admin-configured (shared) connections — enforced at
    # the route layer against the catalog's capability matrix.
    auto_add: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    # --- shared-mode service credential (encrypted at rest, write-only) ---
    shared_secret_encrypted: Mapped[str | None] = mapped_column(String(8192), nullable=True)
    named_secrets_encrypted: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    # Bumped on every secret rotation; members re-fetch when it advances.
    credential_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    # --- per-user OAuth app config (confidential providers only) ---
    oauth_client_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Encrypted at rest; never returned, never sent to a member machine.
    oauth_client_secret_encrypted: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    # Endpoint/scope/redirect overrides (external-IdP fronting etc.) — non-secret.
    oauth_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # --- what the last verification found (stamped by the worker) ---
    #: The outcome of the last SETTLED verification, of any kind. NULL means
    #: nothing has ever been checked — which is a different fact from a check
    #: that ran and found nothing to say (``unsupported``).
    last_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: The verification that produced it. No foreign key on purpose: records are
    #: garbage-collected and the row must survive losing the one it points at.
    last_verification_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    #: ``queued`` | ``running`` while a verification is in flight, else NULL.
    verification_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: When the last settled verification finished, whatever it found — so
    #: "checked 3 days ago" is true of a failure too.
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: The last settled verdict's own sentence, carried whole. Unbounded for the
    #: same reason the record's ``detail`` is: a driver's multi-host failure list
    #: is what an admin acts on, and a width cut it before its last line.
    last_detail: Mapped[str] = mapped_column(Text, nullable=False, server_default="")

    # --- the credential axis, independent of any verification ---
    #: ``present`` normally; ``unreadable`` when the secret box cannot open the
    #: stored ciphertext, which is Alkera's problem and not a rejected login.
    credential_state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="present"
    )
    credential_state_reason: Mapped[str] = mapped_column(
        String(512), nullable=False, server_default=""
    )

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


#: Held to the tenant content policy on its org, like every content table.
register_content_table("team_connections", "org_team_id")

# What erasing an account does to the columns here that name a person. Spelled
# beside the table so the two always load together.
#: Personal connections (owner set) carry the person's credentials; org and team
#: connections (owner NULL) are untouched by this statement.
dispositions.register(
    Disposition(
        "team_connections",
        "owner_user_id",
        Kind.ERASE,
        "a personal connection and its credentials",
        step=dispositions.delete_rows("team_connections", "owner_user_id"),
    )
)
dispositions.register(
    Disposition("team_connections", "created_by_id", Kind.KEEP, dispositions.AUTHOR_REASON)
)
