"""Who may see and change an org's compute grant.

A grant is the platform's own act: it says how many machines an org may run,
what it is billed per minute and until when. It is decided about the PLATFORM,
not about the org — the caller is Alkera staff looking at somebody else's
tenant — so the resource carries no ``org_id`` and the target org arrives as a
fact instead. Reading the grants an org holds is open to the staff floor;
writing or revoking one is platform-admin only, because a grant is the dial
that lets an org spend Alkera's provider money.

Every fact is required on every branch, so a caller that forgets one is denied
rather than allowed by the branch that happened not to need it. An org that
does not exist is an opaque not-found, but only after the caller has proven it
is staff: a stranger must not be able to probe which org ids are real.
"""

from __future__ import annotations

from collections.abc import Mapping

from alkera_core.authz.decision import Decision, allow, deny
from alkera_core.authz.engine import Policy, register, require_attr
from alkera_core.authz.enums import Action, ResourceType
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource

POLICY = "compute.grant"
AUDITED = frozenset({"platform_staff", "platform_admin", "org_exists", "target_org_id"})

NOT_FOUND_MESSAGE = "Organization not found"
STAFF_MESSAGE = "Platform staff role required"
ADMIN_MESSAGE = "Platform admin role required"

SUPPORTED = frozenset({Action.READ, Action.ADMIN})


def decide(
    ctx: ActingContext, action: Action, resource: Resource, attrs: Mapping[str, object]
) -> Decision | None:
    if action not in SUPPORTED:
        return deny(POLICY, "action_not_supported", message="Not allowed")
    staff = require_attr(attrs, "platform_staff", bool)
    is_admin = require_attr(attrs, "platform_admin", bool)
    org_exists = require_attr(attrs, "org_exists", bool)
    require_attr(attrs, "target_org_id", str)
    if not staff:
        return deny(POLICY, "platform_staff_required", message=STAFF_MESSAGE)
    if not org_exists:
        return deny(POLICY, "org_not_found", message=NOT_FOUND_MESSAGE, as_not_found=True)
    if action is Action.READ:
        return allow(POLICY, "staff_read")
    if not is_admin:
        return deny(POLICY, "platform_admin_required", message=ADMIN_MESSAGE)
    return allow(POLICY, "admin_grant")


register(
    Policy(
        name=POLICY,
        resource_type=ResourceType.COMPUTE_GRANT,
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
