"""A connection saved through the open platform's routes, as the web form saves it.

The open platform keeps team and personal connections, their encrypted secret and
their leases; the product adds a server-side check before a save and browser
sign-in. Here the suite's app runs with the product's check and sign-in swapped
for the open defaults, through the real routes the form calls: the catalog says
nothing is checked, the save stores the row in one request, the secret is
encrypted at rest and leased back intact, and a private-range host is refused
unless the reach policy admits private ranges.
"""

from __future__ import annotations

import uuid

import pytest
from alkera_core.auth.secret_box import decrypt_secret
from alkera_core.connections.models import TeamConnection
from alkera_core.connectors import reach
from alkera_core.db.session import AsyncSessionLocal
from backend.api.routes.connections import team_connections as connections_route
from backend.services.connections import ports
from backend.services.connections import team_connections as team_connection_service
from httpx import AsyncClient
from sqlalchemy import select
from tests.conftest import OrgWithAdmin, login

pytestmark = pytest.mark.asyncio

SECRET = "open-generic-sql-pw-7c1"
URL = "postgresql+psycopg://svc@db.example.com:5432/analytics"


@pytest.fixture(autouse=True)
def open_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """The open platform's own answers: no pre-save check, no browser sign-in."""
    monkeypatch.setattr(connections_route, "connection_checks", ports.NoConnectionChecks)
    monkeypatch.setattr(team_connection_service, "connection_oauth", ports.NoConnectionOAuth)


def _generic(handle: str, url: str = URL) -> dict[str, object]:
    fields = {"url": url, "password": SECRET, "environment": "prod"}
    return {
        "plugin": "generic_sql",
        "handle": handle,
        "fields": fields,
        "shared_fields": list(fields),
        "auth_method": "",
    }


async def _row(handle: str) -> TeamConnection:
    async with AsyncSessionLocal() as db:
        found = (
            await db.execute(select(TeamConnection).where(TeamConnection.handle == handle))
        ).scalar_one()
        return found


async def test_the_catalog_says_a_save_is_not_checked(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    forms = await client.get("/api/v1/plugins/connection-forms")
    assert forms.status_code == 200, forms.text
    assert forms.json()["checked_before_save"] is False
    assert "generic_sql" in [c["name"] for c in forms.json()["connectors"]]


@pytest.mark.parametrize("collection", ["team", "personal"])
async def test_a_generic_connection_saves_in_one_request_with_its_secret_encrypted(
    client: AsyncClient, org_admin: OrgWithAdmin, collection: str
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    handle = f"wh_{collection}_{uuid.uuid4().hex[:6]}"
    target = (
        f"/api/v1/teams/{org_admin.org_id}/connections"
        if collection == "team"
        else "/api/v1/me/connections"
    )

    saved = await client.put(target, json=_generic(handle))

    assert saved.status_code == 200, saved.text
    assert SECRET not in saved.text
    assert saved.json()["plugin"] == "generic_sql"
    listed = await client.get(target)
    assert handle in [row["handle"] for row in listed.json()]
    row = await _row(handle)
    assert row.shared_secret_encrypted
    assert SECRET not in row.shared_secret_encrypted
    assert decrypt_secret(row.shared_secret_encrypted) == SECRET
    # The password left the stored URL for the encrypted column.
    assert SECRET not in str(row.shared_values)


async def test_a_member_leases_the_saved_secret_back(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    handle = f"wh_lease_{uuid.uuid4().hex[:6]}"
    saved = await client.put(f"/api/v1/teams/{org_admin.org_id}/connections", json=_generic(handle))
    assert saved.status_code == 200, saved.text

    lease = await client.post(f"/api/v1/me/team-connections/{saved.json()['id']}/credential-lease")

    assert lease.status_code == 200, lease.text
    assert lease.json()["secret"] == SECRET


async def test_a_private_range_host_is_refused_by_the_open_reach_policy(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reach, "reach_policy", lambda: reach.OPEN_REACH_POLICY)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    target = f"/api/v1/teams/{org_admin.org_id}/connections"
    url = "postgresql+psycopg://svc@10.20.0.5:5432/analytics"

    refused = await client.put(target, json=_generic("private_wh", url))

    assert refused.status_code == 422, refused.text
    assert "EGRESS_PRIVATE_ALLOWLIST" in refused.text
    assert "private_wh" not in (await client.get(target)).text


async def test_the_product_reach_policy_admits_the_same_private_host(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        reach, "reach_policy", lambda: reach.ConnectionReachPolicy(admits_private_ranges=True)
    )
    await login(client, org_admin.admin_email, org_admin.admin_password)
    url = "postgresql+psycopg://svc@10.20.0.5:5432/analytics"

    saved = await client.put(
        f"/api/v1/teams/{org_admin.org_id}/connections", json=_generic("vpc_wh", url)
    )

    assert saved.status_code == 200, saved.text
