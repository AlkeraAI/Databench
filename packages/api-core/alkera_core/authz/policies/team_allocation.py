"""Who may set or remove a team's own allowance of budget or storage.

A team's allowance is the ceiling every cap written inside the team is held to,
so it is set from ABOVE the team: an admin of a team above this one (admin
descends, so the org admin is always one). A team's own admin may ration what
the team was given but never raise the team's allowance itself. The org root
has nothing above it, so its own admins — the org admins — set the root's.

The ceiling descends further than one level: a team's allowance may not exceed
the tightest allowance on any team above it (for storage, the org's own ceiling
counts as the top of that chain), whoever writes it. An admin who may raise the
team above raises it first — the refusal names that team and its figure.

Precedence matches every other team write: a team outside the caller's org is
a not-found before anything else, then team admin (by descent), then a proven
address, then the "from above" rule, and only then the ceiling. Setting
(``WRITE``) and removing (``DELETE``) are the same decision up to the ceiling —
removing an allowance widens the team to no limit of its own, which is the
largest raise there is, but names no figure to hold to the ceiling. Every fact
is required on every branch that reaches it, so a caller that forgets one is
denied rather than allowed by the branch that happened not to consult it.
"""

from __future__ import annotations

from collections.abc import Mapping

from alkera_core.authz.decision import Decision, allow, deny
from alkera_core.authz.engine import Policy, register, require_attr
from alkera_core.authz.enums import Action, ResourceType
from alkera_core.authz.policies.member_cap import CEILING_FACTS, ceiling_refusal, team_admin_gate
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource

POLICY = "team_allocation.set"
AUDITED = frozenset(
    {"in_org", "roles", "email_verified", "is_org_root", "is_admin_above", *CEILING_FACTS}
)

#: Setting and removing an allowance; reading the tree is the team admin's
#: and decided with the rest of the Allocations tab.
SUPPORTED = frozenset({Action.WRITE, Action.DELETE})

FROM_ABOVE_MESSAGE = "An admin of a team above this one sets its allowance."
CEILING_MESSAGE = "A team's allowance can't exceed the {amount} allowed to {team}."


def decide(
    ctx: ActingContext, action: Action, resource: Resource, attrs: Mapping[str, object]
) -> Decision | None:
    if action not in SUPPORTED:
        return deny(POLICY, "action_not_supported", message="Not allowed")
    refused = team_admin_gate(POLICY, attrs)
    if refused is not None:
        return refused
    is_org_root = require_attr(attrs, "is_org_root", bool)
    is_admin_above = require_attr(attrs, "is_admin_above", bool)
    # The gate above proved admin of the root itself: that is the org admin.
    if not (is_org_root or is_admin_above):
        return deny(POLICY, "allocation_from_above", message=FROM_ABOVE_MESSAGE)
    if action is Action.WRITE:
        refused = ceiling_refusal(POLICY, attrs, template=CEILING_MESSAGE)
        if refused is not None:
            return refused
    return allow(POLICY, "org_admin_sets_root" if is_org_root else "admin_above_sets")


register(
    Policy(
        name=POLICY,
        resource_type=ResourceType.TEAM_ALLOCATION,
        decide=decide,
        audited_attrs=AUDITED,
    )
)

__all__ = [
    "AUDITED",
    "CEILING_MESSAGE",
    "FROM_ABOVE_MESSAGE",
    "POLICY",
    "SUPPORTED",
    "decide",
]
