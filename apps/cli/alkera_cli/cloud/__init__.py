"""The daemon's cloud mirror: the publisher peer of a cloud chat.

A chat that lives in the cloud (``workspace_objects`` type ``chat``) is driven
by a daemon on the customer's machine. This package is the bridge: a WebSocket
client on the backend's realtime gateway (``transport``), one ``ChatMirror`` per
cloud chat that publishes the local harness's events as durable ``append`` ops
and token deltas on the ephemeral lane while consuming the relays readers send
(``mirror``), the pure event → op translation with its size bound (``publish``),
the per-turn budget (``budget``), and the service that discovers which chats
this machine serves (``service``).

Every REST call the mirror makes carries the device JWT plus the agent
assertion headers, so the backend records ``[user, agent]`` on every write. No
credential ever rides a URL: the socket ticket travels in the subprotocol list.
"""

from __future__ import annotations

from alkera_cli.cloud.budget import TurnBudget, TurnMeter
from alkera_cli.cloud.mirror import ChatMirror
from alkera_cli.cloud.rest import CloudApiError, CloudRestClient
from alkera_cli.cloud.service import CloudMirrorService, MirrorSettings
from alkera_cli.cloud.transport import CloudSocket, DocHandle, DocOpError
from alkera_cli.host.backoff import ReconnectBackoff

__all__ = [
    "ChatMirror",
    "CloudApiError",
    "CloudMirrorService",
    "CloudRestClient",
    "CloudSocket",
    "DocHandle",
    "DocOpError",
    "MirrorSettings",
    "ReconnectBackoff",
    "TurnBudget",
    "TurnMeter",
]
