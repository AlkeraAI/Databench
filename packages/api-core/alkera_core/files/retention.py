"""Retention labels and the version pruner.

Two rules decide how long a node's history lives, and the label wins:

* the **global rule** keeps the newest ``files_version_keep_newest`` versions
  *or* everything younger than ``files_version_keep_window_days`` — whichever
  keeps MORE, so a busy path keeps its last few revisions and a quiet one keeps
  a season of them. Both are settings; the defaults below are the rule Alkera
  hosts, and a deployment that wants deeper history raises them;
* a **retention label** resolved through the node's ancestors overrides it, one
  bound at a time, read from the label row itself. A label naming a number —
  ``{"keep_versions": 5}`` — bounds that one path and leaves the other bound to
  the deployment. Day one seeds one label, ``head-only``, for churn-heavy paths
  such as ``.git/index``, where every write is noise and only the current bytes
  matter; its opposite, ``keep_all``, prunes nothing at all and is how a legal
  hold or an audited path is expressed. A new label is a row, never a branch
  here: both bounds are read out of ``policy``, so an org's ``legal-hold`` and a
  future ``compliance-7y`` reach the pruner without a code change.

Four exemptions sit above both rules and can never be pruned away: the head
version, a pinned (``keep_forever``) version, a held version, and a version some
live thing still points at — an undo inverse inside its window, an unexpired
signed content grant, or a conflict row (whose displaced bytes, on a
versions-only resolution, live nowhere else). Removing one of those would break
a restore, a legal hold, an in-flight download or a conflict's keep, so they
survive even under ``head-only``.

Only version rows are deleted here. Their objects stay in the store until the
reachability sweep collects them, because an object may be shared by another
org's version and deleting it inline would corrupt a stranger's file.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, cast

from sqlalchemy import CursorResult, Table, delete, func, or_, select

from alkera_core.authz.principal import ActingContext
from alkera_core.config import Settings
from alkera_core.config import settings as default_settings
from alkera_core.files.checkpoints import Checkpoints, NoopCheckpoints
from alkera_core.files.errors import InvalidRequest, NotFound
from alkera_core.files.history import emit_node_changed, record
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.history import FileConflict, FileContentGrant, FileRetentionLabel
from alkera_core.models.files.ops import FileOp
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion

#: The default checkpoint seam: production reaches no pause points.
_NO_CHECKPOINTS: Final[Checkpoints] = NoopCheckpoints()

#: Table handles, not mapped classes: a bare statement must not go through the
#: ORM's unit of work, and the repo hygiene scan reserves the class names.
_VERSIONS: Final[Table] = cast(Table, FileVersion.__table__)
_LABELS: Final[Table] = cast(Table, FileRetentionLabel.__table__)
_CONFLICTS: Final[Table] = cast(Table, FileConflict.__table__)
_NODES: Final[Table] = cast(Table, FileNode.__table__)
_OPS: Final[Table] = cast(Table, FileOp.__table__)
_GRANTS: Final[Table] = cast(Table, FileContentGrant.__table__)

#: The label that keeps only the current bytes. Seeded per org on first use.
HEAD_ONLY: Final = "head-only"

#: The folder names whose contents a machine manages rather than a person
#: writes: a tool regenerates every byte under them, so their history is churn
#: and only the current bytes matter. A path with one of these as a segment is
#: treated as ``head-only`` wherever the drive has to decide what a displaced
#: write becomes -- it stays a version in the node's history, and no
#: conflicted-copy file is minted beside it for a person to clean up. The one
#: list: a new machine-managed folder is added here, never a second set.
HEAD_ONLY_DIRS: Final[frozenset[bytes]] = frozenset(
    {b".git", b"node_modules", b"__pycache__", b".venv", b"dist", b"target"}
)


@dataclass(frozen=True, slots=True)
class Policy:
    """The two bounds in force for one node.

    Keep the newest ``newest`` versions, *or* everything inside ``window`` —
    whichever keeps MORE. ``None`` on either bound means that bound keeps
    everything, which is what an explicit ``null`` in a label document and a
    value this reader cannot read both resolve to: this module over-keeps rather
    than over-deletes.
    """

    newest: int | None
    window: timedelta | None


#: The policy document that keeps only the head.
HEAD_ONLY_POLICY: Final[dict[str, Any]] = {"keep_versions": 0, "keep_for_days": 0}
#: The policy document that prunes nothing. An explicit null bound reads as *no
#: bound* — the same sentinel these would carry as nullable columns — while a
#: zero bound reads as *no history*. A label carrying it (``legal-hold``,
#: ``compliance-7y``) is created as a row by an admin or a policy engine and
#: needs nothing from this module.
KEEP_ALL_POLICY: Final[dict[str, Any]] = {"keep_versions": None, "keep_for_days": None}

#: The labels an org gets for free, with the policy each one means.
SEEDED_LABELS: Final[dict[str, dict[str, Any]]] = {
    HEAD_ONLY: dict(HEAD_ONLY_POLICY),
}

#: The Alkera-hosted global rule, and the default behind the
#: ``files_version_keep_newest`` setting: keep this many of the newest versions …
KEEP_NEWEST: Final = 5
#: … or everything written inside this window, whichever keeps more; the default
#: behind ``files_version_keep_window_days``.
KEEP_WINDOW: Final = timedelta(days=90)

#: How many nodes one :func:`prune_versions` call may consider.
PRUNE_BATCH: Final = 500


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One version, reduced to what the keep rules read."""

    id: uuid.UUID
    node_id: uuid.UUID
    seq: int
    created_at: datetime
    keep_forever: bool
    held: bool


def configured_policy(settings: Settings | None = None) -> Policy:
    """The global rule as this deployment configured it.

    A negative bound is a misconfiguration, not an instruction to delete, so it
    resolves to *unbounded* — the same over-keeping answer an unreadable label
    document gets.
    """
    resolved = default_settings if settings is None else settings
    newest = resolved.files_version_keep_newest
    days = resolved.files_version_keep_window_days
    return Policy(
        newest=None if newest < 0 else newest,
        window=None if days < 0 else timedelta(days=days),
    )


def _newest_bound(document: dict[str, Any], fallback: int | None) -> int | None:
    """The ``keep_versions`` bound a label asks for.

    An absent key, or a value this reader cannot read, is ``fallback``; an
    explicit ``null`` is *unbounded*; a count is itself.
    """
    if "keep_versions" not in document:
        return fallback
    value = document["keep_versions"]
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return fallback


def _window_bound(document: dict[str, Any], fallback: timedelta | None) -> timedelta | None:
    """The ``keep_for_days`` bound a label asks for, read like its twin above."""
    if "keep_for_days" not in document:
        return fallback
    value = document["keep_for_days"]
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return timedelta(days=value)
    return fallback


def _policy_of(label: FileRetentionLabel | None, default: Policy) -> Policy:
    """The bounds ``label`` asks for, falling back to ``default`` one bound at a time.

    An explicit ``null`` bound means *unbounded* — keep every version — while a
    zero bound means *keep nothing but the exemptions*. A missing key is
    neither: that bound stays the deployment's, so a label naming only
    ``keep_versions`` still ages out on the deployment's window, and a document
    written by a newer writer with keys this reader does not know keeps history
    rather than silently shedding it.
    """
    if label is None:
        return default
    document = dict(label.policy)
    return Policy(
        newest=_newest_bound(document, default.newest),
        window=_window_bound(document, default.window),
    )


async def label_for(
    repo: FilesRepo,
    node: FileNode,
    chain: Sequence[FileNode],
) -> FileRetentionLabel | None:
    """The label governing ``node``: the nearest labelled ancestor's, or its own.

    ``chain`` is the node's ancestry root-first as ``FilesRepo.chain`` returns
    it. The walk runs from the node outwards, so a subtree can override a label
    an ancestor set — that is what makes ``head-only`` plantable on one path
    while its sibling keeps history.
    """
    ordered: list[FileNode] = [*chain]
    if not ordered or ordered[-1].id != node.id:
        ordered.append(node)
    for candidate in reversed(ordered):
        label_id = candidate.retention_label_id
        if label_id is None:
            continue
        row = (
            await repo.execute_scoped(select(_LABELS).where(_LABELS.c.id == label_id))
        ).one_or_none()
        if row is None:
            continue
        return FileRetentionLabel(
            id=row.id,
            org_team_id=row.org_team_id,
            name=row.name,
            policy=row.policy,
        )
    return None


async def _ensure_label(repo: FilesRepo, name: str) -> FileRetentionLabel:
    """The org's row for ``name``, seeded on first use.

    ``head-only`` is a product promise, not an admin's creation, so an org that
    has never used it still gets it — created here rather than in a migration so
    an org added tomorrow is no different from one added today.
    """
    row = (await repo.execute_scoped(select(_LABELS).where(_LABELS.c.name == name))).one_or_none()
    if row is not None:
        return FileRetentionLabel(
            id=row.id, org_team_id=row.org_team_id, name=row.name, policy=row.policy
        )
    if name not in SEEDED_LABELS:
        raise NotFound()
    label = FileRetentionLabel(
        id=uuid.uuid4(),
        org_team_id=repo.scope.org_team_id,
        name=name,
        policy=dict(SEEDED_LABELS[name]),
    )
    await repo.add(label)
    await repo.flush()
    return label


async def apply_label(
    repo: FilesRepo,
    ctx: ActingContext,
    node: FileNode,
    label_name: str | None,
    *,
    checkpoints: Checkpoints = _NO_CHECKPOINTS,
) -> FileRetentionLabel | None:
    """Put ``label_name`` on ``node`` (``None`` clears it), with its history row.

    Runs inside the caller's ``repo.transaction()``: the node update, the
    ``label`` history row and the outbox announcement commit or roll back
    together, so a labelled node always has the history row that explains it.
    """
    if node.kind not in {"folder", "file"}:
        raise InvalidRequest(code="files.retention_label_unsupported_kind")
    before = node.retention_label_id
    label = None if label_name is None else await _ensure_label(repo, label_name)
    after = None if label is None else label.id
    statement = (
        _NODES.update()
        .where(
            _NODES.c.id == node.id,
            _NODES.c.org_team_id == repo.scope.org_team_id,
            # Compare-and-swap on the value read: a second labeller that ran in
            # between changes the row count, never the winner's decision.
            _NODES.c.retention_label_id.is_(before)
            if before is None
            else _NODES.c.retention_label_id == before,
        )
        .values(retention_label_id=after, etag=_NODES.c.etag + 1)
        .returning(_NODES.c.etag)
    )
    result = cast(CursorResult[Any], await repo.session.execute(statement))
    row = result.one_or_none()
    if row is None:
        raise NotFound()
    node.retention_label_id = after
    await checkpoints.reach("retention.label.applied")
    await record(
        repo,
        ctx,
        node_id=NodeId(node.id),
        kind="label",
        before={"retention_label_id": str(before) if before else None},
        after={"retention_label_id": str(after) if after else None},
    )
    await emit_node_changed(
        repo,
        ctx,
        node_id=NodeId(node.id),
        drive_id=DriveId(node.drive_id),
        version=int(row.etag),
    )
    return label


def _referenced_ids(document: Any, into: set[uuid.UUID]) -> None:
    """Collect every UUID-shaped string anywhere inside a JSON document.

    An undo inverse is a free-form document whose shape differs per operation
    kind, so the pruner does not parse it — it refuses to delete anything the
    inverse *mentions*. Over-keeping a version costs bytes; under-keeping one
    breaks an undo the user was promised.
    """
    if isinstance(document, str):
        try:
            into.add(uuid.UUID(document))
        except ValueError:
            return
    elif isinstance(document, dict):
        for value in cast(dict[str, Any], document).values():
            _referenced_ids(value, into)
    elif isinstance(document, list):
        for value in document:
            _referenced_ids(value, into)


async def _pinned_by_references(repo: FilesRepo) -> set[uuid.UUID]:
    """Version ids a live undo inverse or an unexpired content grant holds.

    Both deadlines are Postgres ``now()``, never the caller's clock: whether an
    undo window or a signed URL is still open is a safety decision, and a worker
    with a skewed clock must not be able to delete bytes someone can still ask
    for.
    """
    referenced: set[uuid.UUID] = set()
    ops = await repo.execute_scoped(
        select(_OPS.c.inverse).where(
            _OPS.c.inverse.is_not(None),
            _OPS.c.undoable_until.is_not(None),
            _OPS.c.undoable_until > func.now(),
        )
    )
    for (inverse,) in ops.all():
        _referenced_ids(inverse, referenced)
    grants = await repo.execute_scoped(
        select(_GRANTS.c.version_id).where(_GRANTS.c.expires_at > func.now())
    )
    referenced.update(version_id for (version_id,) in grants.all())
    return referenced


def _keep_for_node(
    versions: Sequence[_Candidate],
    *,
    head_id: uuid.UUID | None,
    now: datetime,
    policy: Policy,
    referenced: set[uuid.UUID],
) -> set[uuid.UUID]:
    """The ids of ``versions`` that survive, newest first."""
    if policy.newest is None or policy.window is None:
        # The bounds are a union, so one of them keeping everything settles it —
        # which is what ``keep_all`` and a legal hold are.
        return {candidate.id for candidate in versions}
    keep = {
        candidate.id
        for candidate in versions
        if candidate.keep_forever or candidate.held or candidate.id in referenced
    }
    if head_id is not None:
        keep.add(head_id)
    ordered = sorted(versions, key=lambda candidate: candidate.seq, reverse=True)
    # The union, not the smaller of the two: "whichever keeps MORE" is what
    # makes a burst of 300 writes today and a file untouched for a year both
    # keep something a user recognizes. A zero bound contributes nothing, which
    # is how ``head-only`` sheds everything but the head and the exemptions.
    keep.update(candidate.id for candidate in ordered[: policy.newest])
    if policy.window > timedelta(0):
        cutoff = now - policy.window
        keep.update(candidate.id for candidate in ordered if candidate.created_at >= cutoff)
    return keep


async def prune_versions(
    repo: FilesRepo,
    *,
    now: datetime,
    batch: int = PRUNE_BATCH,
    policy: Policy | None = None,
    checkpoints: Checkpoints = _NO_CHECKPOINTS,
) -> int:
    """Delete the version rows no rule keeps. Returns how many were removed.

    ``batch`` bounds the nodes one call considers, so the janitor's pass is a
    bounded unit of work whatever the drive holds. ``policy`` is the global rule
    applied where no label overrides it: it defaults to this deployment's
    settings, and is a parameter so a caller with a rule of its own — or a test
    exercising one — needs no ambient state. Inline bytes go with their row;
    store objects are left to the reachability sweep.
    """
    if batch <= 0:
        raise InvalidRequest(code="files.invalid_batch")
    counts = await repo.execute_scoped(
        select(_VERSIONS.c.node_id)
        .group_by(_VERSIONS.c.node_id)
        .having(func.count() > 1)
        .order_by(_VERSIONS.c.node_id)
        .limit(batch)
    )
    node_ids = [node_id for (node_id,) in counts.all()]
    if not node_ids:
        return 0
    rows = await repo.execute_scoped(
        select(
            _VERSIONS.c.id,
            _VERSIONS.c.node_id,
            _VERSIONS.c.seq,
            _VERSIONS.c.created_at,
            _VERSIONS.c.keep_forever,
            _VERSIONS.c.held,
        ).where(_VERSIONS.c.node_id.in_(node_ids))
    )
    per_node: dict[uuid.UUID, list[_Candidate]] = {}
    for row in rows.all():
        per_node.setdefault(row.node_id, []).append(
            _Candidate(
                id=row.id,
                node_id=row.node_id,
                seq=row.seq,
                created_at=row.created_at,
                keep_forever=row.keep_forever,
                held=row.held,
            )
        )
    referenced = await _pinned_by_references(repo)
    configured = configured_policy() if policy is None else policy
    nodes = await repo.nodes([NodeId(node_id) for node_id in node_ids])
    doomed: list[uuid.UUID] = []
    for node in nodes:
        candidates = per_node.get(node.id, [])
        if not candidates:
            continue
        label = await label_for(repo, node, await repo.chain(node))
        keep = _keep_for_node(
            candidates,
            head_id=node.head_version_id,
            now=now,
            policy=_policy_of(label, configured),
            referenced=referenced,
        )
        doomed.extend(candidate.id for candidate in candidates if candidate.id not in keep)
    if not doomed:
        return 0
    await checkpoints.reach("retention.prune.selected")
    result = cast(
        CursorResult[Any],
        await repo.session.execute(
            delete(_VERSIONS).where(
                _VERSIONS.c.id.in_(doomed),
                _VERSIONS.c.org_team_id == repo.scope.org_team_id,
                # The exemptions again, as a predicate: whatever raced us
                # between the read and here, a pin or a hold set in that window
                # still wins.
                _VERSIONS.c.keep_forever.is_(False),
                _VERSIONS.c.held.is_(False),
                # A grant row is the foreign key that makes a signed URL
                # redeemable, so while one exists the version cannot go —
                # expired or not. Reaping the row is the janitor's job, and
                # only then does the version become prunable.
                ~select(1)
                .select_from(_GRANTS)
                .where(_GRANTS.c.version_id == _VERSIONS.c.id)
                .exists(),
                # And the exemption that moves most: a restore or an undo can
                # make one of the doomed versions a node's head in the window
                # the selection above left open. The foreign key would refuse
                # the delete anyway and abort the whole pass, so naming the
                # head here is both what keeps the restored version and what
                # keeps the rest of the batch going.
                ~select(1)
                .select_from(_NODES)
                .where(_NODES.c.head_version_id == _VERSIONS.c.id)
                .exists(),
                # A conflict names its base, its two sides and so the bytes a
                # versions-only resolution kept nowhere else. Its foreign keys
                # would refuse the delete and abort the pass for good, since
                # the next pass selects the same nodes.
                ~select(1)
                .select_from(_CONFLICTS)
                .where(
                    or_(
                        _CONFLICTS.c.base_version_id == _VERSIONS.c.id,
                        _CONFLICTS.c.theirs_version_id == _VERSIONS.c.id,
                        _CONFLICTS.c.mine_version_id == _VERSIONS.c.id,
                    )
                )
                .exists(),
            )
        ),
    )
    await checkpoints.reach("retention.prune.deleted")
    return int(result.rowcount)


__all__ = [
    "HEAD_ONLY",
    "HEAD_ONLY_DIRS",
    "HEAD_ONLY_POLICY",
    "KEEP_ALL_POLICY",
    "KEEP_NEWEST",
    "KEEP_WINDOW",
    "PRUNE_BATCH",
    "SEEDED_LABELS",
    "Policy",
    "apply_label",
    "configured_policy",
    "label_for",
    "prune_versions",
]
