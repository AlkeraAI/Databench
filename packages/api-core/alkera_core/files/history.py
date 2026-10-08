"""The node history row and the two outbox announcements, both in the caller's
transaction.

Every Files mutation writes three things in one transaction: the row it
changed, its ``file_history`` row, and its outbox row. Nothing here commits —
the caller's ``FilesRepo.transaction()`` does — so a rollback takes all three
away together and a reader can never see a change with no history, or an
announcement of a change that did not happen.

Outbox payloads carry **ids only**. A name or a path in an outbox row would put
the outbox on the erasure path (rows outlive the node they describe by up to
their retention window), so a consumer that wants the name reads the node.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal, get_args
from uuid import UUID, uuid4, uuid5

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from alkera_core.authz.enums import PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files.ids import DriveId, NodeId, OperationId
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.history import HISTORY_KINDS, FileHistory

#: What a history row can be about. Spelled as a ``Literal`` so a typo is a
#: mypy error rather than a CHECK-constraint failure at runtime.
HistoryKind = Literal[
    "create",
    "copy",
    "rename",
    "move",
    "attrs",
    "trash",
    "restore",
    "lock",
    "hold",
    "label",
    "acl",
    "conflict_resolved",
    "conflict",
]
#: The ``before`` / ``after`` documents: a small JSON snapshot of the fields the
#: change touched, never the content.
HistorySnapshot = Mapping[str, Any]

#: How many times :func:`record` recomputes ``seq`` after losing the race for
#: it. Each retry is a fresh read of the committed maximum, so the loop makes
#: progress; the bound is here so a pathological hot node fails loudly instead
#: of spinning.
MAX_SEQ_ATTEMPTS: Final = 8

#: The ``entity`` values the Files outbox rows carry.
ENTITY_FILE_NODE: Final = "file_node"
ENTITY_FILE_OPERATION: Final = "file_operation"
ENTITY_FILE_LEASE: Final = "file_lease"

#: Why a node changed, when the change is one a client renders differently from
#: an ordinary edit. ``live_saved`` is a leased folder's holder landing the
#: bytes it had been reporting as in flight; ``inbound_superseded`` is a write
#: the drive accepted into a leased subtree that the holder's own copy then
#: won. ``live_batch`` names a FOLDER whose children the holder's tree report
#: just changed -- one per folder a batch touched, never one per file -- and,
#: like the other two, it arrives at a machine's rate rather than a person's.
#: ``conflict`` names a node the drive just settled a two-way write on, and the
#: conflicted copy it kept the displaced bytes in: both rows change together,
#: and the pane that lists conflicts has a row to add. ``live_doc_saved`` is a
#: co-edited document writing its text back to the file, as often as people
#: type: the tabs editing it already show the bytes, and the rest re-read at a
#: machine's rate.
#: Anything else carries ``None``: the announcement is a nudge, and a reason is
#: only added where the client would otherwise redraw the row wrong.
NodeChangeReason = Literal[
    "live_saved", "inbound_superseded", "live_batch", "conflict", "live_doc_saved"
]

#: The namespace a non-UUID principal id is folded into. It is a storage
#: contract: every ``acting_principal``, ``actor`` and ``granted_by`` column in
#: the Files schema is a UUID, while an agent's id is a chat session id and a
#: service's is a component name — neither is required to be one. Changing this
#: namespace would renumber every actor already on record.
ACTOR_NAMESPACE: Final = UUID("6f0d5f2c-6c9c-5a9e-9a1f-2a6f0a1c7d31")


def actor_ref(ctx: ActingContext) -> UUID:
    """The UUID that stands for whoever performed ``ctx``'s call.

    Files stores its actor as a UUID everywhere, but only a *user* principal is
    guaranteed to have one: an agent's id is its chat session id, a service's is
    a component name, and a link's is a share token id. So a principal whose id
    already is a UUID keeps it, and any other id is folded — deterministically,
    with no lookup and no collision with a real user id — into a UUIDv5 under
    :data:`ACTOR_NAMESPACE`, keyed by kind *and* id so two kinds sharing a name
    stay distinct.

    Deterministic matters beyond not crashing: ``search.recent`` finds a
    caller's own work by matching ``file_history.acting_principal`` against this
    same reference, so the fold has to give one principal one answer forever.
    """
    return _principal_ref(ctx.acting_principal)


def subject_ref(ctx: ActingContext) -> UUID:
    """The UUID that stands for whoever the call is *for*.

    The twin of :func:`actor_ref`: where that names the link that made the call,
    this names its subject — the delegating user when one exists (an agent's
    work is the human's work, and a personal access token's is its owner's),
    and the acting principal itself otherwise.

    It never assumes the subject's id is a UUID. A service principal has no
    delegating user and a component name for an id, so the columns that take a
    subject (``file_ops.actor``, ``file_trash_ops.actor``,
    ``file_nodes.created_by``, ``file_versions.created_by``,
    ``file_upload_sessions.created_by``) get the same deterministic fold
    :func:`actor_ref` uses rather than a crash, a ``NULL`` or a nil UUID that
    every service would share.
    """
    return _principal_ref(ctx.subject)


async def owner_refs(
    repo: FilesRepo, ctx: ActingContext, drive_id: DriveId, paths: Sequence[str]
) -> list[UUID]:
    """Who each node created at ``paths`` (one ``path_ids`` per node) is created
    by: the ``created_by`` the write stamps, in the order of ``paths``.

    For everyone but a box this is :func:`subject_ref`, with no statement. A
    box on its own machine credential has no delegating user, and its id is a
    machine, never a person a reader knows. What it writes lands in a chat's
    or a workspace's folder, and those bytes are already charged to whoever
    owns that folder (:func:`~alkera_core.files.repo.charged_bytes`). The node
    is credited to the same person: the owner of the deepest charging folder at
    or above it, the chat's before the workspace that holds the chat. That
    writes down a fact the drive already holds and grants nothing: no policy
    reads ``created_by``. A box writing where no charging folder stands keeps
    its own id, which the listing labels without showing it.
    """
    subject = subject_ref(ctx)
    if ctx.subject.kind is not PrincipalKind.MACHINE or not paths:
        return [subject] * len(paths)
    folders = await repo.charging_owners_above(drive_id, paths)
    return [_deepest_owner(path, folders) or subject for path in paths]


async def owner_ref(repo: FilesRepo, ctx: ActingContext, drive_id: DriveId, path: str) -> UUID:
    """:func:`owner_refs` for the one node a create or a version writes."""
    return (await owner_refs(repo, ctx, drive_id, [path]))[0]


def _deepest_owner(path: str, folders: Sequence[tuple[str, UUID]]) -> UUID | None:
    """The owner of the deepest folder in ``folders`` whose path is ``path`` or
    one of its prefixes, label by label."""
    found: UUID | None = None
    depth = -1
    for folder_path, owner in folders:
        if path != folder_path and not path.startswith(f"{folder_path}."):
            continue
        labels = folder_path.count(".")
        if labels > depth:
            found, depth = owner, labels
    return found


def _principal_ref(principal: Principal) -> UUID:
    """``principal``'s id as a UUID, folding an id that is not one."""
    try:
        return UUID(principal.id)
    except ValueError:
        return uuid5(ACTOR_NAMESPACE, f"{principal.kind.value}:{principal.id}")


def _events() -> tuple[Any, Any]:
    """The outbox writer and the event registry, imported on first use.

    ``alkera_core.events`` pulls the observability and compute packages through
    its own barrel, and a Files module must not carry that at import time — the
    library is imported by the CLI and the daemon, where the cost is real. The
    announcement is the only place Files needs it, so it is paid there.
    """
    from alkera_core.events import types as event_types
    from alkera_core.events.outbox import emit

    return emit, event_types.EventType


def _agent_session_id(ctx: ActingContext) -> UUID | None:
    """The chat session an agent link in ``ctx`` is acting inside, if any.

    An agent principal's ``id`` is its chat session id; a non-UUID one (a test
    double, a future session naming scheme) is recorded as no session rather
    than failing the write it describes.
    """
    for link in ctx.delegation_chain:
        if link.kind is PrincipalKind.AGENT:
            try:
                return UUID(link.id)
            except ValueError:
                return None
    return None


async def record(
    repo: FilesRepo,
    ctx: ActingContext,
    *,
    node_id: NodeId,
    kind: HistoryKind,
    before: HistorySnapshot | None,
    after: HistorySnapshot | None,
    op_id: OperationId | None = None,
) -> FileHistory:
    """Append one history row for ``node_id`` in the caller's transaction.

    ``seq`` is dense per node, and it is computed **inside** the INSERT —
    ``SELECT coalesce(max(seq), 0) + 1 FROM file_history WHERE node_id = …`` —
    so no read-then-write window exists on the Python side and no row lock is
    taken on the node.

    Race safety: two writers can still read the same maximum, because under
    READ COMMITTED neither sees the other's uncommitted row. They do not both
    win: ``uq_file_history_node_seq`` makes the second INSERT block on the index
    entry until the first transaction ends, and then either succeed (the first
    rolled back) or raise ``unique_violation`` (the first committed). That
    violation is the signal to recompute, which is what this loop does inside a
    SAVEPOINT so the caller's transaction survives it. The unique index — not
    the ordering of the two statements — is what makes the sequence dense.
    """
    if kind not in get_args(HistoryKind):
        raise ValueError(f"unknown history kind {kind!r}")
    if kind not in HISTORY_KINDS:  # pragma: no cover - the two lists are pinned by a test
        raise ValueError(f"history kind {kind!r} is not in the table's CHECK")
    repo._require_open()
    session = repo.session
    acting = actor_ref(ctx)
    delegating = UUID(ctx.delegating_user.id) if ctx.delegating_user is not None else None
    agent_session = _agent_session_id(ctx)

    last: IntegrityError | None = None
    for _ in range(MAX_SEQ_ATTEMPTS):
        savepoint = await session.begin_nested()
        try:
            written = await repo.insert_history(
                node_id=node_id,
                kind=kind,
                acting_principal=acting,
                delegating_user=delegating,
                agent_session_id=agent_session,
                before=dict(before) if before is not None else None,
                after=dict(after) if after is not None else None,
                op_id=op_id,
            )
        except IntegrityError as exc:
            await savepoint.rollback()
            last = exc
            continue
        await savepoint.commit()
        return written
    raise RuntimeError(
        f"could not allocate a history seq for node {node_id} in {MAX_SEQ_ATTEMPTS} attempts"
    ) from last


async def record_creates(
    repo: FilesRepo,
    ctx: ActingContext,
    created: Sequence[tuple[NodeId, HistorySnapshot]],
) -> None:
    """One ``create`` row for each of many nodes this transaction just minted,
    in ONE statement.

    :func:`record` computes each row's ``seq`` from the node's existing
    history, which costs a statement per row. A node minted in the caller's own
    transaction has no history for anyone to race: its first row is ``seq``
    1, and a whole report of new nodes is written at once.
    """
    if not created:
        return
    repo._require_open()
    delegating = UUID(ctx.delegating_user.id) if ctx.delegating_user is not None else None
    await repo.session.execute(
        text(
            "INSERT INTO file_history (id, org_team_id, node_id, seq, kind, acting_principal, "
            "delegating_user, agent_session_id, before, after) "
            "SELECT t.id, :org, t.node_id, 1, 'create', :actor, :delegating, :agent, NULL, "
            "CAST(t.after AS jsonb) "
            "FROM unnest(CAST(:ids AS uuid[]), CAST(:nodes AS uuid[]), CAST(:afters AS text[])) "
            "AS t(id, node_id, after)"
        ),
        {
            "org": repo.scope.org_team_id,
            "actor": actor_ref(ctx),
            "delegating": delegating,
            "agent": _agent_session_id(ctx),
            "ids": [uuid4() for _ in created],
            "nodes": [node_id for node_id, _ in created],
            "afters": [json.dumps(dict(after)) for _, after in created],
        },
    )


async def emit_node_changed(
    repo: FilesRepo,
    ctx: ActingContext,
    *,
    node_id: NodeId,
    drive_id: DriveId,
    version: int,
    parent_id: NodeId | None = None,
    reason: NodeChangeReason | None = None,
    withdrawn: Sequence[tuple[str, UUID]] = (),
    flush: bool = True,
) -> None:
    """Announce that a node changed. Ids only — no name, path or symlink target.

    ``withdrawn`` names the principals (``(kind, id)``) whose grant on this node
    the change took away. The change feed reads it to tell exactly those
    callers that the node is gone from them, and nobody else that it exists.

    ``parent_id`` is the folder the node is in, when the caller already holds
    it: a client that knows which listing moved refreshes that one instead of
    every listing it has cached. It stays an id, so it discloses no more than
    the node id beside it already does, and it is optional because some callers
    genuinely do not have it — a client handed ``None`` falls back to refreshing
    everything, which is slower and never wrong.

    ``flush=False`` leaves the row pending in the session, for a caller
    announcing several nodes at once: its own flush then writes them all in one
    statement rather than one each.
    """
    emit, event_types = _events()
    repo._require_open()
    await emit(
        repo.session,
        flush=flush,
        org_id=repo.scope.org_team_id,
        type=event_types.FILE_NODE_CHANGED,
        entity=ENTITY_FILE_NODE,
        entity_id=str(node_id),
        version=version,
        payload={
            "node_id": str(node_id),
            "drive_id": str(drive_id),
            "version": version,
            "parent_id": str(parent_id) if parent_id is not None else None,
            "reason": reason,
            **(
                {"withdrawn": [{"kind": kind, "id": str(id_)} for kind, id_ in withdrawn]}
                if withdrawn
                else {}
            ),
        },
    )


#: The drive queued a write, delete or rename under the lease for its holder.
LEASE_CHANGED_INBOUND = "inbound"
#: The holder reported what it is doing to the nodes under its lease.
LEASE_CHANGED_REPORT = "report"


async def emit_lease_changed(
    repo: FilesRepo,
    ctx: ActingContext,
    *,
    lease_node_id: NodeId,
    drive_id: DriveId,
    live_seq: int,
    landing_count: int | None = None,
    subtree: bool = False,
    reason: str | None = None,
    flush: bool = True,
) -> None:
    """Announce that a lease's in-flight plane moved.

    The leased node and the sequence, and nothing else. It is one frame per
    change to the whole subtree rather than one per node, because the client
    reads the plane back through the authorized listing anyway — the frame only
    has to say "this mount is at a newer number than the one you hold".

    A tree report adds two counts' worth: ``landing_count`` (how many files
    under the lease still have bytes on their way) rides the row for a reader
    of the outbox, and ``subtree`` says the batch touched more folders than it
    named one by one, so a client refreshes every folder it has open under the
    lease instead of the ones a per-folder frame would have named.

    ``reason`` says who moved it: :data:`LEASE_CHANGED_INBOUND` when the drive
    queued something for the holder to apply, :data:`LEASE_CHANGED_REPORT` when
    the holder itself reported. A holder drains on the first and never on its
    own report, so a box writing a folder is not rung back into asking the
    drive what it owes after every report it makes.
    """
    emit, event_types = _events()
    repo._require_open()
    await emit(
        repo.session,
        org_id=repo.scope.org_team_id,
        type=event_types.FILE_LEASE_CHANGED,
        entity=ENTITY_FILE_LEASE,
        entity_id=str(lease_node_id),
        version=live_seq,
        payload={
            "lease_node_id": str(lease_node_id),
            "drive_id": str(drive_id),
            "live_seq": live_seq,
            **({"landing_count": landing_count} if landing_count is not None else {}),
            **({"subtree": True} if subtree else {}),
            **({"reason": reason} if reason else {}),
        },
        flush=flush,
    )


async def emit_operation_changed(
    repo: FilesRepo,
    ctx: ActingContext,
    *,
    op_id: OperationId,
    drive_id: DriveId,
    version: int,
) -> None:
    """Announce that a bulk operation advanced. Ids only, same reason."""
    emit, event_types = _events()
    repo._require_open()
    await emit(
        repo.session,
        org_id=repo.scope.org_team_id,
        type=event_types.FILE_OPERATION_CHANGED,
        entity=ENTITY_FILE_OPERATION,
        entity_id=str(op_id),
        version=version,
        payload={"op_id": str(op_id), "drive_id": str(drive_id), "version": version},
    )


__all__ = [
    "ACTOR_NAMESPACE",
    "ENTITY_FILE_LEASE",
    "ENTITY_FILE_NODE",
    "ENTITY_FILE_OPERATION",
    "LEASE_CHANGED_INBOUND",
    "LEASE_CHANGED_REPORT",
    "MAX_SEQ_ATTEMPTS",
    "HistoryKind",
    "HistorySnapshot",
    "NodeChangeReason",
    "actor_ref",
    "emit_lease_changed",
    "emit_node_changed",
    "emit_operation_changed",
    "owner_ref",
    "owner_refs",
    "record",
    "record_creates",
    "subject_ref",
]
