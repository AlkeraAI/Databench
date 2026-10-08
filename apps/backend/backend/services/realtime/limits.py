"""In-process caps on long-lived realtime connections.

An event stream or a socket holds a uvicorn slot, a hub subscription and (for a
socket) a database write every tick, for as long as the client keeps it open —
so, unlike a request, an unbounded number of them is a resource leak a single
tenant can cause on purpose. Each surface has one :class:`ConnectionGate`
counting live connections process-wide, per key (a person, or one machine a
person registered) and per principal (the person and every machine of theirs
together), with the limits read live from settings so they can be tuned (and
tested) without a restart.

Deliberately per process, like the request throttles: there is no shared store,
so the fleet-wide ceiling is ``limit x tasks``. That still turns "open ten
thousand streams" into "open a few hundred" and can never fail open on a cache
outage.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import ClassVar

from alkera_core.config import settings

from backend.services.compute.assertion import verified_machine_id

_AGENT_PREFIX = "agent:"


async def connection_key(
    user_id: object,
    headers: Mapping[str, str],
    *,
    org_id: object,
    credential_id: str | None,
) -> str:
    """The key one live connection is counted under.

    A person's own connections are counted per user. A connection whose agent
    assertion VERIFIES as a machine this user registered on this very session
    (``credential_id``, the session's ``jti``) is the workspace box's — its
    event stream, opened once per process for every chat it runs — and is
    counted under the machine instead, so a box never takes one of its owner's
    slots: with the box on the owner's key, the owner's tabs met the cap and
    lost their live updates. Any other assertion — made up, somebody else's
    machine, malformed — counts as none: the connection is the person's, so a
    fresh id per connection buys no fresh slot. The route that owns the
    request refuses a malformed pair in its own vocabulary.
    """
    machine = await verified_machine_id(
        headers, user_id=user_id, org_id=org_id, credential_id=credential_id
    )
    if machine is None:
        return str(user_id)
    return f"{_AGENT_PREFIX}{user_id}:{machine}"


def _principal_of(key: str) -> str:
    """The person a connection key belongs to: the key itself for a person,
    the user half of ``agent:<user>:<machine>`` for one of their machines."""
    if key.startswith(_AGENT_PREFIX):
        return key[len(_AGENT_PREFIX) :].split(":", 1)[0]
    return key


class ConnectionGate:
    """Counts live connections for one surface, overall, per key (a user id,
    or one of that user's verified machines) and per principal (the user and
    all of their machines together — the ceiling no machine id can step
    outside of).

    The app is single-process asyncio, so the counters are atomic between awaits
    and need no lock. A slot must be released exactly once; the owners wrap their
    stream in a ``release_once`` guard for that.
    """

    _sse: ClassVar[ConnectionGate | None] = None
    _ws: ClassVar[ConnectionGate | None] = None

    def __init__(
        self,
        *,
        total: Callable[[], int],
        per_user: Callable[[], int],
        per_principal: Callable[[], int] | None = None,
    ) -> None:
        self._total_limit = total
        self._per_user_limit = per_user
        self._per_principal_limit = per_principal
        self._active = 0
        self._by_key: dict[str, int] = {}
        self._by_principal: dict[str, int] = {}

    @property
    def active(self) -> int:
        return self._active

    def active_for(self, key: str) -> int:
        return self._by_key.get(key, 0)

    def active_for_principal(self, key: str) -> int:
        return self._by_principal.get(_principal_of(key), 0)

    def try_acquire(self, key: str) -> bool:
        """Take a slot for ``key``; ``False`` when any cap is reached."""
        if self._active >= max(0, self._total_limit()):
            return False
        if self.active_for(key) >= max(0, self._per_user_limit()):
            return False
        principal = _principal_of(key)
        if self._per_principal_limit is not None and self._by_principal.get(principal, 0) >= max(
            0, self._per_principal_limit()
        ):
            return False
        self._active += 1
        self._by_key[key] = self.active_for(key) + 1
        self._by_principal[principal] = self._by_principal.get(principal, 0) + 1
        return True

    def release(self, key: str) -> None:
        if self._active > 0:
            self._active -= 1
        for table, name in ((self._by_key, key), (self._by_principal, _principal_of(key))):
            held = table.get(name, 0)
            if held <= 1:
                table.pop(name, None)
            else:
                table[name] = held - 1

    @classmethod
    def sse(cls) -> ConnectionGate:
        """The gate for ``GET /api/v1/events`` streams."""
        if cls._sse is None:
            cls._sse = cls(
                total=lambda: settings.realtime_sse_max_streams,
                per_user=lambda: settings.realtime_sse_max_streams_per_user,
                per_principal=lambda: settings.realtime_sse_max_streams_per_principal,
            )
        return cls._sse

    @classmethod
    def ws(cls) -> ConnectionGate:
        """The gate for ``WS /api/v1/ws`` sockets."""
        if cls._ws is None:
            cls._ws = cls(
                total=lambda: settings.realtime_ws_max_connections,
                per_user=lambda: settings.realtime_ws_max_connections_per_user,
                per_principal=lambda: settings.realtime_ws_max_connections_per_principal,
            )
        return cls._ws

    @classmethod
    def reset_for_tests(cls) -> None:
        """Forget every held slot on both gates (test isolation between cases)."""
        for gate in (cls._sse, cls._ws):
            if gate is not None:
                gate._active = 0
                gate._by_key.clear()
                gate._by_principal.clear()


__all__ = ["ConnectionGate", "connection_key"]
