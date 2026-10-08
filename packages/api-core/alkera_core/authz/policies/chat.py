"""Who may read, start, speak in and promote out of a chat.

A chat is a workspace object of type ``chat``: it belongs to an org, has an
owner, and is a node in the file tree. A chat is PRIVATE until it is shared.
Its owner reads it; the machine it is bound to reads it (on its own machine
credential, or as an agent on its operator's session whose assertion the route
verified); and anyone holding "Can view" or above on its node (granted there,
inherited from a folder, or granted to a team the caller is on) reads it.
Nobody else in the org does, and every refusal is the same opaque not-found
another org's member gets. The predicate is
:func:`~alkera_core.authz.chat_scope.chat_visible`, which the socket's channel
rules also call, so a chat readable over REST is readable over the socket. The
``visibility_scope`` records the audience a chat was created into (the creator
must be in it); it does not admit readers.

Sending needs the share ladder's WRITE rung. A reader's message is relayed to
the daemon (the document's one writer) but still drives the agent: the answer
lands in the shared document and is billed to the chat's owner. So the
audience says who may WATCH a chat and the rung on its node says who may
DRIVE it. Promoting a result creates a durable object in the org, so it is the
owner's or an org admin's, as is deleting the chat. Starting a chat requires
the creator to be in the audience it is addressed to.

``roles`` are the subject's roles in the ORG, resolved on the org root team,
not the chat's team. The chat's team narrows the audience afterwards; folding
the two together would refuse with the wrong reason and precedence.

Every fact is required on every branch, including branches that do not
consult it, so a route that forgets one is denied. A caller from another org
never reaches the policy (the engine answers an opaque not-found first), and
every denial that could confirm a chat exists is the same opaque not-found.
"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import replace

from alkera_core.authz.chat_scope import (
    PUBLISHER_ROLE,
    chat_read_reason,
    role_may_rename,
    role_may_send,
    scope_readable,
)
from alkera_core.authz.decision import Decision, allow, deny
from alkera_core.authz.engine import MissingAttributeError, Policy, register, require_attr
from alkera_core.authz.enums import Action, ResourceType, Role
from alkera_core.authz.policies._facts import (
    ObjectFacts,
    object_facts,
    optional_uuid,
    require_scope_team,
)
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource

POLICY = "chat.access"
#: Byte-identical to the engine's tenancy refusal and the route's answer for an
#: id nobody holds: an unshared member of the org must not be able to tell a
#: chat that exists from one that does not by the wording of its 404.
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
        "publisher_machine_ids",
        "shared_role",
        "bound_machine_id",
        "asserted_machine_verified",
        "admin_reads_private",
        "owner_departed",
        "in_multi_chat_workspace",
        "workspace_role",
        "payer_stands",
    }
)

#: The actions this policy decides. Anything else is refused outright rather
#: than falling through to a rule that was written for a different verb.
#: ``WRITE`` is the machine's: it reports whether it can publish the chat.
SUPPORTED = frozenset(
    {
        Action.READ,
        Action.CREATE,
        Action.SEND,
        Action.PROMOTE,
        Action.DELETE,
        Action.WRITE,
        Action.CLAIM,
        Action.RENAME,
        Action.LIST_CONNECTIONS,
        Action.LEASE_CONNECTION_CREDENTIAL,
        Action.ANSWER_STANDING,
    }
)
#: The two verbs a box opens on a chat to give its agent the owner's
#: connections: the names, then one credential at a time. Decided the same way
#: on the chat — the box holds it or is told it does not exist — and never a
#: person's door; the connection itself is decided separately, by its own
#: policy, on the owner's entitlement.
CONNECTION_ACTIONS = frozenset({Action.LIST_CONNECTIONS, Action.LEASE_CONNECTION_CREDENTIAL})
#: The fact the ``WRITE`` branch alone requires: the ids of the machines that
#: may speak for this chat — the one it is bound to and the org's current one.
PUBLISHER_MACHINES_ATTR = "publisher_machine_ids"
#: The fact the ``SEND`` branch alone consults: the rung the chat's node in the
#: file tree gives this caller, ``""`` when no grant reaches them. Required on
#: every branch like every other fact, so a door that forgets to resolve it is
#: denied rather than admitted by a branch that happened not to need it.
SHARED_ROLE_ATTR = "shared_role"
#: The machine the chat is bound to, ``""`` when none. The box that publishes
#: a chat reads the transcript it publishes, whoever signed the box in: an
#: agent whose asserted id IS this machine's id reads the chat — provided the
#: assertion is verified (below).
BOUND_MACHINE_ATTR = "bound_machine_id"
#: Whether the agent assertion on this request has been proven to be the
#: machine it names: a live workspace machine of the org whose registered
#: operator is the delegating user, on the very credential the box registered
#: with (``verify_machine_assertion``). The assertion is a header any member
#: can put on their own session, and a machine's id is on every chat it
#: serves, so without this fact "the bound machine" would be whoever typed its
#: id — the operator's browser included. A person's request carries
#: ``False``; no branch that admits a machine reads the asserted id without
#: it, and — like every other fact — it is required on every branch.
MACHINE_VERIFIED_ATTR = "asserted_machine_verified"
#: Whether an org admin reads a chat of their org that nobody shared with them.
#: ONE input, resolved in one place for every door, because "may an admin open
#: a member's private conversation" is a product decision and not a property of
#: whichever route the caller happened to knock on. ``False`` — the default the
#: deployment ships — is the chat surface's long-standing answer: a private
#: chat is the opaque not-found to everybody but its owner, its machine and the
#: people it was shared with, admins included. Flipping it to ``True`` admits an
#: org admin to the READ branch and to nothing else: governance verbs (DELETE,
#: PROMOTE) do not read it, and neither does RENAME, so an admin who may not
#: read a chat can still never retitle it.
ADMIN_READS_PRIVATE_ATTR = "admin_reads_private"

#: Whether the chat is in a workspace that holds several chats (a project or
#: a member's main workspace), where every chat's agent reads and writes the
#: workspace's shared tree. ``False`` for a workspace of one, whose folder is
#: the chat's own.
MULTI_CHAT_WORKSPACE_ATTR = "in_multi_chat_workspace"
#: The caller's rung on that workspace's folder, ``"owner"`` for its owner and
#: ``""`` when none reaches them or the chat is in a workspace of one.
WORKSPACE_ROLE_ATTR = "workspace_role"

#: Whether the chat's owner has left: deactivated, gone, or no longer in the
#: chat's org. Offboarding is that, and it is the one case an org admin
#: deletes a private chat they cannot read.
OWNER_DEPARTED_ATTR = "owner_departed"

#: Whether the chat's payer (the person its turns are billed to when no person
#: is at the box) may still read it: an active member of the chat's org who
#: passes READ on the chat, on the workspace for a chat in a workspace of
#: several. A writer may not spend the money of somebody who can no longer
#: open, stop or delete the chat. Resolved for a person only (a box never
#: sends), so it is required by the ``SEND`` branches alone: a send door that
#: forgets it is refused rather than admitted.
PAYER_STANDS_ATTR = "payer_stands"

#: The workspace owner's rung, as the standing resolver spells it (the Files
#: ladder's ``owner``, named here so this policy imports nothing of Files).
WORKSPACE_OWNER_ROLE = PUBLISHER_ROLE
#: The rungs on a workspace that may delete a chat in it: Full access and
#: Owner, the ladder's rungs that may delete.
DELETE_ROLES = frozenset({"manager", WORKSPACE_OWNER_ROLE})

SEND_DENIED_MESSAGE = "You need edit access to this chat to send a message."
WORKSPACE_SEND_DENIED_MESSAGE = "You need edit access to this workspace to send a message."
WORKSPACE_SEND_DENIED_CODE = "chat_send_requires_workspace_edit"
SEND_DENIED_CODE = "chat_send_requires_edit"
STANDING_ANSWER_DENIED_MESSAGE = (
    "Only the chat's owner can choose Always. Allow or reject this once."
)
STANDING_ANSWER_DENIED_CODE = "standing_answer_owner_only"

RENAME_DENIED_MESSAGE = "You need edit access to this chat to rename it."
RENAME_DENIED_CODE = "chat_rename_requires_edit"

DELETE_DENIED_MESSAGE = "Only the owner or an org admin can delete this chat."
PROMOTE_DENIED_MESSAGE = "Only the owner or an org admin can save results out of this chat."

PAYER_LOST_ACCESS_MESSAGE = (
    "The person this chat bills no longer has access to it. Start a new chat to continue."
)
PAYER_LOST_ACCESS_CODE = "payer_lost_access"

VERIFY_EMAIL_MESSAGE = "Verify your email address to perform this action."
VERIFY_EMAIL_CODE = "email_verification_required"


def _creates_into_audience(facts: ObjectFacts) -> bool:
    """The one place the scope string still admits anybody: a chat is created
    addressed to an audience, and its creator must be in it. The shared
    predicate decides, the team column having already been checked to agree
    with the scope."""
    return scope_readable(
        is_org_admin=facts.is_org_admin,
        team_ids=facts.team_ids,
        visibility_scope=facts.visibility_scope,
        is_owner=facts.is_owner,
    )


def _is_bound_machine(ctx: ActingContext, bound_machine_id: str, *, machine_verified: bool) -> bool:
    """Whether the caller is the machine the chat is bound to, speaking as
    itself: an agent whose asserted id IS the bound machine's id AND whose
    assertion the route verified as that machine. A person, an agent of any
    other id, or an agent naming the right id on a session that did not
    register it, is not."""
    return (
        bool(bound_machine_id)
        and machine_verified
        and ctx.is_agent
        and ctx.acting_principal.id == bound_machine_id
    )


def _reads(
    ctx: ActingContext,
    facts: ObjectFacts,
    *,
    shared_role: str,
    bound_machine_id: str,
    machine_verified: bool,
    admin_reads_private: bool,
) -> str | None:
    """The reason this caller may read the chat, or ``None``.

    Deferred whole to :func:`~alkera_core.authz.chat_scope.chat_read_reason`,
    which is the same answer the objects listing, the chat rail and the socket's
    channel rules are built from. The policy resolves the one fact that is not a
    plain input — whether the caller IS the machine the chat is bound to, which
    depends on the acting context and on the route having verified the assertion
    — and hands the rest over. The reason it returns is what the decision row
    carries, so an auditor can tell a share from ownership, and ownership from
    the deployment's admin setting, without re-deriving any of it.
    """
    return chat_read_reason(
        is_owner=facts.is_owner,
        is_bound_machine=_is_bound_machine(
            ctx, bound_machine_id, machine_verified=machine_verified
        ),
        shared_role=shared_role,
        is_org_admin=facts.is_org_admin,
        admin_reads_private=admin_reads_private,
    )


def decide(
    ctx: ActingContext, action: Action, resource: Resource, attrs: Mapping[str, object]
) -> Decision | None:
    if action not in SUPPORTED:
        return deny(POLICY, "action_not_supported", message="Not allowed")
    facts = object_facts(ctx, attrs)
    team_id = optional_uuid(attrs, "team_id")
    shared_role = require_attr(attrs, SHARED_ROLE_ATTR, str)
    bound_machine_id = require_attr(attrs, BOUND_MACHINE_ATTR, str)
    machine_verified = require_attr(attrs, MACHINE_VERIFIED_ATTR, bool)
    admin_reads_private = require_attr(attrs, ADMIN_READS_PRIVATE_ATTR, bool)
    in_multi_chat_workspace = require_attr(attrs, MULTI_CHAT_WORKSPACE_ATTR, bool)
    workspace_role = require_attr(attrs, WORKSPACE_ROLE_ATTR, str)
    owner_departed = require_attr(attrs, OWNER_DEPARTED_ATTR, bool)
    # The chat's own owner, before a workspace's ownership stands in for it
    # below: a standing answer becomes a rule of the person who owns the chat.
    owns_the_chat = facts.is_owner
    if in_multi_chat_workspace:
        # The chat belongs to its workspace. Whoever started it holds nothing
        # extra there: who owns it is who owns the workspace, the rung that
        # decides every verb is the rung on the workspace, and a rung granted
        # on the chat alone (refused now, read-only before) counts for
        # nothing. Removing someone from the workspace removes the chat from
        # them, theirs or not.
        facts = replace(facts, is_owner=workspace_role == WORKSPACE_OWNER_ROLE)
        shared_role = workspace_role
        owner_departed = False
    if ctx.is_machine:
        # Decided before membership is asked about: a box is a member of no
        # org, and the only thing that admits it is holding the chat.
        return _machine_holds_chat(ctx, action, attrs, bound_machine_id=bound_machine_id)
    if action in CONNECTION_ACTIONS:
        # The connections a chat may use are its owner's, handed to the box
        # that runs the chat so the agent has them without ever holding the
        # owner's login. A person has their own door for their own
        # connections; here they are told nothing, whatever their standing.
        return deny(POLICY, "machine_required", message=NOT_FOUND, as_not_found=True)
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
        # Nothing exists yet, but the chat is already addressed to an audience
        # and its creator — the owner-to-be a route names — must be in it. The
        # resource names the id being created, so the decision row still
        # points at a chat.
        if not _creates_into_audience(facts):
            return deny(POLICY, "not_in_audience", message=NOT_FOUND, as_not_found=True)
        return _verified(facts, "org_member_creates")
    if action is Action.WRITE:
        # The machine reports on the chat it serves whoever signed the box in:
        # its delegating user's own rights on the chat decide nothing here.
        return _machine_reports(ctx, attrs, machine_verified=machine_verified)
    if action is Action.CLAIM:
        # A spare is warmed for one person and is theirs alone: not the
        # machine that holds its session, not an org admin, not anyone a share
        # could reach (a spare has none). Everyone else is told it does not
        # exist — which, as far as they are concerned, is the truth.
        if not facts.is_owner:
            return deny(POLICY, "spare_owner_required", message=NOT_FOUND, as_not_found=True)
        return _verified(facts, "owner_claims")
    if action is Action.DELETE and facts.is_owner:
        return _verified(facts, "owner_deletes")
    if action is Action.DELETE and facts.is_org_admin and owner_departed:
        # Offboarding: the owner is gone, and an org admin removes their
        # conversations without opening one. An active member's private chat
        # stays the same not-found to an admin on DELETE as everywhere else.
        return _verified(facts, "org_admin_offboards")
    reads = _reads(
        ctx,
        facts,
        shared_role=shared_role,
        bound_machine_id=bound_machine_id,
        machine_verified=machine_verified,
        admin_reads_private=admin_reads_private,
    )
    if reads is None:
        # A chat is private until it is shared. Everyone else in the audience
        # (the chat's team, the whole org, its admins) is told the chat does
        # not exist, the same opaque answer another org's member gets.
        return deny(POLICY, "not_in_audience", message=NOT_FOUND, as_not_found=True)
    if action is Action.READ:
        return allow(POLICY, reads)
    if action is Action.DELETE and in_multi_chat_workspace and workspace_role in DELETE_ROLES:
        # Ending a chat of a shared workspace is the workspace's Full access.
        return _verified(facts, "workspace_manager_deletes")
    if action is Action.DELETE and facts.is_org_admin:
        # An org admin deletes a chat they can read (shared with them, or a
        # deployment that opens private chats to admins). One they cannot read
        # is the same not-found here as on every other door, so DELETE is never
        # a way to learn that a private chat exists or to end it unseen.
        return _verified(facts, "org_admin_deletes")
    if action is Action.SEND and in_multi_chat_workspace:
        # In a workspace that holds several chats the agent reads and writes the
        # whole shared tree, so driving it takes edit access on the WORKSPACE,
        # decided on every message. Owning the chat is not enough (a
        # collaborator whose workspace share was revoked keeps reading their
        # chat and stops driving it), and neither is a rung granted on the chat
        # alone, which reaches the transcript and not the tree.
        if not role_may_send(workspace_role):
            return deny(
                POLICY,
                "workspace_send_rung_required",
                message=WORKSPACE_SEND_DENIED_MESSAGE,
                error_code=WORKSPACE_SEND_DENIED_CODE,
            )
        return _payer_stands(attrs) or _verified(facts, "workspace_writer_may_send")
    if action is Action.SEND:
        # Reading is not the gate. A message drives the agent, and what the
        # agent answers is written into everybody's copy of the conversation —
        # so it takes the rung somebody granted on the chat's node, or being
        # the owner whose chat it is. The refusal is a plain 403, not the
        # opaque not-found: the caller can already read this chat, so there is
        # nothing left to hide by pretending it does not exist.
        if not (facts.is_owner or role_may_send(shared_role)):
            return deny(
                POLICY,
                "send_rung_required",
                message=SEND_DENIED_MESSAGE,
                error_code=SEND_DENIED_CODE,
            )
        return _payer_stands(attrs) or _verified(facts, "writer_may_send")
    if action is Action.ANSWER_STANDING:
        # "Always allow" is recorded as a rule of the chat's owner, and the
        # agent applies it in the owner's other chats on the same box without
        # asking. Anyone else who may speak here answers once: a collaborator's
        # call must never become the owner's standing consent.
        if not owns_the_chat:
            return deny(
                POLICY,
                "standing_answer_owner_required",
                message=STANDING_ANSWER_DENIED_MESSAGE,
                error_code=STANDING_ANSWER_DENIED_CODE,
            )
        return _verified(facts, "owner_answers_standing")
    if action is Action.RENAME:
        # A chat's title is the name of its folder in the tree, and naming a
        # node is the Files ladder's WRITE — so the ladder decides it, the same
        # way it decides every other name in the drive: "Can edit" and up, or
        # being the owner whose chat it is. An org admin with no rung renames
        # nothing; before this branch existed the rename lived on the object
        # route under a rule that asked about team scope, which let exactly the
        # wrong people through in both directions.
        if not (facts.is_owner or role_may_rename(shared_role)):
            return deny(
                POLICY,
                "rename_rung_required",
                message=RENAME_DENIED_MESSAGE,
                error_code=RENAME_DENIED_CODE,
            )
        return _verified(facts, "writer_may_rename")
    if action is Action.PROMOTE and (facts.is_owner or facts.is_org_admin):
        # Promoting is NOT governance the way deleting is: it lifts a turn's
        # result out of the conversation and mints a durable object in the org
        # that carries the content — so it sits BELOW the read gate, and an org
        # admin who may not open the chat may not take anything out of it
        # either. Anywhere above this line, an admin nobody shared the chat with
        # would be handed its content through the object policy, which has no
        # idea the object came out of a private conversation.
        return _verified(facts, "owner_promotes")
    # DELETE or PROMOTE by a reader who is neither the owner nor an org admin:
    # they can see the chat, so the refusal need not hide it.
    return deny(
        POLICY,
        "owner_or_org_admin_required",
        message=DELETE_DENIED_MESSAGE if action is Action.DELETE else PROMOTE_DENIED_MESSAGE,
    )


def _machine_holds_chat(
    ctx: ActingContext, action: Action, attrs: Mapping[str, object], *, bound_machine_id: str
) -> Decision:
    """A box on its own credential reaches the chats bound to it and nothing
    else. It reads the transcript it publishes, reads the connections the
    chat's owner may use and leases their credentials one at a time, and
    reports its publisher state; it never sends, renames, deletes, promotes or
    claims — those are a person's verbs, and the box has no person behind it.
    A chat it does not hold is the opaque not-found a stranger gets, whatever
    it asked."""
    holds = bool(bound_machine_id) and ctx.acting_principal.id == bound_machine_id
    if action is Action.READ or action in CONNECTION_ACTIONS:
        if holds:
            return allow(POLICY, "machine_holds_chat")
        return deny(POLICY, "machine_does_not_hold_chat", message=NOT_FOUND, as_not_found=True)
    if action is Action.WRITE:
        machines = publisher_machines(attrs)
        if _may_report(ctx, machines, holds=holds, bound_machine_id=bound_machine_id):
            return allow(POLICY, "machine_holds_chat")
        return deny(POLICY, "machine_does_not_hold_chat", message=NOT_FOUND, as_not_found=True)
    return deny(POLICY, "machine_action_not_allowed", message=NOT_FOUND, as_not_found=True)


def _may_report(
    ctx: ActingContext, machines: frozenset[str], *, holds: bool, bound_machine_id: str
) -> bool:
    """Whether a box on its own credential may report the chat's publisher
    state: the machine the chat is bound to, or — while the chat is bound to
    nothing — a machine that may speak for it. A platform box is never an
    org's current machine, so a chat another box holds is never its to report
    on."""
    return holds or (not bound_machine_id and ctx.acting_principal.id in machines)


def _machine_reports(
    ctx: ActingContext, attrs: Mapping[str, object], *, machine_verified: bool
) -> Decision:
    """The chat's publisher state is the machine's to report: an agent whose
    asserted id names one of the machines that may speak for the chat — the
    one it is bound to, or the org's current workspace machine (the box that
    now serves the org, which may still say it cannot publish a chat bound to
    the box it replaced) — and whose assertion the route verified as that
    machine. A person, an agent of any other id, or a session naming a machine
    it did not register is not a machine and is refused; the email gate does
    not apply, since no human acts here."""
    machines = publisher_machines(attrs)
    if machine_verified and ctx.is_agent and ctx.acting_principal.id in machines:
        return allow(POLICY, "bound_machine_reports")
    return deny(POLICY, "publisher_machine_required", message="Not allowed")


def publisher_machines(attrs: Mapping[str, object]) -> frozenset[str]:
    """The ``publisher_machine_ids`` fact: a set of machine ids as strings.
    Anything else — a list, a set of UUID objects, a lone string — reads as
    missing, and a missing fact denies."""
    held = attrs.get(PUBLISHER_MACHINES_ATTR)
    if not isinstance(held, AbstractSet) or not all(isinstance(item, str) for item in held):
        raise MissingAttributeError(PUBLISHER_MACHINES_ATTR)
    return frozenset(item for item in held if isinstance(item, str))


def _payer_stands(attrs: Mapping[str, object]) -> Decision | None:
    """The refusal when the chat's payer can no longer read it, else ``None``.

    Asked after the sender's own rung, so a caller who may not send at all is
    told that first. The refusal is a plain 403: the sender can read the chat,
    and what it says is what they can do about it (a new chat bills them)."""
    if require_attr(attrs, PAYER_STANDS_ATTR, bool):
        return None
    return deny(
        POLICY,
        "payer_lost_access",
        message=PAYER_LOST_ACCESS_MESSAGE,
        error_code=PAYER_LOST_ACCESS_CODE,
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


register(Policy(name=POLICY, resource_type=ResourceType.CHAT, decide=decide, audited_attrs=AUDITED))

__all__ = [
    "ADMIN_READS_PRIVATE_ATTR",
    "AUDITED",
    "BOUND_MACHINE_ATTR",
    "MACHINE_VERIFIED_ATTR",
    "MULTI_CHAT_WORKSPACE_ATTR",
    "NOT_FOUND",
    "OWNER_DEPARTED_ATTR",
    "PAYER_LOST_ACCESS_CODE",
    "PAYER_LOST_ACCESS_MESSAGE",
    "PAYER_STANDS_ATTR",
    "POLICY",
    "PUBLISHER_MACHINES_ATTR",
    "RENAME_DENIED_CODE",
    "RENAME_DENIED_MESSAGE",
    "SEND_DENIED_CODE",
    "SEND_DENIED_MESSAGE",
    "SHARED_ROLE_ATTR",
    "STANDING_ANSWER_DENIED_CODE",
    "STANDING_ANSWER_DENIED_MESSAGE",
    "SUPPORTED",
    "VERIFY_EMAIL_CODE",
    "VERIFY_EMAIL_MESSAGE",
    "WORKSPACE_ROLE_ATTR",
    "WORKSPACE_SEND_DENIED_CODE",
    "WORKSPACE_SEND_DENIED_MESSAGE",
    "decide",
    "publisher_machines",
]
