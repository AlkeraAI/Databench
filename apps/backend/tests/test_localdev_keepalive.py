"""The backend keeps every local developer box that should be serving, running.

Rows are real ``compute_allocations`` in Postgres; the boxes are containers in
an in-memory Docker (``FakeDocker``), so each test asserts what ``docker ps``
shows after a keep-alive pass: a stopped or deleted box that should be serving
is back up, and a box someone put to sleep or released is left alone.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alkera_core.compute.bootstrap import LOCALDEV_AGENT_SOURCE
from alkera_core.compute.launch import render_node_script
from alkera_core.compute.localdev import (
    IMAGE_CONTEXT,
    DockerResult,
    LocaldevProvider,
    image_tag,
)
from alkera_core.compute.provider import LOCALDEV, NodeLaunch
from alkera_core.config import settings
from alkera_core.models.compute import ComputeAllocation
from alkera_test_support.compute.fake_docker import FakeDocker
from backend.services.compute import localdev_keepalive
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import make_machine_type
from tests.conftest import OrgWithAdmin

SECRETS = {
    "ALKERA_MACHINE_CREDENTIAL": "mc_x",
    "ALKERA_BOX_TOKEN": "t",
    "ALKERA_BOX_TOKEN_EXPIRES": "x",
}


@pytest.fixture
def provider(tmp_path: Path) -> LocaldevProvider:
    source = tmp_path / "src"
    (source / IMAGE_CONTEXT).mkdir(parents=True)
    (source / IMAGE_CONTEXT / "Dockerfile").write_text("FROM ubuntu:24.04\n")
    (source / "pyproject.toml").write_text("[project]\nname='x'\n")
    # The Linux harness the build script stages; a box is never started without it.
    (source / LOCALDEV_AGENT_SOURCE).mkdir(parents=True)
    for name in ("opencode", "rg"):
        (source / LOCALDEV_AGENT_SOURCE / name).write_text("#!/bin/sh\n")
        (source / LOCALDEV_AGENT_SOURCE / name).chmod(0o755)

    async def no_build(root: Path, deadline_s: float) -> DockerResult:
        return DockerResult(1, "", "no harness build in a test")

    return LocaldevProvider(
        enabled=True,
        # Unique per test: the rows of other tests' local boxes share this database.
        project=f"alkera-test-{uuid.uuid4().hex[:8]}",
        source_root=source,
        state_dir=tmp_path / "state",
        docker=FakeDocker(),
        build_agent=no_build,
    )


async def _box(
    session: AsyncSession,
    org: OrgWithAdmin,
    provider: LocaldevProvider,
    *,
    state: str,
    script: str | None = None,
) -> str:
    """A local box with a row in ``state`` and its container up, started from
    ``script``, or from the bootstrap the backend renders for it today."""
    mt = await make_machine_type(session, provider=LOCALDEV, provider_price_per_minute_nanos=0)
    alloc = ComputeAllocation(
        user_id=org.admin_id,
        org_team_id=org.org_id,
        machine_type_id=mt.id,
        lifecycle="workspace",
        origin="provisioned",
        tenancy="pool",
        state=state,
        created_at=datetime.now(UTC),
        state_changed_at=datetime.now(UTC),
    )
    session.add(alloc)
    await session.flush()
    await provider.store_credential(alloc.id, SECRETS)
    name = await provider.run(
        NodeLaunch(
            allocation_id=alloc.id,
            name=f"alkera-node-{alloc.id.hex[:8]}",
            type_code=mt.provider_type_id,
            storage_gb=10,
            script=script or await render_node_script(alloc, mt, config=settings),
        )
    )
    alloc.provider_machine_id = name
    await session.commit()
    return name


def _docker(provider: LocaldevProvider) -> FakeDocker:
    assert isinstance(provider.docker, FakeDocker)
    return provider.docker


@pytest.mark.parametrize("state", ["provisioning", "bootstrapping", "ready", "draining"])
async def test_a_stopped_box_that_should_be_serving_is_started(
    real_session: AsyncSession, org_admin: OrgWithAdmin, provider: LocaldevProvider, state: str
) -> None:
    name = await _box(real_session, org_admin, provider, state=state)
    _docker(provider).containers[name].status = "exited"

    outcomes = await localdev_keepalive.keep_boxes_running(real_session, provider)

    assert outcomes[name] == "started"
    assert _docker(provider).containers[name].status == "running"


async def test_a_deleted_box_that_should_be_serving_is_recreated(
    real_session: AsyncSession, org_admin: OrgWithAdmin, provider: LocaldevProvider
) -> None:
    name = await _box(real_session, org_admin, provider, state="ready")
    _docker(provider).containers.pop(name)

    outcomes = await localdev_keepalive.keep_boxes_running(real_session, provider)

    assert outcomes[name] == "recreated"
    assert _docker(provider).containers[name].status == "running"


@pytest.mark.parametrize("state", ["asleep", "released", "failed", "releasing", "lost"])
async def test_a_box_stopped_on_purpose_is_left_stopped(
    real_session: AsyncSession, org_admin: OrgWithAdmin, provider: LocaldevProvider, state: str
) -> None:
    name = await _box(real_session, org_admin, provider, state=state)
    _docker(provider).containers[name].status = "exited"

    outcomes = await localdev_keepalive.keep_boxes_running(real_session, provider)

    assert name not in outcomes
    assert _docker(provider).containers[name].status == "exited"


async def test_one_box_that_cannot_come_back_does_not_stop_the_others(
    real_session: AsyncSession, org_admin: OrgWithAdmin, provider: LocaldevProvider
) -> None:
    lost = await _box(real_session, org_admin, provider, state="ready")
    kept = await _box(real_session, org_admin, provider, state="ready")
    docker = _docker(provider)
    docker.containers.pop(lost)
    lost_id = docker_allocation(provider, lost)
    await provider.delete_credential(lost_id)
    docker.containers[kept].status = "exited"

    outcomes = await localdev_keepalive.keep_boxes_running(real_session, provider)

    assert outcomes[lost].startswith("error:")
    assert outcomes[kept] == "started"
    assert docker.containers[kept].status == "running"


def docker_allocation(provider: LocaldevProvider, name: str) -> uuid.UUID:
    for launch in provider.state_dir.glob("*/launch.json"):
        if name in launch.read_text():
            return uuid.UUID(launch.parent.name)
    raise AssertionError(f"no state for {name}")


@pytest.mark.parametrize("env", ["staging", "production"])
def test_the_keepalive_starts_nothing_outside_a_local_deployment(
    provider: LocaldevProvider, env: str
) -> None:
    config = settings.model_copy(update={"app_env": env})
    assert localdev_keepalive.start(config=config, provider_factory=lambda _: provider) is None


async def test_a_box_that_went_down_while_the_backend_was_off_is_up_as_it_starts(
    real_session: AsyncSession, org_admin: OrgWithAdmin, provider: LocaldevProvider
) -> None:
    """The first pass runs at start, not one interval later."""
    name = await _box(real_session, org_admin, provider, state="ready")
    _docker(provider).containers[name].status = "exited"
    local = settings.model_copy(update={"app_env": "local"})

    task = localdev_keepalive.start(
        config=local, provider_factory=lambda _: provider, interval=3600
    )
    assert task is not None
    try:
        for _ in range(200):
            if _docker(provider).containers[name].status == "running":
                break
            await asyncio.sleep(0.05)
    finally:
        await localdev_keepalive.stop(task)

    assert _docker(provider).containers[name].status == "running"
    assert task.done()


# -- a box never starts on what the source tree no longer renders ---------------

OLD_BOOTSTRAP = "#!/usr/bin/env bash\n# the bootstrap an older backend rendered\n"


def _kept(provider: LocaldevProvider, name: str) -> str:
    return (
        provider.state_dir / str(docker_allocation(provider, name)) / "bootstrap.sh"
    ).read_text()


async def test_a_stopped_box_on_an_older_bootstrap_is_recreated_on_todays(
    real_session: AsyncSession, org_admin: OrgWithAdmin, provider: LocaldevProvider
) -> None:
    """Started as it was, it would run the old bootstrap again (the start
    command and the prerequisites it stages are in it)."""
    name = await _box(real_session, org_admin, provider, state="ready", script=OLD_BOOTSTRAP)
    before = _docker(provider).containers[name]
    before.status = "exited"

    outcomes = await localdev_keepalive.keep_boxes_running(real_session, provider)

    assert outcomes[name] == "recreated"
    assert _kept(provider, name) != OLD_BOOTSTRAP
    assert _docker(provider).containers[name] is not before
    assert _docker(provider).containers[name].status == "running"
    again = await localdev_keepalive.keep_boxes_running(real_session, provider)
    assert again[name] == "running"


async def test_a_stopped_box_on_an_older_image_is_recreated_on_the_current_one(
    real_session: AsyncSession, org_admin: OrgWithAdmin, provider: LocaldevProvider
) -> None:
    name = await _box(real_session, org_admin, provider, state="ready")
    _docker(provider).containers[name].status = "exited"
    context = provider.source_root / IMAGE_CONTEXT
    (context / "Dockerfile").write_text("FROM ubuntu:24.04\nRUN apt-get install -y quota\n")

    outcomes = await localdev_keepalive.keep_boxes_running(real_session, provider)

    assert outcomes[name] == "recreated"
    assert _docker(provider).containers[name].image == image_tag(context)


async def test_a_running_stale_box_waits_for_its_chats_then_is_recreated(
    real_session: AsyncSession, org_admin: OrgWithAdmin, provider: LocaldevProvider
) -> None:
    """A restart would cut what is in flight; the next start runs today's
    bootstrap meanwhile, and the box is recreated once it holds nothing."""
    name = await _box(real_session, org_admin, provider, state="ready", script=OLD_BOOTSTRAP)
    alloc = await _row(real_session, name)
    alloc.chats_served = 2
    await real_session.commit()
    running = _docker(provider).containers[name]

    outcomes = await localdev_keepalive.keep_boxes_running(real_session, provider)

    assert outcomes[name] == "stale"
    assert _docker(provider).containers[name] is running
    assert _kept(provider, name) != OLD_BOOTSTRAP
    alloc.chats_served = 0
    await real_session.commit()
    outcomes = await localdev_keepalive.keep_boxes_running(real_session, provider)
    assert outcomes[name] == "recreated"
    assert _docker(provider).containers[name] is not running


async def _row(session: AsyncSession, name: str) -> ComputeAllocation:
    from sqlalchemy import select

    found = await session.execute(
        select(ComputeAllocation).where(ComputeAllocation.provider_machine_id == name)
    )
    return found.scalar_one()
