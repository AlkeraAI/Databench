"""The client side of the RPC on asyncio: connect to ``ALKERA_RPC_ENDPOINT``,
present the token in ``hello``, then talk.

The kernel runtime has its own threaded client (standard library, no asyncio
on the reader); this one serves tests, tools and any Python process on
Alkera's side that wants to act as a client.
"""

from __future__ import annotations

import asyncio
import platform
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import frames as f
from .peer import MethodRegistry, NotificationHandler, RpcPeer
from .service import parse_endpoint


def runtime_info() -> dict[str, Any]:
    gil = getattr(sys, "_is_gil_enabled", None)
    return {
        "version": platform.python_version(),
        "implementation": sys.implementation.name,
        "platform": f"{sys.platform}-{platform.machine()}",
        "free_threaded": bool(gil is not None and not gil()),
    }


@dataclass
class ClientSession:
    peer: RpcPeer
    #: The service's answer to ``hello`` (methods, limits, data_dir, ...).
    info: dict[str, Any]
    serve_task: asyncio.Task[None]

    async def close(self) -> None:
        await self.peer.close("client_closed")
        await asyncio.gather(self.serve_task, return_exceptions=True)


async def connect(
    endpoint: str,
    token: str,
    *,
    name: str = "alkera-client",
    version: str = "0.0.0",
    codecs: list[str] | None = None,
    libs: Mapping[str, str | None] | None = None,
    registry: MethodRegistry | None = None,
    on_notification: NotificationHandler | None = None,
    extra_hello: Mapping[str, Any] | None = None,
) -> ClientSession:
    """Connect, send ``hello`` and return the session. Raises ``RpcError``
    when the service refuses the token or the protocol."""
    reader, writer = await asyncio.open_unix_connection(parse_endpoint(endpoint))
    peer = RpcPeer(
        reader,
        writer,
        send_limit=f.FRAME_LIMIT_CLIENT,
        recv_limit=f.FRAME_LIMIT_SERVICE,
        registry=registry,
        on_notification=on_notification,
        allow_files_inbound=True,
    )
    task = asyncio.ensure_future(peer.serve())
    params: dict[str, Any] = {
        "token": token,
        "client": {"name": name, "version": version, "protocols": [f.PROTOCOL]},
        "runtime": runtime_info(),
        "codecs": list(codecs if codecs is not None else [f.CODEC_JSON, f.CODEC_ROWS]),
        "libs": dict(libs or {}),
    }
    params.update(extra_hello or {})
    try:
        info = await peer.request(f.HELLO_METHOD, params)
    except BaseException:
        await peer.close("hello_failed")
        await asyncio.gather(task, return_exceptions=True)
        raise
    return ClientSession(peer, info, task)
