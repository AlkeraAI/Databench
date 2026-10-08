"""Whether a machine size is in stock, in the words a buyer reads: one verdict
for the buy list, the purchase, the admin's offerings and a machine that is
waiting for hardware, so none of them can say something the others do not.

The signals, strongest first: a refusal for lack of hardware on a create of
the same machine type (a type is one size: ``cpu5c`` at 8 vCPUs) in the last
:data:`RECENT_REFUSAL`; the provider's live answer for the size
(:mod:`alkera_core.compute.availability`); the word the catalog refresh
stored. A size nobody could ask about is ``unknown``, never in stock: it can
still be bought, and a purchase the provider then refuses says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal

from alkera_core.compute.availability import Availability
from alkera_core.compute.node_reach import callback_refusal

if TYPE_CHECKING:
    from alkera_core.models.compute import ComputeMachineType

StockState = Literal["in_stock", "limited", "out_of_stock", "unknown"]

#: How each provider is named to a person.
PROVIDER_NAMES: Final[dict[str, str]] = {
    "runpod": "RunPod",
    "ec2": "EC2",
    "localdev": "Local box",
}

#: How long a refusal for lack of hardware reads as out of stock: about two
#: of the org machine reconcile's capacity retries.
RECENT_REFUSAL: Final = timedelta(minutes=10)

#: The catalog's stored stock word for a size with no host free.
OUT_OF_STOCK_WORD: Final = "NONE"

_BY_WORD: Final[dict[str, StockState]] = {
    "HIGH": "in_stock",
    "MEDIUM": "in_stock",
    "LOW": "limited",
    OUT_OF_STOCK_WORD: "out_of_stock",
}
_BY_STATUS: Final[dict[str, StockState]] = {
    "available": "in_stock",
    "limited": "limited",
    "unavailable": "out_of_stock",
}


def provider_name(kind: str) -> str:
    return PROVIDER_NAMES.get(kind, kind)


def no_hardware(kind: str, *, vcpu: int, gpu_name: str = "") -> str:
    """Why a size cannot start now, as one sentence: the card for a GPU
    machine, the vCPU count for a CPU one."""
    name = provider_name(kind)
    if gpu_name:
        return f"{name} has no {gpu_name} free right now."
    return f"{name} has no {vcpu} vCPU hosts right now."


@dataclass(frozen=True, slots=True)
class Stock:
    """The verdict for one size."""

    state: StockState
    #: Whether a purchase of it is let through now.
    can_buy: bool
    #: Why not, as a sentence; empty while it can be bought.
    reason: str = ""


def stock_of(
    *,
    provider: str,
    vcpu: int,
    gpu_name: str,
    stored_word: str,
    live: Availability | None = None,
    refused_at: datetime | None = None,
    now: datetime | None = None,
) -> Stock:
    """The verdict for a size. A refusal for lack of hardware within
    :data:`RECENT_REFUSAL` wins: it is what the provider did, not what it
    advertised. Else the provider's live answer, else the word the catalog
    refresh stored."""
    moment = now or datetime.now(UTC)
    state: StockState
    if refused_at is not None and moment - refused_at < RECENT_REFUSAL:
        state = "out_of_stock"
    elif live is not None and live.status in _BY_STATUS:
        state = _BY_STATUS[live.status]
    else:
        state = _BY_WORD.get((stored_word or "").upper(), "unknown")
    if state != "out_of_stock":
        return Stock(state=state, can_buy=True)
    return Stock(
        state=state, can_buy=False, reason=no_hardware(provider, vcpu=vcpu, gpu_name=gpu_name)
    )


def offer_verdict(
    machine_type: ComputeMachineType,
    *,
    node_api_url: str,
    live: Availability | None = None,
    now: datetime | None = None,
) -> Stock:
    """Whether ``machine_type`` can be bought here now, and why not: the stock
    of its size (:func:`stock_of`), and whether a machine of it could connect
    back to this deployment (:func:`~alkera_core.compute.node_reach.callback_refusal`).
    The buy list, the purchase and the admin's offerings all read this."""
    stock = stock_of(
        provider=machine_type.provider,
        vcpu=machine_type.vcpu,
        gpu_name=machine_type.gpu_name if machine_type.gpu_count > 0 else "",
        stored_word=machine_type.availability or "",
        live=live,
        refused_at=machine_type.capacity_refused_at,
        now=now,
    )
    refused = callback_refusal(machine_type.provider, node_api_url)
    if refused is not None:
        return Stock(state=stock.state, can_buy=False, reason=refused)
    return stock


__all__ = [
    "OUT_OF_STOCK_WORD",
    "PROVIDER_NAMES",
    "RECENT_REFUSAL",
    "Stock",
    "StockState",
    "no_hardware",
    "offer_verdict",
    "provider_name",
    "stock_of",
]
