"""Who may connect the deployment's own Slack workspace to an org, and undo it.

The credential in play is the DEPLOYMENT's -- one ``xoxb-`` workspace token the
operator put in the environment -- so pointing it at an org is the platform's
own act on somebody else's tenant, not the tenant's act on itself. The resource
carries no ``org_id`` (the engine's cross-org guard must not fire) and the
authority is platform ADMIN for everything, the read included: support staff
perform audited actions but do not decide whose Slack workspace this is.

The staff check comes before the admin check so the two refusals read
differently in the decision row. Every fact is required on both actions, so a
caller that forgets one is denied rather than allowed by a branch that happened
not to need it.
"""

from __future__ import annotations

from collections.abc import Mapping

from alkera_core.authz.decision import Decision, allow, deny
from alkera_core.authz.engine import Policy, register, require_attr
from alkera_core.authz.enums import Action, ResourceType
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource

POLICY = "platform.org_slack"
AUDITED = frozenset({"platform_staff", "platform_admin", "operation"})

STAFF_MESSAGE = "Platform staff role required"
ADMIN_MESSAGE = "Platform admin role required"

#: READ shows which workspace the org holds; ADMIN connects and disconnects it.
SUPPORTED = frozenset({Action.READ, Action.ADMIN})


def decide(
    ctx: ActingContext, action: Action, resource: Resource, attrs: Mapping[str, object]
) -> Decision | None:
    if action not in SUPPORTED:
        return deny(POLICY, "action_not_supported", message="Not allowed")
    staff = require_attr(attrs, "platform_staff", bool)
    is_admin = require_attr(attrs, "platform_admin", bool)
    require_attr(attrs, "operation", str)
    if not staff:
        return deny(POLICY, "platform_staff_required", message=STAFF_MESSAGE)
    if not is_admin:
        return deny(POLICY, "platform_admin_required", message=ADMIN_MESSAGE)
    return allow(POLICY, "admin_reads" if action is Action.READ else "admin_changes")


register(
    Policy(
        name=POLICY,
        resource_type=ResourceType.PLATFORM_ORG_SLACK,
        decide=decide,
        audited_attrs=AUDITED,
    )
)

__all__ = ["ADMIN_MESSAGE", "AUDITED", "POLICY", "STAFF_MESSAGE", "SUPPORTED", "decide"]
