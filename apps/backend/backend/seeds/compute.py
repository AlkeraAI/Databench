"""Compute machine-type catalog seed.

Every deployment gets the machine types its own boxes register as and nothing
it could sell: the self-hosted box's type (the box a one-machine install runs
beside its services registers as it), and in a local deployment the developer
box's type with its "Local CPU" offering. No provider flavor is seeded here. A
distribution that sells hosted machines registers its catalog on
:data:`COMPUTE_CATALOG_LOADERS`: each step runs after the open rows, in the
same session, and may use :func:`upsert_machine_types` and
:func:`seed_offerings` below.

Idempotent: upserts by (provider, provider_type_id). Offerings are created
once and never overwritten, so a price a developer set by hand survives a
re-seed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from alkera_core.compute.localdev import LOCALDEV_TYPE_CODE
from alkera_core.compute.provider import LOCALDEV, SELF_HOSTED
from alkera_core.compute.self_hosted import SELF_HOSTED_TYPE_CODE
from alkera_core.config import settings
from alkera_core.extensions import ExtensionPoint
from alkera_core.models.compute import ComputeMachineType
from alkera_core.models.compute_offerings import (
    AUDIENCE_ALL,
    PRICING_FIXED,
    PRICING_PASSTHROUGH,
    ComputeOffering,
)
from alkera_core.money import usd_per_hour_to_nanos_per_minute
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# The live-catalog "out of stock" availability sentinel (matches the refresh core).
_OUT_OF_STOCK = "NONE"


@dataclass(frozen=True)
class CatalogEntry:
    """One machine type a seed writes. ``offered`` is whether it may be
    chosen for a new allocation; a type a box only registers as is not."""

    provider_type_id: str
    display_name: str
    compute_class: str
    gpu_count: int
    vcpu: int
    memory_gb: int
    usd_per_hour: float
    provider: str
    offered: bool = True
    #: What the provider charges per GB-month for the volume a machine keeps.
    storage_rate_per_gb_month_nanos: int = 0


SELF_HOSTED_ENTRY = CatalogEntry(
    SELF_HOSTED_TYPE_CODE,
    "Self-hosted box",
    "cpu",
    0,
    0,
    0,
    0.0,
    provider=SELF_HOSTED,
    offered=False,
)
"""The type a self-hosted box registers as. Never offered: no deployment
starts one, the box starts itself."""

LOCALDEV_ENTRY = CatalogEntry(
    LOCALDEV_TYPE_CODE,
    "Local developer box (gVisor, this machine's Docker)",
    "cpu",
    0,
    2,
    4,
    0.0,
    provider=LOCALDEV,
)
"""The one machine type a local developer box is minted against. Seeded only in
a local deployment: anywhere else the provider refuses, and the catalog must not
offer a box nothing can start."""


def open_catalog(*, local: bool) -> tuple[CatalogEntry, ...]:
    """The rows every deployment seeds, plus the local box in a local one."""
    return (SELF_HOSTED_ENTRY, LOCALDEV_ENTRY) if local else (SELF_HOSTED_ENTRY,)


#: A step that runs after the open rows are seeded, in the same session.
#: Returns a one-line summary.
CatalogLoader = Callable[[AsyncSession], Awaitable[str]]

COMPUTE_CATALOG_LOADERS: ExtensionPoint[CatalogLoader] = ExtensionPoint("compute_catalog_loaders")
"""The machine catalogs a distribution adds. With none registered only the
open rows exist."""

LOCAL_OFFERING_NAME = "Local CPU"
#: The local box's rate, 1000 nano-USD a minute: next to nothing, but it shows
#: on the usage page.
LOCAL_OFFERING_RATE_NANOS = 1000


@dataclass(frozen=True)
class _OfferingSeed:
    name: str
    pricing_mode: str
    fixed_rate_per_minute_nanos: int | None
    storage_gb_default: int
    storage_gb_max: int
    storage_rate_per_gb_month_nanos: int
    idle_stop_minutes_default: int | None
    sort_order: int


def offering_seed_for(entry: CatalogEntry, *, order: int) -> _OfferingSeed:
    """The offering a local deployment sells a seeded machine type as."""
    if entry.provider == LOCALDEV:
        return _OfferingSeed(
            name=LOCAL_OFFERING_NAME,
            pricing_mode=PRICING_FIXED,
            fixed_rate_per_minute_nanos=LOCAL_OFFERING_RATE_NANOS,
            storage_gb_default=10,
            storage_gb_max=50,
            storage_rate_per_gb_month_nanos=0,
            idle_stop_minutes_default=60,
            sort_order=0,
        )
    return _OfferingSeed(
        name=entry.display_name,
        pricing_mode=PRICING_PASSTHROUGH,
        fixed_rate_per_minute_nanos=None,
        storage_gb_default=20,
        storage_gb_max=200,
        storage_rate_per_gb_month_nanos=entry.storage_rate_per_gb_month_nanos,
        idle_stop_minutes_default=60,
        sort_order=order,
    )


async def _machine_type(session: AsyncSession, entry: CatalogEntry) -> ComputeMachineType | None:
    return (
        await session.execute(
            select(ComputeMachineType).where(
                ComputeMachineType.provider == entry.provider,
                ComputeMachineType.provider_type_id == entry.provider_type_id,
            )
        )
    ).scalar_one_or_none()


async def seed_offerings(
    session: AsyncSession, entries: Sequence[CatalogEntry], *, local: bool, first_order: int = 1
) -> int:
    """One offering per offered machine type in ``entries`` that has none
    yet, in a local deployment only. Returns how many were created."""
    if not local:
        return 0
    created = 0
    for order, entry in enumerate(entries, start=first_order):
        if not entry.offered:
            continue
        machine_type = await _machine_type(session, entry)
        if machine_type is None:
            continue
        has_offering = (
            await session.execute(
                select(ComputeOffering.id)
                .where(ComputeOffering.machine_type_id == machine_type.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if has_offering is not None:
            continue
        seed = offering_seed_for(entry, order=order)
        session.add(
            ComputeOffering(
                machine_type_id=machine_type.id,
                name=seed.name,
                pricing_mode=seed.pricing_mode,
                markup_bps=0,
                fixed_rate_per_minute_nanos=seed.fixed_rate_per_minute_nanos,
                storage_gb_default=seed.storage_gb_default,
                storage_gb_max=seed.storage_gb_max,
                storage_rate_per_gb_month_nanos=seed.storage_rate_per_gb_month_nanos,
                audience=AUDIENCE_ALL,
                purchasable=True,
                idle_stop_minutes_default=seed.idle_stop_minutes_default,
                sort_order=seed.sort_order,
            )
        )
        created += 1
    await session.flush()
    return created


@dataclass
class UpsertCounts:
    created: int = 0
    updated: int = 0
    unchanged: int = 0

    def summary(self) -> str:
        return f"created: {self.created}, updated: {self.updated}, unchanged: {self.unchanged}"


async def upsert_machine_types(
    session: AsyncSession,
    entries: Sequence[CatalogEntry],
    *,
    live_prices: Mapping[str, int] | None = None,
) -> UpsertCounts:
    """Write each entry's machine type, or bring an existing one back to a
    usable state. ``live_prices`` (provider type id to nano-USD a minute, per
    vCPU) replaces the static price only when it is in hand: a re-seed with no
    live price never overwrites a refreshed one with the stale snapshot."""
    counts = UpsertCounts()
    live = live_prices or {}
    for entry in entries:
        live_raw = live.get(entry.provider_type_id)
        have_live = live_raw is not None
        if live_raw is None:
            price = usd_per_hour_to_nanos_per_minute(entry.usd_per_hour)
        else:
            # The live CPU price is PER vCPU; the whole-machine price scales by count.
            price = live_raw * max(1, entry.vcpu)
        existing = await _machine_type(session, entry)
        if existing is None:
            session.add(
                ComputeMachineType(
                    provider=entry.provider,
                    provider_type_id=entry.provider_type_id,
                    display_name=entry.display_name,
                    compute_class=entry.compute_class,
                    gpu_count=entry.gpu_count,
                    vcpu=entry.vcpu,
                    memory_gb=entry.memory_gb,
                    provider_price_per_minute_nanos=price,
                    active=True,
                    available_for_new=entry.offered,
                )
            )
            counts.created += 1
            continue
        needs_price = have_live and existing.provider_price_per_minute_nanos != price
        if (
            needs_price
            or existing.display_name != entry.display_name
            or existing.available_for_new != entry.offered
            or not existing.active
            # available_for_new=True but availability still NONE is inconsistent.
            or existing.availability == _OUT_OF_STOCK
        ):
            if needs_price:
                existing.provider_price_per_minute_nanos = price
            existing.display_name = entry.display_name
            # A re-seed makes the catalog usable again: the refresh job (or a test)
            # may have flipped a seeded type unavailable — the seed's job is a working
            # starting catalog. The job re-reconciles from the live feed on its tick.
            existing.available_for_new = entry.offered
            existing.active = True
            if existing.availability == _OUT_OF_STOCK:
                existing.availability = "unknown"
            counts.updated += 1
        else:
            counts.unchanged += 1
    await session.flush()
    return counts


async def seed_compute(session: AsyncSession) -> str:
    """The open machine types, their local offering, then each registered
    :data:`COMPUTE_CATALOG_LOADERS` step."""
    local = settings.is_local
    entries = open_catalog(local=local)
    counts = await upsert_machine_types(session, entries)
    offerings = await seed_offerings(session, entries, local=local)
    offering_note = f"; offerings created: {offerings}" if offerings else ""
    notes = [f"{counts.summary()}{offering_note}"]
    for step in COMPUTE_CATALOG_LOADERS.items():
        notes.append(await step(session))
    return "; ".join(notes)


__all__ = [
    "COMPUTE_CATALOG_LOADERS",
    "LOCALDEV_ENTRY",
    "LOCAL_OFFERING_NAME",
    "LOCAL_OFFERING_RATE_NANOS",
    "SELF_HOSTED_ENTRY",
    "CatalogEntry",
    "CatalogLoader",
    "UpsertCounts",
    "offering_seed_for",
    "open_catalog",
    "seed_compute",
    "seed_offerings",
    "upsert_machine_types",
]
