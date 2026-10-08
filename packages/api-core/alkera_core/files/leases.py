"""Folder leases: one holder owns a subtree's writes for as long as it beats.

A lease is what lets a folder be mounted locally and written at local-disk
speed while everyone else keeps reading the last synced state. Correctness does
not come from the lease being "checked". It comes from three things that are
each a single statement:

* **acquire** is one ``INSERT … ON CONFLICT DO UPDATE … WHERE`` whose predicate
  is the whole grant rule, so two clients racing for the same folder cannot both
  win however the service above them is scheduled;
* **heartbeat** is one ``UPDATE … WHERE epoch = $e AND holder_instance_id = $i``,
  so a holder that was paused past its TTL and superseded learns it from zero
  rows updated rather than from a read it might have taken before the reaper;
* **every fenced write** re-reads the lease row inside its own transaction,
  locked ``FOR NO KEY UPDATE`` (the strength the row's own updates take), so a
  reap that commits mid-request fences the write that has not committed yet.

The lease row sits AFTER the drive, the parent and the node in the fixed Files
lock order, and its in-flight entries (``file_lease_live_entries``) sit after
it. Two transactions that both mean to update the row must take it at update
strength from the start: a ``FOR SHARE`` that is later upgraded is the textbook
deadlock, where each holds the share the other needs to leave. So the fence
takes ``FOR NO KEY UPDATE`` for a writer, and ``FOR SHARE`` only for a read that
holds nothing else open (:data:`LeaseLock`). The cost is that two writers under
ONE lease serialise on its row for the length of their metadata transaction,
which is short and free of object-store I/O. Readers under the lease, and
writers under different leases, never queue.

**A transaction that holds only the lease row updates it once.** Postgres
re-verifies a row's foreign keys on the SECOND update of that row inside one
transaction (the first update's own version is what the second one finds, and
a row this transaction wrote is always re-checked), and the check takes ``FOR
KEY SHARE`` on ``file_nodes`` for the leased folder: a lock on the node taken
after the lease row, the reverse of the order above. The writers' gate on the
folder is a no-key lock (``FilesRepo.lock_lease_gate``), which a key share does
not queue behind, but the rule stands on its own: a row-only transaction that
asks for the folder at any strength is asking for a row above its own in the
fixed order. The heartbeat and the holder's live batch hold nothing but the
lease row, so each updates it once however much it has to say. The synced
stamp a batched beat carries rides the beat's UPDATE, and the batch's stamp
rides its sequence bump (``alkera_core.files.lease_live``). Stamping in a
second statement deadlocks against the holder's own pushes and tree reports,
with the folder held by one transaction and the row by the other.

Every statement here means the same thing by a live lease (``released_at IS
NULL AND reaped_at IS NULL AND expires_at > now()``), and reaped is the half
that is easy to forget. The reaper aborts the holder's upload sessions and
retracts everything it said was in flight, and it stamps ``reaped_at`` on
whatever row is there at that instant, including one a beat has just pushed an
hour into the future. A predicate that read only the deadline would go on
admitting the writes that teardown undid.

Epochs are ``(restore_generation << 32) + seq``. The generation is a platform
row the restore runbook bumps before the database is opened for writes, so an
epoch issued after a restore is above every epoch issued before it, including
the ones the restore lost. Within one generation the high-water-mark table is
the second net: it is raised on every acquire, so a restore that loses
``file_leases`` alone still cannot re-issue an epoch it already handed out.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal, cast

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.authz.enums import PrincipalKind
from alkera_core.authz.principal import ActingContext
from alkera_core.config import Settings, get_settings
from alkera_core.db.locking import LockRank, Strength, lock_rows, lock_text
from alkera_core.files import errors
from alkera_core.files.checkpoints import Checkpoints, NoopCheckpoints
from alkera_core.files.clock import Clock
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.repo import SUBTREE_DEPTH_BIND, FilesRepo, subtree_sql
from alkera_core.models.files.leases import FileLease
from alkera_core.models.files.tree import FileNode

#: The width of the sequence half of an epoch. One generation may issue this
#: many epochs for a single node before it would reach the next generation,
#: which is four billion mounts of one folder between two restores.
EPOCH_SEQ_BITS: Final = 32
#: The TTL a deployment that configures nothing gets, read from the setting's
#: own default so this module and ``files_lease_ttl_seconds`` cannot drift. The
#: value a call actually uses is :func:`lease_ttl`.
LEASE_TTL: Final = timedelta(
    seconds=cast(int, Settings.model_fields["files_lease_ttl_seconds"].default)
)
#: How long a lapsed lease stays un-grantable, so a holder that is merely slow
#: is not immediately raced by the next acquirer.
DEFAULT_GRANT_DELAY: Final = timedelta(seconds=5)


def _overlap_sql(path: str, node_paths: str = "n.path_ids") -> str:
    """Raw SQL for "these two subtrees meet" — the grant rule's own question.

    A lease refuses another over the same bytes, which is nesting in either
    direction, so acquire asks it of the folder it is being handed and a late
    beat asks it again of the folder it is being handed back. One spelling, so
    the two can never answer differently. Like every :func:`subtree_sql` caller
    the statement carrying it must bind :data:`SUBTREE_DEPTH_BIND` and mark
    itself ``# noqa: S608``.
    """
    return f"({subtree_sql(path, node_paths)} OR {subtree_sql(node_paths, path)})"


def lease_ttl() -> timedelta:
    """How long a grant lives here: ``files_lease_ttl_seconds``.

    Read per call rather than bound at import, so the TTL a deployment sets is
    the one the statement writes and the one the route serves its cadence from
    — there is one number, not a literal in the library and a setting nobody
    reads. It is generous on purpose: the fence exists to stop two writers, and
    a holder that is merely unreachable for a while is still the only writer.
    """
    return timedelta(seconds=get_settings().files_lease_ttl_seconds)


#: The default: a service the caller gave no checkpoints runs straight through.
NO_CHECKPOINTS: Checkpoints = NoopCheckpoints()

LeasePurpose = Literal["mount", "box", "share", "chat", "workspace"]

#: The purposes whose lease takes writes from people who hold no fence, into
#: the folder's working tree (:func:`_inbound_admission` decides which tree).
#: A chat's box and a workspace's box both hold a folder people are watching
#: and dropping files into; a mount, a box mount and a share own theirs.
INBOUND_PURPOSES: Final[frozenset[str]] = frozenset({"chat", "workspace"})

#: What kind of thing a lease belongs to. Every value but ``machine`` is the
#: kind of a principal the server itself authenticated from the credential on
#: the request; ``machine`` is a box whose assertion was checked against its
#: registration. There is deliberately no ``agent`` — an agent assertion is two
#: headers any member may put on their own session, so an agent is either a
#: proven machine or nobody at all.
HOLDER_KINDS: Final = ("user", "pat", "service", "machine")

HolderKind = Literal["user", "pat", "service", "machine"]

#: The final-snapshot hook, called ``(repo, node_id, *, lease, changes)``. The
#: snapshot module owns what the changes *are*; this module owns the promise
#: that they land inside the release transaction, and that the ``lease`` they
#: land under is the one this service built (the only context carrying
#: ``final``).
ApplyFinal = Callable[..., Awaitable[None]]


def epoch_for(restore_generation: int, seq: int) -> int:
    """Compose an epoch from the platform generation and the per-node sequence."""
    if restore_generation < 0 or seq < 0:
        raise ValueError("an epoch is composed of non-negative halves")
    if seq >= 1 << EPOCH_SEQ_BITS:
        raise ValueError("the sequence half has overflowed its generation")
    return (restore_generation << EPOCH_SEQ_BITS) + seq


def split_epoch(epoch: int) -> tuple[int, int]:
    """Take an epoch apart into ``(restore_generation, seq)``."""
    if epoch < 0:
        raise ValueError("an epoch is non-negative")
    return epoch >> EPOCH_SEQ_BITS, epoch & ((1 << EPOCH_SEQ_BITS) - 1)


class LeaseConflict(errors.Conflict):
    """A 409 that may carry the holder — but only when the caller may know it.

    The route layer maps this by its base class; the extra ``detail`` is what a
    client shows ("Ana has this folder mounted on her laptop"). It is ``None``
    whenever the caller cannot read the leased node, so a refusal never becomes
    an oracle for a folder they cannot see.
    """

    def __init__(
        self, code: str, message: str = "", *, detail: dict[str, Any] | None = None
    ) -> None:
        super().__init__(code, message)
        self.detail = detail
        # The readability decision is made once, where the holder is looked up:
        # a detail exists exactly when the caller may read the leased node, and
        # the renderer withholds every conflict detail unless a refusal says so.
        self.may_read_named_node = detail is not None


@dataclass(frozen=True, slots=True)
class HolderIdentity:
    """Who a lease belongs to, as the SERVER derived it.

    The whole point of the type is what it cannot be built from. A lease is
    fenced on this value, so if any part of it came from something the request
    chose, the fence would be a bearer token: the epoch, the instance and a
    machine id are all served by the product, and the agent assertion headers
    are client-asserted on any user's own JWT. So there is exactly one
    constructor — :func:`holder_identity` — and it reads only the credential
    the server authenticated and the machine registration it checked.

    ``kind`` travels with ``id`` everywhere the value is stored or compared,
    because the two id spaces are different tables: a ``users`` row and a
    ``compute_allocations`` row are both UUIDs, and a fence that compared the
    uuid alone would let one stand in for the other.
    """

    kind: HolderKind
    id: uuid.UUID


def holder_identity(
    ctx: ActingContext, *, verified_machine_id: str | None = None
) -> HolderIdentity | None:
    """The fencing identity of ``ctx``, or ``None`` for a caller that can hold
    nothing.

    Three cases, and the middle one is the reason this function exists:

    * a credential the server authenticated — a cookie or Bearer JWT, a
      personal access token, a CI or proxy token — IS its principal. An agent
      assertion riding on such a JWT buys nothing: it is not consulted here.
    * an agent is holdership ONLY as a proven machine. ``verified_machine_id``
      is the caller's answer from ``verify_machine_assertion`` — a live
      workspace machine of this org, registered by the user behind this
      request, on the very credential the request carries — and it has to name
      the machine the request asserted, so a verified box cannot fence as
      another one.
    * anything else holds nothing: an unverified assertion, a mismatched one,
      or a principal whose id is not a UUID at all. Fail closed, because the
      alternative is a holder derived from a header.
    """
    principal = ctx.acting_principal
    if principal.kind is PrincipalKind.AGENT:
        if verified_machine_id is None or verified_machine_id != principal.id:
            return None
        try:
            return HolderIdentity(kind="machine", id=uuid.UUID(verified_machine_id))
        except ValueError:
            return None
    try:
        holder = uuid.UUID(principal.id)
    except ValueError:
        return None
    if principal.kind.value not in HOLDER_KINDS:
        return None
    return HolderIdentity(kind=cast(HolderKind, principal.kind.value), id=holder)


async def release_departed_machine(
    session: AsyncSession,
    *,
    node_id: NodeId,
    org_team_id: uuid.UUID,
    machine_id: uuid.UUID,
) -> bool:
    """Hand back the live lease ``machine_id`` holds on ``node_id``, on the
    machine's behalf. True when a lease was ended, False when it held none.

    The platform's verb for a holder that has left service. A release is the
    holder's own — matched on its epoch, its instance and the identity the
    server derived for it — and a box that has stopped beating, been put to
    sleep or left the plane will never make it, so the lease it left behind
    would hold the folder off every other box until its TTL ran out; a chat
    placement moves to a live box would sit unanswered for that long. Nothing
    else about the row is touched: the box's writes were fenced on the epoch,
    and the next grant moves it, so a holder that comes back after all is
    refused the way a superseded one is. Matched on the machine's proven
    identity (``holder_kind`` and ``holder_principal_id``), never on the
    ``machine_id`` column, which is the holder's own word.
    """
    released = (
        await session.execute(
            text(
                "UPDATE file_leases SET released_at = now(), grantable_after = now() "
                "WHERE node_id = :node AND org_team_id = :org "
                "AND holder_kind = 'machine' AND holder_principal_id = :machine "
                "AND released_at IS NULL AND reaped_at IS NULL AND expires_at > now() "
                "RETURNING node_id"
            ),
            {"node": node_id, "org": org_team_id, "machine": machine_id},
        )
    ).first()
    return released is not None


async def end_leases_under(repo: FilesRepo, node: FileNode) -> list[NodeId]:
    """End every live lease on ``node`` or on anything under it. Returns the
    folders whose lease was ended.

    The rule this is the whole of: **a folder in the trash cannot be worked
    on.** A lease is exclusive write on a subtree, and a trashed subtree takes
    no writes from anybody, so a lease on it fences nothing it should and
    blocks the two things a person still does with it — deleting it forever
    and emptying the trash. Trashing ends the leases inside what it trashes,
    and a purge ends any a trash from before this rule left behind, rather
    than refusing forever on a lease whose holder may be gone.

    The holder is told the way a superseded holder is: its next beat is
    fenced, and asking for the folder again is refused as ``files.trashed``,
    so a box stops serving the chat instead of re-taking a folder nobody can
    open. Its writes were fenced on the epoch the whole time, so nothing it
    sends after this lands. A lease on a folder ABOVE ``node`` is untouched —
    trashing a file inside a mount is the mount's own business.

    Runs after the caller has locked the nodes it trashes or purges, so the
    lease rows are taken after the nodes, in the fixed Files lock order.
    """
    rows = (
        await repo.session.execute(
            text(
                "UPDATE file_leases SET released_at = now(), grantable_after = now() "  # noqa: S608
                "WHERE org_team_id = :org "
                "AND released_at IS NULL AND reaped_at IS NULL AND expires_at > now() "
                "AND node_id IN (SELECT n.id FROM file_nodes n WHERE n.org_team_id = :org "
                f"AND {subtree_sql('n.path_ids', 'CAST(:path AS ltree)')}) "
                "RETURNING node_id"
            ),
            {"org": repo.scope.org_team_id, "path": node.path_ids, **SUBTREE_DEPTH_BIND},
        )
    ).all()
    return [NodeId(row.node_id) for row in rows]


def _trashed() -> errors.Conflict:
    return errors.Conflict("files.trashed", "the folder is in the trash")


def _ended() -> errors.Conflict:
    return errors.Conflict("files.lease_ended", "the lease was ended by the server")


@dataclass(frozen=True, slots=True)
class Lease:
    """One grant, as the client needs to see it."""

    node_id: NodeId
    epoch: int
    holder_kind: str
    holder_principal_id: uuid.UUID
    holder_instance_id: str
    machine_id: str
    purpose: str
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime
    grantable_after: datetime
    last_sync_at: datetime | None
    released_at: datetime | None
    #: A manager has forced this lease: the holder learns it here, on its next
    #: heartbeat, and has until ``expires_at`` to finish and release.
    forced: bool = False
    #: Whether a write from someone who is not the holder is taken into the
    #: subtree for the holder to apply, instead of refused.
    accepts_inbound: bool = False


@dataclass(frozen=True, slots=True)
class LeaseContext:
    """What a fenced write carries: the epoch it believes it holds, and who.

    ``epoch`` is ``None`` for a caller that sent no ``X-Alkera-Lease-Epoch``,
    which is not the same as a wrong epoch and is refused differently: no epoch
    is "someone else has this folder", a wrong epoch is "you have been fenced".
    """

    epoch: int | None = None
    instance_id: str | None = None
    #: The holder said its ``If-Match`` is the version its bytes were made on.
    #: A box from before that was so fenced an edit on the drive's head when it
    #: no longer knew its base; a co-edited document cannot trust that one.
    base_known: bool = False
    #: Who is writing, as :func:`holder_identity` derives them from the
    #: credential (and, for a box, from its checked registration) — the half of
    #: the fence that proves the caller IS the holder rather than merely
    #: knowing which epoch is current. ``None`` on a context built without a
    #: caller, and on every caller that can hold nothing, which is refused: a
    #: pair with nobody behind it names a lease it cannot claim.
    holder: HolderIdentity | None = None
    #: Whether the caller may read the node they are writing to. False means the
    #: policy already refused, and a lease must not turn that 404 into a 409.
    can_read_target: bool = True
    #: Whether the caller may read the *leased* node. False strips the holder
    #: from the refusal.
    can_read_holder: bool = True
    #: Whether this is the release applying its own batch — the one write no
    #: storage ceiling refuses, because what it carries exists on the box alone
    #: and refusing it would lose the work. Set by :meth:`LeaseService.release`
    #: on the context it hands its hook, and by nothing a request can reach: a
    #: holder that could claim it on any write would be permanently exempt from
    #: the drive's limits, and the live plane writes under the same fence every
    #: few hundred milliseconds.
    final: bool = False


# Every statement below spells its own predicates out. "Live" is
# ``released_at IS NULL AND expires_at > now()``; "forced" is
# ``grantable_after >= expires_at``, which only ``force_release`` produces, so
# the flag lives on the two timestamps that already decide everything else
# about the lease. They are written literally rather than interpolated from a
# constant so that no Files statement is ever built by string formatting.


def _holder_detail(row: Any) -> dict[str, Any]:
    return {
        "holder": str(row.holder_principal_id),
        "machine": row.machine_id,
        # ISO-8601 rather than the datetime itself: a detail is rendered
        # straight into a JSON body, and the lease facet spells the same
        # timestamp the same way.
        "since": row.acquired_at.isoformat(),
    }


def _leased(row: Any, *, can_read_holder: bool) -> LeaseConflict:
    return LeaseConflict(
        "files.leased",
        "the folder is leased",
        detail=_holder_detail(row) if can_read_holder else None,
    )


def _fenced() -> LeaseConflict:
    return LeaseConflict("files.lease_fenced", "a newer epoch owns this subtree")


def _not_under() -> LeaseConflict:
    """A live lease of the caller's own that does not cover the write — a
    nested lease covers it, or a workspace lease's narrowing leaves it out.
    Not ``files.lease_fenced``: that says the caller's lease is gone, and a
    holder told so drops a folder it still holds."""
    return LeaseConflict("files.lease_mismatch", "that path is not under this lease")


async def _refuse_a_live_claim_elsewhere(
    repo: FilesRepo, epoch: int, instance_id: str, holder: HolderIdentity
) -> None:
    """Raise :func:`_not_under` when the pair still names a live lease."""
    held = await leases_held_by(
        repo.session,
        org_team_id=repo.scope.org_team_id,
        epoch=epoch,
        instance_id=instance_id,
        holder=holder,
    )
    if held:
        raise _not_under()


#: How strongly a fence or a covering read locks the lease row. ``"share"`` is
#: for a transaction that only reads under the lease and holds nothing else on
#: it; ``"no key update"`` is for one that will update the row — bump its
#: sequence, stamp it synced — because a share lock upgraded later deadlocks
#: against any other share holder doing the same.
LeaseLock = Literal["share", "no key update", "none"]

_LOCK_STRENGTH: Final[dict[str, Strength | None]] = {
    "share": "share",
    "no key update": "no_key_update",
    # A read that decides what to OFFER and fences nothing (see
    # :func:`admits_unfenced_write`): it must not queue behind the holder.
    "none": None,
}


async def assert_lease_epoch(
    repo: FilesRepo,
    node_id: NodeId,
    epoch: int,
    instance_id: str,
    *,
    holder: HolderIdentity | None,
    lock: LeaseLock = "share",
) -> None:
    """Refuse unless this caller holds ``node_id`` at this epoch, right now.

    The epoch and the instance say WHICH grant a write belongs to; ``holder``
    says WHO is writing under it, and without that half the pair is a bearer
    token — both halves of it are served (a lease facet names the machine, a
    409 names the holder, the instance is derived from them) and the epoch is a
    small counter anyone may guess. So the row is matched on the caller's own
    derived identity as well: a colleague who replays a holder's pair is
    refused with the same ``files.lease_fenced`` a superseded holder gets,
    which is all a forger learns from it.

    Both halves of the identity are compared. The kind is not decoration: a
    user id and a machine id are UUIDs drawn from different tables, and a fence
    that matched the uuid alone would let a caller who is one be fenced in as
    the other.

    ``holder`` is ``None`` for a caller that can hold nothing — an unverified
    agent assertion above all — and is refused rather than admitted.

    ``lock`` is the strength the row is held at for the rest of the
    transaction. A read under the lease holds it ``FOR SHARE``, so many readers
    never queue on the row while the reaper's reclaim still waits for them. A
    writer holds it ``FOR NO KEY UPDATE`` from this statement on: the row is
    what its sequence bump and its synced stamp update, and taking a share lock
    here only to upgrade it there is the deadlock this module exists to avoid.
    """
    if holder is None:
        raise _fenced()
    statement = select(FileLease.node_id).where(
        FileLease.node_id == node_id,
        FileLease.org_team_id == repo.scope.org_team_id,
        FileLease.epoch == epoch,
        FileLease.holder_instance_id == instance_id,
        FileLease.holder_principal_id == holder.id,
        FileLease.holder_kind == holder.kind,
        FileLease.released_at.is_(None),
        FileLease.reaped_at.is_(None),
        FileLease.expires_at > func.now(),
    )
    strength = _LOCK_STRENGTH[lock]
    if strength is None:
        found = (await repo.session.execute(statement)).first()
    else:
        found = (
            await lock_rows(repo.session, LockRank.FILES_LEASE, statement, strength=strength)
        ).first()
    if found is None:
        await _refuse_a_live_claim_elsewhere(repo, epoch, instance_id, holder)
        raise _fenced()


async def live_lease_purpose(repo: FilesRepo, node_id: NodeId) -> str:
    """The purpose of the live lease on ``node_id`` itself, or ``""`` when
    none is held: what a force-release decides who may cut it off by."""
    row = (
        await repo.session.execute(
            text(
                "SELECT purpose FROM file_leases WHERE node_id = :node AND org_team_id = :org "
                "AND released_at IS NULL AND reaped_at IS NULL AND expires_at > now()"
            ),
            {"node": node_id, "org": repo.scope.org_team_id},
        )
    ).first()
    return str(row.purpose) if row is not None else ""


async def leases_held_by(
    session: AsyncSession,
    *,
    org_team_id: uuid.UUID,
    epoch: int,
    instance_id: str,
    holder: HolderIdentity | None,
) -> frozenset[uuid.UUID]:
    """The nodes whose live lease ``instance_id`` holds at ``epoch``, right now.

    The read side of the fence: a caller that sends the epoch and instance a
    write would be fenced on is, for the length of the request, the proven
    holder of exactly these subtrees — which is what lets the box running a
    chat read the folder it leases back down. Same predicates as
    :func:`assert_lease_epoch`, without the lock: a read holds nothing open.
    Empty for a stale epoch, a released lease or a lapsed one, so a box that
    was superseded reads as a stranger again the moment it is — and empty for
    anyone but the holder itself: the pair is derivable from what the product
    serves, so it names a lease without proving anybody, and what proves the
    holder is the identity the server derived for this request. Empty, too,
    for an agent whose assertion nobody verified: it has no identity to match,
    so the box's own machine id — which rides every chat that box serves —
    holds nothing in the hands of the member who reads it off a row.
    """
    if holder is None:
        return frozenset()
    rows = (
        await session.execute(
            text(
                "SELECT node_id FROM file_leases WHERE org_team_id = :org "
                "AND epoch = :epoch AND holder_instance_id = :instance "
                "AND holder_principal_id = :holder AND holder_kind = :holder_kind "
                "AND released_at IS NULL AND reaped_at IS NULL AND expires_at > now()"
            ),
            {
                "org": org_team_id,
                "epoch": epoch,
                "instance": instance_id,
                "holder": holder.id,
                "holder_kind": holder.kind,
            },
        )
    ).all()
    return frozenset(uuid.UUID(str(row.node_id)) for row in rows)


async def covering_lease(
    repo: FilesRepo, node: FileNode, *, lock: LeaseLock = "no key update"
) -> Any | None:
    """The live lease on ``node`` or on its nearest leased ancestor that
    covers it.

    Ancestor-or-self is the subtree question with its operands the other way
    round -- "is this path under anything leased" -- so it goes through the same
    builder, and one index scan answers it without walking the chain in Python.

    A ``workspace`` lease covers its folder and the shared tree under it, and
    nothing else: a chat's records under ``.chats/`` move under that chat's own
    lease, and a new chat's record folder is created there by the server while
    the box holds the workspace, so a second chat starts in a workspace whose
    first chat is awake.

    Locked at ``lock`` strength, update strength by default: the write this
    covers may bump the lease's sequence or stamp it synced before it commits,
    and that update must not be an upgrade of a weaker lock taken here.
    """
    sql = (
        "SELECT l.* FROM file_leases l JOIN file_nodes n ON n.id = l.node_id "  # noqa: S608
        "WHERE l.org_team_id = :org "
        f"AND {subtree_sql('CAST(:path AS ltree)', 'n.path_ids')} "
        "AND l.released_at IS NULL AND l.reaped_at IS NULL "
        "AND l.expires_at > now() "
        "ORDER BY n.depth DESC"
    )
    params = {"org": repo.scope.org_team_id, "path": node.path_ids, **SUBTREE_DEPTH_BIND}
    strength = _LOCK_STRENGTH[lock]
    if strength is None:
        found = await repo.session.execute(text(sql), params)
    else:
        found = await lock_text(
            repo.session, LockRank.FILES_LEASE, sql, params, strength=strength, of="l"
        )
    rows = found.all()
    for row in rows:
        if row.purpose != "workspace" or await _workspace_covers(repo, row, node):
            return row
    return None


async def _workspace_covers(repo: FilesRepo, lease: Any, node: FileNode) -> bool:
    """Whether the ``workspace`` lease ``lease`` covers ``node``: its folder,
    or its shared tree and what is under it."""
    if node.id == lease.node_id:
        return True
    # Deferred for the same cycle as :func:`_inbound_admission`.
    from alkera_core.files.objects_bridge import lease_working_node

    lease_node = await repo.node(NodeId(lease.node_id))
    working = await lease_working_node(repo, lease_node) if lease_node is not None else None
    if working is None:
        # A folder whose shared tree cannot be found is covered whole, as any
        # other lease's is: the narrowing never opens what it cannot name.
        return True
    return node.id == working.id or _under(node, working)


@dataclass(frozen=True, slots=True)
class InboundAdmission:
    """A write let into a folder that a machine is holding, for it to apply.

    A lease refuses every writer but its holder, which is right for a mount and
    wrong for a chat that is awake: the person is watching the folder the agent
    is working in, and dropping a file into it has to work. So a lease may say
    it accepts inbound writes; the drive then takes the write, records it on the
    live plane, and the holder applies it on its next drain.

    Admission is deliberately the narrowest thing that makes that work. Only the
    chat's working directory and what is under it are open — the chat node
    itself, and the records beside the working directory, stay refused, so a
    colleague with write on a shared chat can never edit the transcript of what
    was said. And the admission is NOT the holder's own epoch: it carries no
    fence, so the write is quota-bounded like anybody else's.
    """

    #: The lease row the write was admitted into, for a caller that needs the
    #: epoch it belongs to.
    lease: Any

    @property
    def epoch(self) -> int:
        """The epoch of the lease this write was admitted into."""
        return int(self.lease.epoch)


async def _inbound_admission(
    repo: FilesRepo, lease: Any, node: FileNode, *, into: bool
) -> InboundAdmission | None:
    """Whether ``lease`` takes this write from someone who holds no fence.

    ``into`` says the write lands a CHILD under ``node`` rather than changing
    ``node`` itself, which is the whole difference between dropping a file into
    the working directory (allowed) and renaming, moving or trashing the working
    directory (refused, because that is the mount the machine is standing on).
    """
    if not getattr(lease, "accepts_inbound", False):
        return None
    # Deferred: ``objects_bridge`` sits above every write service that calls the
    # fence, so importing it at module scope would close a cycle through them.
    from alkera_core.files.objects_bridge import lease_working_node

    lease_node = await repo.node(NodeId(lease.node_id))
    if lease_node is None:
        return None
    working = await lease_working_node(repo, lease_node)
    if working is None:
        return None
    if node.id == working.id:
        return InboundAdmission(lease) if into else None
    if not _under(node, working):
        return None
    return InboundAdmission(lease)


async def admits_unfenced_write(repo: FilesRepo, node: FileNode) -> bool:
    """Whether a write that holds no fence may land on ``node`` now: no live
    lease covers it, or the one that does takes inbound writes there (the
    holder applies them, as it applies a file dropped on it in the browser).

    Read without a lock, so it answers what to offer (whether a co-edited
    file can be written back) and fences nothing: the write itself runs
    :func:`fenced_write` like every other."""
    lease = await covering_lease(repo, node, lock="none")
    if lease is None:
        return True
    return await _inbound_admission(repo, lease, node, into=False) is not None


def _under(node: FileNode, ancestor: FileNode) -> bool:
    """``node`` lives strictly beneath ``ancestor`` in the same drive.

    ``path_ids`` is an ltree of per-drive inos, so one is under the other
    exactly when its path starts with the ancestor's path and a separator —
    which is the same containment the SQL builder expresses, read off rows the
    caller already has rather than costing a statement.
    """
    if node.drive_id != ancestor.drive_id:
        return False
    return str(node.path_ids).startswith(f"{ancestor.path_ids}.")


async def fenced_write(
    repo: FilesRepo,
    node: FileNode,
    *,
    epoch: int | None,
    instance_id: str | None,
    holder: HolderIdentity | None = None,
    can_read_target: bool = True,
    can_read_holder: bool = True,
    into: bool = False,
) -> Any | None:
    """The check every write service runs inside its own transaction.

    It runs **after** the policy decision, never before: a caller who may not
    read the node they are writing to gets the not-found answer, because a 409
    would confirm both that the folder exists and that someone has it mounted.

    Returns the live lease covering the node, if any, so a caller that also
    needs it — to record the epoch, or to tell a hand-back from an ordinary
    write — does not read it a second time. A caller with no epoch whose write
    the lease nonetheless admits gets an :class:`InboundAdmission` instead, and
    is expected to record the difference for the holder to apply.
    """
    if not can_read_target:
        raise errors.NotFound(f"no node {node.id}")
    # The leased folder before its lease row, as every writer under a lease
    # takes them: the holder's tree report locks that folder, then the lease,
    # then rows beneath it, so a write that reached the lease row first and a
    # row under the folder after it would each hold what the other wants. A
    # writer that already locked a node took the folder with it, and this is
    # then a re-lock of a row it holds.
    if await repo.lock_lease_gate(NodeId(node.id)) is None:
        await _refuse_a_dead_claim(repo, node, epoch, instance_id, holder)
        return None
    # Update strength on both reads: a write admitted under the lease bumps its
    # sequence, and the holder's own landing stamps it synced, so the row is
    # taken once at the strength those updates need rather than shared here
    # and upgraded there.
    lease = await covering_lease(repo, node, lock="no key update")
    if lease is None:
        await _refuse_a_dead_claim(repo, node, epoch, instance_id, holder)
        return None
    if epoch is None or instance_id is None:
        admission = await _inbound_admission(repo, lease, node, into=into)
        if admission is not None:
            return admission
        raise _leased(lease, can_read_holder=can_read_holder)
    await assert_lease_epoch(
        repo, NodeId(lease.node_id), epoch, instance_id, holder=holder, lock="no key update"
    )
    return lease


async def fenced_create_into(
    repo: FilesRepo, parent: FileNode, lease: LeaseContext | None
) -> Any | None:
    """``fenced_write_for(..., into=True)`` for a caller about to create in
    ``parent``, with the drive's namespace taken first.

    The fence locks the leased folder and its lease row, and the create after
    it takes the namespace. Every writer takes the namespace first, so a caller
    that fenced first held the folder while waiting for the namespace, and a
    move holding the namespace while waiting for the folder deadlocked with it.
    """
    await repo.lock_namespace(DriveId(parent.drive_id))
    return await fenced_write_for(repo, parent, lease, into=True)


async def _refuse_a_dead_claim(
    repo: FilesRepo,
    node: FileNode,
    epoch: int | None,
    instance_id: str | None,
    holder: HolderIdentity | None,
) -> None:
    """Refuse a write that names this caller's own lease over ``node`` once
    that lease is no longer live (released, reaped or lapsed).

    With no live lease covering the node, the fence would otherwise let the
    write through as anyone's ordinary write. For a caller whose rung on the
    folder admits it anyway (a box on its operator's session, in the
    operator's own home), a lease the server had ENDED (the chat put to sleep,
    deleted, moved) would become no fence at all and a late push would land.
    The caller is claiming a lease, so the claim is checked. A request that
    names no lease, or one that never covered this node, is decided as an
    ordinary write.
    """
    if epoch is None or instance_id is None or holder is None:
        return
    dead = (
        await repo.session.execute(
            text(
                "SELECT 1 FROM file_leases l JOIN file_nodes n ON n.id = l.node_id "  # noqa: S608
                "WHERE l.org_team_id = :org "
                f"AND {subtree_sql('CAST(:path AS ltree)', 'n.path_ids')} "
                "AND l.epoch = :epoch AND l.holder_instance_id = :instance "
                "AND l.holder_principal_id = :holder AND l.holder_kind = :holder_kind "
                "LIMIT 1"
            ),
            {
                "org": repo.scope.org_team_id,
                "path": node.path_ids,
                "epoch": epoch,
                "instance": instance_id,
                "holder": holder.id,
                "holder_kind": holder.kind,
                **SUBTREE_DEPTH_BIND,
            },
        )
    ).first()
    if dead is not None:
        await _refuse_a_live_claim_elsewhere(repo, epoch, instance_id, holder)
        raise _fenced()


def admitted_inbound(covering: Any | None) -> InboundAdmission | None:
    """``covering`` as an admission, or ``None`` when it is not one.

    The one place a write service asks "was this taken on the holder's behalf",
    so no call site has to know that the fence answers two different shapes.
    """
    return covering if isinstance(covering, InboundAdmission) else None


def is_hand_back(covering: Any | None, lease: LeaseContext | None) -> bool:
    """Whether a write is the lease holder's own — the box materializing the
    folder it leases back into Files. True only once the fence has passed:
    a live lease covers the folder and the caller presented its epoch.

    An inbound admission is never one: it was let in precisely because the
    caller presented no epoch, so it stays bounded by the quota the way every
    other member's write is.
    """
    if isinstance(covering, InboundAdmission):
        return False
    return covering is not None and lease is not None and lease.epoch is not None


def is_final_push(covering: Any | None, lease: LeaseContext | None) -> bool:
    """Whether a write is the release applying its own batch — the one write no
    storage ceiling refuses.

    Narrower than :func:`is_hand_back` on purpose, and the two are asked
    different questions. Every fenced write is the holder's own, and that is
    what decides whose live row clears and whose epoch is stamped on the
    version; but only what the release itself carries would be lost by a
    refusal, so only it may land past the drive's limits.

    ``lease.final`` is the server's own word rather than the caller's:
    :meth:`LeaseService.release` sets it on the context it hands the hook while
    the release runs, and no request can produce it. A marker a holder could
    assert would make every holder permanently unbounded — the live plane
    writes under the same fence every few hundred milliseconds, so an exemption
    it could claim would spend an org's whole quota with nothing able to refuse
    it.
    """
    return lease is not None and lease.final and is_hand_back(covering, lease)


async def fenced_write_for(
    repo: FilesRepo,
    node: FileNode,
    lease: LeaseContext | None,
    *,
    into: bool = False,
) -> Any | None:
    """``fenced_write`` for a service that carries the caller's lease context.

    ``None`` is a request that named no lease at all, which is the common case
    and the one that must still be refused inside somebody else's mount.
    Returns what :func:`fenced_write` returns: the covering lease, an
    :class:`InboundAdmission`, or ``None``.
    """
    if lease is None:
        return await fenced_write(repo, node, epoch=None, instance_id=None, into=into)
    return await fenced_write(
        repo,
        node,
        epoch=lease.epoch,
        instance_id=lease.instance_id,
        holder=lease.holder,
        can_read_target=lease.can_read_target,
        can_read_holder=lease.can_read_holder,
        into=into,
    )


class LeaseService:
    """Acquire, hold, hand back and take away a folder lease."""

    def __init__(
        self,
        repo: FilesRepo,
        ctx: ActingContext,
        clock: Clock,
        store: Any | None = None,
        *,
        checkpoints: Checkpoints = NO_CHECKPOINTS,
        grant_delay: timedelta = DEFAULT_GRANT_DELAY,
        apply_final: ApplyFinal | None = None,
        verified_machine_id: str | None = None,
    ) -> None:
        self._repo = repo
        self._ctx = ctx
        self._clock = clock
        self._store = store
        self._checkpoints = checkpoints
        self._grant_delay = grant_delay
        self._apply_final = apply_final
        #: Derived once, here, from the credential and the caller's machine
        #: check — never re-derived per statement, so acquire, heartbeat,
        #: release and every fenced write of this service cannot disagree
        #: about who is asking.
        self._identity = holder_identity(ctx, verified_machine_id=verified_machine_id)

    @property
    def _holder(self) -> HolderIdentity:
        """This caller's fencing identity, or the refusal.

        A caller with none cannot hold a folder and cannot be matched as its
        holder — an agent whose assertion nobody verified above all, since the
        id it asserts is public. It is told exactly what a superseded holder is
        told, so nothing about the live lease leaks back.
        """
        if self._identity is None:
            raise _fenced()
        return self._identity

    async def _require(self, node_id: NodeId) -> FileNode:
        node = await self._repo.node(node_id)
        if node is None:
            raise errors.NotFound(f"no node {node_id}")
        return node

    async def _row(self, node_id: NodeId) -> Any:
        row = (
            await self._repo.session.execute(
                text("SELECT * FROM file_leases WHERE node_id = :node AND org_team_id = :org"),
                {"node": node_id, "org": self._repo.scope.org_team_id},
            )
        ).first()
        if row is None:
            raise errors.NotFound(f"no lease on {node_id}")
        return row

    async def _overlap(self, node: FileNode, purpose: str = "mount") -> Any | None:
        """A live lease on an ancestor or a descendant that refuses this grant.

        A lease strictly *under* one the caller already holds is allowed — that
        is a second machine taking part of their own mount — so an ancestor
        lease held by the same principal is not an overlap. A descendant lease
        is, granting above it would give two holders the same bytes, with one
        exception: a box's ``workspace`` grant over its own ``chat`` leases on
        the workspace's members. Those nest under the workspace lease anyway;
        a box that restarted while holding them is retaking its own workspace,
        and refusing it until they lapsed stopped every member it was resuming.
        """
        rows = (
            await self._repo.session.execute(
                text(
                    "SELECT l.*, n.depth AS lease_depth "  # noqa: S608
                    "FROM file_leases l JOIN file_nodes n ON n.id = l.node_id "
                    "WHERE l.org_team_id = :org AND l.node_id <> :node "
                    f"AND {_overlap_sql('CAST(:path AS ltree)')} "
                    "AND l.released_at IS NULL AND l.reaped_at IS NULL "
                    "AND l.expires_at > now() "
                    "ORDER BY n.depth DESC FOR SHARE OF l"
                ),
                {
                    "org": self._repo.scope.org_team_id,
                    "node": node.id,
                    "path": node.path_ids,
                    **SUBTREE_DEPTH_BIND,
                },
            )
        ).all()
        mine = self._identity
        for row in rows:
            own = (
                mine is not None
                and row.holder_principal_id == mine.id
                and row.holder_kind == mine.kind
            )
            if own and row.lease_depth < node.depth:
                continue
            if own and purpose == "workspace" and row.purpose == "chat":
                continue
            return row
        return None

    async def acquire(
        self,
        node_id: NodeId,
        *,
        instance_id: str,
        machine_id: str,
        purpose: LeasePurpose = "mount",
        ttl: timedelta | None = None,
        can_read_holder: bool = True,
        accepts_inbound: bool | None = None,
        retake: bool = False,
    ) -> Lease:
        """Take the lease, or refuse — naming the holder only when readable.

        ``accepts_inbound`` defaults from the purpose: a chat's lease takes
        what a person drops into the conversation while the box runs — that
        is why the box holds the folder at all — and so does a workspace's,
        into the shared tree its chats and its people work in; a mount does not, because
        the machine owns that tree outright. Written on every grant, a resume
        included, so a lease never keeps the answer of a purpose it no longer
        has.

        The epoch is chosen inside the statement from the greater of the row's
        own epoch, the node's high-water mark and the current generation's
        floor, so it is above every epoch ever issued for this node whether or
        not the previous rows survived a restore.

        **A resume is the same call.** The same principal asking again from the
        same instance is the mount that was killed and has come back, so it
        takes its own lease over rather than waiting out a TTL nobody is using —
        and it keeps its epoch, because nothing was ever fenced: the writes it
        had in flight are still its own. A reaped lease is the exception: the
        reaper undid everything that epoch had in flight, so its holder comes
        back to a NEW epoch rather than to one whose sessions are gone. Every
        other asker still waits for the lease to lapse, and a forced lease is
        not resumable either, because ``grantable_after`` is what a force
        pushes into the future.
        """
        ttl = lease_ttl() if ttl is None else ttl
        holder = self._holder
        # The label a box publishes itself under is the one the server PROVED,
        # never the one the request typed: the column is read back onto every
        # chat row and every refusal, and a box that could name itself anything
        # would put another machine's badge on the folder it is holding. A
        # person's mount keeps the name they gave their own laptop, which is
        # their word about their own device and which nothing decides on.
        machine = str(holder.id) if holder.kind == "machine" else machine_id
        node = await self._require(node_id)
        if node.trashed_at is not None:
            # A trashed folder takes no writes, so there is nothing to hold it
            # for. A box re-taking the folder of a chat that was deleted under
            # it learns here that the chat is gone, rather than resurrecting a
            # lease that would block the folder's purge.
            raise _trashed()
        if retake and await self._ended(node_id):
            # A holder re-taking a lease it believes is still its own, after a
            # fenced beat. A lapse or a reap is re-granted; a lease the server
            # ENDED — its chat put to sleep, deleted or moved — is not, or the
            # box would take straight back the chat that was just ended under it.
            raise _ended()
        blocking = await self._overlap(node, purpose)
        await self._checkpoints.reach("leases.overlap_checked")
        if blocking is not None:
            raise _leased(blocking, can_read_holder=can_read_holder)

        granted = (
            await self._repo.session.execute(
                text(
                    "INSERT INTO file_leases (node_id, org_team_id, epoch, "
                    "holder_principal_kind, holder_kind, holder_principal_id, "
                    "holder_instance_id, machine_id, purpose, acquired_at, heartbeat_at, "
                    "expires_at, grantable_after, accepts_inbound) "
                    "SELECT :node, :org, "
                    "GREATEST(coalesce(h.hwm, 0), "
                    "(SELECT restore_generation FROM file_platform WHERE id = 1) "
                    "* CAST(4294967296 AS bigint)) + 1, "
                    "'user', :holder_kind, :holder, :instance, :machine, :purpose, now(), now(), "
                    "now() + make_interval(secs => :ttl), now(), :inbound "
                    "FROM (SELECT 1) AS _seed "
                    "LEFT JOIN file_lease_epoch_hwm h ON h.node_id = :node "
                    "ON CONFLICT (node_id) DO UPDATE SET "
                    # A resume — the same principal, the same instance, the lease
                    # still live — keeps its epoch: nothing was fenced, so the
                    # writes it had in flight are still its own. A reaped lease
                    # is not live however far ahead a late beat pushed its
                    # deadline, so it is no resume: teardown aborted that
                    # epoch's upload sessions and released their holds, and a
                    # holder handed its old epoch back would be pushing into
                    # sessions that no longer exist.
                    "epoch = CASE WHEN "
                    "(file_leases.released_at IS NULL AND file_leases.reaped_at IS NULL "
                    "AND file_leases.expires_at > now() "
                    "AND file_leases.holder_principal_id = :holder "
                    "AND file_leases.holder_kind = :holder_kind "
                    "AND file_leases.holder_instance_id = :instance) "
                    "THEN file_leases.epoch "
                    "ELSE GREATEST(file_leases.epoch, "
                    "(SELECT coalesce(hwm, 0) FROM file_lease_epoch_hwm WHERE node_id = :node), "
                    "(SELECT restore_generation FROM file_platform WHERE id = 1) "
                    "* CAST(4294967296 AS bigint)) + 1 END, "
                    "holder_principal_kind = 'user', holder_kind = :holder_kind, "
                    "holder_principal_id = :holder, "
                    "holder_instance_id = :instance, machine_id = :machine, "
                    "purpose = :purpose, accepts_inbound = :inbound, heartbeat_at = now(), "
                    "acquired_at = CASE WHEN "
                    "(file_leases.released_at IS NULL AND file_leases.reaped_at IS NULL "
                    "AND file_leases.expires_at > now() "
                    "AND file_leases.holder_principal_id = :holder "
                    "AND file_leases.holder_kind = :holder_kind "
                    "AND file_leases.holder_instance_id = :instance) "
                    "THEN file_leases.acquired_at ELSE now() END, "
                    "expires_at = now() + make_interval(secs => :ttl), "
                    "last_sync_at = CASE WHEN "
                    "(file_leases.released_at IS NULL AND file_leases.reaped_at IS NULL "
                    "AND file_leases.expires_at > now() "
                    "AND file_leases.holder_principal_id = :holder "
                    "AND file_leases.holder_kind = :holder_kind "
                    "AND file_leases.holder_instance_id = :instance) "
                    "THEN file_leases.last_sync_at ELSE NULL END, "
                    "grantable_after = now(), "
                    "released_at = NULL, reaped_at = NULL, "
                    # What the last holder left behind is that grant's story;
                    # a new grant starts with nothing unsynced and nothing swept.
                    "unsynced_count = NULL, unsynced_swept_at = NULL "
                    # A lapsed lease is not immediately up for grabs: a holder
                    # whose beat was merely slow gets the grant delay to come
                    # back before anyone else may take the folder off it. Its
                    # OWN instance is exempt — that is the resume, and racing a
                    # machine against itself would only cost it its epoch.
                    #
                    # A reaped one IS up for grabs, and that disjunct is not
                    # redundant: the reaper reads its own deadline off an
                    # injected clock, so a row can be reaped while Postgres
                    # still reads its `expires_at` in the future. Every other
                    # predicate in this module — the overlap check, the
                    # covering read, the epoch fence — already calls such a row
                    # dead, and the reaper has retracted its live entries,
                    # aborted its sessions and released its holds, so there is
                    # nothing left for the folder to be held for. Without it
                    # the one path that decides who may take the folder next
                    # disagreed with the one that decides whether anybody holds
                    # it, and the folder was ungrantable to anyone but its own
                    # dead instance until the wall clock caught up.
                    # `grantable_after` still applies, so the reaper's own
                    # delay is untouched.
                    "WHERE (file_leases.released_at IS NOT NULL "
                    "OR file_leases.reaped_at IS NOT NULL "
                    "OR file_leases.expires_at + make_interval(secs => :delay) < now() "
                    "OR (file_leases.holder_principal_id = :holder "
                    "AND file_leases.holder_kind = :holder_kind "
                    "AND file_leases.holder_instance_id = :instance) "
                    ") "
                    "AND file_leases.grantable_after <= now() "
                    "RETURNING *"
                ),
                {
                    "node": node_id,
                    "org": self._repo.scope.org_team_id,
                    "holder": holder.id,
                    "holder_kind": holder.kind,
                    "instance": instance_id,
                    "machine": machine,
                    "purpose": purpose,
                    "ttl": ttl.total_seconds(),
                    "delay": self._grant_delay.total_seconds(),
                    "inbound": (
                        purpose in INBOUND_PURPOSES if accepts_inbound is None else accepts_inbound
                    ),
                },
            )
        ).first()
        if granted is None:
            raise _leased(await self._row(node_id), can_read_holder=can_read_holder)
        # Raising the mark on every grant is what makes it a net: a restore that
        # loses this lease row still cannot re-issue the epoch it just handed out.
        await self._raise_hwm(node_id, granted.epoch)
        await self._checkpoints.reach("leases.acquired")
        return _as_lease(granted)

    async def _ended(self, node_id: NodeId) -> bool:
        """Whether the lease row on ``node_id`` was handed back or taken back:
        released, as opposed to lapsed or reaped. A holder that released its
        own lease does not re-take it, so to a re-taking caller this is the
        server ending it."""
        row = (
            await self._repo.session.execute(
                text(
                    "SELECT released_at IS NOT NULL AS ended FROM file_leases "
                    "WHERE node_id = :node AND org_team_id = :org"
                ),
                {"node": node_id, "org": self._repo.scope.org_team_id},
            )
        ).first()
        return bool(row is not None and row.ended)

    async def _raise_hwm(self, node_id: NodeId, epoch: int) -> None:
        await self._repo.session.execute(
            text(
                "INSERT INTO file_lease_epoch_hwm (node_id, org_team_id, hwm) "
                "VALUES (:node, :org, :epoch) "
                "ON CONFLICT (node_id) DO UPDATE "
                "SET hwm = GREATEST(file_lease_epoch_hwm.hwm, EXCLUDED.hwm)"
            ),
            {"node": node_id, "org": self._repo.scope.org_team_id, "epoch": epoch},
        )

    async def heartbeat(
        self,
        node_id: NodeId,
        *,
        epoch: int,
        instance_id: str,
        ttl: timedelta | None = None,
        synced: bool = False,
        extend: bool = True,
    ) -> Lease:
        """Extend the lease, or tell a superseded holder that it is fenced.

        ``extend`` False is a holder that may finish but not stay: the box a
        chat or a workspace was moved off, still beating. The beat forces the
        lease exactly as :meth:`force_release` does (the deadline is kept,
        ``grantable_after`` meets it), so the holder reads ``forced``, hands
        the folder back, and is fenced at the deadline if it does not. A late
        beat on such a lease is not re-granted: it has nothing left to finish.

        One statement carries the identity and the extension together, so a
        holder reaped between its last write and this beat cannot extend a lease
        it no longer owns. A forced lease is *not* extended: the beat still
        succeeds, and its ``forced`` flag is how the holder is told to wrap up.

        ``synced`` is the holder's word that its live plane is running, and it
        stamps ``last_sync_at`` in the SAME statement — on a lease that takes
        inbound writes, the only kind that runs a plane. It is not a second
        UPDATE after the beat: a second update of the row re-checks its foreign
        key to the leased folder, and that lock on the folder, taken after the
        lease row, is the inversion the module docstring describes.

        **A beat that arrives late re-grants rather than fences.** The row still
        naming this epoch, this instance and this principal is the proof that
        nobody took THIS folder — an acquire by anyone else moves all three — so
        a holder whose beats could not land for a while is the only writer this
        folder has ever had, and fencing it would cost the turn it is in the
        middle of for nothing. It is not proof about the folders around it,
        though: a lapse is a window, and inside it a neighbour may legitimately
        acquire an ancestor or a descendant, which the grant rule refuses to
        have two holders of. So the late re-grant asks that rule again, in the
        same statement — a live lease over the same bytes under anybody else and
        the beat is fenced, exactly as :meth:`acquire` would refuse it.

        Two further lapses are not re-granted, because in both the folder
        stopped being simply ours: a lease under a force (``grantable_after`` at
        or past ``expires_at``) is being handed over, and one the reaper has
        already taken apart — its sessions aborted, its in-flight entries
        retracted — is re-taken through :meth:`acquire`, which mints the fresh
        epoch that state deserves.

        A lease on a folder in the trash is fenced too, whoever holds it: see
        :func:`end_leases_under`. That is what stops a box that never heard its
        chat was deleted from keeping the deleted chat's folder held for ever.

        Fenced on the caller like every other statement in this module. The
        epoch and the instance are both derivable from what the product serves,
        and the route admits anyone who may write the folder, so a beat matched
        on the pair alone would let a colleague keep — or, through
        :meth:`release`, end — a lease that is not theirs.
        """
        ttl = lease_ttl() if ttl is None else ttl
        holder = self._holder
        beat = (
            await self._repo.session.execute(
                text(
                    "UPDATE file_leases SET heartbeat_at = now(), "  # noqa: S608
                    "expires_at = CASE WHEN grantable_after >= expires_at "
                    "OR NOT CAST(:extend AS boolean) THEN expires_at "
                    "ELSE now() + make_interval(secs => :ttl) END, "
                    "grantable_after = CASE WHEN CAST(:extend AS boolean) THEN grantable_after "
                    "ELSE GREATEST(grantable_after, expires_at) END, "
                    "last_sync_at = CASE WHEN CAST(:synced AS boolean) AND accepts_inbound "
                    "THEN now() ELSE last_sync_at END "
                    "WHERE node_id = :node AND org_team_id = :org AND epoch = :epoch "
                    "AND holder_instance_id = :instance "
                    "AND holder_principal_id = :holder AND holder_kind = :holder_kind "
                    "AND released_at IS NULL AND reaped_at IS NULL "
                    # A trashed folder is not beaten: nothing may be written
                    # into it, so the lease would only stand between its owner
                    # and deleting it forever. A plain read of the node, never
                    # a lock — this transaction holds the lease row alone.
                    "AND NOT EXISTS (SELECT 1 FROM file_nodes t WHERE t.id = :node "
                    "AND t.trashed_at IS NOT NULL) "
                    "AND (expires_at > now() OR (CAST(:extend AS boolean) "
                    "AND grantable_after < expires_at "
                    "AND NOT EXISTS (SELECT 1 FROM file_leases o "
                    "JOIN file_nodes n ON n.id = o.node_id "
                    "JOIN file_nodes mine ON mine.id = :node "
                    "WHERE o.org_team_id = :org AND o.node_id <> :node "
                    f"AND {_overlap_sql('mine.path_ids')} "
                    "AND o.released_at IS NULL AND o.reaped_at IS NULL "
                    "AND o.expires_at > now() "
                    "AND (o.holder_principal_id <> :holder "
                    "OR o.holder_kind <> :holder_kind)))) RETURNING *"
                ),
                {
                    "node": node_id,
                    "org": self._repo.scope.org_team_id,
                    "epoch": epoch,
                    "instance": instance_id,
                    "holder": holder.id,
                    "holder_kind": holder.kind,
                    "ttl": ttl.total_seconds(),
                    "synced": synced,
                    "extend": extend,
                    **SUBTREE_DEPTH_BIND,
                },
            )
        ).first()
        if beat is None:
            raise _fenced()
        return _as_lease(beat)

    async def release(
        self,
        node_id: NodeId,
        *,
        epoch: int,
        instance_id: str,
        final: Any | None = None,
        unsynced_count: int | None = None,
    ) -> None:
        """Apply the final snapshot and hand the lease back, in one transaction.

        ``unsynced_count`` is how many files the holder still had queued when
        it gave up draining: bytes that stay on its disk. It is recorded on the
        row, where it outlives the lease for as long as the row does.

        The snapshot module owns what ``final`` means; this method guarantees
        only that it runs before the release and inside the same transaction, so a
        reader who sees the lease gone sees every change it carried. The folder
        is re-grantable the instant this commits.

        Only the holder may do it. The route decides this on ``LEASE``, which
        the ladder hands to the writer rung, so a release matched on the epoch
        and the instance alone would be a graceless take-away one rung below
        where the product puts it: taking a folder off a live holder is
        ``force_release``, a manager's verb that leaves the holder one TTL to
        finish and tells it so on its next beat.
        """
        holder = self._holder
        if final is not None:
            if self._apply_final is None:
                raise errors.InvalidRequest("no final-snapshot hook is installed")
            # The hook runs under a context this service builds, and this is the
            # only place one is built with ``final`` on it: the exemption from
            # the drive's ceilings belongs to the release the server is applying,
            # not to anything a holder can put on a request of its own.
            await self._apply_final(
                self._repo,
                node_id,
                lease=LeaseContext(
                    epoch=epoch,
                    instance_id=instance_id,
                    holder=holder,
                    final=True,
                ),
                changes=final,
            )
        await self._checkpoints.reach("leases.final_applied")
        released = (
            await self._repo.session.execute(
                text(
                    "UPDATE file_leases SET released_at = now(), grantable_after = now(), "
                    "unsynced_count = :unsynced "
                    "WHERE node_id = :node AND org_team_id = :org AND epoch = :epoch "
                    "AND holder_instance_id = :instance "
                    "AND holder_principal_id = :holder AND holder_kind = :holder_kind "
                    "AND released_at IS NULL AND reaped_at IS NULL "
                    "AND expires_at > now() RETURNING node_id"
                ),
                {
                    "node": node_id,
                    "org": self._repo.scope.org_team_id,
                    "epoch": epoch,
                    "instance": instance_id,
                    "holder": holder.id,
                    "holder_kind": holder.kind,
                    "unsynced": unsynced_count,
                },
            )
        ).first()
        if released is None:
            raise _fenced()

    async def request_release(self, node_id: NodeId) -> Lease:
        """Ask the holder to hand the folder back. The lease itself is unchanged.

        The caller needs a reader's role only, so this returns the holder for
        the notifier rather than touching the row: a reader who can be told
        "Ana has it" is a reader who can already read the folder.
        """
        return _as_lease(await self._row(node_id))

    async def force_release(
        self,
        node_id: NodeId,
        *,
        ttl: timedelta | None = None,
    ) -> Lease:
        """Give the holder one grace period, then let the lease lapse.

        The holder is not cut off mid-write: ``expires_at`` is pinned one TTL
        out (or kept, when it falls sooner) and the next heartbeat reports
        ``forced``, so a well-behaved client
        finishes its batch and releases. A client that ignores it is fenced when
        the grace runs out, exactly as a crashed one is. ``grantable_after``
        matches the deadline, which both stops anyone grabbing the folder during
        the grace and is what marks the lease forced.
        """
        ttl = lease_ttl() if ttl is None else ttl
        forced = (
            await self._repo.session.execute(
                text(
                    # A transition only ever moves a deadline earlier: a
                    # dead holder's lease is not given a fresh TTL by being
                    # forced, and forcing again adds nothing.
                    "UPDATE file_leases "
                    "SET expires_at = LEAST(expires_at, now() + make_interval(secs => :ttl)), "
                    "grantable_after = LEAST(expires_at, now() + make_interval(secs => :ttl)) "
                    "WHERE node_id = :node AND org_team_id = :org "
                    "AND released_at IS NULL AND reaped_at IS NULL "
                    "AND expires_at > now() RETURNING *"
                ),
                {
                    "node": node_id,
                    "org": self._repo.scope.org_team_id,
                    "ttl": ttl.total_seconds(),
                },
            )
        ).first()
        if forced is None:
            raise errors.NotFound(f"no live lease on {node_id}")
        return _as_lease(forced)


def _as_lease(row: Any) -> Lease:
    return Lease(
        node_id=NodeId(row.node_id),
        epoch=row.epoch,
        holder_kind=row.holder_kind,
        holder_principal_id=row.holder_principal_id,
        holder_instance_id=row.holder_instance_id,
        machine_id=row.machine_id,
        purpose=row.purpose,
        acquired_at=row.acquired_at,
        heartbeat_at=row.heartbeat_at,
        expires_at=row.expires_at,
        grantable_after=row.grantable_after,
        last_sync_at=row.last_sync_at,
        released_at=row.released_at,
        forced=row.grantable_after >= row.expires_at,
        accepts_inbound=bool(row.accepts_inbound),
    )


__all__ = [
    "DEFAULT_GRANT_DELAY",
    "EPOCH_SEQ_BITS",
    "HOLDER_KINDS",
    "LEASE_TTL",
    "ApplyFinal",
    "HolderIdentity",
    "HolderKind",
    "Lease",
    "LeaseConflict",
    "LeaseContext",
    "LeaseLock",
    "LeasePurpose",
    "LeaseService",
    "admits_unfenced_write",
    "assert_lease_epoch",
    "covering_lease",
    "end_leases_under",
    "epoch_for",
    "fenced_create_into",
    "fenced_write",
    "fenced_write_for",
    "holder_identity",
    "lease_ttl",
    "leases_held_by",
    "live_lease_purpose",
    "release_departed_machine",
    "split_epoch",
]
