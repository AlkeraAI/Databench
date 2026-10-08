"""Who may mint, see, revoke and assign the platform's chat boxes.

A platform box is the operator's own machine: minting its credential, taking
it away, and pointing an org's chats at one box are the platform's acts on the
tenants it serves, so the resource carries no ``org_id`` (the engine's
cross-org guard must not fire) and the authority is platform ADMIN for every
change. Reading the machines page — which box is up, how many chats it holds,
which org it serves — is open to the staff floor, because support answers
"why is this org's chat not starting" from it.

An assignment names an org, and one that does not exist is an opaque
not-found, but only after the caller has proven it is staff: a stranger must
not be able to probe which org ids are real. Operations that name no org pass
``org_exists=True`` — there is no org to be missing. Every fact is required on
every branch, so a caller that forgets one is denied rather than allowed by a
branch that happened not to need it.
"""

from __future__ import annotations

from collections.abc import Mapping

from alkera_core.authz.decision import Decision, allow, deny
from alkera_core.authz.engine import Policy, register, require_attr
from alkera_core.authz.enums import Action, ResourceType
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource

POLICY = "platform.machine"
AUDITED = frozenset({"platform_staff", "platform_admin", "operation", "org_exists"})

NOT_FOUND_MESSAGE = "Organization not found"
STAFF_MESSAGE = "Platform staff role required"
ADMIN_MESSAGE = "Platform admin role required"

#: READ lists the boxes and an org's assignment; ADMIN mints, revokes,
#: assigns and unassigns.
SUPPORTED = frozenset({Action.READ, Action.ADMIN})


def decide(
    ctx: ActingContext, action: Action, resource: Resource, attrs: Mapping[str, object]
) -> Decision | None:
    if action not in SUPPORTED:
        return deny(POLICY, "action_not_supported", message="Not allowed")
    staff = require_attr(attrs, "platform_staff", bool)
    is_admin = require_attr(attrs, "platform_admin", bool)
    require_attr(attrs, "operation", str)
    org_exists = require_attr(attrs, "org_exists", bool)
    if not staff:
        return deny(POLICY, "platform_staff_required", message=STAFF_MESSAGE)
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
        resource_type=ResourceType.PLATFORM_MACHINE,
        decide=decide,
        audited_attrs=AUDITED,
    )
)

__all__ = [
    "ADMIN_MESSAGE",
    "AUDITED",
    "NOT_FOUND_MESSAGE",
    "POLICY",
    "STAFF_MESSAGE",
    "SUPPORTED",
    "decide",
]
