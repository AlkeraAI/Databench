"""Who may read, save, edit and delete a chat template.

A chat template is a chat distilled into something reusable: a brief for
whoever starts from it, and the files a new chat begins with. It is a
workspace object like a chat (an org, an owner, an audience, a node in the file
tree) and is saved PRIVATE: its author alone, until a rung on its node (granted
on it, or inherited from the folder it was saved into) lets someone else in.
The audience branch still reads a template addressed to an org or a team, but
no save writes that address unasked.

READ admits the owner, an org admin, the audience the template was created
into, and anyone holding "Can view" or above on its node. Everyone else gets
the opaque not-found a stranger gets, so an existing id cannot be told from a
missing one.

Editing the brief and title needs the share ladder's WRITE rung; the refusal
is a plain 403, since the caller can already see the template. Deleting takes
it away from everyone it was shared with, so it stays the owner's and the org
admins'. Saving is a CREATE addressed to an audience the creator must be in.

Every mutation requires a verified email; no read does.

``roles`` are the subject's roles in the ORG, resolved on the org root team.
Every fact is required on every branch, so a route that forgets to resolve one
is denied. A caller from another org never reaches the policy (the engine
answers an opaque not-found first).
"""

from __future__ import annotations

from collections.abc import Mapping

from alkera_core.authz.chat_scope import role_may_read, role_may_send, scope_readable
from alkera_core.authz.decision import Decision, allow, deny
from alkera_core.authz.engine import Policy, register, require_attr
from alkera_core.authz.enums import Action, ResourceType, Role
from alkera_core.authz.policies._facts import (
    ObjectFacts,
    object_facts,
    optional_uuid,
    require_scope_team,
)
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource

POLICY = "chat_template.access"
#: Byte-identical to the engine's tenancy refusal and to what the routes answer
#: for an id nobody holds: someone outside a template's audience must not be
#: able to tell a template that exists from one that does not.
NOT_FOUND = "Not found"
AUDITED = frozenset(
    {
        "in_org",
        "roles",
        "is_org_admin",
        "owner_user_id",
        "visibility_scope",
        "team_ids",
        "email_verified",
        "team_id",
        "shared_role",
    }
)

#: The actions this policy decides. Anything else is refused outright rather
#: than falling through to a rule written for a different verb.
SUPPORTED = frozenset({Action.READ, Action.CREATE, Action.WRITE, Action.DELETE})

#: The rung the template's node in the file tree gives this caller, ``""`` when
#: no grant reaches them. Required on every branch like every other fact.
SHARED_ROLE_ATTR = "shared_role"

WRITE_DENIED_MESSAGE = "You need edit access to this template to change it."
WRITE_DENIED_CODE = "chat_template.write_rung_required"

VERIFY_EMAIL_MESSAGE = "Verify your email address to perform this action."
VERIFY_EMAIL_CODE = "email_verification_required"


def _in_audience(facts: ObjectFacts) -> bool:
    """Whether the audience the template is addressed to admits this caller.

    The shared predicate decides, the team column having already been checked
    to agree with the scope string.
    """
    return scope_readable(
        is_org_admin=facts.is_org_admin,
        team_ids=facts.team_ids,
        visibility_scope=facts.visibility_scope,
        is_owner=facts.is_owner,
    )


def _reads(facts: ObjectFacts, *, shared_role: str) -> str | None:
    """The reason this caller may read the template, or ``None``.

    The owner first, so their read never turns on a Files fact; then the org's
    admins; then the audience it was addressed to; then the rung its node gives
    the caller, which is how someone outside the audience is let in. The reason
    is what the decision row carries, so an auditor can tell a share from an
    audience without re-deriving it.
    """
    if facts.is_owner:
        return "owner_reads"
    if facts.is_org_admin:
        return "org_admin_reads"
    if _in_audience(facts):
        return "audience_reads"
    if role_may_read(shared_role):
        return "shared_reads"
    return None


def _writes(facts: ObjectFacts, *, shared_role: str) -> bool:
    """Whether this caller may change the template's brief, title and files.

    Its author, an org admin, or someone granted the ladder's WRITE rung on its
    node. Reading it is not enough: the brief is what everyone who starts from
    the template is handed, and a reader rewriting it would be speaking in the
    author's name to every future chat.
    """
    return facts.is_owner or facts.is_org_admin or role_may_send(shared_role)


def decide(
    ctx: ActingContext, action: Action, resource: Resource, attrs: Mapping[str, object]
) -> Decision | None:
    if action not in SUPPORTED:
        return deny(POLICY, "action_not_supported", message="Not allowed")
    facts = object_facts(ctx, attrs)
    team_id = optional_uuid(attrs, "team_id")
    shared_role = require_attr(attrs, SHARED_ROLE_ATTR, str)
    if not facts.in_org:
        return deny(POLICY, "not_in_org", message=NOT_FOUND, as_not_found=True)
    if Role.MEMBER not in facts.roles:
        return deny(POLICY, "org_member_required", message=NOT_FOUND, as_not_found=True)
    if not require_scope_team(facts.visibility_scope, team_id):
        # The scope string and the team column are two spellings of one fact.
        # When a route resolves them out of step we do not get to guess which
        # one names the real audience.
        return deny(POLICY, "scope_team_mismatch", message=NOT_FOUND, as_not_found=True)
    if action is Action.CREATE:
        # Nothing exists yet, but the template is already addressed to an
        # audience and the person saving it must be in that audience.
        if not _in_audience(facts):
            return deny(POLICY, "not_in_audience", message=NOT_FOUND, as_not_found=True)
        return _verified(facts, "org_member_creates")
    reads = _reads(facts, shared_role=shared_role)
    if reads is None:
        return deny(POLICY, "not_in_audience", message=NOT_FOUND, as_not_found=True)
    if action is Action.READ:
        return allow(POLICY, reads)
    if action is Action.WRITE:
        if not _writes(facts, shared_role=shared_role):
            # A plain 403, not the opaque not-found: the caller can already read
            # this template, so pretending it is not there hides nothing.
            return deny(
                POLICY,
                "write_rung_required",
                message=WRITE_DENIED_MESSAGE,
                error_code=WRITE_DENIED_CODE,
            )
        return _verified(facts, "writer_may_edit")
    if facts.is_owner or facts.is_org_admin:
        return _verified(facts, "owner_deletes")
    # A reader — or someone holding even the top rung on the node — may not
    # take the template away from everybody else it was shared with. They can
    # see it, so the refusal need not hide it.
    return deny(POLICY, "owner_or_org_admin_required", message="Not allowed")


def _verified(facts: ObjectFacts, reason: str) -> Decision:
    if not facts.email_verified:
        return deny(
            POLICY,
            "email_verification_required",
            message=VERIFY_EMAIL_MESSAGE,
            error_code=VERIFY_EMAIL_CODE,
        )
    return allow(POLICY, reason)


register(
    Policy(
        name=POLICY,
        resource_type=ResourceType.CHAT_TEMPLATE,
        decide=decide,
        audited_attrs=AUDITED,
    )
)

__all__ = [
    "AUDITED",
    "NOT_FOUND",
    "POLICY",
    "SHARED_ROLE_ATTR",
    "SUPPORTED",
    "VERIFY_EMAIL_CODE",
    "VERIFY_EMAIL_MESSAGE",
    "WRITE_DENIED_CODE",
    "WRITE_DENIED_MESSAGE",
    "decide",
]
