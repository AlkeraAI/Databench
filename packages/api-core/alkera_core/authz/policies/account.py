"""Who may export or delete an account, and read where those requests stand.

An account is decided about as an identity, never as a tenant: the resource
names no org, because a person's export spans every org they are in and their
deletion ends all of those memberships at once.

* The person themselves may read, export and delete, but only acting on their
  own session. An agent acting for them, a personal access token, a CI or proxy
  token is refused: handing over everything about a person, or ending the
  account, is something only the person, signed in, does.
* Platform admins may do all three for somebody else, through the support tool
  (a request the person made through another channel); every such call is also
  on the platform audit log.
* Platform support may read where someone's requests stand, nothing more.
* Anyone else gets an opaque not-found: whether an account id exists is not
  something to confirm.
"""

from __future__ import annotations

from collections.abc import Mapping

from alkera_core.authz.decision import Decision, allow, deny
from alkera_core.authz.engine import Policy, register, require_attr
from alkera_core.authz.enums import Action, ResourceType
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource

POLICY = "account.lifecycle"

SUPPORTED = frozenset({Action.READ, Action.EXPORT, Action.DELETE})

AUDITED = frozenset({"is_self", "acting_directly", "platform_role"})

#: ``platform_role`` values, as the identity row spells them ("" for none).
PLATFORM_ADMIN = "alkera_admin"
PLATFORM_SUPPORT = "alkera_support"

NOT_FOUND_MESSAGE = "Not found"
DIRECT_MESSAGE = "Only you, signed in to your own account, can do this"
DIRECT_CODE = "account_owner_session_required"


def decide(
    ctx: ActingContext, action: Action, resource: Resource, attrs: Mapping[str, object]
) -> Decision | None:
    if action not in SUPPORTED:
        return deny(POLICY, "action_not_supported", message="Not allowed")
    is_self = require_attr(attrs, "is_self", bool)
    acting_directly = require_attr(attrs, "acting_directly", bool)
    platform_role = require_attr(attrs, "platform_role", str)
    if is_self:
        if not acting_directly:
            return deny(
                POLICY, "not_acting_directly", message=DIRECT_MESSAGE, error_code=DIRECT_CODE
            )
        return allow(POLICY, "account_owner")
    if not acting_directly:
        return deny(POLICY, "not_acting_directly", message=NOT_FOUND_MESSAGE, as_not_found=True)
    if platform_role == PLATFORM_ADMIN:
        return allow(POLICY, "platform_admin")
    if platform_role == PLATFORM_SUPPORT and action is Action.READ:
        return allow(POLICY, "platform_support_reads")
    return deny(POLICY, "not_your_account", message=NOT_FOUND_MESSAGE, as_not_found=True)


register(
    Policy(name=POLICY, resource_type=ResourceType.ACCOUNT, decide=decide, audited_attrs=AUDITED)
)

__all__ = [
    "AUDITED",
    "DIRECT_CODE",
    "DIRECT_MESSAGE",
    "NOT_FOUND_MESSAGE",
    "PLATFORM_ADMIN",
    "PLATFORM_SUPPORT",
    "POLICY",
    "SUPPORTED",
    "decide",
]
