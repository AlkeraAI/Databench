"""Who may read a team's usage analytics.

One team's request and spend figures are the reading its own admins are
accountable for, so authority is team admin — and admin descends the ancestor
chain, so an admin of the team or of anything above it (the org root included)
reads it while a sibling team's admin, who holds no role on it, does not.

The org-wide reading is the same decision taken at the org ROOT team: an org
admin is by definition an admin of the root, so the route addresses the root
when no team is named and one branch table covers both scopes.

A team outside the caller's org is refused as not-found before any role is
consulted: naming a foreign team is a mistake, not a request to be told which
organizations have which teams.
"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Set as AbstractSet

from alkera_core.authz.decision import Decision, allow, deny
from alkera_core.authz.engine import MissingAttributeError, Policy, register, require_attr
from alkera_core.authz.enums import Action, ResourceType, Role, expand_roles
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource

POLICY = "team.usage_read"

#: The only verb this policy knows. Changing a team is decided elsewhere; this
#: is the analytics read and nothing else.
SUPPORTED = frozenset({Action.READ})

AUDITED = frozenset({"in_org", "roles", "scope"})


def _roles(attrs: Mapping[str, object]) -> frozenset[Role]:
    """The subject's roles on the team. A collection of plain strings is a
    caller bug and reads as missing."""
    held = attrs.get("roles")
    if not isinstance(held, AbstractSet) or not all(isinstance(role, Role) for role in held):
        raise MissingAttributeError("roles")
    return frozenset(held)


def decide(
    ctx: ActingContext, action: Action, resource: Resource, attrs: Mapping[str, object]
) -> Decision | None:
    if action not in SUPPORTED:
        return deny(POLICY, "action_not_supported", message="Not allowed")
    if not require_attr(attrs, "in_org", bool):
        return deny(POLICY, "team_not_in_org", message="Team not found", as_not_found=True)
    if Role.ADMIN not in expand_roles(_roles(attrs)):
        return deny(POLICY, "team_admin_required", message="team admin role required")
    return allow(POLICY, "team_admin_read")


register(Policy(name=POLICY, resource_type=ResourceType.TEAM, decide=decide, audited_attrs=AUDITED))

__all__ = ["AUDITED", "POLICY", "SUPPORTED", "decide"]
