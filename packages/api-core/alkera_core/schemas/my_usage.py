"""User-facing (self-service) billing shapes — the `/api/v1/me/*` surface.

OBFUSCATION (server-enforced): the monthly included allotment's absolute numbers
NEVER appear on any user surface. The only usage signal exposed is ``pct_used`` —
the share of this cycle's included allotment consumed, rounded to the nearest 0.5%
— plus the reset date. The other credit classes (prepaid, promotional, on-demand)
ARE shown exactly: each is an explicit grant whose size the user already knows
(a purchase, or a bonus they were told about), so the count leaks nothing about
the allotment. There is deliberately no granted / consumed / included / allotment /
monthly-grant field at the top level, and no per-request credit log. The one place
granted and used figures appear is ``credit_classes`` — per bucket, and never for
the included allowance while ``has_opaque_allowance`` holds. Request *counts* (not credit
costs) are fine to surface. A structural test (``test_billing_obfuscation.py``)
enforces this on the schemas.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from alkera_core.model_catalog import CreditClass


class MyCreditClass(BaseModel):
    """One credit bucket on the caller's own seat, in display credits
    (1 credit = $0.001). The included plan allowance is absent while the
    response's ``has_opaque_allowance`` holds: its granted figure is the allotment."""

    credit_class: CreditClass
    granted_credits: int
    used_credits: int
    """Drawn and settled."""
    reserved_credits: int
    """Held for requests still in flight."""
    remaining_credits: int
    """granted - used - reserved, floored at zero."""


class MyPoolCredit(BaseModel):
    """One shared pool the caller can draw from and has something left in, as
    they are allowed to see it. Pools with nothing available to the caller are
    not listed.

    ``whole_pool`` true (an org admin): the pool's own granted, used and
    remaining, the figures the org billing page shows. False: the caller's own
    share — ``granted_usd`` is the limit set on them there (null when none is),
    ``used_usd`` their spend against it, ``remaining_usd`` what they can still
    draw (null without a limit: a member is never shown the pool's balance)."""

    scope: Literal["org", "team"]
    team_id: UUID | None = None
    team_name: str | None = None
    whole_pool: bool
    granted_usd: str | None = None
    used_usd: str
    remaining_usd: str | None = None


class MyBudgetTerm(BaseModel):
    """One team the caller was placed in, and what it allows them this cycle:
    their own cap there held to the tightest allowance on the team's chain,
    else that allowance."""

    scope: Literal["org", "team"]
    team_id: UUID | None = None
    team_name: str | None = None
    limit_usd: str


class MyBudget(BaseModel):
    """The caller's summed budget: the terms of the teams they were placed in,
    added up — the figure the gateway refuses a draw past, counted against
    their spend from every pool since the org's cycle began."""

    limit_usd: str
    used_usd: str
    remaining_usd: str
    resets_at: datetime | None = None
    terms: list[MyBudgetTerm]


class MyPoolLimit(BaseModel):
    """The caller's standing on one shared pool."""

    scope: Literal["org", "team"]
    team_id: UUID | None = None
    team_name: str | None = None
    limit_usd: str | None = None
    """What bounds the caller on this pool: their own cap on it, held to the
    tightest allowance on the team's chain; null when neither is set."""
    used_usd: str
    remaining_usd: str | None = None
    resets_at: datetime | None = None


class MyCreditsResponse(BaseModel):
    """The caller's own plan + usage, obfuscated. Mirrors the ``/billing`` summary's
    usage fields (one shared server helper computes both)."""

    tier_key: str
    tier_name: str
    pct_used: float
    """% of this cycle's included allotment consumed, rounded to the nearest 0.5%."""
    reset_at: datetime | None = None
    """When the included allotment next resets (the cycle boundary, UTC). Null when
    the plan grants no included credits."""
    prepaid_credits: int
    """EXACT remaining prepaid (purchased) credits."""
    promotional_credits: int = 0
    """EXACT remaining promotional (bonus) credits — granted, not purchased."""
    on_demand_credits: int = 0
    """EXACT remaining on-demand credits."""
    admin_cycle_credits: int = 0
    """EXACT remaining credits an Alkera admin granted for this billing cycle."""
    admin_permanent_credits: int = 0
    """EXACT remaining credits an Alkera admin granted that never expire."""
    has_opaque_allowance: bool = False
    """The seat holds a plan allowance this cycle (Free / Plus / Pro, or an
    enterprise seat Alkera assigned an allotment): clients draw its usage as the
    ``pct_used`` bar, never as money. Stays true on an org-managed seat, where
    ``opaque_usage`` is false because every OTHER figure reads as money. False:
    there is no allowance and no bar."""
    credit_classes: list[MyCreditClass] = []
    """Every bucket the caller's seat holds, in burn order, with what was granted,
    used and is left. The included allowance is left out exactly while
    ``has_opaque_allowance`` holds (it is the percentage bar)."""
    opaque_usage: bool = False
    """The seat is on a self-serve plan and holds its allowance this cycle, so
    ``pct_used`` means something and clients show it as a share. False (an
    org-managed enterprise seat, or no allowance): every figure is shown as money."""
    billing_mode: Literal["stripe", "managed", "byok"] = "managed"
    """The deployment's commercial posture. Under ``byok`` the obfuscated %-bar is
    meaningless (there is no allotment) — clients show raw local spend instead."""

    managed_by_org: bool = False
    """The seat is covered by the org's Enterprise plan — CLI/VSC clients hide
    self-serve plan affordances (mirrors the /billing summary's flag)."""

    plan_tier: str = "free"
    """The tier key the caller's org is on (``enterprise`` when org-managed)."""
    cycle_resets_at: datetime | None = None
    """When the caller's cycle next rolls: the seat's period end, else the
    earliest reset of a monthly cap on them. Null when nothing rolls."""
    cycle_used_usd: str = "0.000000"
    """What the caller spent this billing cycle, in USD, whatever paid for it —
    plan allowance, purchased or granted credit, the org pool or a team pool —
    whether or not a limit is set on them. The cycle is the one
    ``cycle_resets_at`` ends. A seat that holds a plan allowance
    leaves out what that allowance paid: that spend beside ``pct_used`` would
    give away the allowance's size. What the seat's granted, promotional or
    purchased credit paid still counts."""
    drawable_pools: list[MyPoolCredit] = []
    """The shared pools the caller can draw from that still hold something for
    them — the org pool first, then team pools — with granted / used / remaining
    as the caller may see them (see :class:`MyPoolCredit`)."""
    limits: list[MyPoolLimit] = []
    """One entry per pool the caller can draw from — the org pool and every
    team pool they are a member of — with the cap set on THEM there, if any."""
    budget: MyBudget | None = None
    """The one ceiling the gateway holds the caller's spend to across every
    pool, or null when nothing bounds them. Every surface that shows a
    remaining share reads THIS, never a fold of ``limits`` — the two would
    disagree the moment a cap sits on an ancestor team."""


class MyUsageModel(BaseModel):
    """Per-model request activity. Request COUNT only on Alkera-billed surfaces;
    under BYOK the raw local spend (provider list cost, nano-USD) is disclosed."""

    model_id: str
    request_count: int
    cost_nanos: int | None = None
    """Raw spend at provider list cost — BYOK only (None everywhere else, so the
    SaaS obfuscation posture is unchanged)."""


class MyUsageBucket(BaseModel):
    """Per-day request activity (see :class:`MyUsageModel` for the BYOK cost field)."""

    date: str
    request_count: int
    cost_nanos: int | None = None


class MyUsageResponse(BaseModel):
    """Request-activity breakdown for a window. On Alkera-billed deployments it
    carries no credit amounts (usage is surfaced as ``pct_used`` via the billing
    summary). Under BYOK there is nothing to obfuscate — the customer pays their
    provider directly — so raw spend at provider list cost is included."""

    window: str
    total_requests: int
    by_model: list[MyUsageModel]
    daily: list[MyUsageBucket]
    total_cost_nanos: int | None = None
    """Raw window spend at provider list cost — BYOK only (None otherwise)."""


class MemberPlanRead(BaseModel):
    """A team admin's read-only view of a member's plan: tier + % used + reset,
    plus the recurring cap that admin set on the team pool.

    Deliberately NOT prepaid or any exact credit count — a manager sees how close a
    member is to their cycle limit, never another user's exact balances. The cap is
    the exception and is not an exception to that rule: it is a figure the reading
    admin (or one above them) wrote, so reading it back reveals nothing they did not
    already decide, and a write-only control is a control nobody can audit."""

    tier_key: str
    tier_name: str
    pct_used: float
    reset_at: datetime | None = None
    pool_limit_nanos: int | None = None
    """The member's recurring cap on this team's pool. None = uncapped (they may
    draw the whole pool, bounded only by its balance)."""
    pool_limit_consumed_nanos: int | None = None
    """Drawn against that cap in the current window, summed over every bucket the
    cap binds. None exactly when ``pool_limit_nanos`` is None."""
