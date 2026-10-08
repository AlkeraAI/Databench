"""Who may see and set the machines Alkera sells, and give an org a machine.

The catalog of offerings is the platform's own: which hardware it sells, at
what price, to whom. Writing it (creating an offering, changing its price,
retiring it) and giving an org a machine free are platform ADMIN acts; reading
the catalog with its provider prices, and an org's machines from the platform
console, is open to the staff floor, because support answers "why can this
org not buy a GPU" from it. Those resources carry no ``org_id``: the decision
is about the platform, so the engine's cross-org guard must not fire.

One operation is a tenant's: ``browse``, the list of offerings an org may buy.
Its resource carries the caller's org, and any member of that org with a
verified email may read it (people choosing a machine for a workspace read the
specs, not only the admins who buy).

Every operation names the facts it needs, and every one of them is required:
a caller that forgets one is denied rather than allowed by a branch that
happened not to read it. An operation is also bound to its action, so a READ
can never be stretched into a catalog write by naming a write operation.
"""

from __future__ import annotations

from collections.abc import Mapping

from alkera_core.authz.decision import Decision, allow, deny
from alkera_core.authz.engine import Policy, register, require_attr
from alkera_core.authz.enums import Action, ResourceType
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource

POLICY = "compute.offering"

#: The tenant's read: the offerings the caller's org may buy.
BROWSE = "browse"
#: The platform console: the catalog with provider prices.
LIST = "list"
CREATE = "create"
UPDATE = "update"
#: An org's machines, read from the platform console.
ORG_MACHINES = "org_machines"
#: Give an org a machine, free until a date.
GRANT = "grant"

#: Which action each operation is decided under.
OPERATIONS: Mapping[str, Action] = {
    BROWSE: Action.READ,
    LIST: Action.READ,
    ORG_MACHINES: Action.READ,
    CREATE: Action.ADMIN,
    UPDATE: Action.ADMIN,
    GRANT: Action.ADMIN,
}

AUDITED = frozenset(
    {
        "operation",
        "platform_staff",
        "platform_admin",
        "org_exists",
        "in_org",
        "is_member",
        "email_verified",
    }
)

NOT_FOUND_MESSAGE = "Organization not found"
STAFF_MESSAGE = "Platform staff role required"
ADMIN_MESSAGE = "Platform admin role required"
MEMBER_MESSAGE = "org member role required"
VERIFY_EMAIL_MESSAGE = "Verify your email address to perform this action."
VERIFY_EMAIL_CODE = "email_verification_required"

SUPPORTED = frozenset({Action.READ, Action.ADMIN})


def decide(
    ctx: ActingContext, action: Action, resource: Resource, attrs: Mapping[str, object]
) -> Decision | None:
    if action not in SUPPORTED:
        return deny(POLICY, "action_not_supported", message="Not allowed")
    operation = require_attr(attrs, "operation", str)
    if OPERATIONS.get(operation) is not action:
        return deny(POLICY, "operation_not_supported", message="Not allowed")
    if operation == BROWSE:
        return _browse(attrs)
    return _platform(action, attrs)


def _browse(attrs: Mapping[str, object]) -> Decision:
    in_org = require_attr(attrs, "in_org", bool)
    is_member = require_attr(attrs, "is_member", bool)
    email_verified = require_attr(attrs, "email_verified", bool)
    if not in_org:
        return deny(POLICY, "not_in_org", message=NOT_FOUND_MESSAGE, as_not_found=True)
    if not is_member:
        return deny(POLICY, "org_member_required", message=MEMBER_MESSAGE)
    if not email_verified:
        return deny(
            POLICY,
            "email_verification_required",
            message=VERIFY_EMAIL_MESSAGE,
            error_code=VERIFY_EMAIL_CODE,
        )
    return allow(POLICY, "member_browses")


def _platform(action: Action, attrs: Mapping[str, object]) -> Decision:
    staff = require_attr(attrs, "platform_staff", bool)
    is_admin = require_attr(attrs, "platform_admin", bool)
    org_exists = require_attr(attrs, "org_exists", bool)
    if not staff:
        return deny(POLICY, "platform_staff_required", message=STAFF_MESSAGE)
    # Only after the caller proved it is staff: a stranger must not learn
    # which org ids are real from the status code.
    if not org_exists:
        return deny(POLICY, "org_not_found", message=NOT_FOUND_MESSAGE, as_not_found=True)
    if action is Action.READ:
        return allow(POLICY, "staff_read")
    if not is_admin:
        return deny(POLICY, "platform_admin_required", message=ADMIN_MESSAGE)
    return allow(POLICY, "admin_changes")


register(
    Policy(
        name=POLICY,
        resource_type=ResourceType.COMPUTE_OFFERING,
        decide=decide,
        audited_attrs=AUDITED,
    )
)

__all__ = [
    "ADMIN_MESSAGE",
    "AUDITED",
    "BROWSE",
    "CREATE",
    "GRANT",
    "LIST",
    "MEMBER_MESSAGE",
    "NOT_FOUND_MESSAGE",
    "OPERATIONS",
    "ORG_MACHINES",
    "POLICY",
    "STAFF_MESSAGE",
    "SUPPORTED",
    "UPDATE",
    "VERIFY_EMAIL_CODE",
    "VERIFY_EMAIL_MESSAGE",
    "decide",
]
