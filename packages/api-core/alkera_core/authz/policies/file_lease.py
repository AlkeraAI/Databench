"""Who may take, or force off, which kind of folder lease.

The Files ladder decides whether a caller may lease a folder at all
(``files.access`` and ``LEASE`` / ``LEASE_FORCE``). This policy decides the
part the ladder cannot see: the KIND of lease, and the kind of folder it lands
on or over. A lease is a claim to be a folder's one writer, and some claims
are a machine's alone:

* **A workspace lease is the bound box's.** It is taken on a native
  workspace's folder by the proven machine its chats are bound to, and by
  nobody else: not by a person, not by a box its operator's rung would admit,
  not on any folder that is not a workspace's. A box reaches a workspace's
  tree because the workspace runs on it, never through whoever signed it in.
* **A person's lease inside a workspace takes Full access.** Holding any part
  of a workspace's tree stops its box from taking the workspace when its
  chats wake, so a mount, a box mount or a share lease on or under a
  workspace's folder needs the rung that may also force a lease off; a
  collaborator who may only edit files cannot park the workspace. (The lease
  table already refuses a lease that would nest with a held one, whoever
  holds it.)
* **Forcing a box's lease off is the owner's or an org admin's, with a
  reason.** A box holding a chat's or a workspace's folder is in the middle
  of somebody's turn; Full access on a subfolder is not enough to cut it off,
  and the reason is on the decision row so whoever reads it later knows why.
* **A chat lease in a workspace is a box's.** Forcing one off is the owner's
  or an org admin's alone, so a caller with no proven machine never takes one
  on or under a workspace's folder: it would park the workspace whatever rung
  they hold. A proven box takes one there only on a chat's own folder, and on
  any chat's folder only when the chat is not bound to another machine.

Every other lease (a person's lease of any purpose outside a workspace, a
box's chat lease on a folder that is no chat's outside one) is the ladder's
answer alone. The route resolves every fact; nothing here reads the database.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from alkera_core.authz.decision import Decision, allow, deny
from alkera_core.authz.engine import Policy, register, require_attr
from alkera_core.authz.enums import Action, ResourceType
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource

POLICY = "files.lease_kind"
AUDITED = frozenset(
    {
        "purpose",
        "node_kind",
        "inside_kind",
        "readable",
        "machine_verified",
        "runs_here",
        "full_access",
        "owner_rung",
        "org_admin",
        "holder_purpose",
        "reason_given",
        # What the person forcing a box's lease off said, capped by the route:
        # the record says why a turn in flight was cut off.
        "reason",
    }
)

#: ``LEASE`` takes a lease; ``LEASE_FORCE`` forces one off.
SUPPORTED = frozenset({Action.LEASE, Action.LEASE_FORCE})

#: What a folder is, for this policy: a native workspace's folder, a chat's
#: folder, or anything else.
NODE_KINDS: Final = frozenset({"workspace", "chat", "plain"})
#: The lease purposes a person may take (a box's own are ``chat`` and
#: ``workspace``).
PERSON_PURPOSES: Final = frozenset({"mount", "box", "share"})
#: The purposes a box takes for the chats it serves.
BOX_PURPOSES: Final = frozenset({"chat", "workspace"})

#: The body code a visible refusal carries.
REFUSED = "files.lease_kind_refused"
NOT_FOUND = "Not found"


def _refuse(reason: str, *, readable: bool) -> Decision:
    """Opaque to a caller who may not read the folder, coded otherwise."""
    if not readable:
        return deny(POLICY, f"{reason}_unreadable", message=NOT_FOUND, as_not_found=True)
    return deny(POLICY, reason, message="Not allowed", error_code=REFUSED)


def decide(
    ctx: ActingContext, action: Action, resource: Resource, attrs: Mapping[str, object]
) -> Decision | None:
    if action not in SUPPORTED:
        return deny(POLICY, "action_not_supported", message="Not allowed")
    purpose = require_attr(attrs, "purpose", str)
    node_kind = require_attr(attrs, "node_kind", str)
    inside_kind = require_attr(attrs, "inside_kind", str)
    readable = require_attr(attrs, "readable", bool)
    machine_verified = require_attr(attrs, "machine_verified", bool)
    runs_here = require_attr(attrs, "runs_here", bool)
    full_access = require_attr(attrs, "full_access", bool)
    owner = require_attr(attrs, "owner_rung", bool)
    org_admin = require_attr(attrs, "org_admin", bool)
    holder_purpose = require_attr(attrs, "holder_purpose", str)
    reason_given = require_attr(attrs, "reason_given", bool)
    if node_kind not in NODE_KINDS or inside_kind not in NODE_KINDS:
        return deny(POLICY, "unknown_node_kind", message="Not allowed")

    if action is Action.LEASE_FORCE:
        if holder_purpose in BOX_PURPOSES:
            if not (owner or org_admin):
                return _refuse("box_lease_force_needs_owner", readable=readable)
            if not reason_given:
                return _refuse("box_lease_force_needs_reason", readable=readable)
            return allow(POLICY, "owner_forces_box_lease")
        return allow(POLICY, "ladder_decides_force")

    if purpose == "workspace":
        if node_kind != "workspace":
            return _refuse("workspace_lease_off_a_workspace", readable=readable)
        if not machine_verified:
            return _refuse("workspace_lease_needs_its_box", readable=readable)
        if not runs_here:
            return _refuse("workspace_runs_elsewhere", readable=readable)
        return allow(POLICY, "bound_box_takes_workspace")

    in_workspace = node_kind == "workspace" or inside_kind == "workspace"
    if purpose not in PERSON_PURPOSES:
        if purpose != "chat":
            return _refuse("unknown_purpose", readable=readable)
        if not machine_verified:
            # A chat lease is a box's claim, which only the owner or an org
            # admin may force off: a person holding one on or in a workspace
            # would stop its box from taking it, whatever rung they hold.
            if in_workspace:
                return _refuse("box_purpose_needs_its_box", readable=readable)
            return allow(POLICY, "ladder_decides_lease")
        if node_kind == "chat":
            if not runs_here:
                return _refuse("chat_runs_elsewhere", readable=readable)
            return allow(POLICY, "bound_box_takes_chat")
        if in_workspace:
            # In a workspace a box takes the workspace, or a chat's own folder;
            # a chat lease on any other folder there would park the workspace.
            return _refuse("chat_lease_off_a_chat", readable=readable)
        return allow(POLICY, "ladder_decides_lease")

    if in_workspace and not full_access:
        return _refuse("workspace_lease_needs_full_access", readable=readable)
    return allow(POLICY, "ladder_decides_lease")


register(
    Policy(
        name=POLICY,
        resource_type=ResourceType.FILE_LEASE,
        decide=decide,
        audited_attrs=AUDITED,
    )
)

__all__ = [
    "AUDITED",
    "BOX_PURPOSES",
    "NODE_KINDS",
    "PERSON_PURPOSES",
    "POLICY",
    "REFUSED",
    "SUPPORTED",
    "decide",
]
