"""Test data and backend doubles for the cloud-sync suites: TeamConnectionRecord
factories, plus the shared-credential backend behind the real client."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from alkera_cli.cloud_sync.client import TeamConnectionsClient
from alkera_cli.plugins.plugin_base.team_connections import TeamConnectionRecord, TeamMemberState

SHARED_VALUES: dict[str, str] = {"host": "db.internal", "dbname": "analytics", "user": "svc"}
"""The admin's raw postgres form input. A record carries what they TYPED, and
the connector refuses to build without a host and a database, so a default short
of that would hand every caller a ``None`` connection instead of the row under
test."""


def rec(record_id: str = "r1", **over: Any) -> TeamConnectionRecord:
    base: dict[str, Any] = {
        "team_id": "t1",
        "team_name": "Data Platform",
        "plugin": "postgres",
        "handle": "wh",
        "shared_values": dict(SHARED_VALUES),
        "auth_mode": "shared",
        "auth_method": "password",
        "auto_add": False,
        "enabled": True,
        "has_shared_secret": True,
        "credential_version": 1,
        "updated_at": datetime(2026, 7, 1, tzinfo=UTC),
    }
    base.update(over)
    return TeamConnectionRecord(id=record_id, **base)


def member_credential_rec(record_id: str = "r1", **over: Any) -> TeamConnectionRecord:
    """A row the admin shaped but left the identity of: each member answers
    ``user`` and ``password`` on their own machine, so the admin's half stops at
    the host and the database."""
    base: dict[str, Any] = {
        "shared_values": {"host": "db.internal", "dbname": "analytics"},
        "auth_mode": "per_user",
        "auth_method": "password",
        "has_shared_secret": False,
        "member_fields": ["password", "user"],
    }
    base.update(over)
    return rec(record_id, **base)


def accepted(**over: Any) -> TeamMemberState:
    """A member who took a shared row and holds the copy of its secret that a
    pre-lease backend sent."""
    base: dict[str, Any] = {"added": True, "local_handle": "wh", "fetched_credential_version": 1}
    base.update(over)
    return TeamMemberState(**base)


#: The admin-side form values each connector needs before it will build on this
#: machine. A caller that wants a row to be LIVE has to supply them — a row that
#: doesn't assemble is a pending row, not a connection.
LIVE_SHARED_VALUES: dict[str, dict[str, str]] = {
    "postgres": dict(SHARED_VALUES),
    "mysql": dict(SHARED_VALUES),
    "tinybird": {"host": "api.tinybird.co"},
    "snowflake": {"account": "acct", "warehouse": "wh", "database": "analytics"},
}


def live_record(record_id: str, plugin: str, handle: str, **over: Any) -> TeamConnectionRecord:
    """A shared, lease-custody row of ``plugin`` that assembles here — the shape
    an admin's connection has once it is set up and this member holds it."""
    base: dict[str, Any] = {
        "plugin": plugin,
        "handle": handle,
        "shared_values": dict(LIVE_SHARED_VALUES.get(plugin, {})),
        "auth_method": "password" if plugin in {"postgres", "mysql"} else "",
        "shared_custody": "lease",
    }
    base.update(over)
    return rec(record_id, **base)


class LeasingBackend:
    """The backend brokering a shared credential, behind the real client. Every
    lease round-trip is recorded, so a dropped cache is visible as a fresh call."""

    def __init__(self) -> None:
        self.leased: list[str] = []

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.leased.append(request.url.path.rsplit("/", 2)[-2])
        return httpx.Response(200, json={"secret": "leased-s3cret"})

    def client(self, *_a: object, **_k: object) -> TeamConnectionsClient:
        return TeamConnectionsClient(
            api_url="https://api.alkera.test",
            token="tok-ada",
            transport=httpx.MockTransport(self._handle),
        )


__all__ = [
    "LIVE_SHARED_VALUES",
    "SHARED_VALUES",
    "LeasingBackend",
    "accepted",
    "live_record",
    "member_credential_rec",
    "rec",
]
