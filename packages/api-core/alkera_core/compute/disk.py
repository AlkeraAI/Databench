"""How big a machine's disks may be, what the buyer may choose, and how much of
the volume one org gets: one set of rules for every provider.

A machine has two disks. The container disk holds the image and scratch and
does not survive a stop; the volume (``/opt/alkera-work``) holds every org's
root and the chats' trees, and does. Each provider states its bounds for both
once, in its own module (``DISK_RULES``), per compute class. An
offering narrows the volume bounds (``storage_gb_default`` and
``storage_gb_max``); :func:`disk_choices` is the intersection the buyer is
offered, computed here and nowhere else. The web and the CLI render it.

:func:`org_slot_bytes` is an org's hard share of a box's volume, the same
arithmetic the box supervisor enforces with a project quota, so the server can
refuse a size before any money moves. On a box that holds only one org (one
bought for it, or one that cannot keep orgs apart), that org gets the whole
volume it paid for.

Standard library only, beside the box isolation vocabulary: the box's root
process imports this.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from alkera_core.compute.box_contract import BoxCapability
from alkera_core.compute.box_isolation import IsolationProfile
from alkera_core.compute.tenancy import POOL_TENANCY

#: The smallest share an org is given when a volume is split.
MIN_ORG_SLOT_BYTES: Final = 1 << 30
#: The tenancy the bootstrap hands the box.
ENV_MACHINE_TENANCY: Final = "ALKERA_MACHINE_TENANCY"
#: The deployment override of every org's share, in MiB.
ENV_ORG_DISK_QUOTA_MB: Final = "ALKERA_ORG_DISK_QUOTA_MB"
#: How full a machine's volume is when it reads "almost out of disk".
DISK_FULL_FRACTION: Final = 0.95


class GrowRule(StrEnum):
    """What growing a volume needs on a provider."""

    #: In place, while the machine runs: an EBS volume, then the box's
    #: ``resize2fs`` (only on a box that names ``DISK_GROW_ONLINE``).
    ONLINE = "online"
    #: Only stopped: the machine is drained, stopped, grown and started.
    RESTART = "restart"
    #: Not at all; a bigger disk is new hardware.
    NEVER = "never"


@dataclass(frozen=True, slots=True)
class DiskBounds:
    """The sizes one disk may take, in GB."""

    min_gb: int
    max_gb: int
    default_gb: int

    def __post_init__(self) -> None:
        if not 0 <= self.min_gb <= self.default_gb <= self.max_gb:
            raise ValueError(
                f"disk bounds must satisfy 0 <= min <= default <= max, got "
                f"{self.min_gb}, {self.default_gb}, {self.max_gb}"
            )

    def admits(self, size_gb: int) -> bool:
        return self.min_gb <= size_gb <= self.max_gb


@dataclass(frozen=True, slots=True)
class DiskRules:
    """One provider's disk facts for one compute class."""

    container: DiskBounds
    volume: DiskBounds
    #: Whether the provider bills the volume while the machine is stopped
    #: (RunPod does; the quote says so before purchase).
    volume_billed_while_stopped: bool
    grow: GrowRule


#: What a built-in provider module declares, as ``DISK_RULES``: its rules per
#: ``(kind, compute class)``.
DiskRulesTable = Mapping[tuple[str, str], DiskRules]

#: Rules registered from outside the built-in providers (an extension, a test
#: double). They win over a built-in's for the same pair.
_RULES: dict[tuple[str, str], DiskRules] = {}


def register_disk_rules(kind: str, compute_class: str, rules: DiskRules) -> None:
    """State disk rules for a provider that is not built in, or replace a
    built-in's. A later registration for the same pair replaces the earlier."""
    _RULES[(kind, compute_class)] = rules


def _all_rules() -> dict[tuple[str, str], DiskRules]:
    """Every rule in force, read from the built-in provider modules each time
    rather than left by a side effect of their import: the answer is whole
    whatever imported what first, and whichever copy of this module asks."""
    from alkera_core.compute.provider import builtin_provider_modules

    rules: dict[tuple[str, str], DiskRules] = {}
    for module in builtin_provider_modules():
        declared: DiskRulesTable = getattr(module, "DISK_RULES", {})
        rules.update(declared)
    rules.update(_RULES)
    return rules


def registered_disk_pairs() -> frozenset[tuple[str, str]]:
    """Every provider and compute class that stated its disk rules."""
    return frozenset(_all_rules())


def disk_rules_for(kind: str, compute_class: str) -> DiskRules | None:
    """The rules for ``kind`` and ``compute_class``, or ``None`` for a provider
    that stated none (its offerings are then bounded by the offering alone)."""
    return _all_rules().get((kind, compute_class))


def grow_rule(rules: DiskRules | None, capabilities: Collection[str] | None) -> GrowRule:
    """How one machine's volume may grow: its provider's rule, except that an
    online grow needs a box that grows its filesystem into the bigger volume
    (:attr:`BoxCapability.DISK_GROW_ONLINE`). A box that never said so (an
    older build, or one that has not beaten since it started) is not grown:
    the org would pay for space its filesystem cannot reach."""
    if rules is None:
        return GrowRule.NEVER
    if rules.grow == GrowRule.ONLINE and BoxCapability.DISK_GROW_ONLINE not in (capabilities or ()):
        return GrowRule.NEVER
    return rules.grow


@dataclass(frozen=True, slots=True)
class DiskChoices:
    """What a buyer may choose for one offering: the server's numbers, which
    the clients render and never derive."""

    volume: DiskBounds
    container_gb: int
    volume_billed_while_stopped: bool
    grow: GrowRule


class DiskSpecError(ValueError):
    """An offering's volume bounds fall outside its provider's."""


def check_offering_bounds(
    *, storage_gb_default: int, storage_gb_max: int, rules: DiskRules | None
) -> None:
    """Refuse (:class:`DiskSpecError`) offering bounds the provider cannot make:
    a default outside its volume bounds, or a maximum past its ceiling. An
    admin's save asks this; a buyer's choices are only ever narrowed."""
    if rules is None:
        return
    if not rules.volume.admits(storage_gb_default) or storage_gb_max > rules.volume.max_gb:
        raise DiskSpecError(
            f"This provider makes volumes from {rules.volume.min_gb} to {rules.volume.max_gb} GB."
        )


def disk_choices(
    *, storage_gb_default: int, storage_gb_max: int, rules: DiskRules | None
) -> DiskChoices:
    """The intersection of an offering's volume bounds with its provider's.
    The provider's minimum is the floor (an offering's default is a default,
    not a minimum); its maximum is the ceiling. Raises
    :class:`DiskSpecError` when the two do not overlap, which an admin save
    checks first."""
    if rules is None:
        # A provider that stated nothing: the offering's own bounds, the volume
        # quoted as billed while stopped, and no growing in place.
        return DiskChoices(
            volume=DiskBounds(1, storage_gb_max, storage_gb_default),
            container_gb=0,
            volume_billed_while_stopped=True,
            grow=GrowRule.NEVER,
        )
    low = rules.volume.min_gb
    high = min(storage_gb_max, rules.volume.max_gb)
    if not low <= storage_gb_default <= high:
        raise DiskSpecError(
            f"the offering's default of {storage_gb_default} GB is outside the provider's "
            f"{rules.volume.min_gb} to {rules.volume.max_gb} GB"
        )
    return DiskChoices(
        volume=DiskBounds(low, high, storage_gb_default),
        container_gb=rules.container.default_gb,
        volume_billed_while_stopped=rules.volume_billed_while_stopped,
        grow=rules.grow,
    )


def orgs_on_box(profile: IsolationProfile, *, tenancy: str | None, workers: int) -> int:
    """How many orgs a box may hold at once. Only a pool box is shared; an
    org's own box, one bought for it and a person's box each hold one org, as
    does a box that cannot keep orgs apart (``single_org``). A box that does
    not know its tenancy is read as shared: the smaller share, never another
    org's room."""
    if profile == IsolationProfile.SINGLE_ORG or (tenancy is not None and tenancy != POOL_TENANCY):
        return 1
    return max(1, workers)


def disk_nearly_full(resources: Mapping[str, object] | None) -> bool:
    """Whether the volume a box reported on its last beat is
    :data:`DISK_FULL_FRACTION` full or more. For an org's machine the org is
    alone on the box, so its share is the whole volume (:func:`orgs_on_box`)
    and the volume's fill is the org's. A beat with no disk figures (an older
    box, a box that could not read its volume) is never full."""
    used = (resources or {}).get("disk_used_bytes")
    total = (resources or {}).get("disk_total_bytes")
    if not isinstance(used, int) or not isinstance(total, int) or total <= 0:
        return False
    return used >= total * DISK_FULL_FRACTION


def org_slot_bytes(disk_total: int, *, orgs: int, env: Mapping[str, str] | None = None) -> int:
    """One org's hard share of a box's volume: what the deployment names, else
    an even share across the ``orgs`` the box may hold
    (:func:`orgs_on_box`), never below :data:`MIN_ORG_SLOT_BYTES`. A box that
    holds one org gives it the whole volume it paid for."""
    raw = (env or {}).get(ENV_ORG_DISK_QUOTA_MB, "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw) << 20
    return max(MIN_ORG_SLOT_BYTES, disk_total // max(1, orgs))


__all__ = [
    "DISK_FULL_FRACTION",
    "ENV_MACHINE_TENANCY",
    "ENV_ORG_DISK_QUOTA_MB",
    "MIN_ORG_SLOT_BYTES",
    "DiskBounds",
    "DiskChoices",
    "DiskRules",
    "DiskRulesTable",
    "DiskSpecError",
    "GrowRule",
    "check_offering_bounds",
    "disk_choices",
    "disk_nearly_full",
    "disk_rules_for",
    "grow_rule",
    "org_slot_bytes",
    "orgs_on_box",
    "register_disk_rules",
    "registered_disk_pairs",
]
