"""What a box's root process reads and mints on its machine credential.

A box that serves several orgs runs one process per org. The root process
holds the machine credential and nothing of any tenant's: it reads which chats
are bound to its machine, in which org, and in what state (ids and states, no
titles, no content), and mints for each org a short-lived credential bound to
that org, which is the only credential that org's process gets.

In-flight HTTP shapes only: plain ``BaseModel``.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from alkera_core.schemas.objects import SessionState


class MachineWorkerCredentialRequest(BaseModel):
    """The org a worker credential is minted for."""

    org_id: UUID


class MachineWorkerCredentialRead(BaseModel):
    """A freshly minted org-bound worker credential. ``token`` is the bearer
    the org's process presents; it reaches ``org_id`` and nothing else, and
    stops working at ``expires_at`` or the moment the machine credential that
    minted it is revoked, whichever is first."""

    token: str
    org_id: UUID
    expires_at: datetime
    expires_in: int = Field(ge=1)


class MachineRoutingEntry(BaseModel):
    """One chat bound to the machine, as the root process routes it: which
    org's process serves it and whether that process should be holding it.
    Ids and states only: what the chat says is read by that org's process,
    on its own credential."""

    chat_id: UUID
    org_id: UUID
    workspace_id: UUID | None = None
    #: Where the chat's agent session stands, as every other read of the chat
    #: derives it.
    state: SessionState = "asleep"
    #: A person's message nothing has answered yet, or a turn still shown
    #: working: the chat should be open now.
    pending_turn: bool = False
    #: A reader opened the chat while it was asleep and nothing has answered.
    wake_requested: bool = False
    #: How many times the server ended this chat's service under its box; a
    #: process holding it at a lower value drops it.
    end_seq: int = 0


class MachineRoutingRead(BaseModel):
    items: list[MachineRoutingEntry]
    next_cursor: str | None = None


__all__ = [
    "MachineRoutingEntry",
    "MachineRoutingRead",
    "MachineWorkerCredentialRead",
    "MachineWorkerCredentialRequest",
]
