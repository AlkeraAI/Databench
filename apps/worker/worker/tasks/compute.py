"""The compute plane's periodic cores: the metering tick and the reachability sweep.

``run_meter`` runs every minute: it verifies each live allocation against the
provider, bills the whole minutes elapsed at the pinned granted rate against the
same fungible credits an LLM token draws, records the pinned true cost beside
them, and cuts a machine off — visibly — the moment its account cannot cover
the next minute (see ``alkera_core.compute.meter``). Idempotent: a re-run within
the same minute bills nothing twice, so a missed tick self-heals on the next.

``run_sweep`` is the pass that announces what a machine's own silence cannot: every
workspace machine whose reachability changed without a heartbeat to say so — the
box that went quiet (``unreachable``) — and the recovery a heartbeat may have
missed. It terminates nothing; a machine that is truly gone is the meter's to
reap.

It carries one other consequence of the same silence: a promoted result whose
payload a departed machine never delivered and never refused. That object waits
in ``pending_upload`` with no rows and no reason — "Saving…" for ever — and
nothing else is in a position to end the wait. It rides this sweep rather than a
schedule of its own because it IS this fact, read from the object's side, and
because this sweep already runs well inside the deadline.

Both take the provider through a module-level factory so a test substitutes a
fake, and both take ``now`` so a test drives them across minute boundaries.

The meter needs a provider key to do ANY of its job: without one every status
poll answers 401, so every row is skipped — nothing billed, no true cost
recorded, no credit cutoff, no lapsed-grant stop, and the only trace is a
per-row warning nobody reads. :func:`check_money_path_provider_key` is the boot
gate that refuses to serve the meter's queue in that state, and a tick that
skipped every row for that reason says so once, loudly, in its own summary.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from alkera_core.compute.availability import Availability, SizeQuery
from alkera_core.compute.machines import sweep_reachability
from alkera_core.compute.meter import HeartbeatWatch, compute_metering, meter_and_cutoff
from alkera_core.compute.node_reconcile import reconcile_nodes
from alkera_core.compute.nodes import NodeProvider, node_provider_for_kind
from alkera_core.compute.org_reconcile import reconcile_org_machines
from alkera_core.compute.provider import (
    EC2,
    RUNPOD,
    ComputeProvider,
    NodeDescription,
    NodeLaunch,
    PodPhase,
    PodStatus,
    ProviderPod,
    kinds_with,
    provider_for_kind,
    registered_kinds,
)
from alkera_core.compute.reconcile import TruncationWatch, reconcile_pods
from alkera_core.compute.refresh import fetch_and_refresh
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.logging import get_logger
from alkera_core.models.compute import (
    COMPUTE_TERMINAL_STATES,
    ComputeAllocation,
    ComputeMachineType,
)
from alkera_core.objects import end_abandoned_turns, end_untaken_wakes, expire_stalled_promotes
from alkera_core.temporal import QUEUE_FOR, TaskQueue, WorkflowType
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = get_logger(__name__)

METER_QUEUE: TaskQueue = QUEUE_FOR[WorkflowType.COMPUTE_METER]
"""The queue that runs the money path's metering tick."""


class ComputeProviderKeyMissingError(RuntimeError):
    """This worker is set to serve the compute meter with no provider key."""


def check_money_path_provider_key(queues: Iterable[TaskQueue]) -> None:
    """Refuse to serve the metering queue with no provider key.

    A meter that cannot reach the provider is not a degraded meter, it is no
    meter: every row is skipped, so nothing is billed, no true cost is
    recorded, and no machine is ever cut off for credit or a lapsed grant. That
    is a money path failing open, so it fails LOUDLY at boot instead — the same
    shape as the production settings validator. A laptop keeps its leniency
    (``compute_require_provider_key`` unset in local dev), and so does a
    deployment that runs no compute plane and sets the flag false."""
    if METER_QUEUE not in set(queues) or _a_catalog_provider_is_configured():
        return
    message = (
        f"this worker serves the {METER_QUEUE.value!r} queue, which runs "
        f"{WorkflowType.COMPUTE_METER.value!r}, but no provider the platform starts "
        "machines at is configured: every provider poll would fail, so no compute "
        "minute would be billed and no machine would ever be cut off. Configure a "
        "provider, drop the queue from ALKERA_TEMPORAL_TASK_QUEUES, or set "
        "COMPUTE_REQUIRE_PROVIDER_KEY=false on a deployment that runs no compute plane"
    )
    if settings.compute_provider_key_required:
        raise ComputeProviderKeyMissingError(message)
    log.warning("compute.provider_key_missing", detail=message)


def _a_catalog_provider_is_configured() -> bool:
    """Whether any registered provider the platform starts machines at from
    the catalog holds what it needs to answer a poll."""
    return any(
        provider_for_kind(kind, settings).configured()
        for kind in kinds_with(lambda traits: traits.catalog_provisioned)
    )


class ProviderByMachine:
    """The meter's provider, resolved per machine rather than fixed to one kind.

    It is a full :class:`ComputeProvider` that ROUTES: the meter asks about a
    machine by its provider id alone, so the tick first reads which kind every
    live row's machine type names and sends each id-keyed question to that kind's
    provider. A machine id the map does not name (a row that appeared mid-tick)
    goes to the default kind, as before. The provisioning and catalog methods
    the meter never calls are on the interface so this composite satisfies it in
    full; they route to the machine's own kind, or to the default when the call
    carries no id to route by."""

    def __init__(self, kinds: dict[str, str], build: Callable[[str], ComputeProvider]) -> None:
        self._kinds = kinds
        self._build = build
        self._built: dict[str, ComputeProvider] = {}

    kind = RUNPOD

    def for_kind(self, kind: str) -> ComputeProvider:
        if kind not in self._built:
            self._built[kind] = self._build(kind)
        return self._built[kind]

    def _for(self, machine_id: str) -> ComputeProvider:
        return self.for_kind(self._kinds.get(machine_id, RUNPOD))

    def configured(self) -> bool:
        return self.for_kind(RUNPOD).configured()

    # -- id-keyed: routed to the machine's own kind ---------------------------

    async def pod_status(self, pod_id: str) -> PodStatus:
        return await self._for(pod_id).pod_status(pod_id)

    async def terminate_pod(self, pod_id: str) -> None:
        await self._for(pod_id).terminate_pod(pod_id)

    async def describe(self, machine_id: str) -> NodeDescription:
        return await self._for(machine_id).describe(machine_id)

    async def stop(self, machine_id: str) -> None:
        await self._for(machine_id).stop(machine_id)

    async def start(self, machine_id: str) -> None:
        await self._for(machine_id).start(machine_id)

    async def grow_volume(self, machine_id: str, size_gb: int) -> None:
        await self._for(machine_id).grow_volume(machine_id, size_gb)

    async def terminate(self, machine_id: str) -> None:
        await self._for(machine_id).terminate(machine_id)

    # -- kind-keyed / default-routed (never called by the meter) --------------

    async def create_pod(
        self, *, name: str, machine_type: ComputeMachineType, ssh_public_key: str
    ) -> str:
        return await self.for_kind(machine_type.provider).create_pod(
            name=name, machine_type=machine_type, ssh_public_key=ssh_public_key
        )

    async def run(self, launch: NodeLaunch) -> str:
        return await self.for_kind(RUNPOD).run(launch)

    async def store_credential(self, allocation_id: UUID, secrets: dict[str, str]) -> None:
        await self.for_kind(RUNPOD).store_credential(allocation_id, secrets)

    async def delete_credential(self, allocation_id: UUID) -> None:
        await self.for_kind(RUNPOD).delete_credential(allocation_id)

    async def bind_credential(self, allocation_id: UUID, machine_id: str) -> None:
        await self._for(machine_id).bind_credential(allocation_id, machine_id)

    async def find(self, allocation_id: UUID) -> NodeDescription | None:
        return await self.for_kind(RUNPOD).find(allocation_id)

    async def list_pods(self, *, name_prefix: str = "") -> list[ProviderPod]:
        return await self.for_kind(RUNPOD).list_pods(name_prefix=name_prefix)

    def normalize_status(self, raw_status: str) -> PodPhase:
        return self.for_kind(RUNPOD).normalize_status(raw_status)

    async def catalog_prices(self, sizes: list[SizeQuery] | None = None) -> dict[str, int]:
        return await self.for_kind(RUNPOD).catalog_prices(sizes)

    async def catalog_entries(
        self, sizes: list[SizeQuery] | None = None
    ) -> dict[str, dict[str, Any]]:
        return await self.for_kind(RUNPOD).catalog_entries(sizes)

    async def availability(self, sizes: list[SizeQuery]) -> dict[str, Availability]:
        return await self.for_kind(RUNPOD).availability(sizes)


async def _machine_kinds(db: AsyncSession) -> dict[str, str]:
    """provider machine id -> provider kind, for every row the meter may poll."""
    rows = await db.execute(
        select(ComputeAllocation.provider_machine_id, ComputeMachineType.provider)
        .join(ComputeMachineType, ComputeMachineType.id == ComputeAllocation.machine_type_id)
        .where(
            ComputeAllocation.state.not_in(COMPUTE_TERMINAL_STATES),
            ComputeAllocation.provider_machine_id != "",
        )
    )
    return {machine_id: kind for machine_id, kind in rows.all()}


def make_node_provider(kind: str) -> NodeProvider:
    """The node provider the reconcile drives for ``kind`` — module-level so a
    test substitutes a fake."""
    return node_provider_for_kind(kind, settings)


def make_provider() -> ComputeProvider:
    """The RunPod provider, built through the registry — module-level so a test
    substitutes a fake. It is the default the meter falls back to and one of the
    providers the reconcile lists (see :func:`reconcile_providers`)."""
    return provider_for_kind(RUNPOD, settings)


def make_ec2_provider() -> ComputeProvider:
    """The EC2 provider, built through the registry — module-level so a test
    substitutes a fake EC2."""
    return provider_for_kind(EC2, settings)


def make_kind_provider(kind: str) -> ComputeProvider:
    """Any other registered provider, built through the registry — module-level
    so a test substitutes a fake."""
    return provider_for_kind(kind, settings)


def reconcile_providers() -> list[tuple[str, ComputeProvider]]:
    """``(kind, provider)`` for every provider the reconcile lists this pass.

    One provider-agnostic reconcile, not a RunPod pass plus a separate EC2 sweep:
    RunPod is always listed (an unconfigured key answers 401, which the pass
    reads as "not here" and skips), and every other registered kind once its
    provider says it is configured, so a new provider is reconciled by registering it. Each kind
    scopes its own write-off, so a pass only closes a row whose machine type
    belongs to the provider it actually listed."""
    kinds = registered_kinds()
    providers: list[tuple[str, ComputeProvider]] = []
    if RUNPOD in kinds:
        providers.append((RUNPOD, make_provider()))
    if EC2 in kinds:
        ec2 = make_ec2_provider()
        if ec2.configured():
            providers.append((EC2, ec2))
    for kind in kinds:
        if kind in (RUNPOD, EC2):
            continue
        provider = make_kind_provider(kind)
        if provider.configured():
            providers.append((kind, provider))
    return providers


async def make_meter_provider(db: AsyncSession) -> ComputeProvider:
    """The provider the tick meters through: per machine, by its type's kind.
    A test that substitutes :func:`make_provider` still meters through it."""
    return ProviderByMachine(
        await _machine_kinds(db),
        lambda kind: make_provider() if kind == RUNPOD else provider_for_kind(kind, settings),
    )


METER_PERIOD = timedelta(minutes=1)
"""How often the metering tick is scheduled (``worker.schedules``)."""


def _heartbeat_watch() -> HeartbeatWatch:
    """The watch this process meters under. A tick that arrives later than the
    ready window plus one tick period after the previous one means the meter
    (or its database) was away in between, and the boxes silent through that
    gap were not heard because nothing was listening."""
    return HeartbeatWatch(
        max_gap=timedelta(seconds=settings.compute_heartbeat_ready_seconds) + METER_PERIOD
    )


HEARTBEAT_WATCH = _heartbeat_watch()
"""Process-wide: the question it answers is about THIS meter's own run. A
worker restart starts it over, which is exactly the fresh window every box is
owed after the platform was away."""


async def run_meter(now: datetime | None = None) -> dict[str, int]:
    """One metering pass over every live allocation. Returns the tick's counts."""
    moment = now or datetime.now(UTC)
    try:
        async with AsyncSessionLocal() as db:
            provider = await make_meter_provider(db)
            summary = await meter_and_cutoff(
                db,
                provider=provider,
                now=moment,
                # The optional fleet-wide session lease fallback; None (the
                # default) = no fleet-wide time cap. Each allocation's own lease
                # always wins, and a workspace machine has none.
                max_minutes=settings.compute_max_lease_minutes,
                watch=HEARTBEAT_WATCH,
                send_notice=compute_metering().notice_sender(),
            )
    except Exception:
        # A tick the database refused heard nobody: the silence that piled up
        # meanwhile is the platform's, and the next tick must not bill it to
        # the boxes.
        HEARTBEAT_WATCH.broke()
        raise
    result = {
        "checked": summary.checked,
        "metered": summary.metered,
        "terminated": summary.terminated,
        "skipped": summary.skipped,
        "provider_errors": summary.provider_errors,
    }
    log.info("compute.meter", **result, reasons=sorted(set(summary.reasons)))
    if summary.provider_errors and not summary.metered:
        # The provider refused us and NOT ONE minute was billed: a stalled money
        # path, not a row-level hiccup. Deliberately not "every row failed" — one
        # permanently unmeterable row would then mask an outage forever. One line
        # an operator can alert on, beside the per-row warnings nobody reads.
        log.error("compute.meter.provider_unreachable", **result)
    return result


_COUNTED = ("listed", "adopted", "terminated", "written_off", "held", "deferred_terminations")


async def run_reconcile(now: datetime | None = None) -> dict[str, int]:
    """One pass comparing this deployment's pods AT EACH PROVIDER with the rows
    that are supposed to own them: adopt what a row lost, terminate what no row
    will ever bill or stop, write off a create the provider never confirmed.

    ONE provider-agnostic pass, run per provider the reconcile lists — RunPod
    and (once configured) EC2 both reconcile the same way, rather than RunPod
    through this pass and EC2 through a separate orphan sweep. Each kind scopes
    its own write-off. Returns the pass's summed counts."""
    moment = now or datetime.now(UTC)
    totals = dict.fromkeys(_COUNTED, 0)
    truncated_streak = 0
    for kind, provider in reconcile_providers():
        async with AsyncSessionLocal() as db:
            # The KIND is passed beside the provider: the pass may only write off
            # a row whose machine type belongs to the provider it actually listed.
            summary = await reconcile_pods(
                db,
                provider=provider,
                provider_kind=kind,
                now=moment,
                watch=_truncation_watch(kind),
            )
        for key in _COUNTED:
            totals[key] += getattr(summary, key)
        truncated_streak = max(truncated_streak, summary.truncated_streak)
        if summary.unavailable:
            # This provider could not be asked at all. Said once, loudly, per
            # kind: while it is true nothing is looking for that provider's pods.
            log.error("compute.reconcile.provider_unreachable", provider=kind)
    result: dict[str, int] = {**totals, "truncated_streak": truncated_streak}
    async with AsyncSessionLocal() as db:
        nodes = await reconcile_nodes(db, providers=make_node_provider, now=moment)
    result.update(
        nodes_checked=nodes.checked,
        nodes_moved=nodes.moved,
        nodes_provider_errors=nodes.provider_errors,
    )
    log.info("compute.reconcile.nodes", edges=nodes.edges, **result)
    return result


async def run_org_reconcile(now: datetime | None = None) -> dict[str, int]:
    """One pass making every org machine's provider machine match what its org
    asked for (``alkera_core.compute.org_reconcile``). The retry policy's waits
    are real sleeps here; a test drives the core with its own. Returns the
    pass's counts."""
    moment = now or datetime.now(UTC)
    async with AsyncSessionLocal() as db:
        summary = await reconcile_org_machines(
            db, providers=make_node_provider, config=settings, sleep=asyncio.sleep, now=moment
        )
    return {
        "checked": summary.checked,
        "acted": sum(summary.steps.values()),
        "announced": summary.announced,
        "idle_stops": summary.idle_stops,
        "provider_errors": summary.provider_errors,
    }


async def run_refresh_catalog(now: datetime | None = None) -> dict[str, int]:
    """Pull each configured provider's live catalog and reconcile its own rows'
    price + stock. Provider-scoped, so one provider's feed never blanks another's
    catalog; money-safe, since it never re-rates a running allocation (the meter
    bills the allocation's pinned prices). Returns the summed counts."""
    result: dict[str, int] = {"updated": 0, "marked_unavailable": 0, "reappeared": 0, "added": 0}
    for kind, provider in reconcile_providers():
        if not provider.configured():
            continue
        async with AsyncSessionLocal() as db:
            outcome = await fetch_and_refresh(db, provider=provider, provider_kind=kind)
        if outcome is not None:
            for key, value in outcome.items():
                result[key] = result.get(key, 0) + value
    log.info("compute.catalog_refresh", **result)
    return result


async def run_sweep(now: datetime | None = None) -> int:
    """One pass over what a machine's silence leaves unsaid: every live
    workspace machine whose reachability changed, every promoted result whose
    payload nobody is coming to deliver, and every running turn whose worker
    stopped reporting on it. Returns how many frames it announced in total."""
    moment = now or datetime.now(UTC)
    async with AsyncSessionLocal() as db:
        changed = await sweep_reachability(db, now=moment)
    async with AsyncSessionLocal() as db:
        expired = await expire_stalled_promotes(db, now=moment)
    async with AsyncSessionLocal() as db:
        abandoned = await end_abandoned_turns(db, now=moment)
    async with AsyncSessionLocal() as db:
        untaken = await end_untaken_wakes(db, now=moment)
    announced = changed + expired + abandoned + untaken
    log.info(
        "compute.sweep",
        announced=announced,
        machines=changed,
        promotes=expired,
        turns=abandoned,
        wakes=untaken,
    )
    return announced


RECONCILE_QUEUE: TaskQueue = QUEUE_FOR[WorkflowType.COMPUTE_RECONCILE]
"""The queue that runs the pod reconciliation pass (the money path's, too)."""

_TRUNCATION_WATCHES: dict[str, TruncationWatch] = {}
"""Per provider kind: how many passes in a row ended on a listing that kind
could not be trusted to have returned whole. Process-wide and per-kind because
the question — has THIS worker's write-off for THIS provider stalled — is asked
of each provider separately; a worker restart starts each count over, which is
right, since a fresh process has made no passes."""


def _truncation_watch(kind: str) -> TruncationWatch:
    return _TRUNCATION_WATCHES.setdefault(kind, TruncationWatch())


CATALOG_QUEUE: TaskQueue = QUEUE_FOR[WorkflowType.COMPUTE_CATALOG]
"""The queue that runs the live compute-catalog refresh."""


__all__ = [
    "CATALOG_QUEUE",
    "METER_QUEUE",
    "RECONCILE_QUEUE",
    "ComputeProviderKeyMissingError",
    "ProviderByMachine",
    "check_money_path_provider_key",
    "make_ec2_provider",
    "make_kind_provider",
    "make_meter_provider",
    "make_node_provider",
    "make_provider",
    "reconcile_providers",
    "run_meter",
    "run_reconcile",
    "run_refresh_catalog",
    "run_sweep",
]
