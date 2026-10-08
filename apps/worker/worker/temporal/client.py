"""The worker's view of the shared client: one connection per process, named
``worker`` in the Temporal UI."""

from __future__ import annotations

from alkera_core.temporal import shared_client
from temporalio.client import Client

WORKER_COMPONENT = "worker"


async def get_client() -> Client:
    """The process-wide client, connected on first use with the worker identity."""
    return await shared_client(component=WORKER_COMPONENT)
