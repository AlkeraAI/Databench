"""The supervisor's calls to the backend, on the box's machine credential:
the claim, the beats, the ids-only routing and each org worker's credential."""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping, Sequence
from typing import Final

from alkera_cli.supervisor.http import Api
from alkera_cli.supervisor.org_routing import RouteEntry, parse_routing

#: The header a machine credential rides in (``alkera_core.auth.machine_token``;
#: a test holds the two together without the supervisor importing the auth
#: package, which loads the database layer).
MACHINE_CREDENTIAL_HEADER: Final = "X-Alkera-Machine-Credential"
#: How many routing pages one read follows (500 chats a page).
ROUTING_MAX_PAGES: Final = 64


class MachineApi:
    """The four calls the supervisor makes, on the machine credential."""

    def __init__(self, *, api_url: str, credential: str) -> None:
        self.credential = credential
        self._api = Api(
            api_url,
            {"Authorization": f"Bearer {credential}", MACHINE_CREDENTIAL_HEADER: credential},
        )

    def __repr__(self) -> str:
        return "MachineApi(credential=...)"

    async def claim(self, body: Mapping[str, object]) -> str:
        data = await self._api.call("POST", "/api/v1/machines/claim", dict(body))
        machine_id = data.get("id") or data.get("machine_id") if isinstance(data, Mapping) else None
        if not isinstance(machine_id, str) or not machine_id:
            raise ValueError("the claim answered no machine id")
        return machine_id

    async def heartbeat(self, machine_id: str, body: Mapping[str, object]) -> None:
        await self._api.call("POST", f"/api/v1/machines/{machine_id}/heartbeat", dict(body))

    async def read(self) -> Sequence[RouteEntry]:
        """The backend's ids-only routing for this machine, every page."""
        entries: list[RouteEntry] = []
        cursor: str | None = None
        for _ in range(ROUTING_MAX_PAGES):
            query = urllib.parse.urlencode({"limit": 500, **({"cursor": cursor} if cursor else {})})
            body = await self._api.call("GET", f"/api/v1/machines/me/routing?{query}")
            entries.extend(parse_routing(body))
            nxt = body.get("next_cursor") if isinstance(body, Mapping) else None
            cursor = nxt if isinstance(nxt, str) and nxt else None
            if cursor is None:
                return entries
        raise ValueError("the routing did not end within its page limit")

    async def worker_credential(self, org_id: str) -> tuple[str, float]:
        """A credential bound to (this machine, ``org_id``), minted on the
        machine credential: the only one the org's worker gets. With how many
        seconds it lives."""
        body = await self._api.call(
            "POST", "/api/v1/machines/me/worker-credentials", {"org_id": org_id}
        )
        token = body.get("token") if isinstance(body, Mapping) else None
        lives = body.get("expires_in") if isinstance(body, Mapping) else None
        if not isinstance(token, str) or not token or not isinstance(lives, int) or lives < 1:
            raise ValueError("the worker credential answer is malformed")
        return token, float(lives)


__all__ = ["MACHINE_CREDENTIAL_HEADER", "ROUTING_MAX_PAGES", "MachineApi"]
