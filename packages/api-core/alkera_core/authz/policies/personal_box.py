"""Who may list a person's own boxes and take one's standing away.

A personal box is registered through the device flow on the person's own
session and runs their private chats, files and tool runs. Its machine
credential never expires, so the person needs a way to end it that does not
wait for a logout-all: a laptop handed on, a box set up from a session they
no longer trust.

* The person whose box it is may list their boxes and revoke any of them,
  signed in as themselves on any of their own credentials, in the org the box
  is bound to: the route counts a box in another org as not theirs here. An agent acting for
  them is refused: an agent must not be able to take its person's compute away
  any more than it can mint it.
* Anyone else, and anyone naming a box that is not a personal box of theirs,
  gets an opaque not-found: whether a box id exists is not something to
  confirm.
"""

from __future__ import annotations

from collections.abc import Mapping

from alkera_core.authz.decision import Decision, allow, deny
from alkera_core.authz.engine import Policy, register, require_attr
from alkera_core.authz.enums import Action, PrincipalKind, ResourceType
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource

POLICY = "compute.personal_box"

SUPPORTED = frozenset({Action.READ, Action.DELETE})

AUDITED = frozenset({"is_owner"})

NOT_FOUND_MESSAGE = "Not found"
AGENT_MESSAGE = "Only you can do this, not an agent acting for you"
AGENT_CODE = "personal_box_owner_required"


def decide(
    ctx: ActingContext, action: Action, resource: Resource, attrs: Mapping[str, object]
) -> Decision | None:
    if action not in SUPPORTED:
        return deny(POLICY, "action_not_supported", message="Not allowed")
    # The box is a personal box whose credential was minted for the caller.
    is_owner = require_attr(attrs, "is_owner", bool)
    if ctx.acting_principal.kind is not PrincipalKind.USER and not ctx.is_agent:
        return deny(POLICY, "not_a_person", message=NOT_FOUND_MESSAGE, as_not_found=True)
    if not is_owner:
        return deny(POLICY, "not_your_box", message=NOT_FOUND_MESSAGE, as_not_found=True)
    if ctx.is_agent:
        return deny(POLICY, "agent_acting", message=AGENT_MESSAGE, error_code=AGENT_CODE)
    return allow(POLICY, "box_owner")


register(
    Policy(
        name=POLICY,
        resource_type=ResourceType.PERSONAL_BOX,
        decide=decide,
        audited_attrs=AUDITED,
    )
)

__all__ = [
    "AGENT_CODE",
    "AGENT_MESSAGE",
    "AUDITED",
    "NOT_FOUND_MESSAGE",
    "POLICY",
    "SUPPORTED",
    "decide",
]
