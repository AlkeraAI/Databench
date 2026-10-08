"""Who may move Alkera's own money by hand.

Credit granted to a customer, a standing grant that repeats it, a spend cap on
a pool member, a user's usage refunded and erased, and the prices a model is
sold at or costs Alkera — every one of these is spent or charged against
Alkera's provider keys, so the authority is platform ADMIN. Support staff read
every figure and perform every other audited action, but do not decide what a
customer is given or billed.

The resource carries no ``org_id``: it is the platform's act on its own money,
not the tenant's, so the engine's cross-org guard must not fire. The staff
check comes before the admin check so the two refusals read differently in the
decision row. The operation is a closed vocabulary (:class:`Operation`): a
money write the policy has not been told about is refused rather than filed
under a name nobody can search the decisions for, so adding one means naming it
here first.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from alkera_core.authz.decision import Decision, allow, deny
from alkera_core.authz.engine import Policy, register, require_attr
from alkera_core.authz.enums import Action, ResourceType
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource

POLICY = "platform.billing"
AUDITED = frozenset({"platform_staff", "platform_admin", "operation"})

STAFF_MESSAGE = "Platform staff role required"
ADMIN_MESSAGE = "Platform admin role required"


class Operation(StrEnum):
    """The money writes the platform performs by hand, one name each."""

    GRANT = "grant"
    RECURRING_GRANT_CREATE = "recurring_grant_create"
    RECURRING_GRANT_UPDATE = "recurring_grant_update"
    RECURRING_GRANT_DELETE = "recurring_grant_delete"
    MEMBER_CAP_SET = "member_cap_set"
    MEMBER_CAP_CLEAR = "member_cap_clear"
    SELL_PRICE_SET = "sell_price_set"
    SELL_PRICE_DELETE = "sell_price_delete"
    PROVIDER_COST_SET = "provider_cost_set"
    PROVIDER_COST_DELETE = "provider_cost_delete"
    #: Refund a user's model usage to the balances it drew on and erase its
    #: records — money handed back, so the same authority as a grant.
    USAGE_RESET = "usage_reset"


OPERATIONS: frozenset[str] = frozenset(op.value for op in Operation)

#: Every money write is the one ADMIN verb; reads stay on the staff floor and
#: never reach this policy.
SUPPORTED = frozenset({Action.ADMIN})


def decide(
    ctx: ActingContext, action: Action, resource: Resource, attrs: Mapping[str, object]
) -> Decision | None:
    if action not in SUPPORTED:
        return deny(POLICY, "action_not_supported", message="Not allowed")
    staff = require_attr(attrs, "platform_staff", bool)
    is_admin = require_attr(attrs, "platform_admin", bool)
    operation = require_attr(attrs, "operation", str)
    if operation not in OPERATIONS:
        return deny(POLICY, "unknown_operation", message="Not allowed")
    if not staff:
        return deny(POLICY, "platform_staff_required", message=STAFF_MESSAGE)
    if not is_admin:
        return deny(POLICY, "platform_admin_required", message=ADMIN_MESSAGE)
    return allow(POLICY, "admin_moves_money")


register(
    Policy(
        name=POLICY,
        resource_type=ResourceType.PLATFORM_BILLING,
        decide=decide,
        audited_attrs=AUDITED,
    )
)

__all__ = [
    "ADMIN_MESSAGE",
    "AUDITED",
    "OPERATIONS",
    "POLICY",
    "STAFF_MESSAGE",
    "SUPPORTED",
    "Operation",
    "decide",
]
