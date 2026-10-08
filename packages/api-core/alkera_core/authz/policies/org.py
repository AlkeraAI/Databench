"""Who may change an organization-wide integration.

Today that is exactly one thing: the Slack workspace an org is connected to.
Connecting one binds every mention in that workspace to this org's chats, and
disconnecting one silently stops a channel people are using — both are org-wide
effects, so the authority is org-wide too: ADMIN of the org ROOT team, and
nothing below it.

"Admin of a team inside the org" is deliberately NOT enough. Permission descent
gives a root admin authority over every team below, but never the reverse, and a
team admin binding the whole organization's Slack workspace would be exactly
that reverse. The route therefore resolves the caller's roles at the root team
and hands the answer in; the policy never walks a chain itself.
"""

from __future__ import annotations

from collections.abc import Mapping

from alkera_core.authz.decision import Decision, allow, deny
from alkera_core.authz.engine import Policy, register, require_attr
from alkera_core.authz.enums import Action, ResourceType
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource

POLICY = "org.integration"

#: The verbs this policy knows. READ is the status read, ADMIN the connect and
#: the disconnect — both are org-wide changes and both take the same authority.
SUPPORTED = frozenset({Action.READ, Action.ADMIN})

AUDITED = frozenset({"integration", "operation", "is_org_root", "org_admin", "org_member"})


def decide(
    ctx: ActingContext, action: Action, resource: Resource, attrs: Mapping[str, object]
) -> Decision | None:
    if action not in SUPPORTED:
        return deny(POLICY, "action_not_supported", message="Not allowed")
    if not require_attr(attrs, "is_org_root", bool):
        # A team id reached a route that only ever decides about the org itself.
        # Answered as not-found: naming a team here is a mistake, not a request
        # to be told which teams exist.
        return deny(
            POLICY,
            "not_the_org_root",
            message="Not found",
            as_not_found=True,
        )
    if action is Action.READ:
        if not require_attr(attrs, "org_member", bool):
            return deny(POLICY, "not_in_org", message="Not found", as_not_found=True)
        # Any member may see WHETHER their org is connected: it is the fact the
        # link flow's refusals already tell them, and hiding it only makes the
        # "ask an org admin" instruction unactionable.
        return allow(POLICY, "org_member_reads")
    if not require_attr(attrs, "org_admin", bool):
        return deny(
            POLICY,
            "not_org_admin",
            message="Only an organization admin can change this integration",
            error_code="org_admin_required",
        )
    return allow(POLICY, "org_admin")


register(Policy(name=POLICY, resource_type=ResourceType.ORG, decide=decide, audited_attrs=AUDITED))

__all__ = ["AUDITED", "POLICY", "SUPPORTED", "decide"]
