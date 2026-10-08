"""Crash-report + client-error endpoints (/api/v1/errors/...).

Covers: auth requirement, persistence + listing, the server-side redaction
pass, and the log-only client-error path.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from tests.conftest import OrgWithAdmin, login

pytestmark = pytest.mark.asyncio


async def test_submit_and_list_crash_report(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.post(
        "/api/v1/errors/reports",
        json={
            "component": "cli",
            "message": "harness crashed during turn",
            "error_type": "HarnessCrashError",
            "comment": "happened right after I hit enter",
            "app_version": "1.2.3",
            "platform": "darwin/arm64",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["component"] == "cli"
    assert body["message"] == "harness crashed during turn"
    assert body["comment"] == "happened right after I hit enter"
    assert str(body["user_id"]) == str(org_admin.admin_id)

    listing = await client.get("/api/v1/errors/reports")
    assert listing.status_code == 200
    rows = listing.json()
    assert any(r["id"] == body["id"] and r["component"] == "cli" for r in rows)


async def test_crash_report_is_scrubbed_server_side(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.post(
        "/api/v1/errors/reports",
        json={
            "component": "daemon",
            "message": "failed opening /Users/secretuser/app.py with Bearer abcdef0123456789",
            "comment": "my key sk-proj-ABCDEFGHIJKLMNOP1234",
            "context": {"password": "hunter2", "input_tokens": 5},
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # Home dir reduced, bearer token redacted in the message.
    assert "/Users/secretuser" not in body["message"]
    assert "~/app.py" in body["message"]
    assert "abcdef0123456789" not in body["message"]
    # Provider key redacted in the comment.
    assert "sk-proj-ABCDEFGHIJKLMNOP1234" not in body["comment"]
    # Sensitive context key removed; benign count preserved.
    assert body["context"]["password"] == "[redacted]"
    assert body["context"]["input_tokens"] == 5


async def test_crash_report_requires_auth(client: AsyncClient) -> None:
    """A caller with no credential is refused with the code a client keys off.

    ``unauthorized`` is the 401 vocabulary the auth dependency speaks — the one
    that hands the user back to login, as distinct from ``token_expired``,
    which is the only code that invites a silent refresh-and-retry. The web
    realtime clients branch on exactly this string, so it is the contract; the
    generic ``auth_required`` is only what the status-code fallback produces for
    a 401 nobody typed.
    """
    resp = await client.post("/api/v1/errors/reports", json={"component": "web", "message": "boom"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


async def test_report_client_error_event(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.post(
        "/api/v1/errors/events",
        json={
            "component": "web",
            "message": "Uncaught TypeError: cannot read properties of undefined",
            "error_type": "TypeError",
            "url": "https://app.example.com/dashboard",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["received"] is True
    assert body["trace_id"]


async def test_client_error_requires_auth(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/errors/events", json={"message": "boom"})
    assert resp.status_code == 401
