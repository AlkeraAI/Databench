"""What a caller may do to one node of the Files tree.

The policy is a table over ``allowed_actions``, never over a role. The ladder
that produces that set lives in :mod:`alkera_core.files.authz`, so a different
ladder changes nothing here.

Three rules shape the branch order:

* **A node that is not there is decided here too.** ``exists`` is one more
  fact, not an early return in the caller: a missing node, a node in another
  org and a same-org node the caller may not read all get the same opaque
  not-found and leave the same decision row, so a caller cannot tell them
  apart by counting queries or watching a decision log.
* **403 only when the caller can already read.** A "forbidden" confirms the
  node exists, so a caller who cannot ``READ`` it gets the not-found whatever
  they asked; a caller who can read gets a ``403`` carrying :data:`FORBIDDEN`.
* **The flags are re-checked here.** The decider has already removed the
  actions a flag forbids, so these branches are a second net: if a decider bug
  leaves ``WRITE`` standing on a held node, the policy still refuses it and the
  decision row names the flag.

An agent never shares: a delegated principal cannot widen the reach of the
human it acts for, so it is refused before the table is consulted.

A box on its own machine credential (or a worker credential it minted) is
decided before either. It is a member of no org and holds no rung anyone
granted. What admits it is a positive fact (the node sits in the folder of a
chat or workspace bound to its machine, or under a live lease it holds), and
outside those folders every answer is the opaque not-found, so a box learns
nothing about a drive it does not run chats in. Inside them it meets the same
table as everyone else, and it never shares.

A copy reads its source, so the ladder hands ``copy`` to every rung that reads.
Four facts about the source still refuse it here, after the table:

* the node is in the trash (it is restored, or it is gone);
* its seal covers what it holds (``no_download`` without the self-only mark),
  since a copy would shed the seal. A chat folder is sealed self-only and its
  copy is stamped the same way, so a chat is the one sealed thing a person may
  duplicate;
* a folder above it is marked "do not pass this on", since the copy lands
  where the source's sharing rules do not follow;
* the caller reads it only on loan (every admitting grant carries an expiry or
  a condition), which the copy would outlive.
"""

from __future__ import annotations

from collections.abc import Mapping

from alkera_core.authz.decision import Decision, allow, deny
from alkera_core.authz.engine import Policy, register, require_attr
from alkera_core.authz.enums import Action, ResourceType
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource

POLICY = "files.access"
AUDITED = frozenset(
    {
        "exists",
        "in_org",
        "allowed_actions",
        "flags",
        "is_agent",
        "is_link",
        "drive_kind",
        "held",
        "locked",
        "trashed",
        "seal_self_only",
        "no_reshare_chain",
        "read_via_conditional_grant",
        "agent_machine_verified",
        "chat_subtree",
        "chat_bound_elsewhere",
        "workspace_subtree",
        "workspace_bound_elsewhere",
        "holds_lease",
        "machine_runs_it",
        "grant_within_rung",
    }
)

#: The body code a 403 carries. The caller can read the node, so naming the
#: refusal costs nothing they did not already know.
FORBIDDEN = "files.forbidden"

#: The detail text of the opaque denial: byte-identical to the one a
#: nonexistent id produces, so the two are indistinguishable.
NOT_FOUND = "Not found"

#: The action that decides whether a denial may be visible at all.
READ = Action.READ.value

#: The reason a decision row carries when the node was not there at all. It is
#: the row's only trace of the difference: the caller's answer is the same
#: opaque not-found an unreadable node produces.
NO_SUCH_NODE = "no_such_node"

#: What an agent may do only as the chat's OWN proven machine: take a folder's
#: lease over, change what is in it, and write the snapshot of what it did. The
#: two agent headers are a claim any member can put on their own session and a
#: box's id is public — it rides every chat that box serves — so an agent whose
#: assertion nobody checked against the registration is a person spelling a
#: public id, and none of the three is theirs. Proving the assertion is not the
#: whole answer either: one box credential serves every chat that box runs, so
#: the proof passes identically on a colleague's chat, and what settles which
#: chat is the box's own is the chat's own binding. Their own session still
#: reaches all three: this narrows what the assertion buys, never what the
#: human has.
MACHINE_ONLY = frozenset({Action.LEASE.value, Action.WRITE.value, Action.SNAPSHOT.value})

#: Flag → the actions it forbids outright, re-checked after the ladder.
HELD_BLOCKS = frozenset({Action.WRITE.value, Action.DELETE.value})
LOCKED_BLOCKS = frozenset({Action.WRITE.value})
FROZEN_FLAG = "frozen"
#: What survives a frozen drive. Mirrors ``files.authz.actions.READ_ONLY_ACTIONS``
#: as a table of platform action names so the policy has no Files import.
FROZEN_ALLOWS = frozenset(
    {Action.READ.value, Action.EXPORT.value, Action.LEASE_REQUEST.value, Action.COPY.value}
)
#: The flag that, without the self-only mark, seals a subtree: a copy of it
#: would carry the bytes out from under the seal.
NO_DOWNLOAD_FLAG = "no_download"

#: The body code the two reach refusals carry. Distinct from :data:`FORBIDDEN`
#: because it is not "you lack the rung": the caller may read the source and
#: may copy in general, and what is refused is making a second copy that the
#: restriction on the first can never reach. A surface that offers "duplicate"
#: and "save as template" says so with this rather than with a generic 403.
COPY_REFUSED = "files.copy_refused"

#: The body code a share above the sharer's own rung carries. Visible for the
#: same reason the copy refusals are: the caller holds ``SHARE`` and so reads
#: the node; what is refused is minting, or withdrawing, a rung they lack.
GRANT_EXCEEDS_RUNG = "files.grant_exceeds_rung"

#: The flag on a chat's own record — the manifest, the transcript and the
#: decision and cost logs, the digest, the runtime directory — and what it
#: refuses everyone but the machine holding the chat's lease. Mirrors
#: ``files.authz.decider.RECORD_BLOCKS`` as platform action names so the
#: policy has no Files import: a rewrite, a purge, and a restore of a trashed
#: record, which puts back what the chat had said was gone.
RECORD_FLAG = "record"
RECORD_BLOCKS = frozenset({Action.WRITE.value, Action.DELETE.value, Action.RESTORE.value})

#: The body code a person changing a chat's record gets. Its own code because
#: it is not "you lack the rung": the chat's owner, a member with edit and an
#: org admin all hold WRITE on the folder, and what is refused is the one thing
#: nobody but the box may do. A surface that offers "rename" or "delete" on a
#: file in a chat folder says why with this rather than with a generic 403.
CHAT_RECORD_READ_ONLY = "files.chat_record_read_only"


def _refuse(
    reason: str, *, allowed_actions: frozenset[str], error_code: str = FORBIDDEN
) -> Decision:
    """The refusal for a caller who has ``allowed_actions`` on this node.

    Opaque when they cannot read it — a 403 would confirm the node exists —
    and a coded 403 when they can. The visible code is the one thing that
    varies: an unreadable node is refused identically whatever the reason, so
    the code never leaks past the read gate.
    """
    if READ not in allowed_actions:
        return deny(POLICY, f"{reason}_unreadable", message=NOT_FOUND, as_not_found=True)
    return deny(POLICY, reason, message="Not allowed", error_code=error_code)


def decide(
    ctx: ActingContext, action: Action, resource: Resource, attrs: Mapping[str, object]
) -> Decision | None:
    exists = require_attr(attrs, "exists", bool)
    in_org = require_attr(attrs, "in_org", bool)
    allowed_actions = require_attr(attrs, "allowed_actions", frozenset)
    flags = require_attr(attrs, "flags", frozenset)
    is_agent = require_attr(attrs, "is_agent", bool)
    require_attr(attrs, "is_link", bool)
    require_attr(attrs, "drive_kind", str)
    held = require_attr(attrs, "held", bool)
    locked = require_attr(attrs, "locked", bool)
    trashed = require_attr(attrs, "trashed", bool)
    seal_self_only = require_attr(attrs, "seal_self_only", bool)
    no_reshare_chain = require_attr(attrs, "no_reshare_chain", bool)
    read_via_conditional_grant = require_attr(attrs, "read_via_conditional_grant", bool)
    agent_machine_verified = require_attr(attrs, "agent_machine_verified", bool)
    chat_subtree = require_attr(attrs, "chat_subtree", bool)
    chat_bound_elsewhere = require_attr(attrs, "chat_bound_elsewhere", bool)
    workspace_subtree = require_attr(attrs, "workspace_subtree", bool)
    workspace_bound_elsewhere = require_attr(attrs, "workspace_bound_elsewhere", bool)
    # What a machine runs: a chat's folder, or a native workspace's tree; and
    # whether the one governing this node runs on some other machine.
    machine_tree = chat_subtree or workspace_subtree
    elsewhere = chat_bound_elsewhere or workspace_bound_elsewhere
    holds_lease = require_attr(attrs, "holds_lease", bool)
    machine_runs_it = require_attr(attrs, "machine_runs_it", bool)
    grant_within_rung = require_attr(attrs, "grant_within_rung", bool)

    if not exists:
        # Never an allow and never a 403: there is nothing to be forbidden
        # from, and saying so would confirm which ids are free.
        return deny(POLICY, NO_SUCH_NODE, message=NOT_FOUND, as_not_found=True)

    if not in_org:
        # Never a 403: a caller outside the org is not told the node is there.
        return deny(POLICY, "not_in_org", message=NOT_FOUND, as_not_found=True)

    if ctx.is_machine:
        # A box on its own machine credential reaches the folders of the chats
        # bound to its machine — the chat folder and everything staged under
        # it — and nothing else on the drive. Every refusal outside them is the
        # opaque not-found whatever the ladder handed it: a box holds no rung a
        # person granted, so a folder it is turned away from is one it must not
        # learn is there. Inside them it is held to the same table as everyone
        # else (the actions it holds, the flags, the copy rules), and it never
        # shares: a machine hands out nobody's reach.
        if not machine_tree:
            return deny(POLICY, "machine_outside_chat", message=NOT_FOUND, as_not_found=True)
        if not agent_machine_verified:
            return deny(POLICY, "machine_unverified", message=NOT_FOUND, as_not_found=True)
        if elsewhere:
            return deny(POLICY, "machine_mismatch", message=NOT_FOUND, as_not_found=True)
        if not machine_runs_it:
            # Not bound elsewhere is not the same as bound here: a chat or
            # workspace no machine runs, or one shared with the whole org, is
            # no box's. The box is admitted only by the positive fact.
            return deny(POLICY, "machine_does_not_run", message=NOT_FOUND, as_not_found=True)
        if action is Action.SHARE:
            return _refuse("machine_may_not_share", allowed_actions=allowed_actions)

    if is_agent and action is Action.SHARE:
        return _refuse("agent_may_not_share", allowed_actions=allowed_actions)

    if is_agent and machine_tree and action.value in MACHINE_ONLY:
        # Decided before the rung, so the row says WHY: a member who holds edit
        # on a shared chat has every one of these actions from their own role,
        # and "action_not_allowed" would read as a permissions problem for a
        # caller whose permissions are fine. What they lack is the proof that
        # they are the box.
        if not agent_machine_verified:
            return _refuse("machine_unverified", allowed_actions=allowed_actions)
        if elsewhere:
            # A proven box, on a chat (or workspace) that is not the one it runs. Its own
            # reason, never the one above: the assertion is fine and re-sending
            # it will not help, and an operator reading the row has to be able
            # to tell a box that failed to prove itself from one that proved
            # itself on somebody else's conversation.
            return _refuse("machine_mismatch", allowed_actions=allowed_actions)

    if RECORD_FLAG in flags and action.value in RECORD_BLOCKS and not holds_lease:
        # Decided before the rung for the same reason the machine branches
        # are: the chat's owner holds every one of these actions on their own
        # folder, so "action_not_allowed" would read as a permissions problem
        # for a caller whose permissions are fine. The record is the box's to
        # write; the decider has already taken the actions away, and this is
        # the second net that names the reason on the row and in the body.
        return _refuse(
            "chat_record", allowed_actions=allowed_actions, error_code=CHAT_RECORD_READ_ONLY
        )

    if action.value not in allowed_actions:
        return _refuse("action_not_allowed", allowed_actions=allowed_actions)

    if action is Action.SHARE and not grant_within_rung:
        # A share hands out, and takes back, up to the sharer's own rung.
        # Without this, "Full access" — which is where SHARE lives — was a
        # one-request ladder to ``owner`` for the sharer themselves, for the
        # whole org, or over a real owner's grant.
        return _refuse(
            "grant_exceeds_rung", allowed_actions=allowed_actions, error_code=GRANT_EXCEEDS_RUNG
        )

    if held and action.value in HELD_BLOCKS:
        return _refuse("held", allowed_actions=allowed_actions)
    if locked and action.value in LOCKED_BLOCKS:
        return _refuse("locked", allowed_actions=allowed_actions)
    if FROZEN_FLAG in flags and action.value not in FROZEN_ALLOWS:
        return _refuse("frozen", allowed_actions=allowed_actions)
    if action is Action.COPY:
        if trashed:
            return _refuse("trashed", allowed_actions=allowed_actions)
        if NO_DOWNLOAD_FLAG in flags and not seal_self_only:
            return _refuse("sealed", allowed_actions=allowed_actions)
        if no_reshare_chain:
            # A copy lands in a drive the original's sharing rules do not
            # follow it into, so it is a re-share by another route. A folder
            # above the source having said "this goes no further" refuses it
            # exactly as the share verb is refused on the source itself.
            return _refuse("no_reshare", allowed_actions=allowed_actions, error_code=COPY_REFUSED)
        if read_via_conditional_grant:
            # Everything reading this source for the caller lapses — an expiry,
            # or a condition on who they still are. A copy does not: it would
            # outlive the grant it was taken under, which is the one way a
            # temporary read turns into a permanent one.
            return _refuse(
                "conditional_grant", allowed_actions=allowed_actions, error_code=COPY_REFUSED
            )

    if ctx.is_machine:
        return allow(
            POLICY, "machine_holds_workspace" if workspace_subtree else "machine_holds_chat"
        )
    return allow(POLICY, "allowed_action")


register(
    Policy(name=POLICY, resource_type=ResourceType.FILE_NODE, decide=decide, audited_attrs=AUDITED)
)

__all__ = [
    "AUDITED",
    "CHAT_RECORD_READ_ONLY",
    "COPY_REFUSED",
    "FORBIDDEN",
    "FROZEN_ALLOWS",
    "FROZEN_FLAG",
    "GRANT_EXCEEDS_RUNG",
    "HELD_BLOCKS",
    "LOCKED_BLOCKS",
    "MACHINE_ONLY",
    "NOT_FOUND",
    "NO_DOWNLOAD_FLAG",
    "NO_SUCH_NODE",
    "POLICY",
    "RECORD_BLOCKS",
    "RECORD_FLAG",
    "decide",
]
