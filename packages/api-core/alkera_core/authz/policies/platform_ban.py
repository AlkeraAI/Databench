"""Who may see and change the platform's bans.

A ban shuts a person — or every address at a domain — out of the whole product,
and the person on the other side is answered as if they never existed. That is
the platform's own act on somebody else's tenant, so the resource carries no
``org_id`` (the engine's cross-org guard must not fire) and the authority is
platform ADMIN for everything, the register included: support staff perform
audited actions but do not see or change who is shut out.

The staff check comes before the admin check so the two refusals read
differently in the decision row — a tenant reaching the route at all is
recorded as such, and a support caller as "admin required". Every fact is
required on both actions, so a caller that forgets one is denied rather than
allowed by a branch that happened not to need it. Refusals that are about the
TARGET rather than the caller — banning oneself, banning a staff account —
are input errors the route answers as 422; they are not authority questions
and do not live here.
"""

from __future__ import annotations

from collections.abc import Mapping

from alkera_core.authz.decision import Decision, allow, deny
from alkera_core.authz.engine import Policy, register, require_attr
from alkera_core.authz.enums import Action, ResourceType
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource

POLICY = "platform.ban"
AUDITED = frozenset({"platform_staff", "platform_admin", "kind", "operation"})

STAFF_MESSAGE = "Platform staff role required"
ADMIN_MESSAGE = "Platform admin role required"

#: READ lists a register; ADMIN bans and lifts.
SUPPORTED = frozenset({Action.READ, Action.ADMIN})


def decide(
    ctx: ActingContext, action: Action, resource: Resource, attrs: Mapping[str, object]
) -> Decision | None:
    if action not in SUPPORTED:
        return deny(POLICY, "action_not_supported", message="Not allowed")
    staff = require_attr(attrs, "platform_staff", bool)
    is_admin = require_attr(attrs, "platform_admin", bool)
    require_attr(attrs, "kind", str)
    require_attr(attrs, "operation", str)
    if not staff:
        return deny(POLICY, "platform_staff_required", message=STAFF_MESSAGE)
    if not is_admin:
        return deny(POLICY, "platform_admin_required", message=ADMIN_MESSAGE)
    return allow(POLICY, "admin_reads" if action is Action.READ else "admin_changes")


register(
    Policy(
        name=POLICY,
        resource_type=ResourceType.PLATFORM_BAN,
        decide=decide,
        audited_attrs=AUDITED,
    )
)

__all__ = ["ADMIN_MESSAGE", "AUDITED", "POLICY", "STAFF_MESSAGE", "SUPPORTED", "decide"]
