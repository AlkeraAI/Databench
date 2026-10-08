"""Where a distribution adds to the gateway beyond the meter.

The open gateway authenticates people's sessions and streams provider traffic.
Two things belong to whoever runs it as a hosted service and bills for it:

* **proxy tokens.** A self-hosted gateway in proxy mode calls a hosted one with
  its org's proxy token. Accepting that token means standing up the identity
  its usage is funded through, which is the biller's. The biller registers a
  :data:`ProxyIdentity` on :data:`PROXY_IDENTITIES`; with none registered a
  proxy token is refused like any credential the gateway does not accept.
* **routes.** Extra routers (the proxy heartbeat) are registered on
  :data:`GATEWAY_ROUTERS` and included after the gateway's own.

Both are read when the app is built or a request arrives, which closes them,
so everything is registered during composition.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from alkera_core.extensions import ExtensionError, ExtensionPoint
from alkera_core.models import ProxyToken, User
from fastapi import APIRouter
from sqlalchemy.ext.asyncio import AsyncSession

#: Resolves an active proxy token to the identity its requests run as, creating
#: or refreshing whatever funds them. Called inside the caller's transaction.
ProxyIdentity = Callable[[AsyncSession, ProxyToken], Awaitable[User]]

PROXY_IDENTITIES: ExtensionPoint[ProxyIdentity] = ExtensionPoint("gateway_proxy_identity")
GATEWAY_ROUTERS: ExtensionPoint[APIRouter] = ExtensionPoint("gateway_routers")


def proxy_identity() -> ProxyIdentity | None:
    """The registered resolver, or ``None`` when this gateway accepts no proxy
    token. One at most: two would leave who funds a request to registration order."""
    registered = PROXY_IDENTITIES.items()
    if len(registered) > 1:
        raise ExtensionError("more than one proxy identity is registered on the gateway")
    return registered[0] if registered else None


__all__ = ["GATEWAY_ROUTERS", "PROXY_IDENTITIES", "ProxyIdentity", "proxy_identity"]
