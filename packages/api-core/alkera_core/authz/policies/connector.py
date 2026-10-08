"""What may be done to a connection: fetch its shared credential, or move it
between owners.

Every credential denial past the action check is an opaque "Connection not
found": a connection a member is not entitled to must never leak, and the same
404 covers a disabled row, a per-user row and a row with no stored secret (the
gates the credential route re-checks on every call).

A row with an ``owner_user_id`` is one person's own connection, and only that
person may open it. The entitlement query already says so; the owner clause
here repeats it against the subject's own id rather than trusting a caller's
computed boolean, because a bug that widened entitlement would otherwise hand
one member's warehouse credential to their team.

A box on its own machine credential is a member of nothing, so entitlement is
asked about the person it acts for: the OWNER of the chat or workspace the box
holds (sharing a workspace shares every connection its owner may use). The
route resolves that owner's entitlement and the binding; the policy compares
the binding against the box itself rather than trusting a "holds" boolean, so
a route that resolved the wrong chat or workspace cannot hand out a
credential. The same personal-row and enabled gates apply as for the person,
in the same order. One difference: a per-user row is the owner's own grant,
which a box may lease for the owner (the route hands out the owner's token,
never anybody else's); a person's door still hands out shared bundles only.

Moving a connection carries its stored credential to a new audience, so it asks
for both ends: the caller must already manage the row where it is, and must
administer where it is going. "Just me" is the one destination that needs no
second admin, because the only person it reaches is the caller. A caller who
cannot even see the row is refused as a not-found, exactly as a credential
fetch is; one who can see it but does not manage it gets a plain refusal,
because the row is already on their screen and hiding it would only puzzle
them. An agent is refused outright: a chat that could re-address its own
workspace's credential to a team is a credential leak with a human's name on
it.
"""

from __future__ import annotations

from collections.abc import Mapping

from alkera_core.authz.decision import Decision, allow, deny
from alkera_core.authz.engine import Policy, register, require_attr
from alkera_core.authz.enums import Action, PrincipalKind, ResourceType
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource

POLICY = "connector.credential"
NOT_FOUND = "Connection not found"
NOT_MANAGER = "You can't change this connection."
NOT_DEST_ADMIN = "You're not an admin of the team you're moving this connection to."
AGENT_REFUSED = "An agent can't change who a connection is for."
SUPPORTED = frozenset({Action.FETCH_CREDENTIAL, Action.MOVE})
#: The facts a machine's fetch alone requires: the person the box acts for
#: (the owner of the chat it opened the door through), the machine that chat
#: is bound to, and the chat itself so the decision row names which chat the
#: credential left the server for.
FOR_USER_ATTR = "for_user_id"
CHAT_BOUND_MACHINE_ATTR = "chat_bound_machine_id"
CHAT_ATTR = "chat_id"
#: The workspace a box opened the door through, ``""`` on the chat door;
#: ``CHAT_BOUND_MACHINE_ATTR`` then names the machine holding the workspace.
WORKSPACE_ATTR = "workspace_id"
AUDITED = frozenset(
    {
        "member_entitled",
        "enabled",
        "auth_mode",
        "has_shared_secret",
        "team_id",
        "owner_user_id",
        FOR_USER_ATTR,
        CHAT_BOUND_MACHINE_ATTR,
        CHAT_ATTR,
        WORKSPACE_ATTR,
        "source_visible",
        "source_managed",
        "dest_personal",
        "dest_admin",
        "dest_team_id",
    }
)


def _decide_move(ctx: ActingContext, attrs: Mapping[str, object]) -> Decision:
    """Both ends of the move, in the order that leaks least."""
    if ctx.subject.kind is not PrincipalKind.USER:
        return deny(POLICY, "user_required", message=NOT_FOUND, as_not_found=True)
    if ctx.is_agent:
        return deny(POLICY, "agent_cannot_move", message=AGENT_REFUSED)
    if not require_attr(attrs, "source_visible", bool):
        return deny(POLICY, "not_visible", message=NOT_FOUND, as_not_found=True)
    if not require_attr(attrs, "source_managed", bool):
        return deny(POLICY, "not_source_manager", message=NOT_MANAGER)
    # The caller is the only person a personal connection reaches.
    if require_attr(attrs, "dest_personal", bool):
        return allow(POLICY, "move_to_own")
    if not require_attr(attrs, "dest_admin", bool):
        return deny(POLICY, "not_dest_admin", message=NOT_DEST_ADMIN)
    return allow(POLICY, "move_to_administered_team")


def _credential_gates(
    attrs: Mapping[str, object], *, for_user: str, owner_grant: bool = False
) -> Decision | None:
    """The gates on the row itself, shared by the person's fetch and the
    machine's: entitlement of the person acted for, then the personal-row
    owner clause against that same person, then the row's own switches.
    ``None`` means every gate passed. ``owner_grant``: the machine leasing for
    the owner, for whom a per-user row's own grant is the credential."""
    if not require_attr(attrs, "member_entitled", bool):
        return deny(POLICY, "not_entitled", message=NOT_FOUND, as_not_found=True)
    # Empty means a team row, which the entitlement clause above already settled.
    owner = require_attr(attrs, "owner_user_id", str)
    if owner and owner != for_user:
        return deny(POLICY, "not_owner", message=NOT_FOUND, as_not_found=True)
    if not require_attr(attrs, "enabled", bool):
        return deny(POLICY, "connection_disabled", message=NOT_FOUND, as_not_found=True)
    auth_mode = require_attr(attrs, "auth_mode", str)
    if owner_grant and auth_mode == "per_user":
        # The owner's own grant is leased for them by the route; there is no
        # shared bundle to require.
        return None
    if auth_mode != "shared":
        return deny(POLICY, "auth_mode_not_shared", message=NOT_FOUND, as_not_found=True)
    if not require_attr(attrs, "has_shared_secret", bool):
        return deny(POLICY, "no_shared_secret", message=NOT_FOUND, as_not_found=True)
    return None


def _decide_machine_fetch(ctx: ActingContext, attrs: Mapping[str, object]) -> Decision:
    """A box on its machine credential leases a credential for the owner of a
    chat or workspace it holds. The binding is compared here, against the
    box's own id, exactly as the chat and workspace policies compare it: a
    route that resolved one bound to another machine, or to none, is refused
    before any fact about the connection is read. Exactly one of the chat and
    the workspace is named. The person acted for must be named: a chat or
    workspace with no owner acts for nobody and gets nothing."""
    bound = require_attr(attrs, CHAT_BOUND_MACHINE_ATTR, str)
    if not bound or bound != ctx.acting_principal.id:
        return deny(POLICY, "machine_does_not_hold_chat", message=NOT_FOUND, as_not_found=True)
    chat = require_attr(attrs, CHAT_ATTR, str)
    workspace = require_attr(attrs, WORKSPACE_ATTR, str)
    if bool(chat) == bool(workspace):
        return deny(POLICY, "scope_unnamed", message=NOT_FOUND, as_not_found=True)
    for_user = require_attr(attrs, FOR_USER_ATTR, str)
    if not for_user:
        return deny(POLICY, "chat_has_no_owner", message=NOT_FOUND, as_not_found=True)
    refused = _credential_gates(attrs, for_user=for_user, owner_grant=True)
    if refused is not None:
        return refused
    scope = "chat" if chat else "workspace"
    per_user = require_attr(attrs, "auth_mode", str) == "per_user"
    return allow(POLICY, f"machine_for_{scope}_owner" + ("_grant" if per_user else ""))


def decide(
    ctx: ActingContext, action: Action, resource: Resource, attrs: Mapping[str, object]
) -> Decision | None:
    if action is Action.MOVE:
        return _decide_move(ctx, attrs)
    if action is not Action.FETCH_CREDENTIAL:
        return deny(POLICY, "action_not_supported", message="Not allowed")
    if ctx.is_machine:
        return _decide_machine_fetch(ctx, attrs)
    if ctx.subject.kind is not PrincipalKind.USER:
        return deny(POLICY, "user_required", message=NOT_FOUND, as_not_found=True)
    refused = _credential_gates(attrs, for_user=ctx.subject.id)
    if refused is not None:
        return refused
    return allow(
        POLICY, "owner" if require_attr(attrs, "owner_user_id", str) else "member_entitled"
    )


register(
    Policy(name=POLICY, resource_type=ResourceType.CONNECTOR, decide=decide, audited_attrs=AUDITED)
)

__all__ = [
    "AGENT_REFUSED",
    "AUDITED",
    "CHAT_ATTR",
    "CHAT_BOUND_MACHINE_ATTR",
    "FOR_USER_ATTR",
    "NOT_DEST_ADMIN",
    "NOT_FOUND",
    "NOT_MANAGER",
    "POLICY",
    "SUPPORTED",
    "WORKSPACE_ATTR",
    "decide",
]
