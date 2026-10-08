"""The platform's rule for a setting it holds about one tenant.

Several things about an org are the platform's decision, not the org's: its
storage ceiling, whether its files open live, which email domains its SSO
speaks for. All of them follow one rule. Every platform staff member may read
where the org stands; only a platform ADMIN may change it. The staff check comes
before the admin check so the two refusals read differently in the decision
row, and every fact is required on both actions, so a caller that forgets one
is denied rather than allowed by a branch that happened not to need it.

Each such policy keeps its own name and resource type and builds its
``decide`` here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from alkera_core.authz.decision import Decision, allow, deny
from alkera_core.authz.engine import require_attr
from alkera_core.authz.enums import Action
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource

AUDITED = frozenset({"platform_staff", "platform_admin", "operation"})
STAFF_MESSAGE = "Platform staff role required"
ADMIN_MESSAGE = "Platform admin role required"
#: READ shows where the org stands; ADMIN sets and clears it.
SUPPORTED = frozenset({Action.READ, Action.ADMIN})

Decide = Callable[[ActingContext, Action, Resource, Mapping[str, object]], Decision | None]


def staff_reads_admin_changes(policy: str) -> Decide:
    """The ``decide`` of a platform policy named ``policy`` that follows the rule."""

    def decide(
        ctx: ActingContext, action: Action, resource: Resource, attrs: Mapping[str, object]
    ) -> Decision | None:
        if action not in SUPPORTED:
            return deny(policy, "action_not_supported", message="Not allowed")
        staff = require_attr(attrs, "platform_staff", bool)
        is_admin = require_attr(attrs, "platform_admin", bool)
        require_attr(attrs, "operation", str)
        if not staff:
            return deny(policy, "platform_staff_required", message=STAFF_MESSAGE)
        if action is Action.READ:
            return allow(policy, "admin_reads" if is_admin else "staff_reads")
        if not is_admin:
            return deny(policy, "platform_admin_required", message=ADMIN_MESSAGE)
        return allow(policy, "admin_changes")

    return decide


__all__ = [
    "ADMIN_MESSAGE",
    "AUDITED",
    "STAFF_MESSAGE",
    "SUPPORTED",
    "Decide",
    "staff_reads_admin_changes",
]
