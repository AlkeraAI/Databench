"""An open install serves the org's own model keys with no license token.

Runs in a fresh interpreter with only the open backend's extensions installed
(``backend.open_product``): the suite's own app installed the product, whose
signed license gates the surface, and a point freezes once read. Nothing
licenses an open install, so every feature is its operator's: an org admin
lists the org's model providers with no ``ALKERA_ENTITLEMENTS`` set.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.xdist_group("open_model_providers")]

PROBE = """
import asyncio, json, secrets
from datetime import UTC, datetime

from backend import open_product

open_product.install()

from alkera_core.auth import encode_cli_token, register_token
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.entitlements import Feature, entitled_feature_names, has_feature
from alkera_core.extensions import installed_extensions
from alkera_core.models import TeamRole, TokenType, User
from backend.app_factory import create_app
from backend.services.org import memberships, teams
from httpx import ASGITransport, AsyncClient

settings.alkera_entitlements = None
settings.self_hosted = True


async def main():
    async with AsyncSessionLocal() as db:
        org = await teams.create_org_rows(db, org_name=f"Open keys {secrets.token_hex(4)}")
        user = User(home_org_team_id=org.id, email=f"k-{secrets.token_hex(6)}@alkera.dev",
                    first_name="K", last_name="Admin", email_verified_at=datetime.now(UTC))
        db.add(user)
        await db.flush()
        await memberships.add_member(db, team_id=org.id, user_id=user.id, role=TeamRole.ADMIN)
        token, claims = encode_cli_token(user_id=user.id, email=user.email,
                                         org_team_id=org.id, platform_role=None)
        await register_token(db, claims=claims, token_type=TokenType.CLI)
        await db.commit()

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://api") as c:
        listed = await c.get("/api/v1/org/model-providers",
                             headers={"Authorization": f"Bearer {token}"})
    body = listed.json() if listed.status_code == 200 else {}
    print("REPORT" + json.dumps({
        "installed": list(installed_extensions()),
        "byok": has_feature(Feature.BYOK),
        "features": entitled_feature_names(),
        "status": listed.status_code,
        "providers": sorted(p["provider"] for p in body.get("providers", [])),
    }))


asyncio.run(main())
"""


@pytest.fixture(scope="module")
def report(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    tmp = tmp_path_factory.mktemp("open_model_providers")
    env = {**os.environ, "ALKERA_HOME": str(tmp / "home"), "NO_COLOR": "1"}
    env.pop("ALKERA_ENTITLEMENTS", None)
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env=env,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert result.returncode == 0, result.stderr[-4000:]
    line = next(r for r in result.stdout.splitlines() if r.startswith("REPORT"))
    parsed: dict[str, Any] = json.loads(line.removeprefix("REPORT"))
    return parsed


@pytest.mark.timeout(300)
def test_an_open_install_has_every_feature_without_a_license(report: dict[str, Any]) -> None:
    assert "alkera.signed_license" not in report["installed"]
    assert report["byok"] is True
    assert report["features"] == ["byok"]


@pytest.mark.timeout(300)
def test_an_org_admin_lists_the_org_model_keys_on_an_open_install(
    report: dict[str, Any],
) -> None:
    assert report["status"] == 200
    assert report["providers"] == ["anthropic", "bedrock", "openai"]
