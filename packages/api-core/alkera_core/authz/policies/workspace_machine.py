"""Who may change the machine a workspace runs on.

Moving a workspace stops whatever runs in it and starts it again somewhere
else, on hardware somebody pays for. Two things must both hold:

* the caller has **Full access or Owner** on the workspace: they own it, or its
  folder gives them the ladder's ``manager`` or ``owner`` rung. "Can edit"
  starts chats in a workspace; it does not decide where every chat in it runs;
* the caller may **use the target**: the org's default placement is everyone's,
  and an org machine is usable by the people in its audience (a pool machine's
  audience is the whole org). Which org machine is in the caller's org, live
  and in their audience is resolved by the caller through the ``org_machine``
  facts and arrives here as ``target_usable``.

A workspace the caller cannot read is the same opaque not-found every other
door gives. A machine never decides here, nor does an agent: a move ends turns
and spends money on the person's behalf, which is the person's own call.

``WRITE`` is asking for a move and canceling one. Every fact is required on
every branch, so a door that forgets one is denied.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from alkera_core.authz.chat_scope import PUBLISHER_ROLE, chat_read_reason
from alkera_core.authz.decision import Decision, allow, deny
from alkera_core.authz.engine import MissingAttributeError, Policy, register, require_attr
from alkera_core.authz.enums import Action, ResourceType, Role
from alkera_core.authz.policies._facts import object_facts
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource

POLICY = "workspace_machine.move"
NOT_FOUND = "Not found"

#: The rungs that decide where a workspace runs: Full access and Owner.
MOVE_ROLES: Final = frozenset({"manager", PUBLISHER_ROLE})

SHARED_ROLE_ATTR = "shared_role"
ADMIN_READS_PRIVATE_ATTR = "admin_reads_private"
#: ``default`` (the org's default placement) or ``org_machine``.
TARGET_KIND_ATTR = "target_kind"
#: Whether the caller may use the target org machine: in its org, not deleted,
#: and the caller in its audience (any member for a pool machine).
TARGET_USABLE_ATTR = "target_usable"
TARGET_DEFAULT: Final = "default"
TARGET_ORG_MACHINE: Final = "org_machine"
TARGET_KINDS: Final = frozenset({TARGET_DEFAULT, TARGET_ORG_MACHINE})

AUDITED = frozenset(
    {
        "in_org",
        "roles",
        "is_org_admin",
        "owner_user_id",
        "email_verified",
        SHARED_ROLE_ATTR,
        ADMIN_READS_PRIVATE_ATTR,
        TARGET_KIND_ATTR,
        TARGET_USABLE_ATTR,
    }
)
SUPPORTED = frozenset({Action.WRITE})

FULL_ACCESS_MESSAGE = "You need full access to this workspace to change its machine."
FULL_ACCESS_CODE = "workspace_move_requires_full_access"
TARGET_MESSAGE = "You can't use that machine."
TARGET_CODE = "machine_not_usable"
AGENT_MESSAGE = "An agent can't move a workspace to another machine."
VERIFY_EMAIL_MESSAGE = "Verify your email address to perform this action."
VERIFY_EMAIL_CODE = "email_verification_required"


def decide(
    ctx: ActingContext, action: Action, resource: Resource, attrs: Mapping[str, object]
) -> Decision | None:
    if action not in SUPPORTED:
        return deny(POLICY, "action_not_supported", message="Not allowed")
    facts = object_facts(ctx, attrs)
    shared_role = require_attr(attrs, SHARED_ROLE_ATTR, str)
    admin_reads_private = require_attr(attrs, ADMIN_READS_PRIVATE_ATTR, bool)
    target_kind = require_attr(attrs, TARGET_KIND_ATTR, str)
    target_usable = require_attr(attrs, TARGET_USABLE_ATTR, bool)
    if target_kind not in TARGET_KINDS:
        raise MissingAttributeError(TARGET_KIND_ATTR)
    if ctx.is_machine:
        return deny(POLICY, "machine_action_not_allowed", message=NOT_FOUND, as_not_found=True)
    if not facts.in_org:
        return deny(POLICY, "not_in_org", message=NOT_FOUND, as_not_found=True)
    if Role.MEMBER not in facts.roles:
        return deny(POLICY, "org_member_required", message=NOT_FOUND, as_not_found=True)
    reads = chat_read_reason(
        is_owner=facts.is_owner,
        is_bound_machine=False,
        shared_role=shared_role,
        is_org_admin=facts.is_org_admin,
        admin_reads_private=admin_reads_private,
    )
    if reads is None:
        return deny(POLICY, "not_in_audience", message=NOT_FOUND, as_not_found=True)
    if ctx.is_agent:
        return deny(POLICY, "agent_may_not_move", message=AGENT_MESSAGE)
    if not (facts.is_owner or shared_role in MOVE_ROLES):
        return deny(
            POLICY, "full_access_required", message=FULL_ACCESS_MESSAGE, error_code=FULL_ACCESS_CODE
        )
    if target_kind == TARGET_ORG_MACHINE and not target_usable:
        return deny(POLICY, "target_not_usable", message=TARGET_MESSAGE, error_code=TARGET_CODE)
    if not facts.email_verified:
        return deny(
            POLICY,
            "email_verification_required",
            message=VERIFY_EMAIL_MESSAGE,
            error_code=VERIFY_EMAIL_CODE,
        )
    if target_kind == TARGET_DEFAULT:
        return allow(
            POLICY, "owner_moves_to_default" if facts.is_owner else "full_access_to_default"
        )
    return allow(POLICY, "owner_moves" if facts.is_owner else "full_access_moves")


register(
    Policy(
        name=POLICY,
        resource_type=ResourceType.WORKSPACE_MACHINE,
        decide=decide,
        audited_attrs=AUDITED,
    )
)

__all__ = [
    "ADMIN_READS_PRIVATE_ATTR",
    "AGENT_MESSAGE",
    "AUDITED",
    "FULL_ACCESS_CODE",
    "FULL_ACCESS_MESSAGE",
    "MOVE_ROLES",
    "NOT_FOUND",
    "POLICY",
    "SHARED_ROLE_ATTR",
    "SUPPORTED",
    "TARGET_CODE",
    "TARGET_DEFAULT",
    "TARGET_KINDS",
    "TARGET_KIND_ATTR",
    "TARGET_MESSAGE",
    "TARGET_ORG_MACHINE",
    "TARGET_USABLE_ATTR",
    "decide",
]
