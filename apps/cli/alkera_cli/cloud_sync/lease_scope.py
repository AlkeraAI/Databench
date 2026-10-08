"""The chat a connection credential is being leased for, while a tool runs.

On a shared box one process serves chats of several owners, and a credential a
chat may use is decided for THAT chat by the server. The tool dispatch sets the
chat here before a tool call (it is the one place every transport passes
through), the connector's credential lease reads it on the worker thread the
call runs on (``asyncio.to_thread`` copies the context), and the lease is asked
for and cached per chat. A share revoked for one chat then stops that chat's
leases at the server's next answer, never served from a lease another chat
made.

``None`` outside a scoped tool call (a person's own daemon, the schema loader),
where a lease is the person's or the box's own, as before.

``LEASE_WORKSPACE`` is the same for a notebook statement: the workspace whose
kernel ran it. A workspace's connections are its owner's (sharing a workspace
shares them), leased for that workspace alone.

A per-user credential on a box is the workspace owner's own grant: it is
leased only for a named chat or workspace and cached under that name, never
box-wide (:func:`lease_scope_key` refuses to answer without one).
"""

from __future__ import annotations

from contextvars import ContextVar

LEASE_CHAT: ContextVar[str | None] = ContextVar("alkera_lease_chat", default=None)
LEASE_WORKSPACE: ContextVar[str | None] = ContextVar("alkera_lease_workspace", default=None)


def lease_scope() -> tuple[str | None, str | None]:
    """The ``(chat, workspace)`` a lease is for on this thread; at most one is set."""
    return LEASE_CHAT.get(), LEASE_WORKSPACE.get()


def lease_scope_key() -> str | None:
    """``chat:<id>`` or ``workspace:<id>`` for the current lease, ``None`` when
    no chat or workspace is named."""
    chat, workspace = lease_scope()
    if chat:
        return f"chat:{chat}"
    if workspace:
        return f"workspace:{workspace}"
    return None


__all__ = ["LEASE_CHAT", "LEASE_WORKSPACE", "lease_scope", "lease_scope_key"]
