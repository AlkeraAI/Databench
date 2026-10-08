"""The boot canary for tenant isolation, against the real database and route.

The judge is pinned fact by fact in ``packages/api-core/tests/test_row_security_judge.py``;
here the snapshot is read from a real Postgres (and a real policy is switched
off inside a transaction that is rolled back), and ``/health/ready`` is driven
with the canary on and off.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from alkera_core.config import settings
from alkera_core.db.row_security import judge, take_snapshot, tenant_tables
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.readiness import probe_latch
from backend.services.infra import row_security_canary
from httpx import AsyncClient
from sqlalchemy import text

pytestmark = [pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def _fresh_canary() -> Iterator[None]:
    row_security_canary.reset()
    yield
    row_security_canary.reset()


async def test_this_database_passes_the_canary() -> None:
    """The registry and the migrations agree: every tenant table the ORM
    names is enabled, forced and policed here, the tenant role is neither
    superuser nor BYPASSRLS, and the login may assume it."""
    expected = tenant_tables()
    async with AsyncSessionLocal() as db:
        snapshot = await take_snapshot(db, expected=expected)
        await db.rollback()
    assert judge(snapshot, expected=expected) == []
    assert snapshot.probe_saw_rows is False
    assert {t.name for t in snapshot.tables} == expected


@pytest.mark.skipif(
    bool(os.environ.get("ALKERA_TEST_APP_LOGIN")),
    reason="altering a table takes its owner; the non-superuser tier's login is not",
)
async def test_a_table_that_stops_forcing_row_security_is_named(fx: Any) -> None:
    await fx.drive()
    await fx._session.commit()
    expected = tenant_tables()
    async with AsyncSessionLocal() as db:
        await db.execute(text("ALTER TABLE file_nodes NO FORCE ROW LEVEL SECURITY"))
        await db.execute(text("ALTER TABLE file_drives DISABLE ROW LEVEL SECURITY"))
        snapshot = await take_snapshot(db, expected=expected)
        await db.rollback()
    problems = judge(snapshot, expected=expected)
    assert "tenant table file_nodes does not force row-level security" in problems
    assert "tenant table file_drives does not enable row-level security" in problems
    # The behavioural probe, not only the catalog: with the policy off, the
    # tenant role with no org set reads a drive that exists.
    assert snapshot.probe_saw_rows is True
    assert "role alkera_files_app read file_drives rows with no org set" in problems


async def test_readiness_serves_when_the_canary_passes(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "row_security_canary", True)
    resp = await client.get("/health/ready")
    assert resp.status_code == 200, resp.text


async def test_readiness_refuses_with_a_named_reason_until_the_canary_passes(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tenant table without its policy takes the task out of rotation with
    a reason an orchestrator's log can name, and nothing in the public body
    says which table. A failure is not remembered: once the fact is fixed,
    the next probe serves."""
    monkeypatch.setattr(settings, "row_security_canary", True)
    # A table the registry says must be policed but the database never did.
    monkeypatch.setattr(
        row_security_canary, "tenant_tables", lambda: tenant_tables() | {"file_stores"}
    )
    for _ in range(2):
        refused = await client.get("/health/ready")
        assert refused.status_code == 503, refused.text
        assert refused.json()["detail"] == "row-level security canary failed"
        assert "file_stores" not in refused.text

    monkeypatch.setattr(row_security_canary, "tenant_tables", tenant_tables)
    assert (await client.get("/health/ready")).status_code == 200


async def test_a_failed_canary_is_never_graced_as_a_dependency_blip(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The readiness grace is for a dependency that could not be asked. A canary
    that answered and named a problem is a fact about this task's database
    role, so a task that was ready before still leaves rotation at once."""
    monkeypatch.setattr(settings, "row_security_canary", True)
    monkeypatch.setattr(settings, "health_ready_grace_seconds", 900.0)
    probe_latch.ready()
    monkeypatch.setattr(
        row_security_canary, "tenant_tables", lambda: tenant_tables() | {"file_stores"}
    )
    refused = await client.get("/health/ready")
    assert refused.status_code == 503, refused.text
    assert refused.json()["detail"] == "row-level security canary failed"


async def test_a_passed_canary_is_not_asked_again(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "row_security_canary", True)
    assert (await client.get("/health/ready")).status_code == 200
    monkeypatch.setattr(
        row_security_canary, "tenant_tables", lambda: tenant_tables() | {"file_stores"}
    )
    assert (await client.get("/health/ready")).status_code == 200


async def test_the_canary_is_off_where_the_deployment_says(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "row_security_canary", False)
    monkeypatch.setattr(
        row_security_canary, "tenant_tables", lambda: tenant_tables() | {"file_stores"}
    )
    assert (await client.get("/health/ready")).status_code == 200


@pytest.mark.parametrize(
    ("app_env", "override", "enabled"),
    [
        pytest.param("production", None, True, id="production"),
        pytest.param("staging", None, True, id="staging"),
        pytest.param("local", None, False, id="local"),
        pytest.param("local", True, True, id="local-opted-in"),
        pytest.param("production", False, False, id="production-opted-out"),
    ],
)
def test_the_canary_is_on_by_default_where_tenants_are_served(
    app_env: str, override: bool | None, enabled: bool
) -> None:
    configured = settings.model_copy(update={"app_env": app_env, "row_security_canary": override})
    assert configured.row_security_canary_enabled is enabled
