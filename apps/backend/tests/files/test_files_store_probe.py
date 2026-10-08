"""The object-store probe in readiness, and the gauge it drives.

The contract under test is the degradation split: when the object store stops
answering, this instance leaves rotation so no new content transfer is routed to
it, while everything that never touches the store — the metadata surface and
inline bytes, both of which live in Postgres — is unaffected by the probe.

The outage is never mocked: a real filesystem store is wrapped in ``FaultyStore``
with a scheduled ``unavailable`` fault, installed through the same
``set_store_factory`` seam production fills from settings, so the code path is
production's. The healthy leg injects its answer at the same seam rather than
reading whatever store the machine happens to be running, so what these tests
say about readiness holds on a laptop beside a live store and on a CI runner
with none.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alkera_core import deployment_health as dh
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.deployment_health import (
    FILES_STORE_PROBE_KEY,
    CheckContext,
    _check_files_store,
    probe_files_store,
)
from alkera_core.files.clock import SystemClock
from alkera_core.files.store.keys import DomainId
from alkera_core.files.store.protocol import ObjectStore
from alkera_core.files.store.scoped import DomainStore, FilesystemScoped
from alkera_core.models import DeploymentHealthCheck, DeploymentHealthRun
from alkera_core.observability.metrics import _FILES_STORE_AVAILABLE
from alkera_core.readiness import probe_latch
from alkera_test_support.files.faulty_store import Fault, FaultSchedule, FaultyStore
from backend.services.files.store import set_store_factory
from backend.services.ops import ops_summary
from httpx import AsyncClient
from sqlalchemy import delete
from tests.conftest import OrgWithAdmin, login

# The async cases below carry no `anyio` marker on purpose. The suite runs
# `asyncio_mode = "auto"` with a SESSION-scoped loop, and the `client` fixture —
# plus the autouse async fixtures every backend test gets — open asyncpg
# connections on that loop. `pytest.mark.anyio` instead runs the test body on a
# fresh loop, so the pooled connection the readiness probe checks out belongs to
# another loop: `/health/ready` answers "database unreachable" and teardown dies
# terminating the connection. Auto mode puts body and fixtures on the one loop.


class _FaultyAdminScoped:
    """A scoped factory whose bucket-wide handle carries a fault schedule.

    Domain handles stay untouched: a store outage the *probe* sees must not be
    simulated by breaking the handles the request path uses, or the test would
    prove nothing about the split.
    """

    def __init__(self, inner: FilesystemScoped, schedule: FaultSchedule) -> None:
        self._inner = inner
        self._admin = FaultyStore(inner.admin(), schedule)

    async def for_domain(self, domain_id: DomainId) -> DomainStore:
        return await self._inner.for_domain(domain_id)

    def admin(self) -> ObjectStore:
        return self._admin


@pytest.fixture
def unavailable_store(files_on: None, files_store: Path) -> Iterator[FaultyStore]:
    """Files enabled, with a store whose every admin call answers "unavailable"."""
    factory = _FaultyAdminScoped(
        FilesystemScoped(files_store, clock=SystemClock()),
        FaultSchedule([Fault(kind="unavailable", count=1000)]),
    )
    set_store_factory(factory)
    yield factory.admin()  # type: ignore[return-value]
    set_store_factory(None)


class _AnsweringAdmin:
    """A bucket-wide handle that answers the probe's HEAD with an absence.

    The probe key names an object no store ever writes, so "answered, and there
    is nothing under that key" is the healthy reply — the one every driver gives
    on a fresh bucket. It is spelled here rather than borrowed from the
    filesystem driver because that driver reaches the probe key through its
    containment walk, and a store root that has never had a byte written under
    the probe's domain answers that walk differently per platform: an absence
    where the walk is done with lstat, an OSError where the kernel opens the
    directory itself. These two tests own readiness's branch on the probe's
    OUTCOME, so the outcome is the input here, not something inherited from a
    driver they are not testing.
    """

    def __init__(self) -> None:
        self.keys: list[str] = []

    async def head(self, key: str) -> None:
        self.keys.append(key)
        return None


class _AnsweringAdminScoped:
    """The twin of :class:`_FaultyAdminScoped`: a real store for the request
    path, an admin handle whose answer the probe is being read against."""

    def __init__(self, inner: FilesystemScoped) -> None:
        self._inner = inner
        self._admin = _AnsweringAdmin()

    async def for_domain(self, domain_id: DomainId) -> DomainStore:
        return await self._inner.for_domain(domain_id)

    def admin(self) -> ObjectStore:
        return self._admin  # type: ignore[return-value]


@pytest.fixture
def answering_store(files_on: None, files_store: Path) -> Iterator[_AnsweringAdmin]:
    """Files enabled, with a store whose every admin call answers."""
    factory = _AnsweringAdminScoped(FilesystemScoped(files_store, clock=SystemClock()))
    set_store_factory(factory)
    yield factory.admin()  # type: ignore[return-value]
    set_store_factory(None)


def _gauge() -> float | None:
    return _FILES_STORE_AVAILABLE._value.get()


async def test_ready_is_503_naming_the_store_when_the_probe_cannot_reach_it(
    client: AsyncClient, unavailable_store: FaultyStore
) -> None:
    """Writes must fail fast, so the instance leaves rotation and says why."""
    response = await client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert "store" in body["detail"]
    # The database is not what failed, and readiness must not blame it.
    assert body["db"] == "ok"
    assert [call.method for call in unavailable_store.calls] == ["head"]


async def test_a_store_outage_after_a_ready_probe_is_graced_but_still_named(
    client: AsyncClient, unavailable_store: FaultyStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store is shared by every task, so its outage takes every task out of
    rotation at once and a replacement cannot reach it either. A task that has
    been ready stays in rotation inside the grace window; the body still names
    the store, so nothing reading it is told the task is healthy."""
    monkeypatch.setattr(settings, "health_ready_grace_seconds", 900.0)
    probe_latch.ready()

    response = await client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert "store" in body["detail"]
    assert body["db"] == "ok"


async def test_ready_is_200_and_the_gauge_is_1_when_the_store_answers(
    client: AsyncClient, answering_store: _AnsweringAdmin
) -> None:
    response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert _gauge() == 1
    # The 200 came from the probe having run and been answered, not from a
    # readiness path that skipped the store.
    assert answering_store.keys == [FILES_STORE_PROBE_KEY]


async def test_a_store_outage_drives_the_gauge_to_zero(
    client: AsyncClient, unavailable_store: FaultyStore
) -> None:
    """The gauge is the alertable signal: flat 0 for as long as the outage lasts,
    without waiting for a user's upload to fail — and back to 1 on the first
    probe the store answers, so an alert clears itself."""
    await client.get("/health/ready")
    assert _gauge() == 0

    assert await probe_files_store(_AnsweringAdmin()) is True  # the store came back
    assert _gauge() == 1


async def test_readiness_skips_the_probe_entirely_when_files_is_disabled(
    client: AsyncClient, files_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Files off is a deployment choice: readiness is exactly what it was before
    the probe existed, and no store call is made."""
    factory = _FaultyAdminScoped(
        FilesystemScoped(files_store, clock=SystemClock()),
        FaultSchedule([Fault(kind="unavailable", count=1000)]),
    )
    set_store_factory(factory)
    monkeypatch.setattr(settings, "files_enabled", False)
    try:
        response = await client.get("/health/ready")
    finally:
        set_store_factory(None)

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert factory.admin().calls == []  # type: ignore[union-attr]


async def test_the_probe_reads_a_key_no_org_can_own(
    unavailable_store: FaultyStore,
) -> None:
    """A probe that HEADed a real object would go green or red on that object's
    existence; this one only proves the store answered."""
    assert await probe_files_store(unavailable_store) is False

    assert [call.key for call in unavailable_store.calls] == [FILES_STORE_PROBE_KEY]
    assert FILES_STORE_PROBE_KEY == "domains/00000000-0000-0000-0000-000000000000/health/probe"


@pytest.mark.parametrize(
    ("enabled", "expected"),
    [
        pytest.param(True, "fail", id="enabled-and-down"),
        pytest.param(False, "skipped", id="disabled"),
    ],
)
async def test_the_deployment_health_check_reports_the_same_outage(
    unavailable_store: FaultyStore,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    expected: str,
) -> None:
    """A store outage is a deployment-health failure too, but only where Files
    is meant to be serving."""
    monkeypatch.setattr(settings, "files_enabled", enabled)
    ctx = CheckContext(
        trigger="manual",
        session_factory=None,  # type: ignore[arg-type] — this check touches no session
        http_client=None,  # type: ignore[arg-type] — nor any HTTP client
        files_admin_store=lambda: unavailable_store,
    )

    (result,) = await _check_files_store(ctx)

    assert result.key == "files_store"
    assert result.status == expected


# ---------------------------------------------------------------------------
# Who supplies the store: the caller, never alkera_core reaching into an app
# ---------------------------------------------------------------------------


@pytest.fixture
async def _no_snapshot() -> AsyncIterator[None]:
    """``run_and_persist`` full-replaces the deployment-health snapshot; leave
    none behind for the next test."""
    yield
    async with AsyncSessionLocal() as db:
        await db.execute(delete(DeploymentHealthCheck))
        await db.execute(delete(DeploymentHealthRun))
        await db.commit()


async def test_a_check_given_no_store_is_skipped_even_beside_the_backends_store(
    unavailable_store: FaultyStore,
) -> None:
    """alkera_core used to import the backend's store factory when no store was
    passed, so the probe silently depended on which apps the process had
    installed. With no store given it now reports skipped, and the backend's
    process-wide store installed beside it is never touched."""
    ctx = CheckContext(
        trigger="manual",
        session_factory=None,  # type: ignore[arg-type] — this check touches no session
        http_client=None,  # type: ignore[arg-type] — nor any HTTP client
    )

    (result,) = await _check_files_store(ctx)

    assert (result.key, result.status) == ("files_store", "skipped")
    assert unavailable_store.calls == []


@pytest.mark.usefixtures("_no_snapshot")
async def test_run_and_persist_probes_the_store_it_is_given(
    unavailable_store: FaultyStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run probes the handle its caller passes, not the process's own."""
    monkeypatch.setattr(dh, "registered_checks", lambda: (_check_files_store,))
    given = _AnsweringAdmin()

    results = await dh.run_and_persist(
        trigger="manual",
        files_admin_store=lambda: given,  # type: ignore[arg-type,return-value]
    )

    assert [(r.key, r.status) for r in results] == [("files_store", "ok")]
    assert given.keys == [FILES_STORE_PROBE_KEY]
    assert unavailable_store.calls == []


@pytest.mark.usefixtures("_no_snapshot")
async def test_the_manual_run_probes_the_backends_store(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    unavailable_store: FaultyStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The org-admin run passes the backend's own store, so an outage there is a
    failed check and not a skipped one."""
    monkeypatch.setattr(settings, "self_hosted", True)
    monkeypatch.setattr(dh, "registered_checks", lambda: (_check_files_store,))
    await login(client, org_admin.admin_email, org_admin.admin_password)

    response = await client.post("/api/v1/org/deployment-health/run")

    assert response.status_code == 200, response.text
    checks = {c["key"]: c["status"] for c in response.json()["checks"]}
    assert checks["files_store"] == "fail"
    assert [call.method for call in unavailable_store.calls] == ["head"]


async def test_the_ops_summary_probes_the_backends_store(
    unavailable_store: FaultyStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ops_summary, "HEALTH_CHECKS", (_check_files_store,))

    health = await ops_summary.health()

    assert [(c.key, c.status) for c in health.checks] == [("files_store", "fail")]
    assert [call.method for call in unavailable_store.calls] == ["head"]
