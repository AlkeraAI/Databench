"""Two admins saving the org's SSO connection for the first time, at once,
or minting its first SCIM token: both are taken, one after the other, and one
row is left (:mod:`tests._first_save_race`)."""

from __future__ import annotations

from typing import Any

import pytest
from alkera_core.models import SsoConnection
from backend.services.identity import sso as sso_service
from sqlalchemy.ext.asyncio import AsyncSession
from tests._first_save_race import Save, refused_saves, rows_for
from tests.conftest import OrgWithAdmin

pytestmark = pytest.mark.asyncio


async def _sso(db: AsyncSession, org: OrgWithAdmin, n: int) -> Any:
    return await sso_service.upsert_oidc_connection(
        db,
        org_team_id=org.org_id,
        issuer=f"https://idp-{n}.example.test",
        client_id=f"client-{n}",
        client_secret="secret",
        enabled=False,
    )


async def _scim(db: AsyncSession, org: OrgWithAdmin, n: int) -> Any:
    return await sso_service.mint_scim_token(db, org.org_id)


@pytest.mark.parametrize(
    "save",
    [pytest.param(_sso, id="sso-connection"), pytest.param(_scim, id="scim-token")],
)
async def test_two_first_saves_at_once_are_both_taken(org_admin: OrgWithAdmin, save: Save) -> None:
    assert await refused_saves(save, org_admin) == []
    assert await rows_for(SsoConnection, SsoConnection.org_team_id, org_admin) == 1
