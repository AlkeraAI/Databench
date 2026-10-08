"""Live compute-catalog refresh.

Pulls the provider's live catalog (price + stock) on a schedule and reconciles
it onto ``compute_machine_types``:

- a type in the feed: update ``provider_price_per_minute_nanos``,
  ``availability``, ``available_for_new`` and ``synced_at``;
- a known type absent from the feed, or with ``max_count == 0`` (the provider
  does not sell it): ``available_for_new = False``. The row is retained, never
  deleted, so a running allocation keeps its name and bills on its pinned
  prices. A type merely out of stock (``availability == NONE``) stays for sale
  and reads out of stock;
- a type that reappears with stock: ``available_for_new = True`` again;
- the hardware the feed describes (GPU model, GPU memory, boot disk, and for a
  flavor sold by the vCPU its memory) is copied onto the type, so the buy
  dialog and the machine card name what a person is paying for.

Money safety: this never touches a running ``ComputeAllocation`` and never
changes how one bills (the meter reads the allocation's pinned prices). A feed
fetch failure leaves the catalog exactly as it is, so a provider outage cannot
make every type look unavailable.

A feed entry the catalog does not hold is added only when its provider
registered a :data:`CatalogAdopter` for it (:func:`register_catalog_adopter`),
so a curated catalog stays curated. RunPod adopts its GPU types, which no seed
carries, so a platform admin can make an offering of one. Nothing added this
way is for sale until an admin makes an offering.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.compute.availability import SizeQuery
from alkera_core.compute.provider import (
    ComputeProvider,
    ComputeProviderError,
    ensure_builtin_providers,
)
from alkera_core.models.compute import ComputeMachineType

#: Builds the machine type for a feed entry (its code and the entry) the
#: catalog does not hold yet, or ``None`` to leave the entry out. The refresh
#: fills in the provider, its price, stock and hardware like any other row.
CatalogAdopter = Callable[[str, Mapping[str, Any]], ComputeMachineType | None]

_ADOPTERS: dict[str, CatalogAdopter] = {}


def register_catalog_adopter(provider_kind: str, adopter: CatalogAdopter) -> None:
    """Let ``provider_kind``'s refresh add the feed entries ``adopter`` builds a
    type for. One per provider; registering again replaces it."""
    _ADOPTERS[provider_kind] = adopter


def _entry_available(entry: dict[str, Any]) -> bool:
    """Whether the provider still sells a live catalog entry for new machines.
    A size with no stock right now is still sold: it reads out of stock to a
    buyer (``alkera_core.compute.stock``), and comes back when the stock
    does, instead of vanishing from the buy list."""
    return int(entry.get("max_count") or 0) > 0


def _describe(machine_type: ComputeMachineType, entry: dict[str, Any]) -> None:
    """Copy the hardware an entry describes onto the type. A field the entry
    leaves out or leaves blank keeps what the type already says: a feed that
    stops describing a card must not erase what an admin or an earlier feed
    recorded."""
    gpu_name = str(entry.get("gpu_name") or "").strip()
    if gpu_name:
        machine_type.gpu_name = gpu_name[:64]
    gpu_memory_gb = _whole(entry.get("gpu_memory_gb"))
    if gpu_memory_gb > 0:
        machine_type.gpu_memory_gb = gpu_memory_gb
    disk_gb = _whole(entry.get("disk_gb"))
    if disk_gb > 0:
        machine_type.disk_gb = disk_gb
    # A flavor sold by the vCPU gives each vCPU a fixed memory: the machine's
    # is its vCPU count times that, the provider's arithmetic rather than a
    # number someone typed.
    ram_per_vcpu = _whole(entry.get("memory_gb_per_vcpu"))
    vcpu = machine_type.vcpu or 0
    if vcpu > 0 and ram_per_vcpu > 0:
        machine_type.memory_gb = vcpu * ram_per_vcpu


def _whole(value: object) -> int:
    """A non-negative whole number from a feed field, 0 when it is not one."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int | float):
        return max(int(value), 0)
    if isinstance(value, str):
        try:
            return max(int(float(value)), 0)
        except ValueError:
            return 0
    return 0


def whole_machine_price(machine_type: ComputeMachineType, per_unit_price_nanos: int) -> int:
    """The provider's whole-machine per-minute price from its per-unit rate: a CPU
    flavor is priced PER vCPU, a GPU flavor PER GPU. Skipping the scaling
    under-records an N-unit machine's cost at a 1-unit rate."""
    if machine_type.compute_class == "cpu":
        return per_unit_price_nanos * max(1, machine_type.vcpu)
    return per_unit_price_nanos * max(1, machine_type.gpu_count)


def _adopt(
    entries: Mapping[str, Mapping[str, Any]], provider_kind: str, *, known: set[str]
) -> list[ComputeMachineType]:
    """The types ``provider_kind``'s adopter builds for the entries the
    catalog does not hold, keyed as the feed names them."""
    ensure_builtin_providers()  # a provider module registers its adopter at import
    adopt = _ADOPTERS.get(provider_kind)
    if adopt is None:
        return []
    out: list[ComputeMachineType] = []
    for code, entry in entries.items():
        fresh = None if code in known else adopt(code, entry)
        if fresh is not None:
            fresh.provider = provider_kind
            fresh.provider_type_id = code
            fresh.available_for_new = False
            out.append(fresh)
    return out


async def refresh_catalog(
    db: AsyncSession,
    *,
    entries: dict[str, dict[str, Any]],
    provider_kind: str,
    now: datetime | None = None,
) -> dict[str, int]:
    """Reconcile the live ``entries`` (from ``ComputeProvider.catalog_entries``)
    onto the stored catalog, adding the entries the provider's adopter takes.
    Returns ``{updated, marked_unavailable, reappeared, added}``.

    ``provider_kind`` scopes it to that provider's rows: ``entries`` came from ONE
    provider's feed, so a row of a DIFFERENT provider is not evidenced by it and
    must not be marked unavailable for being absent — the whole reason a
    per-provider refresh does not blank the other provider's catalog when it runs.

    Pure DB + the passed-in feed — no network — so it is unit-testable. The caller
    fetches ``entries`` (best-effort; on a fetch failure it must NOT call this with
    an empty dict, or every known type would be marked unavailable).
    """
    stamp = now or datetime.now(UTC)
    updated = marked_unavailable = reappeared = 0
    rows = list(
        (
            await db.execute(
                select(ComputeMachineType).where(ComputeMachineType.provider == provider_kind)
            )
        )
        .scalars()
        .all()
    )
    adopted = _adopt(entries, provider_kind, known={mt.provider_type_id for mt in rows})
    db.add_all(adopted)
    rows.extend(adopted)
    for mt in rows:
        entry = entries.get(mt.provider_type_id)
        if entry is None:
            # Absent from the live feed -> not available for new machines (retained).
            if mt.available_for_new:
                mt.available_for_new = False
                mt.availability = "unknown"
                marked_unavailable += 1
            continue
        was_available = mt.available_for_new
        price = int(entry.get("price_nanos") or 0)
        if price > 0:
            mt.provider_price_per_minute_nanos = whole_machine_price(mt, price)
        mt.availability = str(entry.get("availability") or "unknown").upper()
        _describe(mt, entry)
        available = _entry_available(entry)
        mt.available_for_new = available
        mt.synced_at = stamp
        if any(mt is new for new in adopted):
            continue  # counted as added
        if available and not was_available:
            reappeared += 1
        elif not available and was_available:
            marked_unavailable += 1
        else:
            updated += 1
    await db.commit()
    return {
        "updated": updated,
        "marked_unavailable": marked_unavailable,
        "reappeared": reappeared,
        "added": len(adopted),
    }


async def _catalog_sizes(db: AsyncSession, provider_kind: str) -> list[SizeQuery]:
    """The sizes this provider's active catalog rows name, so a provider that
    prices only what it is asked (EC2) knows which instance types to price."""
    rows = (
        await db.execute(
            select(ComputeMachineType.provider_type_id, ComputeMachineType.vcpu).where(
                ComputeMachineType.provider == provider_kind,
                ComputeMachineType.active.is_(True),
            )
        )
    ).all()
    return [SizeQuery(code=code, vcpu=vcpu) for code, vcpu in rows]


async def fetch_and_refresh(
    db: AsyncSession, *, provider: ComputeProvider, provider_kind: str
) -> dict[str, int] | None:
    """Fetch ``provider_kind``'s live catalog via ``provider.catalog_entries()``
    then reconcile it onto that provider's rows.

    Returns None (a no-op) when the provider names no catalog rows and adopts
    none from its feed, or when the fetch fails or comes back empty — the
    stored catalog is left intact rather than blanked, so a provider outage or
    a suspicious empty response never makes every type unavailable. The sizes
    to price are read from the DB, because a provider with no fixed catalog
    (EC2) prices exactly the types the catalog holds."""
    sizes = await _catalog_sizes(db, provider_kind)
    ensure_builtin_providers()
    if not sizes and provider_kind not in _ADOPTERS:
        return None
    try:
        entries = await provider.catalog_entries(sizes)
    except ComputeProviderError:
        return None
    if not entries:
        return None
    return await refresh_catalog(db, entries=entries, provider_kind=provider_kind)


__all__ = [
    "CatalogAdopter",
    "fetch_and_refresh",
    "refresh_catalog",
    "register_catalog_adopter",
    "whole_machine_price",
]
