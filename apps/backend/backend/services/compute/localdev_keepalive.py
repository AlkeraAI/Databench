"""Keep every local developer box that should be serving, running.

A local box is a Docker container on the developer's machine, and plenty of
things stop it that never stop an EC2 instance: Docker quitting, the machine
sleeping or rebooting, someone running ``docker stop`` or ``docker rm``. The
container's ``unless-stopped`` restart policy covers a Docker or machine
restart; this pass covers everything else. At backend start, and then every
:data:`INTERVAL_SECONDS`, it asks the ``localdev`` provider to bring up each box
whose row says it should be serving (``provisioning``, ``bootstrapping``,
``ready`` or ``draining``): a stopped container is started, a deleted one is
recreated from what the provider kept. A box someone put to sleep (``asleep``)
or released is left alone: that stop was meant. So is the box of an org machine
its org turned off or deleted, even while its row is still ``ready`` or
``draining`` on the way down: the org's word decides, not the row's state.

Every pass also renders each box's bootstrap again, as a new box would get it,
and hands it to the provider: a box never starts on a bootstrap or image older
than the source tree it serves from. A box running an older one is recreated
once nothing is in flight on it.

Runs only in a local deployment; anywhere else :func:`start` starts nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

from alkera_core.compute.launch import render_node_script
from alkera_core.compute.localdev import LocaldevProvider, make_localdev_provider
from alkera_core.compute.provider import LOCALDEV, ComputeProviderError
from alkera_core.config import Settings, settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.logging import get_logger
from alkera_core.models.compute import (
    BOOTSTRAPPING,
    DRAINING,
    PROVISIONING,
    READY,
    ComputeAllocation,
    ComputeMachineType,
)
from alkera_core.models.org_machines import POWER_ON, OrgMachine
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

log = get_logger(__name__)

#: How often the pass looks again once the backend is up.
INTERVAL_SECONDS = 30.0
#: The states in which a box should be serving, so a stopped container is a
#: box to bring back. ``asleep`` is not here: that box was stopped on purpose.
SERVING_STATES: tuple[str, ...] = (PROVISIONING, BOOTSTRAPPING, READY, DRAINING)


async def boxes_to_keep(db: AsyncSession) -> list[tuple[ComputeAllocation, ComputeMachineType]]:
    """Every local box that should be serving, with its machine type: its row
    is in a serving state and, when an org machine holds it, the org wants
    that machine on and has not deleted it. A box whose org machine this
    session cannot read is left alone: restarting a box nobody asked for is
    the worse mistake."""
    rows = await db.execute(
        select(ComputeAllocation, ComputeMachineType)
        .join(ComputeMachineType, ComputeMachineType.id == ComputeAllocation.machine_type_id)
        .outerjoin(OrgMachine, OrgMachine.id == ComputeAllocation.org_machine_id)
        .where(
            ComputeMachineType.provider == LOCALDEV,
            ComputeAllocation.state.in_(SERVING_STATES),
            ComputeAllocation.provider_machine_id.is_not(None),
            or_(
                ComputeAllocation.org_machine_id.is_(None),
                and_(OrgMachine.desired_power == POWER_ON, OrgMachine.deleted_at.is_(None)),
            ),
        )
    )
    return [(alloc, machine_type) for alloc, machine_type in rows.tuples()]


async def keep_boxes_running(
    db: AsyncSession, provider: LocaldevProvider, *, config: Settings = settings
) -> dict[str, str]:
    """Bring up every local box that should be serving, on the bootstrap the
    backend renders for it now. Returns each box's outcome (``running``,
    ``stale``, ``started``, ``recreated``, or the error), so a caller and a
    test can see what happened. One box failing never stops the pass from
    reaching the next."""
    outcomes: dict[str, str] = {}
    for alloc, machine_type in await boxes_to_keep(db):
        machine_id = alloc.provider_machine_id or ""
        try:
            script = await render_node_script(alloc, machine_type, config=config)
            outcome = await provider.ensure_running(
                machine_id, script=script, may_restart=not alloc.chats_served
            )
        except (ComputeProviderError, ValueError) as exc:
            outcome = f"error: {exc}"
            log.warning("compute.localdev.keepalive_failed", machine_id=machine_id, error=str(exc))
        else:
            if outcome != "running":
                log.info(
                    "compute.localdev.box_brought_back", machine_id=machine_id, outcome=outcome
                )
        outcomes[machine_id] = outcome
    return outcomes


async def _loop(provider: LocaldevProvider, interval: float, config: Settings) -> None:
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await keep_boxes_running(db, provider, config=config)
        except Exception as exc:
            # The pass is a convenience: a database or Docker hiccup is logged
            # and the next tick tries again, never taking the backend down.
            log.warning("compute.localdev.keepalive_pass_failed", error=str(exc))
        await asyncio.sleep(interval)


def start(
    *,
    config: Settings = settings,
    provider_factory: Callable[[Settings], LocaldevProvider] = make_localdev_provider,
    interval: float = INTERVAL_SECONDS,
) -> asyncio.Task[None] | None:
    """Start the keep-alive, or return ``None`` outside a local deployment. The
    caller holds the task and hands it to :func:`stop` on shutdown. The first
    pass runs at once, so a box that went down while the backend was off comes
    back as the backend starts."""
    if not config.is_local:
        return None
    provider = provider_factory(config)
    if not provider.configured():
        log.info("compute.localdev.keepalive_disabled", reason="not configured")
        return None
    return asyncio.create_task(_loop(provider, interval, config), name="localdev-keepalive")


async def stop(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


__all__ = [
    "INTERVAL_SECONDS",
    "SERVING_STATES",
    "boxes_to_keep",
    "keep_boxes_running",
    "start",
    "stop",
]
