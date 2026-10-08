"""The settings-driven Temporal client every service builds its connection from.

The backend's nudges, the worker runtime and the deployment health probe all
connect the same way — same address, namespace, credentials, TLS decision and
data converter — so the decision lives here once. Two things are deliberate:

- ``pydantic_data_converter`` is the ONLY converter. Everything that crosses
  Temporal history is a ``VersionedModel`` (history outlives the process that
  wrote it), and the pydantic converter is what serializes those faithfully.
  A client, worker or test environment built with any other converter would
  read another's payloads wrong, so no code path may pick a different one.
- The process-wide ``shared_client()`` is built lazily on first use, never at
  import. Importing this module in a request handler, a test or a CLI command
  must not open a connection.
"""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from temporalio.client import Client, TLSConfig
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.contrib.pydantic import pydantic_data_converter

from alkera_core.config import Settings
from alkera_core.config import settings as default_settings
from alkera_core.logging import get_logger

log = get_logger(__name__)

DEFAULT_COMPONENT = "alkera"
"""The identity prefix when a caller does not name itself (``backend``,
``worker-money``, …). The identity is what the Temporal UI shows against a
started workflow or a polling worker, so a real component name is worth passing."""

_CERT_MARKER = "BEGIN CERTIFICATE"
_KEY_MARKER = "PRIVATE KEY"


def default_identity(component: str) -> str:
    """``<component>@<host>:<pid>`` — unique per process, readable in the UI."""
    return f"{component}@{socket.gethostname()}:{os.getpid()}"


@dataclass(frozen=True, slots=True)
class ClientOptions:
    """Everything ``Client.connect`` needs, resolved from settings and inspectable
    without opening a connection (the tests pin the resolution rules here)."""

    target_host: str
    namespace: str
    api_key: str | None
    tls: bool | TLSConfig
    identity: str


def _pem_bytes(value: str, marker: str) -> bytes:
    """Inline PEM text (it carries the marker) as bytes, else the bytes of the file
    at that path — the same two shapes ``OUTBOUND_CA_BUNDLE`` accepts."""
    if marker in value:
        return value.encode()
    return Path(value).read_bytes()


def client_options(
    settings: Settings = default_settings,
    *,
    identity: str | None = None,
    component: str = DEFAULT_COMPONENT,
) -> ClientOptions:
    """Resolve the connection options from ``settings``.

    TLS: a client certificate + key pair turns on mutual TLS (a ``TLSConfig`` with
    both PEMs, which implies TLS whatever ``TEMPORAL_TLS`` says). Otherwise TLS is
    ``settings.temporal_tls_enabled`` — the explicit flag, else on exactly when an
    API key is set. Half a pair is refused rather than silently connecting without
    the client certificate the operator meant to present.
    """
    cert = settings.temporal_tls_client_cert
    key = settings.temporal_tls_client_key
    if bool(cert) != bool(key):
        raise ValueError(
            "TEMPORAL_TLS_CLIENT_CERT and TEMPORAL_TLS_CLIENT_KEY must be set together"
        )
    tls: bool | TLSConfig
    if cert and key:
        tls = TLSConfig(
            client_cert=_pem_bytes(cert, _CERT_MARKER),
            client_private_key=_pem_bytes(key, _KEY_MARKER),
        )
    else:
        tls = settings.temporal_tls_enabled
    return ClientOptions(
        target_host=settings.temporal_address,
        namespace=settings.temporal_namespace,
        # Empty == unset: the deploy seeds every secret as "" when it is not in use.
        api_key=settings.temporal_api_key or None,
        tls=tls,
        identity=identity or default_identity(component),
    )


async def connect_client(
    settings: Settings = default_settings,
    *,
    identity: str | None = None,
    component: str = DEFAULT_COMPONENT,
) -> Client:
    """Open a new connection to the configured Temporal frontend."""
    opts = client_options(settings, identity=identity, component=component)
    return await Client.connect(
        opts.target_host,
        namespace=opts.namespace,
        api_key=opts.api_key,
        tls=opts.tls,
        identity=opts.identity,
        data_converter=pydantic_data_converter,
    )


_shared: Client | None = None
_shared_lock: asyncio.Lock | None = None


async def shared_client(*, component: str = DEFAULT_COMPONENT) -> Client:
    """The one client this process shares; connected lazily on first use.

    Concurrent first callers (a burst of webhook nudges on a cold backend) wait on
    one lock so exactly one connection is opened. ``component`` names the identity
    and only matters to the caller that actually opens the connection.
    """
    global _shared, _shared_lock
    if _shared is not None:
        return _shared
    if _shared_lock is None:
        _shared_lock = asyncio.Lock()
    async with _shared_lock:
        if _shared is None:
            _shared = await connect_client(component=component)
        return _shared


def reset_shared_client() -> None:
    """Forget the shared client so the next ``shared_client()`` reconnects.

    The SDK's client owns no resource that needs an explicit close — its channel
    is released with the object — so forgetting the reference IS the shutdown.
    Tests call this between environments; a long-lived process calls it when its
    settings change under it.
    """
    global _shared, _shared_lock
    _shared = None
    _shared_lock = None


async def close_shared_client() -> None:
    """The graceful-shutdown spelling of :func:`reset_shared_client` (async so a
    future SDK that grows a real close can be awaited here without touching callers)."""
    reset_shared_client()


async def start_workflow_best_effort(
    client: Client,
    workflow: str,
    *,
    id: str,
    task_queue: str,
    args: Sequence[Any] = (),
    signal: str | None = None,
    timeout_s: float = 2.0,
    fail_log_event: str,
) -> bool:
    """Start (or signal-with-start) a workflow without ever raising.

    This is the nudge primitive: a webhook or an admin route wants the worker to
    look at new work *now*, but the request must succeed whether or not the
    orchestrator answers — the schedules are the safety net. Two shapes, never
    combined:

    - ``signal`` given → signal-with-start. A running workflow with that id gets
      the signal (a drain performs one more pass); otherwise one is started with
      the signal already delivered.
    - ``signal`` omitted → start with ``USE_EXISTING``: a second nudge for the
      same entity attaches to the run already in flight instead of duplicating it.

    ``args`` are the workflow's positional arguments — the entity id per-entity
    work takes (a drain takes none and reads its clock and page from defaults).
    Bounded by ``timeout_s`` end to end; any failure is logged under
    ``fail_log_event`` and reported as ``False``.
    """
    rpc_timeout = timedelta(seconds=timeout_s)
    positional: dict[str, Any] = {"args": list(args)} if args else {}
    try:
        if signal is None:
            started = client.start_workflow(
                workflow,
                id=id,
                task_queue=task_queue,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                rpc_timeout=rpc_timeout,
                **positional,
            )
        else:
            started = client.start_workflow(
                workflow,
                id=id,
                task_queue=task_queue,
                start_signal=signal,
                rpc_timeout=rpc_timeout,
                **positional,
            )
        await asyncio.wait_for(started, timeout_s)
    except Exception as exc:
        log.warning(
            fail_log_event,
            workflow=workflow,
            workflow_id=id,
            task_queue=task_queue,
            error=str(exc) or type(exc).__name__,
        )
        return False
    return True
