"""How grants, the caller and a node's facts become an :class:`EffectiveAccess`.

:class:`LadderDecider` is pure — no session, no clock, no request — so every
branch below is reachable from a unit test with plain data in and plain data
out, which is what makes the branch table in ``packages/api-core/tests/authz``
exhaustive rather than aspirational.

The order is fixed, and every step but the signpost rule only ever removes:

1. the cross-org gate — a node in another org yields no role and ``in_org``
   false, and the engine turns that into an opaque not-found;
2. the candidate grants — those whose principal is one of the caller's (their
   own user id, the teams their materialized memberships put them in, the org)
   and whose conditions and expiry hold;
3. the effective role, the ladder's maximum over those candidates, widened by
   the org-admin descent rule (an org owner/admin is at least ``manager`` on any
   drive of their org);
4. the signpost rule, the one step that adds: a traversal-only container carries
   no grants by design, so every member of its org gets ``READ`` on it and
   nothing else — otherwise the drive root, which is the only entrance a client
   has, is refused to everyone but an org admin;
5. the node flags, which narrow for *everyone*, managers and owners included;
6. the agent confinement, which drops ``SHARE`` unconditionally, drops the three
   actions only the chat's own proven machine may take, and empties the set
   outside the leased subtree.

Nothing here ever adds an action after a flag has removed it, so a reader can
check "can this flag be escaped?" by reading downward once.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from alkera_core.authz.principal import ActingContext
from alkera_core.chat_records import is_chat_record_name
from alkera_core.files.authz.actions import READ_ONLY_ACTIONS, FilesAction
from alkera_core.files.authz.grants import (
    CONDITION_TEAM_ROLE,
    CONDITION_TEAM_ROLE_ADMIN,
    CallerIdentity,
    Grant,
    Principal,
    grant_admits,
    principal_matcher,
)
from alkera_core.files.authz.ladder import DEFAULT_LADDER, ROLE_WRITER, RoleLadder
from alkera_core.files.history import subject_ref
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode


class NodeFlag(StrEnum):
    """A restriction that narrows every role on a node. There are no deny ACEs
    in Files; this is the only way access is ever reduced."""

    #: Readers get metadata and preview; nobody gets the bytes out.
    NO_DOWNLOAD = "no_download"
    #: Nobody may grant on this node.
    NO_RESHARE = "no_reshare"
    #: Only the lock holder may write.
    LOCKED = "locked"
    #: A legal hold: nobody writes and nobody deletes, holder included.
    HELD = "held"
    #: The drive is read-only (org teardown, over quota).
    FROZEN = "frozen"
    #: A chat's own record: only the machine proven to hold the chat's lease
    #: may change it.
    RECORD = "record"


#: ``file_nodes.flags`` is a bitfield; these are the bits Files defines today.
NO_DOWNLOAD_BIT = 1 << 0
NO_RESHARE_BIT = 1 << 1
HELD_BIT = 1 << 2
#: Marks a folder whose contents are DELIVERABLES — what a run is meant to hand
#: back — rather than the conversation's own working material. It is not a
#: decision the decider reads (the capability it changes is NO_DOWNLOAD, which
#: it clears at create time); it exists so the exception descends with the
#: folder instead of being re-derived from a name at every level.
ARTIFACT_BIT = 1 << 3
#: Marks a node whose NO_DOWNLOAD covers ITSELF and nothing beneath it. A chat
#: folder carries it: the conversation is not a thing that leaves as a file (nor
#: as a zip of the folder it is stored in), but the attachments a person gave
#: it, the outputs it produced and the scratch it worked in are theirs to take.
#: Every other sealed folder — a report, a held subtree — still seals what it
#: holds, which is why this is a mark a creator sets rather than a rule about
#: chats hidden inside the descent.
SEAL_SELF_ONLY_BIT = 1 << 4
#: Marks a node that is a chat's own RECORD rather than its working
#: material: the manifest that pins the agent session, the transcript and the
#: decision and cost logs the next box replays, the digest that hashes them,
#: the runtime directory and everything inside it. A person with write on the
#: chat may read them, but only the machine proven to hold the chat's lease may
#: write, rename, move, trash or restore one — otherwise a colleague with a
#: shared chat could edit the record of what was said, and the next box would
#: rebuild a gated tool call from a transcript somebody else wrote. It is a
#: mark rather than a name check at decision time so the rule survives a
#: rename and descends with the runtime folder; the mark itself is set from
#: the name at BIRTH, by :func:`flags_for_child`, which is the one place the
#: name rule is read.
RECORD_BIT = 1 << 5

#: What a new child takes from its parent. The decider reads only the node's
#: OWN ``flags``, never its chain, so a restriction that is supposed to cover
#: a subtree has to be stamped on each node as it is created. Otherwise a
#: scratch file inside a NO_DOWNLOAD chat folder would be downloadable.
#:
#: ``RECORD`` descends so the runtime directory covers what the harness writes
#: inside it, however deep. ``SEAL_SELF_ONLY`` is deliberately absent: it says
#: something about the node it sits on, so passing it down would
#: seal-but-not-seal every level in turn.
INHERITED_FLAGS = NO_DOWNLOAD_BIT | ARTIFACT_BIT | RECORD_BIT


def flags_for_child(
    parent_flags: int,
    *,
    artifact: bool = False,
    parent_subtype: str | None = None,
    name: bytes | str | None = None,
) -> int:
    """The ``flags`` a node created under ``parent_flags`` is born with.

    Two things stop NO_DOWNLOAD descending, and they answer different needs.
    ``artifact`` is the exception for a DELIVERABLE: a report the member asked
    for and cannot download is not a report. It is sticky — the marked folder
    passes it on — so everything the run writes beneath it is downloadable too.
    ``SEAL_SELF_ONLY`` on the parent is the exception for a CONTAINER that is
    itself un-exportable but holds ordinary files: the chat folder, whose
    working material a person opens, edits and takes away like any other file.

    ``parent_subtype`` and ``name`` are the birth of a RECORD: a child made
    directly under a chat's folder under one of the chat's record names is
    marked as the chat's record, whoever makes it — the mark follows the name
    and the position, never the writer, so a person who plants a file under a
    record's name before the box's first push has planted a record they may
    not touch again. A record folder (the runtime directory) passes the mark
    down through the inheritance above. Every creation path hands both in; a
    caller that hands neither is making nothing a chat folder holds.
    """
    inherited = parent_flags & INHERITED_FLAGS
    if parent_flags & SEAL_SELF_ONLY_BIT:
        inherited &= ~NO_DOWNLOAD_BIT
    if artifact:
        inherited |= ARTIFACT_BIT
    if inherited & ARTIFACT_BIT:
        inherited &= ~NO_DOWNLOAD_BIT
    if parent_subtype == CHAT_SUBTYPE and name is not None and is_chat_record_name(name):
        inherited |= RECORD_BIT
    return inherited


_FLAG_BITS: Mapping[NodeFlag, int] = {
    NodeFlag.NO_DOWNLOAD: NO_DOWNLOAD_BIT,
    NodeFlag.NO_RESHARE: NO_RESHARE_BIT,
    NodeFlag.HELD: HELD_BIT,
    NodeFlag.RECORD: RECORD_BIT,
}

#: The node state that means "locked"; the rest of the states say nothing about
#: who may act, only about what the tree is doing.
STATE_LOCKED = "locked"

#: The role the org-admin descent rule floors every org admin at.
ORG_ADMIN_FLOOR = "manager"

#: The rung a proven machine holds inside the folder of a chat it runs: what
#: running the chat takes — read, write, lease and snapshot — and nothing a
#: person decides about the chat: no share, no force-take, no purge.
MACHINE_RUNG = ROLE_WRITER

#: What only a proven machine may do inside a chat's folder: take the lease over
#: it, change what is in it, and write the snapshot of what it did. A person
#: reaches all three through their own session; an agent reaches them only by
#: being the box, which is a fact the caller resolves rather than one the
#: request asserts.
MACHINE_ONLY_ACTIONS: frozenset[FilesAction] = frozenset(
    {FilesAction.LEASE, FilesAction.WRITE, FilesAction.SNAPSHOT}
)

#: What a RECORD refuses everyone but the machine holding the chat's lease.
#: Renaming, moving and trashing ride WRITE; a purge rides DELETE; and
#: RESTORE is here because bringing a trashed record back is as much a rewrite
#: of what the chat says happened as trashing it was. READ, EXPORT, COPY and
#: COMMENT stay: a record is a thing a person may see, not one they may change.
RECORD_BLOCKS: frozenset[FilesAction] = frozenset(
    {FilesAction.WRITE, FilesAction.DELETE, FilesAction.RESTORE}
)


@dataclass(frozen=True, slots=True)
class AccessFacts:
    """Everything about the caller and the world that the decision needs, all
    resolved by the caller so the decider stays pure.

    ``team_ids`` comes from the *materialized* membership rows, which already
    carry every ancestor team — so an admin of a parent team is admin below it
    without this module walking a chain.
    """

    #: The teams the caller is a member of, ancestors included.
    team_ids: frozenset[uuid.UUID] = frozenset()
    #: The subset of those teams the caller administers.
    team_admin_ids: frozenset[uuid.UUID] = frozenset()
    #: The caller is an owner or admin of the org that owns this drive.
    org_admin: bool = False
    #: Whether the org-admin floor reaches INTO a chat's folder. A chat is
    #: private until its owner shares it, and every door onto the chat asks one
    #: setting before letting an unshared org admin in; the folder behind the
    #: chat holds the transcript, the attachments and the working tree, so it
    #: asks the same one. ``False`` — the fail-closed default a caller that has
    #: not resolved it gets — leaves an org admin inside a chat folder with
    #: exactly the rung the chat's owner granted them, which for an unshared
    #: chat is none. Outside chat folders the floor applies as it always has.
    org_admin_reaches_chats: bool = False
    is_agent: bool = False
    is_link: bool = False
    #: The node an agent's lease is rooted at; an agent outside it gets nothing.
    leased_subtree: uuid.UUID | None = None
    #: The machine an agent request was PROVEN to be, ``None`` for everyone
    #: else. The assertion itself is two headers any member can put on their
    #: own session, and a box's id is public — it rides every chat the box
    #: serves — so the id as sent proves nothing. Only the caller may fill this
    #: in, and only after checking the assertion against the registration (a
    #: live workspace machine of the org, registered by the operator behind
    #: this request, on the very credential the request carries). Left ``None``
    #: an agent keeps what its user may read and loses every action that
    #: changes a chat's folder.
    agent_machine_id: str | None = None
    #: The nodes whose LIVE lease the caller proved it holds on this request —
    #: the fence it sent (epoch + instance) names the lease's current holder.
    #: A chat folder is ``NO_DOWNLOAD`` for everyone, its owner included; the
    #: one reader that must still get the bytes is the box running the chat,
    #: and this is how the decider tells that box from a person: the box is an
    #: agent, its assertion was VERIFIED, and the bytes it asks for sit under a
    #: folder it holds the lease on. Resolved by the route from the lease table
    #: against the identity the server derived — never from the caller's word,
    #: which for an agent is a header naming a public id.
    held_leases: frozenset[uuid.UUID] = frozenset()
    #: The chat folders in reach of this request whose chat runs on a machine
    #: that is NOT the one on ``agent_machine_id``. A box holds ONE credential
    #: for every chat it serves, so the assertion that proves it is a machine
    #: proves it on every chat folder in the org at once; what says WHICH chat
    #: is its own is the chat row's binding, and that is a per-node fact the
    #: route resolves. Empty for everyone whose assertion was not verified —
    #: they have already lost the three actions this narrows — and empty when
    #: every chat in reach is either this machine's or bound to none at all,
    #: which is the state a chat sits in before a box has taken it.
    chats_bound_elsewhere: frozenset[uuid.UUID] = frozenset()
    #: The chat folders in reach of this request whose chat runs on the machine
    #: on ``agent_machine_id``: bound to it. A chat bound to no machine is no
    #: box's (placement binds a chat before its box touches the folder). The
    #: box's rung inside those
    #: folders is its own, :data:`MACHINE_RUNG`, and owes nothing to whoever
    #: operates it — the org-admin floor stops at a chat's folder, and the box
    #: running a member's chat is not the admin who signed it in. Empty for
    #: everyone whose assertion was not verified, which is how a revoked or
    #: forged machine gets nothing from it.
    chats_run_here: frozenset[uuid.UUID] = frozenset()
    #: The native workspace folders in reach of this request that run on the
    #: machine on ``agent_machine_id``: some live chat of the workspace is
    #: bound to it. A workspace's shared tree is reached by the box through
    #: this binding and its lease, never through the rung of whoever operates
    #: the box. Empty for everyone whose assertion was not verified.
    workspaces_run_here: frozenset[uuid.UUID] = frozenset()
    #: The native workspace folders in reach whose chats are bound to some
    #: OTHER machine and none to this one: a box proven on its own workspace is
    #: proven on every workspace in the org, and this is what tells them apart.
    workspaces_bound_elsewhere: frozenset[uuid.UUID] = frozenset()
    #: Who holds the lock, when the node is locked.
    lock_holder_id: uuid.UUID | None = None
    #: When "now" is, for grant expiry. ``None`` means expiry is not evaluated
    #: here because the source already filtered it.
    now: datetime | None = None
    #: The open bag ABAC conditions and future facts read from.
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EffectiveAccess:
    """What one caller may do to one node.

    ``role`` is internal: it never leaves ``files/authz``. Everything outside
    consumes ``allowed_actions``, the capabilities built from it, or a typed
    ``Authorized`` handle.
    """

    role: str | None
    allowed_actions: frozenset[str]
    flags: frozenset[str]
    in_org: bool
    #: ``NO_RESHARE`` is set on this node or on one of the folders above it.
    #: ``flags`` carries only the node's OWN bits, and re-sharing is the one
    #: restriction a subtree has to answer for as a whole: a folder marked
    #: "do not pass this on" is not passed on by copying what is inside it into
    #: a drive the marker does not reach.
    no_reshare_chain: bool = False
    #: Every grant that gave this caller READ carries an expiry or a condition.
    #: Such a read is on loan — it lapses, or it lasts only while the caller
    #: still administers the team it was made to — so what it admits must not be
    #: turned into a permanent second copy the grant can no longer reach.
    #: ``False`` whenever any plain grant, ownership of the org or the signpost
    #: rule would have given the same read anyway.
    read_via_conditional_grant: bool = False
    #: This node is a chat's folder or lives inside one. Resolved here because
    #: it is a question about the chain, and read by the policy: what only a
    #: proven machine may do is scoped to the folders a machine runs.
    in_chat_subtree: bool = False
    #: The chat this node belongs to runs on a machine other than the one the
    #: request proved it is. A question about the chain like the one above —
    #: the binding sits on the chat folder, which may be several levels up —
    #: and read by the policy so a box turned away from a colleague's chat is
    #: told it is the wrong machine rather than an unproven one.
    chat_bound_elsewhere: bool = False
    #: This node is a native workspace's folder or lives in its shared tree,
    #: with no chat's folder between them (a chat's records under ``.chats/``
    #: are the chat's). Read by the policy for the same reason the chat
    #: subtree is: what only a proven machine may do is scoped to the folders
    #: a machine runs.
    in_workspace_subtree: bool = False
    #: The workspace this node belongs to runs on a machine other than the one
    #: the request proved it is.
    workspace_bound_elsewhere: bool = False
    #: The caller is the proven machine holding a live lease that covers this
    #: node — the one writer a chat's RECORD admits. Resolved here from the
    #: leases the route verified and the chain, and read by the policy so a
    #: person refused a record is told it is read-only rather than that they
    #: lack a rung they may well hold.
    holds_lease: bool = False
    #: The caller is the proven machine running this node: the chat or
    #: workspace governing it is bound to that machine, or the machine holds a
    #: live lease covering it. The one positive fact that admits a box on its
    #: own credential; read by the policy, which refuses a box without it.
    machine_runs_it: bool = False

    def allows(self, action: FilesAction) -> bool:
        return action.value in self.allowed_actions


class AccessDecider(Protocol):
    """How grants become an :class:`EffectiveAccess`."""

    def decide(
        self,
        ctx: ActingContext,
        node: FileNode,
        chain: Sequence[FileNode],
        grants: Sequence[Grant],
        drive: FileDrive,
        facts: AccessFacts,
    ) -> EffectiveAccess: ...


def node_flags(node: FileNode, drive: FileDrive) -> frozenset[NodeFlag]:
    """The restrictions in force on ``node``: its own bits, its state, and the
    drive-wide freeze."""
    flags = {flag for flag, bit in _FLAG_BITS.items() if node.flags & bit}
    if node.state == STATE_LOCKED:
        flags.add(NodeFlag.LOCKED)
    if drive.frozen_reason is not None:
        flags.add(NodeFlag.FROZEN)
    return frozenset(flags)


def _caller_user_id(ctx: ActingContext) -> uuid.UUID:
    """Who the caller counts as: the acting user, or the user an agent or
    personal access token is delegating for.

    Folded through ``subject_ref``, so a subject whose id is not a UUID (a
    service principal) still has one identity to match grants and lock holders
    against instead of ``None``, which matched a user grant never and its own
    lock never either. The fold cannot collide with a real user id, so a
    non-user subject still satisfies no ``user`` grant."""
    return subject_ref(ctx)


def _names(
    principal: Principal, ctx: ActingContext, facts: AccessFacts, *, user_id: uuid.UUID | None
) -> bool:
    if ctx.is_machine:
        # A box (on its machine credential or a worker credential) is named by
        # no grant. Both carry an org id, the customer's for a worker and for a
        # box the org runs itself, so an "everyone in the org" grant would
        # otherwise name the box too.
        return False
    matcher = principal_matcher(principal.kind)
    if matcher is None:
        return False
    return matcher(
        principal,
        CallerIdentity(
            user_id=user_id,
            team_ids=facts.team_ids,
            team_admin_ids=facts.team_admin_ids,
            org_id=ctx.acting_principal.org_id,
            extra=facts.extra,
        ),
    )


def names_caller(principal: Principal, ctx: ActingContext, facts: AccessFacts) -> bool:
    """Whether a grant naming ``principal`` would name this caller.

    The same match the decider makes when it picks a node's candidate grants,
    for a surface that holds a principal but no grant: the change feed asking
    whether a withdrawn grant was this caller's.
    """
    return _names(principal, ctx, facts, user_id=_caller_user_id(ctx))


def _in_subtree(node: FileNode, chain: Sequence[FileNode], root_id: uuid.UUID) -> bool:
    """``node`` is ``root_id`` or lives under it.

    Read off the ancestor chain the caller already resolved: ``path_ids`` labels
    are per-drive inos, so a node id cannot be recognised in a path without a
    second query, and the chain is the same derived truth without one.
    """
    if node.id == root_id:
        return True
    return any(ancestor.id == root_id for ancestor in chain)


CHAT_SUBTYPE = "chat"
#: The ``subtype`` of a workspace's own folder: ``files/`` and ``.chats/`` below
#: it are where that workspace's chats work.
WORKSPACE_SUBTYPE = "workspace"
#: The object folders a machine runs: a chat's, and a native workspace's.
_RUN_SUBTYPES = frozenset({CHAT_SUBTYPE, WORKSPACE_SUBTYPE})


def _innermost_run_folder(node: FileNode, chain: Sequence[FileNode]) -> FileNode | None:
    """The nearest chat or workspace folder at or above ``node``: the one
    whose binding governs it. A chat's records under a workspace's ``.chats/``
    are the chat's; the workspace's shared tree is the workspace's."""
    for folder in (node, *reversed(chain)):
        if folder.subtype in _RUN_SUBTYPES:
            return folder
    return None


def _in_workspace_subtree(node: FileNode, chain: Sequence[FileNode]) -> bool:
    """``node`` is governed by a native workspace's folder (its shared tree,
    its records folder, the folder itself), not by a chat inside it."""
    folder = _innermost_run_folder(node, chain)
    return folder is not None and folder.subtype == WORKSPACE_SUBTYPE


def _workspace_bound_elsewhere(
    node: FileNode, chain: Sequence[FileNode], facts: AccessFacts
) -> bool:
    """The workspace governing ``node`` runs on some OTHER machine."""
    if not facts.workspaces_bound_elsewhere:
        return False
    folder = _innermost_run_folder(node, chain)
    return (
        folder is not None
        and folder.subtype == WORKSPACE_SUBTYPE
        and folder.id in facts.workspaces_bound_elsewhere
    )


def _in_chat_subtree(node: FileNode, chain: Sequence[FileNode]) -> bool:
    """``node`` is a chat's folder, or lives inside one.

    Read off the ancestor chain the caller already resolved, and off the node's
    own ``subtype`` rather than off its name: what makes a folder a chat is the
    object behind it, and a rule keyed on ``.alkerachat`` would be escaped by a
    rename.
    """
    if node.subtype == CHAT_SUBTYPE:
        return True
    return any(ancestor.subtype == CHAT_SUBTYPE for ancestor in chain)


def _in_private_tree(node: FileNode, chain: Sequence[FileNode]) -> bool:
    """``node`` is in a tree a chat works in: a chat's folder, or a workspace's
    folder with the shared tree its chats read and write. The org-admin floor
    stops at both, the way the chat and workspace doors refuse an admin
    nobody shared them with."""
    subtypes = (CHAT_SUBTYPE, WORKSPACE_SUBTYPE)
    return node.subtype in subtypes or any(ancestor.subtype in subtypes for ancestor in chain)


def _chat_bound_elsewhere(node: FileNode, chain: Sequence[FileNode], facts: AccessFacts) -> bool:
    """A chat folder on this node's path runs on some OTHER machine.

    Read off the chain for the same reason the subtree test is: a file three
    levels inside a chat is governed by the binding on the chat folder above
    it, and a rule that only looked at the node itself would confine the box
    on the folder and let it write everything in it. Any chat on the path
    answering "not yours" settles it — the fail-closed reading of a tree that
    should not nest chats in the first place.
    """
    if not facts.chats_bound_elsewhere:
        return False
    if node.id in facts.chats_bound_elsewhere:
        return True
    return any(ancestor.id in facts.chats_bound_elsewhere for ancestor in chain)


def _runs_here(node: FileNode, chain: Sequence[FileNode], facts: AccessFacts) -> bool:
    """The chat whose folder holds ``node`` runs on the machine this request
    proved it is.

    Decided on the INNERMOST chat folder on the path — the one whose binding
    governs the node — so a chat nested in another chat's tree is its own
    machine's, not its parent's. Only a verified agent asks: a person, and an
    agent whose assertion nobody checked, run no chat.
    """
    if not facts.is_agent or facts.agent_machine_id is None:
        return False
    folder = _innermost_run_folder(node, chain)
    if folder is None:
        return False
    if folder.subtype == WORKSPACE_SUBTYPE:
        return folder.id in facts.workspaces_run_here
    return folder.id in facts.chats_run_here


def _holds_lease_over(node: FileNode, chain: Sequence[FileNode], facts: AccessFacts) -> bool:
    """The caller is the box holding a live lease that covers ``node``.

    Only an agent counts: what this feeds — the download escape, and the
    machine rung a departing holder keeps while it finishes — is for the
    runtime that serves a chat, never for a person who mounted the folder; the
    platform's own process is not a download.

    And only a PROVEN one. The route resolves ``held_leases`` from the lease
    table against the identity it derived, so an unverified assertion already
    matches nothing; the second reading here is the fail-closed one, because
    the escape this feeds is the single way bytes leave a sealed chat, and an
    agent that never proved which machine it is must not be the caller it opens
    for whatever a future route hands in.
    """
    if not facts.is_agent or facts.agent_machine_id is None or not facts.held_leases:
        return False
    return any(_in_subtree(node, chain, root) for root in facts.held_leases)


def _grant_is_conditional(grant: Grant) -> bool:
    """A grant that does not stand on its own: it runs out, or it holds only
    while some other fact about the caller does."""
    return grant.expires_at is not None or bool(grant.conditions)


def _no_reshare_chain(node: FileNode, chain: Sequence[FileNode]) -> bool:
    """``NO_RESHARE`` anywhere from the node up to the root of its chain."""
    if node.flags & NO_RESHARE_BIT:
        return True
    return any(ancestor.flags & NO_RESHARE_BIT for ancestor in chain)


class LadderDecider:
    """Today's decider: the ladder's maximum, then the flags, then the agent
    confinement."""

    def __init__(self, ladder: RoleLadder = DEFAULT_LADDER) -> None:
        self._ladder = ladder

    def decide(
        self,
        ctx: ActingContext,
        node: FileNode,
        chain: Sequence[FileNode],
        grants: Sequence[Grant],
        drive: FileDrive,
        facts: AccessFacts,
    ) -> EffectiveAccess:
        flags = node_flags(node, drive)
        flag_names = frozenset(flag.value for flag in flags)
        # The tenancy floor as the platform engine asks it: the caller's own
        # org for everyone, plus the orgs a box on its machine credential is
        # assigned to serve — whose drives are the only ones it ever addresses.
        if not ctx.serves(node.org_team_id):
            return EffectiveAccess(None, frozenset(), flag_names, in_org=False)

        user_id = _caller_user_id(ctx)
        candidates = [
            grant for grant in grants if self._is_candidate(grant, ctx, facts, user_id=user_id)
        ]
        role = self._ladder.max([grant.role for grant in candidates])
        in_chat = _in_chat_subtree(node, chain)
        in_workspace = _in_workspace_subtree(node, chain)
        # The floor stops at a chat's folder, and at a workspace's, unless the
        # deployment opens chats to org admins: inside one, an admin nobody
        # shared it with is a stranger to it, the way they are to its own doors.
        # And an agent never carries its operator's floor into a workspace's
        # tree: a box reaches that tree because the workspace runs on it, never
        # because an admin signed the box in.
        floor_applies = (
            facts.org_admin
            and (facts.org_admin_reaches_chats or not _in_private_tree(node, chain))
            and not (facts.is_agent and in_workspace)
        )
        if floor_applies and self._ladder.rank(ORG_ADMIN_FLOOR) > self._ladder.rank(role):
            role = ORG_ADMIN_FLOOR
        # The box running a chat holds a rung of its own on the chat's folder,
        # decided by the chat's binding, so a member's chat stays runnable by
        # the org's box when the admin who operates it may not open it. The
        # box holding the folder's LIVE lease keeps that rung until it lets
        # go, whatever the binding says now: the binding decides who may take
        # a folder, the lease decides who may finish on it. A chat moved off
        # a box mid-turn leaves that box the only copy of its last writes,
        # and the fence (epoch, instance, the identity the server derived)
        # already refuses every other box — so the departing holder's final
        # push and release land instead of being turned away as a stranger's.
        holder = _holds_lease_over(node, chain, facts)
        machine_runs_it = _runs_here(node, chain, facts) or holder
        if machine_runs_it and self._ladder.rank(MACHINE_RUNG) > self._ladder.rank(role):
            role = MACHINE_RUNG

        actions = set(self._ladder.actions_for(role))
        # A box is no member, so the signposts are not its to list either:
        # with no grant and no floor, a box outside what it runs holds no
        # action at all, and a listing that filters on this decision alone
        # shows it nothing the policy would refuse it.
        if node.traversal_only and not ctx.is_machine:
            # `/`, `home/` and `Teams/` carry no grants — one there would trickle
            # down and hand every member every drive — so without this a member
            # who is not an org admin has no role on the drive root and the whole
            # surface answers the opaque 404. They are signposts: every member
            # may list them, and READ is all the rule adds, so the container
            # stays unwritable and ungrantable. What a listing then shows is cut
            # by the same decision per child, so `home/` still names one home to
            # the person who owns it and nobody else's.
            actions.add(FilesAction.READ)
        actions = self._narrow(actions, flags, facts, user_id=user_id, holder=holder)
        # A chat bound to another box is not "elsewhere" to the box still
        # holding its lease: that box is the one the binding moved away from,
        # and it is finishing, not intruding.
        bound_elsewhere = _chat_bound_elsewhere(node, chain, facts) and not holder
        ws_elsewhere = _workspace_bound_elsewhere(node, chain, facts) and not holder
        actions = self._confine(
            actions, node, chain, facts, bound_elsewhere=bound_elsewhere or ws_elsewhere
        )
        return EffectiveAccess(
            role=role,
            allowed_actions=frozenset(action.value for action in actions),
            flags=flag_names,
            in_org=True,
            no_reshare_chain=_no_reshare_chain(node, chain),
            read_via_conditional_grant=self._read_is_on_loan(
                candidates,
                standing_read=floor_applies or machine_runs_it or node.traversal_only,
            ),
            in_chat_subtree=in_chat,
            chat_bound_elsewhere=bound_elsewhere,
            in_workspace_subtree=in_workspace,
            workspace_bound_elsewhere=ws_elsewhere,
            holds_lease=holder,
            machine_runs_it=machine_runs_it,
        )

    # -- steps ------------------------------------------------------------

    def _is_candidate(
        self,
        grant: Grant,
        ctx: ActingContext,
        facts: AccessFacts,
        *,
        user_id: uuid.UUID | None,
    ) -> bool:
        if ctx.is_machine:
            # A box holds no rung a person granted: what it reaches comes from
            # the machine rung alone, through the binding of the chat or
            # workspace it runs or the live lease it holds. Every grant a
            # person made names people, teams or the org, and a box is none.
            return False
        # Expiry is always judged: a caller that resolved no clock is judged
        # at the wall clock, never as though the grant could not run out.
        return grant_admits(
            grant,
            CallerIdentity(
                user_id=user_id,
                team_ids=facts.team_ids,
                team_admin_ids=facts.team_admin_ids,
                org_id=ctx.acting_principal.org_id,
                extra=facts.extra,
            ),
            now=facts.now if facts.now is not None else datetime.now(UTC),
        )

    def _read_is_on_loan(self, candidates: Sequence[Grant], *, standing_read: bool) -> bool:
        """Whether every grant that reads this node for the caller is temporary.

        ``standing_read`` is the caller whose read owes nothing to a grant at
        all — an org admin, or the signpost rule on a traversal-only container —
        and settles it: their read cannot lapse, so it is not on loan. Otherwise
        the answer is over the grants that actually READ: a caller holding one
        plain grant and three expiring ones is reading on the plain one, and a
        caller holding only expiring ones is reading on borrowed time. No grant
        that reads at all means the read came from somewhere else (or there is
        no read), which is not a loan either.
        """
        if standing_read:
            return False
        reading = [grant for grant in candidates if self._role_reads(grant.role)]
        if not reading:
            return False
        return all(_grant_is_conditional(grant) for grant in reading)

    def _role_reads(self, role: str) -> bool:
        return FilesAction.READ in self._ladder.actions_for(role)

    def _narrow(
        self,
        actions: set[FilesAction],
        flags: frozenset[NodeFlag],
        facts: AccessFacts,
        *,
        user_id: uuid.UUID | None,
        holder: bool = False,
    ) -> set[FilesAction]:
        if NodeFlag.FROZEN in flags:
            actions &= set(READ_ONLY_ACTIONS)
        if NodeFlag.NO_DOWNLOAD in flags and not holder:
            # The bytes may not leave the platform — except to the box that is
            # running the chat, which pulls the folder it leases into the
            # directory the agent works in. ``holder`` is true only for an agent
            # inside a subtree whose live lease it proved it holds (the same
            # fence that lets it write there); a person holding the same lease,
            # or the box on a folder it does not hold, is still refused.
            actions.discard(FilesAction.EXPORT)
        if NodeFlag.NO_RESHARE in flags:
            actions.discard(FilesAction.SHARE)
        if NodeFlag.LOCKED in flags and (user_id is None or facts.lock_holder_id != user_id):
            actions.discard(FilesAction.WRITE)
        if NodeFlag.HELD in flags:
            actions.discard(FilesAction.WRITE)
            actions.discard(FilesAction.DELETE)
        if NodeFlag.RECORD in flags and not holder:
            # The manifest, the logs, the digest and the runtime directory are
            # what the chat SAYS happened. A person with write on a shared chat
            # — its owner and an org admin included — may read them and may not
            # rewrite them; the one writer is the machine proven to hold the
            # chat's lease, which is the process that produces them. The
            # owner's own words reach the record through that box, never
            # through the drive.
            actions -= RECORD_BLOCKS
        return actions

    def _confine(
        self,
        actions: set[FilesAction],
        node: FileNode,
        chain: Sequence[FileNode],
        facts: AccessFacts,
        *,
        bound_elsewhere: bool = False,
    ) -> set[FilesAction]:
        if not facts.is_agent:
            return actions
        actions.discard(FilesAction.SHARE)
        machine_tree = _in_chat_subtree(node, chain) or _in_workspace_subtree(node, chain)
        if (facts.agent_machine_id is None or bound_elsewhere) and machine_tree:
            # Everything that takes a chat's folder over or writes the record of
            # what happened in it belongs to the box running the chat, and being
            # that box is two facts, not one. The assertion is the first: anyone
            # may send the two headers on their own session and anyone who can
            # see a chat can read the machine id off it, so an agent whose
            # assertion nobody verified is a person spelling a public id. The
            # binding is the second: a box holds ONE credential for every chat
            # it serves, so the proof it is a machine passes identically on a
            # colleague's chat, and only the chat's own machine may hold,
            # rewrite or snapshot it. Failing either, the agent keeps its
            # user's reads and loses the three actions that act as the machine.
            actions -= MACHINE_ONLY_ACTIONS
        if facts.leased_subtree is None or not _in_subtree(node, chain, facts.leased_subtree):
            return set()
        return actions


#: The decider the product runs on.
DEFAULT_DECIDER = LadderDecider()


def rung_within(access: EffectiveAccess, role: str, ladder: RoleLadder = DEFAULT_LADDER) -> bool:
    """Whether ``role`` is at or below the rung ``access`` holds.

    The ceiling on a share: a person hands out, and takes back, up to what
    they hold and nothing above it, so ``SHARE`` on the manager rung cannot
    mint an ``owner``. A role the ladder does not know ranks below every rung
    and is never above the caller's; the grant path refuses it on its own.
    """
    return ladder.rank(role) <= ladder.rank(access.role)


def effective_role(
    ctx: ActingContext,
    node: FileNode,
    chain: Sequence[FileNode],
    grants: Sequence[Grant],
    drive: FileDrive,
    facts: AccessFacts,
    *,
    decider: AccessDecider = DEFAULT_DECIDER,
) -> EffectiveAccess:
    """The one function route facts, policy attrs and ``capabilities`` are built
    from. ``decider`` is a parameter so the platform engine's implementation
    slots in without a caller changing."""
    return decider.decide(ctx, node, chain, grants, drive, facts)


__all__ = [
    "ARTIFACT_BIT",
    "CHAT_SUBTYPE",
    "CONDITION_TEAM_ROLE",
    "CONDITION_TEAM_ROLE_ADMIN",
    "DEFAULT_DECIDER",
    "HELD_BIT",
    "INHERITED_FLAGS",
    "MACHINE_ONLY_ACTIONS",
    "MACHINE_RUNG",
    "NO_DOWNLOAD_BIT",
    "NO_RESHARE_BIT",
    "ORG_ADMIN_FLOOR",
    "RECORD_BIT",
    "RECORD_BLOCKS",
    "SEAL_SELF_ONLY_BIT",
    "STATE_LOCKED",
    "WORKSPACE_SUBTYPE",
    "AccessDecider",
    "AccessFacts",
    "EffectiveAccess",
    "LadderDecider",
    "NodeFlag",
    "effective_role",
    "flags_for_child",
    "names_caller",
    "node_flags",
    "rung_within",
]
