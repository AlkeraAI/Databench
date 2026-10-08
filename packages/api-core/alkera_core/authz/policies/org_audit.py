"""Who may write into an organization's agent-activity trail.

The org audit chain is read by admins and written by two very different
things: the server itself, for every action it takes, and a member's daemon —
a laptop, or a chat box serving the org's chats — reporting what its agent
did. Only the second needs deciding, and the decision is one question: is the
caller still a member of the organization the events are filed under? The
engine answers the tenancy half before this policy runs (a trail belonging to
another org is an opaque not-found), and the standing is taken at the org ROOT,
because the organization's own trail is not something a member row on a team
below confers.

The organization's PLAN is deliberately not a fact here. Evidence is recorded
for every organization and the plan decides who may READ it — that gate lives
on the three admin read routes. Gating the write dropped evidence nobody can
get back, and, because a refused batch makes a daemon treat its credential as
dead, it left every box in a plan-less org logging a 403 for ever with its
spool thrown away.

Membership itself was never checked before: a CLI token lives ninety days and
a membership does not, so an organization went on accepting entries into its
tamper-evident chain from someone it had already removed. Refusing here puts
that on record as a decision, under a code the daemon can tell apart from
"your credential is no good".
"""

from __future__ import annotations

from collections.abc import Mapping

from alkera_core.authz.decision import Decision, allow, deny
from alkera_core.authz.engine import Policy, register, require_attr
from alkera_core.authz.enums import Action, ResourceType
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource

POLICY = "org_audit.agent_report"

#: WRITE is the agent-activity ingest — the only thing a client does here. The
#: admin read surface is not decided by this policy.
SUPPORTED = frozenset({Action.WRITE})

AUDITED = frozenset({"is_org_root", "org_member"})

NOT_FOUND_MESSAGE = "Not found"
MEMBER_MESSAGE = "Only a member of this organization can report agent activity"
MEMBER_CODE = "org_member_required"


def decide(
    ctx: ActingContext, action: Action, resource: Resource, attrs: Mapping[str, object]
) -> Decision | None:
    if action not in SUPPORTED:
        return deny(POLICY, "action_not_supported", message="Not allowed")
    # Every fact on every branch: a missing one must never read as a pass.
    is_org_root = require_attr(attrs, "is_org_root", bool)
    org_member = require_attr(attrs, "org_member", bool)
    if not is_org_root:
        # A team id reached a route that only ever decides about the org's own
        # trail. Answered as not-found: naming one here is a mistake, not a
        # request to be told which teams exist.
        return deny(POLICY, "not_the_org_root", message=NOT_FOUND_MESSAGE, as_not_found=True)
    if not org_member:
        return deny(POLICY, "not_in_org", message=MEMBER_MESSAGE, error_code=MEMBER_CODE)
    return allow(POLICY, "org_member_reports")


register(
    Policy(name=POLICY, resource_type=ResourceType.ORG_AUDIT, decide=decide, audited_attrs=AUDITED)
)

__all__ = [
    "AUDITED",
    "MEMBER_CODE",
    "MEMBER_MESSAGE",
    "POLICY",
    "SUPPORTED",
    "decide",
]
