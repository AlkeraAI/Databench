"""The node bundle route, through the real app and real Postgres: a node with
a live machine credential gets the bundle and its digest, anything else is a
401 before the deployment says whether it holds one, and the open build renders
bootstraps with the source its composition names."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from alkera_core.auth.machine_token import MACHINE_CREDENTIAL_HEADER
from alkera_core.config import settings
from backend.services.credentials import machine_credentials as machine_credential_service
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import make_machine_type
from tests.conftest import OrgWithAdmin, app_client

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

ROUTE = "/api/v1/machines/node-bundle"
BODY = b"a node bundle built from source"


@pytest.fixture
def bundle_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "alkera-linux-x64.tar.gz").write_bytes(BODY)
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "version": "2.0.0+abc",
                "targets": {
                    "linux-x64": {
                        "file": "alkera-linux-x64.tar.gz",
                        "sha256": hashlib.sha256(BODY).hexdigest(),
                        "size": len(BODY),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "node_bundle_dir", str(tmp_path))
    return tmp_path


async def _credential(session: AsyncSession, org: OrgWithAdmin) -> tuple[str, object]:
    machine_type = await make_machine_type(session, provider="ssh", compute_class="cpu")
    row, raw = await machine_credential_service.mint(
        session,
        org_id=org.org_id,
        created_by=org.admin_id,
        machine_type=machine_type,
        tenancy="dedicated",
        label="node",
    )
    await session.commit()
    return raw, row.id


def _client() -> AsyncClient:
    """The app as a node reaches it: no session, no org named."""
    return app_client(names_org=False)


async def test_a_node_gets_the_bundle_and_its_digest(
    real_session: AsyncSession, org_admin: OrgWithAdmin, bundle_dir: Path
) -> None:
    raw, _ = await _credential(real_session, org_admin)
    async with _client() as client:
        digest = await client.get(
            f"{ROUTE}/linux-x64.sha256", headers={MACHINE_CREDENTIAL_HEADER: raw}
        )
        bundle = await client.get(f"{ROUTE}/linux-x64", headers={MACHINE_CREDENTIAL_HEADER: raw})
    assert digest.status_code == 200, digest.text
    assert digest.text == f"{hashlib.sha256(BODY).hexdigest()}  alkera-linux-x64.tar.gz\n"
    assert bundle.status_code == 200
    assert bundle.content == BODY
    assert bundle.headers["x-alkera-bundle-version"] == "2.0.0+abc"


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no-credential"),
        pytest.param({MACHINE_CREDENTIAL_HEADER: "alkm_not-a-real-one"}, id="unknown-credential"),
        pytest.param({"Authorization": "Bearer something"}, id="a-person-not-a-node"),
    ],
)
async def test_anything_but_a_live_node_credential_is_401(
    bundle_dir: Path, headers: dict[str, str]
) -> None:
    async with _client() as client:
        resp = await client.get(f"{ROUTE}/linux-x64", headers=headers)
    assert resp.status_code == 401
    assert BODY not in resp.content


async def test_a_revoked_credential_is_401(
    real_session: AsyncSession, org_admin: OrgWithAdmin, bundle_dir: Path
) -> None:
    raw, credential_id = await _credential(real_session, org_admin)
    await machine_credential_service.revoke(real_session, credential_id)  # type: ignore[arg-type]
    await real_session.commit()
    async with _client() as client:
        resp = await client.get(f"{ROUTE}/linux-x64", headers={MACHINE_CREDENTIAL_HEADER: raw})
    assert resp.status_code == 401


@pytest.mark.parametrize(
    ("target", "configured"),
    [
        pytest.param("linux-arm64", True, id="target-not-built"),
        pytest.param("windows-x64", True, id="not-a-target"),
        pytest.param("linux-x64", False, id="no-bundle-dir"),
    ],
)
async def test_a_bundle_the_deployment_does_not_hold_is_404_for_a_node(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    bundle_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    configured: bool,
) -> None:
    raw, _ = await _credential(real_session, org_admin)
    if not configured:
        monkeypatch.setattr(settings, "node_bundle_dir", "")
    async with _client() as client:
        resp = await client.get(f"{ROUTE}/{target}", headers={MACHINE_CREDENTIAL_HEADER: raw})
    assert resp.status_code == 404


def test_the_open_composition_renders_with_the_served_daemon_source() -> None:
    probe = (
        "from backend.open_product import install; install()\n"
        "from alkera_core.compute.daemon_source import daemon_source\n"
        "print(daemon_source().name)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False, timeout=120
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip().splitlines()[-1] == "served"
