"""Who may read, start, rename, add a chat to, and delete a workspace.

A workspace holds chats that share one file tree. It is PRIVATE until shared,
exactly as a chat is: its owner reads it, anyone holding a rung on its folder
reads it ("Can view" and up, granted on the folder, inherited from one above,
or made to a team the caller is on), and an org admin reads it only where the
deployment lets an org admin read a private chat. Everyone else in the org is
told it does not exist, the same opaque answer another org's member gets. The
read is :func:`~alkera_core.authz.chat_scope.chat_read_reason`, the predicate
every door onto a chat decides through, so a workspace and the chats it holds
cannot be read on two different rules.

Sharing a workspace shares its chats: a chat's folder sits under the
workspace's (or, for a workspace of one, IS it), so a rung granted on the
workspace's folder reaches every chat in it through the drive's own
inheritance. This policy decides the workspace; the chats keep deciding
through ``chat.access``.

Renaming the workspace and adding a chat to it take the share ladder's WRITE
rung, or being the owner: a chat added to a workspace drives an agent that
uses the OWNER's connections, so "may watch" is not "may start one". Deleting
it ends every chat in it, which is governance: the owner's or an org admin's,
never a rung's. Starting one is open to any verified member, addressed to
themselves.

A box decides here for one thing only: the box that holds the workspace (its
kernel sandbox, which runs the workspace's notebooks) lists the connections
attached to it and leases their credentials, on its own machine credential.
Every other machine, and every person, is told the workspace does not exist
for those verbs; a person reads their connections on their own door.
Everything else a box does goes through a chat's policy, which admits the
machine the chat is bound to. Every fact is required on every branch, so a
door that forgets one is denied rather than admitted by a branch that did not
need it.
"""

from __future__ import annotations

from collections.abc import Mapping

from alkera_core.authz.chat_scope import (
    chat_read_reason,
    role_may_rename,
    role_may_send,
    scope_readable,
)
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

POLICY = "workspace.access"
#: Byte-identical to the engine's tenancy refusal, so an unshared member cannot
#: tell a workspace that exists from one that does not.
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
        "admin_reads_private",
        "owner_departed",
        "is_main",
        "bound_machine_id",
    }
)
#: ``WRITE`` is adding a chat to the workspace; ``LIST_CONNECTIONS`` is the box
#: holding the workspace reading the connections attached to it.
SUPPORTED = frozenset(
    {
        Action.READ,
        Action.CREATE,
        Action.RENAME,
        Action.WRITE,
        Action.DELETE,
        Action.LIST_CONNECTIONS,
        Action.LEASE_CONNECTION_CREDENTIAL,
    }
)
#: What the box holding a workspace does there: list the workspace owner's
#: connections and lease their credentials one at a time.
CONNECTION_ACTIONS = frozenset({Action.LIST_CONNECTIONS, Action.LEASE_CONNECTION_CREDENTIAL})
#: The machine that holds the workspace (as the box reported it), ``""`` when
#: none: the one machine that may list the workspace's connections.
BOUND_MACHINE_ATTR = "bound_machine_id"
#: The rung the workspace's folder gives this caller, ``""`` when none.
SHARED_ROLE_ATTR = "shared_role"
#: Whether an org admin reads a workspace nobody shared with them: the same
#: deployment setting that decides it for a chat, resolved in one place.
ADMIN_READS_PRIVATE_ATTR = "admin_reads_private"
#: Whether this is its owner's main workspace, which is not deleted: it is
#: where a chat lands when nobody named a place, and it is made again the
#: next time it is asked for.
IS_MAIN_ATTR = "is_main"
#: Whether the workspace's owner has left (deactivated, gone, or no longer in
#: its org): the one case an org admin deletes a workspace they cannot read.
OWNER_DEPARTED_ATTR = "owner_departed"

RENAME_DENIED_MESSAGE = "You need edit access to this workspace to rename it."
RENAME_DENIED_CODE = "workspace_rename_requires_edit"
WRITE_DENIED_MESSAGE = "You need edit access to this workspace to start a chat in it."
WRITE_DENIED_CODE = "workspace_write_requires_edit"
DELETE_DENIED_MESSAGE = "Only the owner or an org admin can delete this workspace."
MAIN_DELETE_DENIED_MESSAGE = "A main workspace cannot be deleted."
MAIN_DELETE_DENIED_CODE = "workspace_main_not_deletable"
AGENT_DELETE_DENIED_MESSAGE = "An agent can't delete a workspace."
AGENT_CREATE_DENIED_MESSAGE = "An agent can't start a workspace."
AGENT_CREATE_DENIED_CODE = "workspace_create_by_agent"
VERIFY_EMAIL_MESSAGE = "Verify your email address to perform this action."
VERIFY_EMAIL_CODE = "email_verification_required"


def decide(
    ctx: ActingContext, action: Action, resource: Resource, attrs: Mapping[str, object]
) -> Decision | None:
    if action not in SUPPORTED:
        return deny(POLICY, "action_not_supported", message="Not allowed")
    facts = object_facts(ctx, attrs)
    team_id = optional_uuid(attrs, "team_id")
    shared_role = require_attr(attrs, SHARED_ROLE_ATTR, str)
    admin_reads_private = require_attr(attrs, ADMIN_READS_PRIVATE_ATTR, bool)
    is_main = require_attr(attrs, IS_MAIN_ATTR, bool)
    owner_departed = require_attr(attrs, OWNER_DEPARTED_ATTR, bool)
    bound_machine_id = require_attr(attrs, BOUND_MACHINE_ATTR, str)
    if ctx.is_machine:
        holds = bool(bound_machine_id) and ctx.acting_principal.id == bound_machine_id
        if action in CONNECTION_ACTIONS and holds:
            return allow(POLICY, "machine_holds_workspace")
        return deny(POLICY, "machine_action_not_allowed", message=NOT_FOUND, as_not_found=True)
    if action in CONNECTION_ACTIONS:
        # A person's connections are read and leased on their own door; these
        # are the box's, and a person is told the workspace does not exist.
        return deny(POLICY, "machine_required", message=NOT_FOUND, as_not_found=True)
    if not facts.in_org:
        return deny(POLICY, "not_in_org", message=NOT_FOUND, as_not_found=True)
    if Role.MEMBER not in facts.roles:
        return deny(POLICY, "org_member_required", message=NOT_FOUND, as_not_found=True)
    if not require_scope_team(facts.visibility_scope, team_id):
        return deny(POLICY, "scope_team_mismatch", message=NOT_FOUND, as_not_found=True)
    if action is Action.CREATE:
        if not _creates_into_audience(facts):
            return deny(POLICY, "not_in_audience", message=NOT_FOUND, as_not_found=True)
        if ctx.is_agent and not is_main:
            # A project workspace is a place people share; an agent acting
            # for its user may have the user's own main workspace made, which
            # is made on first ask anyway, and nothing else.
            return deny(
                POLICY,
                "agent_may_not_create_project",
                message=AGENT_CREATE_DENIED_MESSAGE,
                error_code=AGENT_CREATE_DENIED_CODE,
            )
        return _verified(facts, "org_member_creates")
    if action is Action.DELETE and facts.is_org_admin and owner_departed and not ctx.is_agent:
        # Offboarding: the owner is gone, and an org admin ends their
        # workspaces (a departed member's main one included) without reading
        # them. An active member's private workspace stays the same not-found.
        return _verified(facts, "org_admin_offboards")
    reads = chat_read_reason(
        is_owner=facts.is_owner,
        is_bound_machine=False,
        shared_role=shared_role,
        is_org_admin=facts.is_org_admin,
        admin_reads_private=admin_reads_private,
    )
    if reads is None:
        return deny(POLICY, "not_in_audience", message=NOT_FOUND, as_not_found=True)
    if action is Action.READ:
        return allow(POLICY, reads)
    if action is Action.DELETE and ctx.is_agent:
        # Ending a workspace ends every chat in it, collaborators' included: an
        # agent acting for its user never makes that call.
        return deny(POLICY, "agent_may_not_delete", message=AGENT_DELETE_DENIED_MESSAGE)
    if action is Action.DELETE and (facts.is_owner or facts.is_org_admin):
        # An org admin deletes a workspace they can read; one they cannot is
        # the same not-found as on every other door.
        if is_main:
            return deny(
                POLICY,
                "main_not_deletable",
                message=MAIN_DELETE_DENIED_MESSAGE,
                error_code=MAIN_DELETE_DENIED_CODE,
            )
        return _verified(facts, "owner_deletes" if facts.is_owner else "org_admin_deletes")
    if action is Action.RENAME:
        if not (facts.is_owner or role_may_rename(shared_role)):
            return deny(
                POLICY,
                "rename_rung_required",
                message=RENAME_DENIED_MESSAGE,
                error_code=RENAME_DENIED_CODE,
            )
        return _verified(facts, "writer_may_rename")
    if action is Action.WRITE:
        if not (facts.is_owner or role_may_send(shared_role)):
            return deny(
                POLICY,
                "write_rung_required",
                message=WRITE_DENIED_MESSAGE,
                error_code=WRITE_DENIED_CODE,
            )
        return _verified(facts, "writer_may_add_chat")
    # DELETE by a reader who is neither the owner nor an org admin: they can
    # see it, so the refusal need not hide it.
    return deny(POLICY, "owner_or_org_admin_required", message=DELETE_DENIED_MESSAGE)


def _creates_into_audience(facts: ObjectFacts) -> bool:
    return scope_readable(
        is_org_admin=facts.is_org_admin,
        team_ids=facts.team_ids,
        visibility_scope=facts.visibility_scope,
        is_owner=facts.is_owner,
    )


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
    Policy(name=POLICY, resource_type=ResourceType.WORKSPACE, decide=decide, audited_attrs=AUDITED)
)

__all__ = [
    "ADMIN_READS_PRIVATE_ATTR",
    "AGENT_CREATE_DENIED_CODE",
    "AGENT_CREATE_DENIED_MESSAGE",
    "AGENT_DELETE_DENIED_MESSAGE",
    "AUDITED",
    "BOUND_MACHINE_ATTR",
    "CONNECTION_ACTIONS",
    "DELETE_DENIED_MESSAGE",
    "IS_MAIN_ATTR",
    "MAIN_DELETE_DENIED_CODE",
    "MAIN_DELETE_DENIED_MESSAGE",
    "NOT_FOUND",
    "OWNER_DEPARTED_ATTR",
    "POLICY",
    "RENAME_DENIED_CODE",
    "RENAME_DENIED_MESSAGE",
    "SHARED_ROLE_ATTR",
    "SUPPORTED",
    "WRITE_DENIED_CODE",
    "WRITE_DENIED_MESSAGE",
    "decide",
]
