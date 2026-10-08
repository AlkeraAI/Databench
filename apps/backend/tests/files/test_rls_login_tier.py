"""The guard of the tier that runs tenant isolation as a non-superuser login.

``make test-rls-login`` runs the two-org fuzz, the Files RLS suite and the boot
canary with the app engine connected as a login made the way a deployment
makes its runtime login (``ALKERA_TEST_APP_LOGIN``; see the root conftest). A
tier that silently fell back to the superuser would prove nothing, so this
module fails it outright unless the engine really is that login. Outside the
tier it is skipped.
"""

from __future__ import annotations

import os

import pytest
from alkera_core.db.row_security import judge, take_snapshot, tenant_tables
from alkera_core.db.session import AsyncSessionLocal
from sqlalchemy import text

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("ALKERA_TEST_APP_LOGIN"),
        reason="runs in the non-superuser tier only (make test-rls-login)",
    ),
]


async def test_the_app_engine_is_the_non_superuser_login() -> None:
    async with AsyncSessionLocal() as db:
        who, superuser = (
            await db.execute(
                text("SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user")
            )
        ).one()
    assert who == os.environ["ALKERA_TEST_APP_LOGIN"]
    assert superuser is False


async def test_the_canary_passes_under_the_deployment_shaped_login() -> None:
    expected = tenant_tables()
    async with AsyncSessionLocal() as db:
        snapshot = await take_snapshot(db, expected=expected)
        await db.rollback()
    assert snapshot.login.superuser is False
    assert judge(snapshot, expected=expected) == []
