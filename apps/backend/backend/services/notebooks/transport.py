"""How a request reaches the engine on the box that holds a notebook.

:class:`NotebookTransport` is the seam: the service asks it to deliver one
request (a run, a kernel action, a person's widget message, an install, a
fresh snapshot) to the machine serving the notebook, and the box answers with
the kernel's events, posted to the notebook's events route under its own
credential (:mod:`backend.services.notebooks.feed` accepts them).

Its one implementation today, :class:`MachineChannelTransport`, rides the
box's machine channel: the request is a ``machine.request`` of kind
``notebook`` (:class:`~alkera_core.schemas.realtime.machine.NotebookMachineRequest`)
written to the outbox addressed to ``machine:<machine_id>``, so it reaches the
one socket that proved it is that machine, in order with every other request,
whatever its size (the ephemeral lane caps a message below 8 KiB; a widget
message may be a mebibyte). When the org-bound worker credential lands, a
transport on the org worker's own channel replaces it behind this interface.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import EventType, emit
from alkera_core.events.types import Entity
from alkera_core.schemas.realtime.machine import (
    NotebookMachineRequest,
    NotebookRequestOp,
    machine_channel,
)
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class KernelHost:
    """Where a notebook's engine runs: the machine holding its folder, in the
    notebook's org."""

    org_id: uuid.UUID
    drive_id: uuid.UUID
    item_id: uuid.UUID
    machine_id: str
    #: The leased folder holding the notebook and its path under it, which
    #: the box finds the notebook on its disk by.
    lease_node_id: uuid.UUID | None = None
    path: str | None = None


class NotebookTransport(Protocol):
    """Carries one request to the engine serving a notebook."""

    async def send(
        self,
        host: KernelHost,
        op: NotebookRequestOp,
        body: Mapping[str, Any],
        *,
        request_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """Deliver ``op`` with ``body`` to ``host`` under ``request_id`` (a
        fresh one when ``None``); the request's id, which the box names on the
        events it posts in answer (an ``answer`` event for a request that
        wants one)."""
        ...


class MachineChannelTransport:
    """The box's machine channel, through the outbox (see the module
    docstring). Each request is its own committed transaction, so it reaches
    the box whatever becomes of the caller's."""

    def __init__(self, session_factory: Callable[[], AsyncSession] = AsyncSessionLocal) -> None:
        self._sessions = session_factory

    async def send(
        self,
        host: KernelHost,
        op: NotebookRequestOp,
        body: Mapping[str, Any],
        *,
        request_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        request = NotebookMachineRequest(
            request_id=request_id or uuid.uuid4(),
            drive_id=host.drive_id,
            item_id=host.item_id,
            op=op,
            body=dict(body),
            lease_node_id=host.lease_node_id,
            path=host.path,
        )
        async with self._sessions() as db:
            await emit(
                db,
                org_id=host.org_id,
                type=EventType.NOTEBOOK_EVENT,
                entity=Entity.NOTEBOOK,
                entity_id=machine_channel(host.machine_id),
                payload=request.model_dump(mode="json"),
            )
            await db.commit()
        return request.request_id


__all__ = ["KernelHost", "MachineChannelTransport", "NotebookTransport"]
