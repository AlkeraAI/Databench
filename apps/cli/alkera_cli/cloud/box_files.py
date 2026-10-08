"""The Files clients a provisioned box speaks with: one for transfers, one for
lease beats.

A chat folder is pulled and pushed whole, so the transfer budget bounds a
REQUEST, not a folder — and a box moving a large folder across a slow link is
the deployment that has to be able to raise it, which is why an unnamed budget
comes from the environment rather than from a constant. Every request also
finishes inside a deadline of its own (a large body is paced by its size, never
cut off at a JSON answer's budget), so a server that trickles cannot hold a
take, and the chat behind it, for ever. Connecting is bounded apart from the
budget, and a host that could not be connected to answers the next folder at
once rather than one two-minute wait per chat.

The lease beats go out on a client of their own: their own connection pool and
a short deadline. Sharing the transfers' pool, a beat could queue behind the
pull of a large folder and land after the lease it was keeping had lapsed.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from alkera_cli.cloud.limits import (
    files_connect_timeout_seconds,
    files_timeout_seconds,
    folder_beat_timeout_seconds,
    origin_outage_seconds,
)
from alkera_cli.files.deadline import DeadlineTransport
from alkera_cli.files.outage import OutageAwareTransport
from alkera_cli.files.push import FilesApi

__all__ = ["BoxFilesClients", "box_files_clients"]


@dataclass(frozen=True, slots=True)
class BoxFilesClients:
    files: FilesApi
    http: httpx.Client
    beats: httpx.Client


def _budgets(timeout: float | httpx.Timeout | None) -> tuple[httpx.Timeout, float]:
    """The per-operation timeouts and the whole-request deadline. A caller
    that spelled out an httpx budget of its own keeps it whole."""
    if isinstance(timeout, httpx.Timeout):
        parts = (timeout.connect, timeout.read, timeout.write, timeout.pool, 1.0)
        return timeout, max(part for part in parts if part is not None)
    total = files_timeout_seconds() if timeout is None else timeout
    return httpx.Timeout(total, connect=min(total, files_connect_timeout_seconds())), total


def box_files_clients(
    *, api_url: str, auth: httpx.Auth, timeout: float | httpx.Timeout | None = None
) -> BoxFilesClients:
    """The box's transfer client and its beat client, each request signed by
    ``auth``: neither holds a bearer of its own."""
    from alkera_sdk.client import AlkeraClient

    budgets, total = _budgets(timeout)
    api = AlkeraClient(
        base_url=api_url,
        timeout=budgets,
        httpx_args={
            "auth": auth,
            "transport": OutageAwareTransport(
                DeadlineTransport(httpx.HTTPTransport(), total=total),
                window=origin_outage_seconds(),
            ),
        },
    )
    beat_budget = folder_beat_timeout_seconds()
    beats = AlkeraClient(
        base_url=api_url,
        timeout=httpx.Timeout(beat_budget),
        httpx_args={
            "auth": auth,
            "transport": DeadlineTransport(httpx.HTTPTransport(), total=beat_budget),
        },
    )
    return BoxFilesClients(
        files=api.files,
        http=api.raw_client.get_httpx_client(),
        beats=beats.raw_client.get_httpx_client(),
    )
