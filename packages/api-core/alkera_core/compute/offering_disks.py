"""An offering's disk choices: its own volume bounds narrowed by its provider's
(:mod:`alkera_core.compute.disk`). The buyer's list, the quote, the purchase,
a grant and an admin's save all ask here, so no two of them can disagree on
what may be bought."""

from __future__ import annotations

from alkera_core.compute.disk import DiskChoices, DiskSpecError, disk_choices, disk_rules_for
from alkera_core.models.compute import ComputeMachineType
from alkera_core.models.compute_offerings import ComputeOffering


def disk_choices_for(offering: ComputeOffering, machine_type: ComputeMachineType) -> DiskChoices:
    """What a buyer of ``offering`` may choose. Raises
    :class:`~alkera_core.compute.disk.DiskSpecError` for an offering whose
    bounds its provider cannot make."""
    return disk_choices(
        storage_gb_default=offering.storage_gb_default,
        storage_gb_max=offering.storage_gb_max,
        rules=disk_rules_for(machine_type.provider, machine_type.compute_class),
    )


def storage_refusal(
    offering: ComputeOffering, machine_type: ComputeMachineType, storage_gb: int
) -> str | None:
    """Why a volume of ``storage_gb`` cannot be bought on ``offering``, in the
    words the buyer reads, or ``None`` when it can."""
    try:
        choices = disk_choices_for(offering, machine_type)
    except DiskSpecError:
        return "This machine can't be bought with any disk size right now."
    if choices.volume.admits(storage_gb):
        return None
    return f"Storage must be between {choices.volume.min_gb} and {choices.volume.max_gb} GB."


__all__ = ["disk_choices_for", "storage_refusal"]
