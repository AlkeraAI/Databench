"""Who may cap a member's storage inside one team's folder.

The same relationship a member's budget cap on a team pool is decided on (see
``_member_cap``): a team outside the caller's org is a not-found, then team
admin by descent, then a proven address. A team's storage ceiling — the
tightest allowance on the team or any team above it, the org's own storage
ceiling included — is set from above the team, so no cap written inside the
team may exceed it, whoever writes it; and a change that widens the caller's
OWN storage (a higher own cap, or removing their own cap, which falls back to
the ceiling or to no limit) needs an admin of a team above this one — the org
admin always is. A team admin still sets and removes other members' caps
within the ceiling, and may lower their own.

Setting is ``WRITE``; removing is ``DELETE`` and consults no ceiling (a removal
names no figure to compare with it).
"""

from __future__ import annotations

from collections.abc import Mapping

from alkera_core.authz.decision import Decision, allow, deny
from alkera_core.authz.engine import Policy, register
from alkera_core.authz.enums import Action, ResourceType
from alkera_core.authz.policies.member_cap import (
    CEILING_FACTS,
    refuse_from_inside,
    team_admin_gate,
)
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource

POLICY = "storage.team_member_cap"
AUDITED = frozenset(
    {
        "in_org",
        "roles",
        "email_verified",
        "is_org_admin",
        "is_admin_above",
        "target_is_self",
        "raises_limit",
        *CEILING_FACTS,
    }
)

SUPPORTED = frozenset({Action.WRITE, Action.DELETE})

OWN_RAISE_MESSAGE = "Only an admin of a team above this one can raise your own storage here."
CEILING_MESSAGE = "A member's storage can't exceed the {amount} allowed to {team}."


def decide(
    ctx: ActingContext, action: Action, resource: Resource, attrs: Mapping[str, object]
) -> Decision | None:
    if action not in SUPPORTED:
        return deny(POLICY, "action_not_supported", message="Not allowed")
    refused = team_admin_gate(POLICY, attrs)
    if refused is not None:
        return refused
    refused = refuse_from_inside(
        POLICY,
        attrs,
        own_raise_reason="own_storage_raise",
        own_raise_message=OWN_RAISE_MESSAGE,
        ceiling_message=CEILING_MESSAGE if action is Action.WRITE else None,
    )
    if refused is not None:
        return refused
    return allow(POLICY, "team_admin_set" if action is Action.WRITE else "team_admin_clear")


register(
    Policy(
        name=POLICY,
        resource_type=ResourceType.TEAM_STORAGE_CAP,
        decide=decide,
        audited_attrs=AUDITED,
    )
)

__all__ = ["AUDITED", "CEILING_MESSAGE", "OWN_RAISE_MESSAGE", "POLICY", "SUPPORTED", "decide"]
