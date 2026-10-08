"""Whether, and how far, an org machine's disk may grow now: one answer for
the route that grows it, the quote that prices it and the read model that
offers it, so the button, the price and the refusal never disagree.

How a volume grows is :func:`alkera_core.compute.disk.grow_rule`'s to say (the
provider's rule, and for an online grow the box's word that it grows its
filesystem); how big it may get is the offering's choices
(:func:`alkera_core.compute.offering_disks.disk_choices_for`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from alkera_core.compute.disk import DiskSpecError, GrowRule, disk_rules_for, grow_rule
from alkera_core.compute.offering_disks import disk_choices_for
from alkera_core.compute.org_machines import org_machine_state
from alkera_core.schemas.org_machines import DiskGrowRead

from backend.services.compute.org_machine_access import MachineRow, OrgMachineError

#: A machine whose disk may be grown: one that is up, or stopped with its
#: disk kept. Anything between is mid-change and waits.
GROWABLE_STATES = frozenset({"running", "stopped"})

DISKS_ONLY_GROW = "Disks can only grow. Move to new hardware for a smaller one."


@dataclass(frozen=True, slots=True)
class DiskGrowOffer:
    """The sizes the disk may grow to, and how it grows."""

    min_gb: int
    max_gb: int
    grow: GrowRule

    def read(self) -> DiskGrowRead:
        return DiskGrowRead(
            min_gb=self.min_gb, max_gb=self.max_gb, restarts=self.grow == GrowRule.RESTART
        )


def grow_offer(row: MachineRow, *, now: datetime) -> DiskGrowOffer | OrgMachineError:
    """What the machine's disk may grow to now, or why it may not."""
    state, _ = org_machine_state(row.machine, row.allocation, now=now)
    if state not in GROWABLE_STATES:
        return OrgMachineError(
            "machine_busy", "Wait until the machine is running or stopped to grow its disk."
        )
    capabilities = row.allocation.capabilities_json if row.allocation is not None else None
    rule = grow_rule(
        disk_rules_for(row.machine_type.provider, row.machine_type.compute_class), capabilities
    )
    if rule == GrowRule.NEVER:
        return OrgMachineError(
            "disk_cannot_grow",
            "This machine's disk can't grow in place. Move to new hardware for a bigger one.",
        )
    try:
        high = disk_choices_for(row.offering, row.machine_type).volume.max_gb
    except DiskSpecError:
        high = 0
    if high <= row.machine.storage_gb:
        return OrgMachineError(
            "disk_at_max",
            "This machine's disk is as big as it can be. Move to new hardware for a bigger one.",
        )
    return DiskGrowOffer(min_gb=row.machine.storage_gb + 1, max_gb=high, grow=rule)


def check_grow(row: MachineRow, volume_gb: int, *, now: datetime) -> DiskGrowOffer:
    """The offer ``volume_gb`` fits in, or the refusal: a size no larger than
    today's is 422 ``disk_not_larger``, one past the offering 422
    ``storage_out_of_range``, a machine that cannot grow now 409."""
    if volume_gb <= row.machine.storage_gb:
        raise OrgMachineError("disk_not_larger", DISKS_ONLY_GROW, status=422)
    offer = grow_offer(row, now=now)
    if isinstance(offer, OrgMachineError):
        raise offer
    if volume_gb > offer.max_gb:
        raise OrgMachineError(
            "storage_out_of_range",
            f"Storage must be between {offer.min_gb} and {offer.max_gb} GB.",
            status=422,
        )
    return offer


__all__ = ["GROWABLE_STATES", "DiskGrowOffer", "check_grow", "grow_offer"]
