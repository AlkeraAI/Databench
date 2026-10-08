"""Request and response shapes for a team's Allocations tab.

A budget is written as a plain USD decimal string (``"12.50"``) that the
server converts to exact nano-USD, so no client does money arithmetic. A
storage figure is a plain non-negative integer with a stated maximum: the JSON
must carry an integer (``1e3`` and ``1000.0`` are refused, not rounded), the
schema names the largest figure it takes, and a figure past it is a 422 that
says so rather than a row the database cannot hold.

Every cap slot a reader sees carries a ``version`` — a tag of the figure and
its last write, ``"unset"`` for an empty slot. A write sends it back in
``If-Match`` and is refused with a 409 when the slot has moved, so two tabs
editing one cap cannot silently overwrite each other.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, Field
from pydantic_core import PydanticCustomError

from alkera_core.money import MAX_BUDGET_USD, usd_to_nanos
from alkera_core.units import MAX_CEILING_BYTES, format_bytes

#: Digits, optionally a point and up to nine decimals (nano-USD precision).
#: Nothing else: no sign, no exponent, no thousands separator, no currency mark.
_PLAIN_USD = re.compile(r"^[0-9]+(?:\.[0-9]{1,9})?$")


def _plain_usd(value: str) -> str:
    """A USD amount exactly as a person types one. ``-5`` and ``1e3`` are
    refused rather than read as $5 and $1,000; the figure is bounded so it
    can never overflow the column it becomes."""
    text = value.strip()
    if not _PLAIN_USD.match(text):
        raise PydanticCustomError(
            "usd_not_plain", "Enter a plain amount in USD, like 12.50 — digits and a point only."
        )
    if usd_to_nanos(text) > MAX_BUDGET_USD * 1_000_000_000:
        raise PydanticCustomError(
            "budget_too_large",
            "A budget can be at most ${max} per cycle.",
            {"max": f"{MAX_BUDGET_USD:,}"},
        )
    return text


#: A USD amount a money route accepts: a plain non-negative decimal string. The
#: schema names the bound (``x-maximum-usd``) so a client refuses at the same figure.
PlainUsd = Annotated[
    str,
    Field(json_schema_extra={"x-maximum-usd": MAX_BUDGET_USD}),
    AfterValidator(_plain_usd),
]


Resource = Literal["budget", "storage"]
Window = Literal["cycle", "none"]


def _at_most_storage(value: int) -> int:
    if value > MAX_CEILING_BYTES:
        raise PydanticCustomError(
            "storage_too_large",
            "A storage limit can be at most {max}.",
            {"max": format_bytes(MAX_CEILING_BYTES)},
        )
    return value


#: A storage figure in bytes as a route accepts it: a plain non-negative JSON
#: integer, no larger than :data:`MAX_CEILING_BYTES`.
StorageBytes = Annotated[
    int,
    Field(strict=True, ge=0, json_schema_extra={"maximum": MAX_CEILING_BYTES}),
    AfterValidator(_at_most_storage),
]


class TeamAllocationUpdate(BaseModel):
    """Set a team's allowance: ``amount_usd`` for budget, ``limit_bytes`` for storage."""

    amount_usd: PlainUsd | None = Field(default=None, description="USD per cycle, e.g. '50.00'")
    #: The budget in nano-USD, as clients written before ``amount_usd`` send it.
    #: Still honoured so an older client's write is applied rather than
    #: silently ignored; a write carrying both is refused.
    limit_nanos: int | None = Field(
        default=None,
        ge=0,
        le=MAX_BUDGET_USD * 1_000_000_000,
        description="Deprecated: nano-USD per cycle. Send amount_usd.",
    )
    limit_bytes: StorageBytes | None = None
    window: Window = "cycle"

    def budget_nanos(self) -> int | None:
        """The budget this write asks for in nano-USD, whichever field named it."""
        if self.amount_usd is not None:
            return usd_to_nanos(self.amount_usd)
        return self.limit_nanos


class TeamAllocationRead(BaseModel):
    team_id: UUID
    resource: Resource
    limit_nanos: int | None
    limit_bytes: int | None
    window: Window
    version: str
    """The slot's tag after this write — what the next conditional write sends."""


class AllowanceCeiling(BaseModel):
    """The tightest allowance that binds a row from above, and who set it.

    On a team row it is the lowest allocation on the teams ABOVE this one; on
    a member row it is the lowest on this team or any above it. A figure on
    the row past this ceiling is not what binds — the ceiling is."""

    limit: int
    team_id: UUID
    team_name: str


class BudgetAllocation(BaseModel):
    limit_nanos: int | None
    window: Window


class StorageAllocation(BaseModel):
    limit_bytes: int | None


class TeamAllocationRow(BaseModel):
    team_id: UUID
    name: str
    parent_team_id: UUID | None
    depth: int
    budget: BudgetAllocation | None
    storage: StorageAllocation | None
    spent_nanos: int
    budget_version: str
    storage_version: str
    budget_ceiling: AllowanceCeiling | None = None
    storage_ceiling: AllowanceCeiling | None = None
    can_set: bool
    """Whether the caller may set or clear this team's own allowance: an admin
    of a team above it, or the org admin on the org root. False for the team's
    own admin reading their own row — the control is not offered to them."""


class MemberAllocationRow(BaseModel):
    user_id: UUID
    email: str
    name: str
    budget_limit_nanos: int | None
    storage_limit_bytes: int | None
    spent_nanos: int
    budget_version: str
    storage_version: str
    budget_ceiling: AllowanceCeiling | None = None
    storage_ceiling: AllowanceCeiling | None = None


class TeamAllocationTree(BaseModel):
    team: TeamAllocationRow
    sub_teams: list[TeamAllocationRow]
    members: list[MemberAllocationRow]


class MyAllocations(BaseModel):
    """The caller's own allowances summed over their teams; ``None`` = no limit."""

    budget_limit_nanos: int | None
    storage_limit_bytes: int | None


__all__ = [
    "AllowanceCeiling",
    "BudgetAllocation",
    "MemberAllocationRow",
    "MyAllocations",
    "Resource",
    "StorageAllocation",
    "StorageBytes",
    "TeamAllocationRead",
    "TeamAllocationRow",
    "TeamAllocationTree",
    "TeamAllocationUpdate",
    "Window",
]
