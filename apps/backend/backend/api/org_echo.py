"""Echo the request's org on every response that resolved one.

Every credential door stashes the acting context on ``request.state``
(``backend.auth.dependencies``); its org is the credential's org. This
middleware copies it into an ``X-Alkera-Org`` response header, whatever the
credential (a session, a CLI token, a personal access, CI or proxy token, a
machine credential) and whatever the outcome (a refusal the route raised is
still a response in that org). A client compares it with the org it rendered;
the matching request-side assertion is checked in the dependency, before the
route runs.

Pure ASGI rather than ``BaseHTTPMiddleware`` for the same reason as the other
middleware here: it only rewrites the header block of ``http.response.start``
and never touches a streamed body.
"""

from __future__ import annotations

from typing import Any

from alkera_core.auth.tenancy import ORG_HEADER
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class OrgEchoMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Created here, so a ``Request`` built anywhere downstream (even on a
        # shallow copy of the scope) writes into this same dict.
        state: dict[str, Any] = scope.setdefault("state", {})

        async def send_with_org(message: Message) -> None:
            if message["type"] == "http.response.start":
                ctx = state.get("acting_context")
                org_id = getattr(ctx, "org_id", None)
                if org_id is not None:
                    MutableHeaders(scope=message)[ORG_HEADER] = str(org_id)
            await send(message)

        await self.app(scope, receive, send_with_org)


__all__ = ["OrgEchoMiddleware"]
