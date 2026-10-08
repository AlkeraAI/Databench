"""How much of a box one chat may use, by the plan its org is on.

A chat's vCPU and memory limits come from one of three places, in this order:

1. the org's own override — set by platform staff on the org's settings row,
   the way an enterprise customer's dedicated box is sized;
2. the plan tier — ``free`` / ``plus`` / ``pro`` each get a small, distinct
   figure, so a paying seat gets a bigger sandbox than a free one on the same
   pool box;
3. the box's own default — what a node applies to a chat whose row names no
   figure. On a dedicated box that is a large leak guard (one chat must not
   take the whole machine); on a pool box it is the smallest tier.

An ``enterprise`` org has no tier figure of its own: it runs on a dedicated box
whose leak guard, or its override, is the bound. A tier this table does not
know fails closed to the smallest, never to "no limit".

The server resolves (1) and (2) into the chat row the box reads; the box keeps
(3) as the floor for a row that names nothing, and its pool floor is the
smallest tier from this same table so the two ends cannot disagree.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

#: ``(vcpu, memory_mb)`` per plan tier. Each tier is distinct and small; the
#: figures grow with the plan.
TIER_LIMITS: Final[Mapping[str, tuple[int, int]]] = {
    "free": (1, 2048),
    "plus": (2, 4096),
    "pro": (4, 8192),
}

#: The tier an unknown or missing tier falls closed to.
SMALLEST_TIER: Final = "free"

#: The tier that carries no figure of its own: its bound is the org override
#: or the dedicated box's leak guard.
OVERRIDE_ONLY_TIERS: Final[frozenset[str]] = frozenset({"enterprise"})

#: The floor a box applies to a chat whose row names no limit and that is not
#: on a dedicated box: the smallest tier.
POOL_FLOOR: Final[tuple[int, int]] = TIER_LIMITS[SMALLEST_TIER]

#: The leak guard a dedicated box applies to a chat whose row names no limit.
#: Large on purpose — the box is one org's — but a bound, so a runaway chat
#: cannot take the whole machine from the org's other chats.
DEDICATED_GUARD: Final[tuple[int, int]] = (8, 32768)


def tier_limits(
    tier: str | None, *, table: Mapping[str, tuple[int, int]] | None = None
) -> tuple[int, int] | None:
    """The ``(vcpu, memory_mb)`` a plan tier is entitled to, or ``None`` when
    the tier carries no figure of its own (enterprise: the override or the
    dedicated box's guard decides).

    ``table`` replaces the built-in figures (a deployment's own settings); it
    must still name the smallest tier. A tier the table does not know — a typo,
    a plan this build predates, ``None`` — resolves to the smallest tier's
    figures rather than to no limit."""
    figures = TIER_LIMITS if table is None else table
    key = (tier or "").strip().lower()
    if key in OVERRIDE_ONLY_TIERS:
        return None
    found = figures.get(key)
    if found is None:
        found = figures[SMALLEST_TIER]
    return found


def resolve_limits(
    tier: str | None,
    *,
    override_vcpu: int | None,
    override_memory_mb: int | None,
    table: Mapping[str, tuple[int, int]] | None = None,
) -> tuple[int | None, int | None]:
    """The figures the chat row carries: each of vCPU and memory is the org's
    override where one is set, else the tier's figure, else ``None`` (the box
    decides). The two are resolved independently so an override of one leaves
    the other on its tier."""
    by_tier = tier_limits(tier, table=table)
    tier_vcpu, tier_memory = by_tier if by_tier is not None else (None, None)
    vcpu = override_vcpu if isinstance(override_vcpu, int) and override_vcpu > 0 else tier_vcpu
    memory = (
        override_memory_mb
        if isinstance(override_memory_mb, int) and override_memory_mb > 0
        else tier_memory
    )
    return vcpu, memory


__all__ = [
    "DEDICATED_GUARD",
    "OVERRIDE_ONLY_TIERS",
    "POOL_FLOOR",
    "SMALLEST_TIER",
    "TIER_LIMITS",
    "resolve_limits",
    "tier_limits",
]
