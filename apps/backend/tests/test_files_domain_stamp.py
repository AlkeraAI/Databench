"""Provisioning an org must not wait on the object store.

An org's first Files touch — the first team it makes, the first member it
admits, the first chat it warms — makes its dedup domain, and making a domain
stamps that domain's prefix in the object store with ``meta/owner.json``.
Nothing in the request reads the marker; only the ``files.gc`` collector does.
So a store that is unreachable, or reachable and mute, must cost the request a
deadline and not a connect-and-retry ladder.

Two shapes, because they fail differently: a closed port answers at once with a
refusal, and a socket that accepts and never replies answers never — with no
deadline that request does not come back at all.

What is left owed is recorded on the domain row, and the janitor's
``owner_markers`` sweeper is what settles it once the store is back.
"""

from __future__ import annotations

import asyncio
import socket
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind
from alkera_core.authz.principal import ActingContext
from alkera_core.config import Settings
from alkera_core.config import settings as process_settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files.clock import Clock, SystemClock
from alkera_core.files.ids import OrgScope
from alkera_core.files.ownership import read_owner, store_identity
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.scoped import FilesystemScoped, ScopedStoreFactory
from alkera_core.files.sweepers import OwnerMarkers, SweepDeps
from backend.services.files import store as store_module
from backend.services.files.context import build_files_context
from backend.services.files.store import build_store_factory, set_store_factory
from httpx import AsyncClient
from sqlalchemy import text
from tests.conftest import OrgWithAdmin, login
from worker.tasks.files import stamp_domain_marker

pytestmark = pytest.mark.asyncio

#: Generous on purpose. The deadline these cases set is a quarter of a second
#: and one org makes one domain, so anything near this bound is the driver's
#: own ladder coming back — and with no deadline at all the black-hole case
#: never finishes, which is the failure this bound is really watching for.
BOUNDED_SECONDS = 15.0


@pytest.fixture(autouse=True)
def store_is_rebuilt_per_case() -> Iterator[None]:
    """The factory is process-global and built from settings on first use, so
    a case that repoints the settings must drop it on both sides."""
    set_store_factory(None)
    yield
    set_store_factory(None)


@pytest.fixture
def refused_endpoint() -> Iterator[str]:
    """A port nothing is listening on: every connect is refused at once."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    yield f"http://127.0.0.1:{port}"


@pytest.fixture
def black_hole_endpoint() -> Iterator[str]:
    """A socket that accepts the connection and never answers a byte.

    The worse of the two: a refusal at least comes back. This is the shape a
    wedged store, a dropped route or a saturated load balancer presents, and
    the only thing that ever ends the wait is the caller's own deadline.
    """
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(64)
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        listener.close()


def _point_at_s3(monkeypatch: pytest.MonkeyPatch, endpoint: str, *, deadline: float) -> None:
    monkeypatch.setattr(process_settings, "files_store_provider", "s3_compatible")
    monkeypatch.setattr(process_settings, "files_store_endpoint", endpoint)
    monkeypatch.setattr(process_settings, "files_store_bucket", "alkera-stamp-case")
    monkeypatch.setattr(process_settings, "files_store_stamp_timeout_seconds", deadline)


@dataclass(frozen=True)
class StoreUnderTest:
    """One deployment's store: the settings that name it, and the factory the
    request path and the repair both vend their handles from."""

    config: Settings
    factory: ScopedStoreFactory


def _filesystem_store(root: Path) -> StoreUnderTest:
    """A deployment serving Files from a directory — what a self-hosted install
    runs, and what every CI job runs, since none of them stands a bucket up."""
    root.mkdir(parents=True, exist_ok=True)
    config = process_settings.model_copy(
        update={
            "files_enabled": True,
            "files_store_provider": "filesystem",
            "files_store_root": root,
            "files_store_endpoint": None,
            "files_store_bucket": None,
        }
    )
    return StoreUnderTest(config=config, factory=build_store_factory(config, clock=SystemClock()))


def _s3_store() -> StoreUnderTest:
    """The endpoint this session is configured against, when one is listening.

    Skipped rather than faked: a session whose store is absent has already been
    redirected onto the filesystem driver by the root conftest, so running this
    leg would prove the same driver twice under a name that said otherwise.
    """
    if process_settings.files_store_provider == "filesystem":
        pytest.skip("this session runs the filesystem driver: no S3 endpoint to prove")
    return StoreUnderTest(
        config=process_settings.model_copy(update={"files_enabled": True}),
        factory=build_store_factory(process_settings, clock=SystemClock()),
    )


@pytest.fixture(params=["filesystem", "s3"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> StoreUnderTest:
    """Every provider a deployment may actually run.

    Neither leg covers the other: CI has no object store, so only the
    filesystem one runs there — and that is exactly the driver whose first
    write to a new domain was broken, which is how a marker that worked against
    SeaweedFS all along still stamped nothing on a Linux runner.
    """
    if request.param == "filesystem":
        return _filesystem_store(tmp_path / "bucket")
    return _s3_store()


def _point_at(monkeypatch: pytest.MonkeyPatch, store: StoreUnderTest) -> None:
    """Make the request path serve ``store``."""
    for name in (
        "files_store_provider",
        "files_store_endpoint",
        "files_store_bucket",
        "files_store_root",
    ):
        monkeypatch.setattr(process_settings, name, getattr(store.config, name))


def _point_at_directory(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(process_settings, "files_store_provider", "filesystem")
    monkeypatch.setattr(process_settings, "files_store_endpoint", None)
    monkeypatch.setattr(process_settings, "files_store_bucket", None)
    monkeypatch.setattr(process_settings, "files_store_root", root)


def _settings_for(root: Path) -> Settings:
    return process_settings.model_copy(
        update={
            "files_enabled": True,
            "files_store_provider": "filesystem",
            "files_store_root": root,
            "files_store_endpoint": None,
            "files_store_bucket": None,
        }
    )


def _service_ctx(org_team_id: uuid.UUID) -> ActingContext:
    return ActingContext.for_service(
        token_id=org_team_id,
        org_id=org_team_id,
        label="stamp_case",
        credential=CredentialKind.CI_TOKEN,
    )


async def _provision(client: AsyncClient, admin: OrgWithAdmin) -> float:
    """Make the org's first team — the request that makes its dedup domain and
    stamps the prefix. Returns how long it took."""
    await login(client, admin.admin_email, admin.admin_password)
    started = time.perf_counter()
    resp = await client.post("/api/v1/teams", json={"name": f"Team {uuid.uuid4().hex[:6]}"})
    elapsed = time.perf_counter() - started
    assert resp.status_code == 201, resp.text
    return elapsed


async def _domain_of(org_team_id: uuid.UUID) -> tuple[uuid.UUID, object]:
    """The org's dedup domain and what its row says about the marker."""
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT id, owner_marked_at FROM dedup_domains WHERE org_team_id = :org"),
                {"org": org_team_id},
            )
        ).one()
    return uuid.UUID(str(row[0])), row[1]


async def _repair(org_team_id: uuid.UUID, store: StoreUnderTest) -> int:
    """Run the janitor's marker sweeper for one org against a real store."""
    factory = store.factory
    identity = store_identity(store.config)
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO file_stores (id, driver, bucket, endpoint, region, "
                "capabilities, transfer_modes) "
                "VALUES (:id, :driver, :bucket, :endpoint, :region, '{}'::jsonb, "
                "ARRAY['single']) ON CONFLICT DO NOTHING"
            ),
            {"id": uuid.uuid4(), **identity},
        )
        await session.commit()
    config = store.config
    async with AsyncSessionLocal() as session:
        deps = SweepDeps(
            repo=FilesRepo(session, OrgScope(org_team_id=org_team_id)),
            ctx=_service_ctx(org_team_id),
            stamp_domain=lambda domain, created_at: stamp_domain_marker(
                session, factory, domain, created_at=created_at, config=config
            ),
        )
        outcome = await OwnerMarkers(deps).run(SystemClock().now())
        await session.commit()
    return outcome.swept


@pytest.mark.parametrize(
    "endpoint_fixture",
    [
        pytest.param("refused_endpoint", id="the store refuses the connection"),
        pytest.param("black_hole_endpoint", id="the store accepts and never answers"),
    ],
)
async def test_provisioning_an_org_is_bounded_when_the_store_will_not_answer(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    endpoint_fixture: str,
) -> None:
    """The bug this exists for. The request has no use for the marker, so an
    object store that will not answer costs the deadline and no more — and what
    it could not say is recorded as owed rather than forgotten."""
    endpoint: str = request.getfixturevalue(endpoint_fixture)
    _point_at_s3(monkeypatch, endpoint, deadline=0.25)

    elapsed = await _provision(client, org_admin)

    assert elapsed < BOUNDED_SECONDS, f"the request took {elapsed:.1f}s against {endpoint}"
    _, marked_at = await _domain_of(org_admin.org_id)
    assert marked_at is None


async def test_a_domain_the_store_would_not_take_is_stamped_once_it_is_back(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    refused_endpoint: str,
    store: StoreUnderTest,
) -> None:
    """The org is whole the moment the store is: what the refused stamp left
    behind is a row saying the statement is owed, and the sweeper settles it
    against the real store without the org doing anything — on whichever
    provider this deployment actually serves Files from."""
    _point_at_s3(monkeypatch, refused_endpoint, deadline=0.25)
    await _provision(client, org_admin)
    domain_id, marked_at = await _domain_of(org_admin.org_id)
    assert marked_at is None

    swept = await _repair(org_admin.org_id, store)

    assert swept == 1
    _, settled = await _domain_of(org_admin.org_id)
    assert settled is not None
    assert await read_owner(store.factory.admin(), str(domain_id)) is not None


async def test_a_store_that_answers_is_stamped_at_creation_and_only_once(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    store: StoreUnderTest,
) -> None:
    """The ordinary case must not quietly become "the sweeper does it later": a
    reachable store is stamped by the request that makes the domain, the row
    records it, and the marker is written exactly once — a later Files call
    over the same drive leaves the marker it finds standing, because the
    marker's date is evidence about the bytes underneath it."""
    _point_at(monkeypatch, store)

    await _provision(client, org_admin)

    domain_id, marked_at = await _domain_of(org_admin.org_id)
    assert marked_at is not None
    admin_store = store.factory.admin()
    first = await read_owner(admin_store, str(domain_id))
    assert first is not None
    async with AsyncSessionLocal() as session:
        await build_files_context(session, _service_ctx(org_admin.org_id))
        await session.commit()
    assert await read_owner(admin_store, str(domain_id)) == first
    _, still = await _domain_of(org_admin.org_id)
    assert still == marked_at


async def test_the_store_client_is_built_before_the_deadline_starts(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The deadline is for the STORE, not for constructing the thing that talks
    to it. On the first Files call of a process the driver is built and its
    credentials resolved — on AWS that reaches IMDS — and a build inside the
    window would spend the budget before a byte was sent, leaving the first
    domain of every process owed for a reason that has nothing to do with
    whether the store is reachable.

    The build is made slower than the whole deadline and the store is made to
    spend most of what the deadline allows. Both together fit only if the build
    is outside it.
    """
    root = tmp_path / "slow-to-build"
    root.mkdir()
    _point_at_directory(monkeypatch, root)
    monkeypatch.setattr(process_settings, "files_store_stamp_timeout_seconds", 0.4)
    real_build = store_module.build_store_factory

    class Dawdling:
        """A store that takes most of the deadline to answer — as a real one
        does — so what is left of the budget is what decides this case."""

        def __init__(self, inner: Any) -> None:
            self._inner = inner

        async def for_domain(self, domain_id: Any) -> Any:
            await asyncio.sleep(0.3)
            return await self._inner.for_domain(domain_id)

        def admin(self) -> Any:
            return self._inner.admin()

    def slow_build(settings: Settings, *, clock: Clock) -> Any:
        time.sleep(0.6)
        return Dawdling(real_build(settings, clock=clock))

    monkeypatch.setattr(store_module, "build_store_factory", slow_build)

    await _provision(client, org_admin)

    domain_id, marked_at = await _domain_of(org_admin.org_id)
    assert marked_at is not None
    admin_store = FilesystemScoped(root, clock=SystemClock()).admin()
    assert await read_owner(admin_store, str(domain_id)) is not None


async def test_a_settled_domain_is_never_restamped_by_the_repair(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    store: StoreUnderTest,
) -> None:
    """The asymmetric half of the case above. Having recorded the stamp at
    creation, the sweeper must find nothing to do — a pass that re-asked the
    store about every domain it already settled would put one object read per
    org back on every five-minute tick, forever."""
    _point_at(monkeypatch, store)
    await _provision(client, org_admin)

    swept = await _repair(org_admin.org_id, store)

    assert swept == 0
