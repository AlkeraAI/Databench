"""Who may read, create, edit, export, re-run and fill a workspace object.

A workspace object (a saved query, a promoted result, the chat it came out of)
belongs to one org, has an owner, and carries the audience it was saved to.
Reading is the audience question (:func:`~alkera_core.authz.chat_scope.scope_readable`);
changing it, exporting it or spending compute re-running it is the owner's or
an org admin's. An unrecognised scope reads to nobody but an org admin. Creating one addresses it
to an audience the creator must be in; a route names the creator as the
owner-to-be, and the creator owns what they create.

``upload_payload`` is the daemon's route and no one else's: it is how the
machine that ran the query hands the cloud the full result behind a promotion.
It requires an **agent-asserted** principal (a daemon presenting its user's
JWT plus the agent headers) acting for a user who could have written the
object themselves. A browser session, a CI token or a personal access token
holding the same user's permissions is refused, because none of them is the
machine that produced the payload.

A box on its own machine credential is decided before any of that. It is a
member of no org; what admits it is that the object came out of a chat bound
to its machine (the route resolves that one fact), and then only to read the
object and to fill (or refuse to fill) its payload, which is the box's whole
job on a result. Every other verb, and every object of a chat it does not
run, is the same opaque not-found a stranger gets.

``roles`` are the subject's roles in the ORG, resolved on the org root team;
the object's scope narrows the audience afterwards. Every fact is required on
every branch, including branches that do not consult it: a route that forgets
one is denied, never allowed by the branch that happened not to need it. Every
denial that could confirm the object exists is the same opaque not-found the
engine gives a caller from another org.
"""

from __future__ import annotations

from collections.abc import Mapping

from alkera_core.authz.chat_scope import scope_readable
from alkera_core.authz.decision import Decision, allow, deny
from alkera_core.authz.engine import Policy, register, require_attr
from alkera_core.authz.enums import Action, ResourceType, Role
from alkera_core.authz.policies._facts import ObjectFacts, object_facts
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource

POLICY = "objects.access"
NOT_FOUND = "Object not found"
AUDITED = frozenset(
    {
        "in_org",
        "roles",
        "is_org_admin",
        "owner_user_id",
        "visibility_scope",
        "team_ids",
        "email_verified",
        "held_by_machine",
    }
)

#: The actions this policy decides.
SUPPORTED = frozenset(
    {
        Action.READ,
        Action.CREATE,
        Action.WRITE,
        Action.EXPORT,
        Action.RERUN,
        Action.UPLOAD_PAYLOAD,
    }
)

#: The fact the machine branch alone consults: whether the object came out of
#: a chat bound to the acting machine. ``False`` for every caller that is not
#: a box — a person is never a machine — so no route pays for the read that
#: resolves it unless a box is asking. Required on every branch like every
#: other fact, so a door that forgets it is denied rather than admitted.
HELD_BY_MACHINE_ATTR = "held_by_machine"
#: What a box may do to a result of its own chat: read it, and deliver or
#: refuse the payload behind it.
MACHINE_ACTIONS = frozenset({Action.READ, Action.UPLOAD_PAYLOAD})

#: The actions that write or spend, and so need a verified email. Exporting an
#: object you may already read changes nothing and is not gated on it.
NEEDS_VERIFIED_EMAIL = frozenset({Action.CREATE, Action.WRITE, Action.RERUN, Action.UPLOAD_PAYLOAD})

VERIFY_EMAIL_MESSAGE = "Verify your email address to perform this action."
VERIFY_EMAIL_CODE = "email_verification_required"


def decide(
    ctx: ActingContext, action: Action, resource: Resource, attrs: Mapping[str, object]
) -> Decision | None:
    if action not in SUPPORTED:
        return deny(POLICY, "action_not_supported", message="Not allowed")
    held_by_machine = require_attr(attrs, HELD_BY_MACHINE_ATTR, bool)
    if ctx.is_machine:
        if held_by_machine and action in MACHINE_ACTIONS:
            return allow(POLICY, "machine_holds_source_chat")
        return deny(POLICY, "machine_does_not_hold_object", message=NOT_FOUND, as_not_found=True)
    facts = object_facts(ctx, attrs)
    if not facts.in_org:
        return deny(POLICY, "not_in_org", message=NOT_FOUND, as_not_found=True)
    if Role.MEMBER not in facts.roles:
        return deny(POLICY, "org_member_required", message=NOT_FOUND, as_not_found=True)
    if not scope_readable(
        is_org_admin=facts.is_org_admin,
        team_ids=facts.team_ids,
        visibility_scope=facts.visibility_scope,
        is_owner=facts.is_owner,
    ):
        return deny(POLICY, "not_in_audience", message=NOT_FOUND, as_not_found=True)
    if action is Action.READ:
        return allow(POLICY, "in_audience")
    if action is Action.UPLOAD_PAYLOAD and not ctx.is_agent:
        return deny(POLICY, "agent_principal_required", message="Not allowed")
    if not (facts.is_owner or facts.is_org_admin):
        return deny(POLICY, "owner_or_org_admin_required", message="Not allowed")
    if action in NEEDS_VERIFIED_EMAIL and not facts.email_verified:
        return deny(
            POLICY,
            "email_verification_required",
            message=VERIFY_EMAIL_MESSAGE,
            error_code=VERIFY_EMAIL_CODE,
        )
    return allow(POLICY, _reason(action, facts))


def _reason(action: Action, facts: ObjectFacts) -> str:
    who = "owner" if facts.is_owner else "org_admin"
    return f"{who}_{action.value}"


register(
    Policy(
        name=POLICY,
        resource_type=ResourceType.WORKSPACE_OBJECT,
        decide=decide,
        audited_attrs=AUDITED,
    )
)

__all__ = [
    "AUDITED",
    "HELD_BY_MACHINE_ATTR",
    "MACHINE_ACTIONS",
    "NEEDS_VERIFIED_EMAIL",
    "NOT_FOUND",
    "POLICY",
    "SUPPORTED",
    "VERIFY_EMAIL_CODE",
    "VERIFY_EMAIL_MESSAGE",
    "decide",
]
