"""``fsck``: the consistency check between the metadata and the bytes.

The scrub asks "are these bytes still what they were". ``fsck`` asks the other
question — "do the rows and the objects still agree, and is anything stuck" —
and it is the one report that reads *both* sides. Every state machine in the
design has a deadline; this is where a thing past its deadline becomes visible
instead of lingering.

Two rules make the report safe to run in production against a live domain:

Nothing is ever deleted, and metadata that names bytes is never rewritten
    ``repair_safe`` fixes exactly three things — a drifted ``dir_stats`` row
    (recomputed from the tree), a stale ``acl_rewriting`` flag (cleared by
    recomputing the cache from the chain, which is the truth meanwhile), and a
    ``committing`` upload session (re-queued so its worker picks it up). Every
    other finding is reported and, where it names a row an operator must judge,
    quarantined. A dangling reference is *never* healed by pointing the version
    somewhere else and an orphan object is *never* deleted: the sweeper owns
    deletion, behind its breaker and its two phases, and a check that also
    deletes has no second opinion when it is wrong.

The findings are codes, not prose
    A planted fault has to be findable by exactly one code, or "fsck found it"
    means nothing. The codes are :data:`CHECKS`, and the report is a flat list
    so a caller filters rather than walks a shape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final

from sqlalchemy import text

from alkera_core.config import get_settings
from alkera_core.files.gc import DELETED_WINDOW, Janitor
from alkera_core.files.ids import DomainId, OrgScope
from alkera_core.files.ops import HEARTBEAT_DEADLINE
from alkera_core.files.repo import SUBTREE_DEPTH_BIND, FilesRepo, subtree_sql
from alkera_core.files.store.keys import DOMAIN_PREFIX
from alkera_core.files.sweepers import INCOMING_GRACE
from alkera_core.files.uploads import COMMITTING_DEADLINE

# -- the check codes -----------------------------------------------------

#: A ``ready`` version whose object is not in the store. The version is
#: quarantined: its bytes are gone and only an operator can decide between a
#: restore and a tombstone.
DANGLING_REFERENCE: Final = "fsck.dangling_reference"
#: The object is there but not the size the row promised.
OBJECT_SIZE_MISMATCH: Final = "fsck.object_size_mismatch"
#: An inline version whose stored bytes are not ``size_bytes`` long.
INLINE_SIZE_MISMATCH: Final = "fsck.inline_size_mismatch"
#: An object under ``objects/`` that no version references.
ORPHAN_OBJECT: Final = "fsck.orphan_object"
#: A node's head names a version that belongs to another node, or to nothing.
BAD_HEAD_POINTER: Final = "fsck.bad_head_pointer"
#: An object addressed by a version but living under another domain's prefix.
OBJECT_OUTSIDE_PREFIX: Final = "fsck.object_outside_prefix"
#: A folder whose cached stats disagree with the tree beneath it.
DIR_STATS_DRIFT: Final = "fsck.dir_stats_drift"

# One code per stuck state of the session and operation state machines.
COMMITTING_STALE: Final = "fsck.committing_session_stale"
HOLD_SUM_MISMATCH: Final = "fsck.hold_sum_mismatch"
NODE_FLAG_WITHOUT_OP: Final = "fsck.node_flag_without_op"
TRASHED_PAST_PURGE: Final = "fsck.trashed_past_purge"
OP_WITHOUT_HEARTBEAT: Final = "fsck.op_without_heartbeat"
EXPIRED_LEASE_LIVE_SESSIONS: Final = "fsck.expired_lease_live_sessions"
INCOMING_PAST_TTL: Final = "fsck.incoming_past_ttl"
DELETED_PAST_WINDOW: Final = "fsck.deleted_past_window"
#: A replay record the janitor should already have dropped: its ``expires_at``
#: (the deadline its scope gave it, which the sweeper enforces) is past
#: Postgres ``now()``.
IDEMPOTENCY_KEY_EXPIRED: Final = "fsck.idempotency_key_expired"
#: A file still carrying the holder's report with no lease behind it that
#: could still explain it: no lease row on the file or any folder above it, or
#: only one released or lapsed longer ago than the unsynced grace. Reported,
#: never repaired here: the lease reaper's grace sweep is what clears it.
HOLDER_FACET_WITHOUT_LEASE: Final = "fsck.holder_facet_without_lease"

CHECKS: Final[tuple[str, ...]] = (
    DANGLING_REFERENCE,
    OBJECT_SIZE_MISMATCH,
    INLINE_SIZE_MISMATCH,
    ORPHAN_OBJECT,
    BAD_HEAD_POINTER,
    OBJECT_OUTSIDE_PREFIX,
    DIR_STATS_DRIFT,
    COMMITTING_STALE,
    HOLD_SUM_MISMATCH,
    NODE_FLAG_WITHOUT_OP,
    TRASHED_PAST_PURGE,
    OP_WITHOUT_HEARTBEAT,
    EXPIRED_LEASE_LIVE_SESSIONS,
    INCOMING_PAST_TTL,
    DELETED_PAST_WINDOW,
    IDEMPOTENCY_KEY_EXPIRED,
    HOLDER_FACET_WITHOUT_LEASE,
)

#: The findings ``--repair-safe`` is allowed to act on. Everything else is
#: reported only. This tuple is the whole authority: a new check is
#: report-only until it is deliberately added here.
REPAIRABLE: Final[tuple[str, ...]] = (
    DIR_STATS_DRIFT,
    NODE_FLAG_WITHOUT_OP,
    COMMITTING_STALE,
)

#: Findings that go in the report but never in front of an operator. An
#: expired replay record needs no judgement — the janitor's own sweeper drops
#: it on the next pass — and it is not one of the byte- or tree-shaped things
#: ``file_quarantine`` is allowed to hold.
#: A facet with no lease behind it is the lease reaper's to clear on its next
#: pass, so it too is reported and left for the sweeper that owns it.
NEVER_QUARANTINED: Final[tuple[str, ...]] = (IDEMPOTENCY_KEY_EXPIRED, HOLDER_FACET_WITHOUT_LEASE)

#: How long a node may wear ``moving``/``acl_rewriting`` without a live op.
NODE_FLAG_DEADLINE: Final = timedelta(minutes=10)
#: The grace on top of ``purge_after`` before a trashed node is stuck.
PURGE_GRACE: Final = timedelta(days=1)
#: How many folders one run recomputes ``dir_stats`` for.
DIR_STATS_SAMPLE: Final = 200
#: How many keys one listing page pulls.
LIST_PAGE: Final = 1000

#: The states in which a node must be backed by a live operation.
FLAGGED_STATES: Final[tuple[str, ...]] = ("moving", "acl_rewriting")
#: The upload-session states that still own their staged bytes and their hold.
LIVE_SESSION_STATES: Final[tuple[str, ...]] = ("open", "uploading", "committing")
#: Operation states that mean a worker is supposed to be on it.
LIVE_OP_STATES: Final[tuple[str, ...]] = ("queued", "running")


@dataclass(frozen=True, slots=True)
class FsckFinding:
    """One thing that disagrees, named by the check that found it."""

    code: str
    kind: str
    """The ``file_quarantine`` kind the referent is, so a finding routes."""
    ref_id: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def repairable(self) -> bool:
        return self.code in REPAIRABLE


@dataclass(frozen=True, slots=True)
class FsckReport:
    """Everything one pass found, and what ``--repair-safe`` actually fixed."""

    domain_id: DomainId
    findings: tuple[FsckFinding, ...]
    repaired: tuple[FsckFinding, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.findings

    def by_code(self, code: str) -> tuple[FsckFinding, ...]:
        return tuple(finding for finding in self.findings if finding.code == code)

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(finding.code for finding in self.findings)


async def run_fsck(
    janitor: Janitor,
    *,
    org: OrgScope,
    domain_id: DomainId,
    repair_safe: bool = False,
) -> FsckReport:
    """Check one domain, and with ``repair_safe`` fix only the safe subset."""
    repo = janitor._repo_for_org(org)
    findings: list[FsckFinding] = []

    async with repo.transaction():
        findings.extend(await _check_versions(repo, janitor, domain_id))
        findings.extend(await _check_head_pointers(repo, domain_id))
        findings.extend(await _check_dir_stats(repo, domain_id))
        findings.extend(await _check_state_machines(repo, domain_id))
    findings.extend(await _check_store(repo, janitor, domain_id))

    await janitor._checkpoints.reach("fsck.after_checks")

    for finding in findings:
        if not finding.repairable and finding.code not in NEVER_QUARANTINED:
            await _quarantine(repo, finding)

    repaired: list[FsckFinding] = []
    if repair_safe:
        for finding in findings:
            if finding.repairable and await _repair(repo, finding):
                repaired.append(finding)
        await janitor._checkpoints.reach("fsck.after_repair")
    return FsckReport(domain_id=domain_id, findings=tuple(findings), repaired=tuple(repaired))


# -- versions vs the store ----------------------------------------------


async def _check_versions(
    repo: FilesRepo, janitor: Janitor, domain_id: DomainId
) -> list[FsckFinding]:
    """Every version's bytes: present, the right size, in the right prefix."""
    findings: list[FsckFinding] = []
    rows = (
        await repo.session.execute(
            text(
                "SELECT v.id, v.store_key, v.size_bytes, "
                "octet_length(v.inline_bytes) AS inline_len "
                "FROM file_versions v "
                "JOIN file_nodes n ON n.id = v.node_id "
                "JOIN file_drives d ON d.id = n.drive_id "
                "WHERE v.org_team_id = :org AND d.dedup_domain_id = :domain"
            ),
            {"org": str(repo.scope.org_team_id), "domain": str(domain_id)},
        )
    ).all()
    for version_id, store_key, size_bytes, inline_len in rows:
        if store_key is None:
            if inline_len is not None and int(inline_len) != int(size_bytes):
                findings.append(
                    FsckFinding(
                        code=INLINE_SIZE_MISMATCH,
                        kind="version",
                        ref_id=str(version_id),
                        detail={"declared": int(size_bytes), "stored": int(inline_len)},
                    )
                )
            continue
        if store_key.startswith(DOMAIN_PREFIX):
            # A relative key that carries a domain prefix addresses somebody
            # else's namespace once it is made absolute; that is the fault, and
            # it is not one a head lookup would ever surface.
            findings.append(
                FsckFinding(
                    code=OBJECT_OUTSIDE_PREFIX, kind="version", ref_id=str(version_id), detail={}
                )
            )
            continue
        info = await janitor._store.head(f"{DOMAIN_PREFIX}{domain_id}/{store_key}")
        if info is None:
            findings.append(
                FsckFinding(
                    code=DANGLING_REFERENCE, kind="version", ref_id=str(version_id), detail={}
                )
            )
        elif info.size != int(size_bytes):
            findings.append(
                FsckFinding(
                    code=OBJECT_SIZE_MISMATCH,
                    kind="version",
                    ref_id=str(version_id),
                    detail={"declared": int(size_bytes), "stored": info.size},
                )
            )
    return findings


async def _check_store(repo: FilesRepo, janitor: Janitor, domain_id: DomainId) -> list[FsckFinding]:
    """The other direction: objects the metadata does not account for."""
    findings: list[FsckFinding] = []
    async with repo.transaction():
        referenced = {
            row[0]
            for row in await repo.session.execute(
                text(
                    "SELECT v.store_key FROM file_versions v "
                    "JOIN file_nodes n ON n.id = v.node_id "
                    "JOIN file_drives d ON d.id = n.drive_id "
                    "WHERE v.org_team_id = :org AND d.dedup_domain_id = :domain "
                    "AND v.store_key IS NOT NULL"
                ),
                {"org": str(repo.scope.org_team_id), "domain": str(domain_id)},
            )
        }
        live_sessions = {
            str(row[0])
            for row in await repo.session.execute(
                text(
                    "SELECT id FROM file_upload_sessions "
                    "WHERE org_team_id = :org AND dedup_domain_id = :domain "
                    "AND state = ANY(:states) AND expires_at + CAST(:grace AS interval) > now()"
                ),
                {
                    "org": str(repo.scope.org_team_id),
                    "domain": str(domain_id),
                    "states": list(LIVE_SESSION_STATES),
                    "grace": INCOMING_GRACE,
                },
            )
        }
    base = f"{DOMAIN_PREFIX}{domain_id}/"

    for key in await _list(janitor, base + "objects/"):
        if key not in referenced:
            findings.append(FsckFinding(code=ORPHAN_OBJECT, kind="object", ref_id=key, detail={}))

    for key in await _list(janitor, base + "incoming/"):
        session = key.split("/")[1] if key.count("/") >= 2 else ""
        if session not in live_sessions:
            findings.append(
                FsckFinding(code=INCOMING_PAST_TTL, kind="object", ref_id=key, detail={})
            )

    cutoff = janitor._clock.now() - DELETED_WINDOW
    for key in await _list(janitor, base + "deleted/"):
        written = await _written_at(janitor, base + key)
        if written is not None and written <= cutoff:
            findings.append(
                FsckFinding(code=DELETED_PAST_WINDOW, kind="object", ref_id=key, detail={})
            )
    return findings


async def _list(janitor: Janitor, absolute_prefix: str) -> list[str]:
    """Every domain-relative key under a prefix, paged."""
    keys: list[str] = []
    after: str | None = None
    strip = absolute_prefix[: absolute_prefix.index("/", len(DOMAIN_PREFIX)) + 1]
    while True:
        page = await janitor._store.list_prefix(absolute_prefix, after=after, limit=LIST_PAGE)
        keys.extend(key[len(strip) :] for key in page.keys)
        if page.next_after is None:
            return keys
        after = page.next_after


async def _written_at(janitor: Janitor, absolute_key: str) -> Any:
    if janitor._age_source is None:
        return None
    return await janitor._age_source.written_at(absolute_key)


# -- the tree ------------------------------------------------------------


async def _check_head_pointers(repo: FilesRepo, domain_id: DomainId) -> list[FsckFinding]:
    """A head must name a version of that very node, and nothing else."""
    rows = (
        await repo.session.execute(
            text(
                "SELECT n.id FROM file_nodes n "
                "JOIN file_drives d ON d.id = n.drive_id "
                "LEFT JOIN file_versions v ON v.id = n.head_version_id "
                "WHERE n.org_team_id = :org AND d.dedup_domain_id = :domain "
                "AND n.head_version_id IS NOT NULL "
                "AND (v.id IS NULL OR v.node_id <> n.id)"
            ),
            {"org": str(repo.scope.org_team_id), "domain": str(domain_id)},
        )
    ).all()
    return [
        FsckFinding(code=BAD_HEAD_POINTER, kind="node", ref_id=str(row[0]), detail={})
        for row in rows
    ]


async def _check_dir_stats(repo: FilesRepo, domain_id: DomainId) -> list[FsckFinding]:
    """Recompute a sample of folders and report the ones that disagree.

    The recomputation is a subtree aggregate over ``file_nodes`` and the head
    versions beneath, which is the definition the cache is *supposed* to hold —
    so the comparison is against an independent second computation, not against
    the deltas that produced the cached number in the first place.

    "Beneath" is spelled the way :meth:`FilesRepo.subtree_predicate` spells it.
    The truncated-prefix conjunct is the expression the GiST index is
    built on, the exact test behind it keeps a root deeper than the
    truncation correct, and the drive equality is what makes the pair a
    *subtree* at all — ``path_ids`` labels are per-drive inos, so the same
    chain exists in every drive and only the drive tells two of them apart.
    RLS already hides another tenant's rows; this does not lean on it.
    """
    rows = (
        await repo.session.execute(
            text(
                "SELECT s.node_id, s.bytes, s.files, s.direct_children, "  # noqa: S608
                "COALESCE(agg.bytes, 0), COALESCE(agg.files, 0), COALESCE(kids.n, 0) "
                "FROM file_dir_stats s "
                "JOIN file_nodes f ON f.id = s.node_id "
                "JOIN file_drives d ON d.id = f.drive_id "
                "LEFT JOIN LATERAL ("
                "  SELECT SUM(v.size_bytes) AS bytes, COUNT(*) AS files "
                "  FROM file_nodes c JOIN file_versions v ON v.id = c.head_version_id "
                "  WHERE c.drive_id = f.drive_id "
                f"  AND {subtree_sql('c.path_ids', 'f.path_ids')} AND c.id <> f.id "
                # A trashed node still holds its bytes until it is purged, and
                # the trash charges only the child count, so the recount counts
                # it too. Direct children below is the half that does drop.
                "  AND c.kind = 'file'"
                ") agg ON TRUE "
                "LEFT JOIN LATERAL ("
                "  SELECT COUNT(*) AS n FROM file_nodes k "
                "  WHERE k.parent_id = f.id AND k.trashed_at IS NULL"
                ") kids ON TRUE "
                "WHERE s.org_team_id = :org AND d.dedup_domain_id = :domain "
                "ORDER BY s.node_id LIMIT :limit"
            ),
            {
                "org": str(repo.scope.org_team_id),
                "domain": str(domain_id),
                "limit": DIR_STATS_SAMPLE,
                **SUBTREE_DEPTH_BIND,
            },
        )
    ).all()
    findings: list[FsckFinding] = []
    for node_id, cached_bytes, cached_files, cached_kids, real_bytes, real_files, real_kids in rows:
        if (int(cached_bytes), int(cached_files), int(cached_kids)) == (
            int(real_bytes),
            int(real_files),
            int(real_kids),
        ):
            continue
        findings.append(
            FsckFinding(
                code=DIR_STATS_DRIFT,
                kind="node",
                ref_id=str(node_id),
                detail={
                    "cached": [int(cached_bytes), int(cached_files), int(cached_kids)],
                    "recomputed": [int(real_bytes), int(real_files), int(real_kids)],
                },
            )
        )
    return findings


# -- stuck states ---------------------------------------------------------


async def _check_state_machines(repo: FilesRepo, domain_id: DomainId) -> list[FsckFinding]:
    """Everything past its deadline, one query per stuck state.

    Deadlines are compared against Postgres ``now()`` inside the statement —
    never a Python instant — because the deadline is the safety clock and two
    workers' wall clocks disagree.
    """
    org = str(repo.scope.org_team_id)
    domain = str(domain_id)
    findings: list[FsckFinding] = []

    rows = await repo.session.execute(
        text(
            "SELECT id FROM file_upload_sessions "
            "WHERE org_team_id = :org AND dedup_domain_id = :domain "
            "AND state = 'committing' AND created_at < now() - CAST(:deadline AS interval)"
        ),
        {"org": org, "domain": domain, "deadline": COMMITTING_DEADLINE},
    )
    findings.extend(
        FsckFinding(code=COMMITTING_STALE, kind="upload_session", ref_id=str(row[0]))
        for row in rows
    )

    # Σholds == Σopen: every session that still owns its staged bytes must also
    # still own the quota those bytes were reserved against, and a session that
    # has let go of its bytes must have let go of the hold. A hold that outlives
    # its session is a drive that can never be filled again.
    rows = await repo.session.execute(
        text(
            "SELECT id, state, quota_hold_bytes, declared_size FROM file_upload_sessions "
            "WHERE org_team_id = :org AND dedup_domain_id = :domain "
            "AND ((state = ANY(:live) AND quota_hold_bytes <> declared_size) "
            "  OR (NOT (state = ANY(:live)) AND quota_hold_bytes <> 0))"
        ),
        {"org": org, "domain": domain, "live": list(LIVE_SESSION_STATES)},
    )
    findings.extend(
        FsckFinding(
            code=HOLD_SUM_MISMATCH,
            kind="upload_session",
            ref_id=str(row[0]),
            detail={"state": row[1], "hold": int(row[2]), "open": int(row[3])},
        )
        for row in rows
    )

    rows = await repo.session.execute(
        text(
            "SELECT n.id, n.state FROM file_nodes n "
            "JOIN file_drives d ON d.id = n.drive_id "
            "WHERE n.org_team_id = :org AND d.dedup_domain_id = :domain "
            "AND n.state = ANY(:flagged) AND n.updated_at < now() - CAST(:deadline AS interval) "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM file_ops o WHERE o.drive_id = n.drive_id "
            "  AND o.state = ANY(:live) AND o.heartbeat_at > now() - CAST(:deadline AS interval))"
        ),
        {
            "org": org,
            "domain": domain,
            "flagged": list(FLAGGED_STATES),
            "live": list(LIVE_OP_STATES),
            "deadline": NODE_FLAG_DEADLINE,
        },
    )
    findings.extend(
        FsckFinding(
            code=NODE_FLAG_WITHOUT_OP, kind="node", ref_id=str(row[0]), detail={"state": row[1]}
        )
        for row in rows
    )

    rows = await repo.session.execute(
        text(
            "SELECT n.id FROM file_nodes n "
            "JOIN file_trash_ops t ON t.id = n.trash_op_id "
            "JOIN file_drives d ON d.id = n.drive_id "
            "WHERE n.org_team_id = :org AND d.dedup_domain_id = :domain "
            "AND n.trashed_at IS NOT NULL AND t.purge_after + CAST(:grace AS interval) < now()"
        ),
        {"org": org, "domain": domain, "grace": PURGE_GRACE},
    )
    findings.extend(
        FsckFinding(code=TRASHED_PAST_PURGE, kind="node", ref_id=str(row[0])) for row in rows
    )

    rows = await repo.session.execute(
        text(
            "SELECT o.id FROM file_ops o "
            "JOIN file_drives d ON d.id = o.drive_id "
            "WHERE o.org_team_id = :org AND d.dedup_domain_id = :domain "
            "AND o.state = 'running' "
            "AND (o.heartbeat_at IS NULL OR o.heartbeat_at < now() - CAST(:deadline AS interval))"
        ),
        {"org": org, "domain": domain, "deadline": HEARTBEAT_DEADLINE},
    )
    findings.extend(
        FsckFinding(code=OP_WITHOUT_HEARTBEAT, kind="node", ref_id=str(row[0])) for row in rows
    )

    rows = await repo.session.execute(
        text(
            "SELECT l.node_id FROM file_leases l "
            "JOIN file_nodes n ON n.id = l.node_id "
            "JOIN file_drives d ON d.id = n.drive_id "
            "WHERE l.org_team_id = :org AND d.dedup_domain_id = :domain "
            "AND l.expires_at < now() AND l.released_at IS NULL "
            "AND EXISTS ("
            "  SELECT 1 FROM file_upload_sessions s "
            "  WHERE s.org_team_id = l.org_team_id AND s.lease_epoch = l.epoch "
            "  AND s.state = ANY(:live))"
        ),
        {"org": org, "domain": domain, "live": list(LIVE_SESSION_STATES)},
    )
    findings.extend(
        FsckFinding(code=EXPIRED_LEASE_LIVE_SESSIONS, kind="node", ref_id=str(row[0]))
        for row in rows
    )

    # A holder facet says "these bytes are on a machine, on their way". It is
    # true only while some lease on the file or above it is live, or gone for
    # less than the grace the reaper gives the machine to come back.
    rows = await repo.session.execute(
        text(
            "SELECT n.id FROM file_nodes n "  # noqa: S608 - interpolates builders' own text
            "JOIN file_drives d ON d.id = n.drive_id "
            "WHERE n.org_team_id = :org AND d.dedup_domain_id = :domain "
            "AND n.kind = 'file' AND n.trashed_at IS NULL AND n.holder_size IS NOT NULL "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM file_leases l JOIN file_nodes a ON a.id = l.node_id "
            "  WHERE l.org_team_id = :org "
            f"  AND {subtree_sql('n.path_ids', 'a.path_ids')} "
            "  AND ((l.released_at IS NULL AND l.reaped_at IS NULL AND l.expires_at > now()) "
            "    OR coalesce(l.released_at, l.reaped_at, l.expires_at) "
            "       > now() - make_interval(secs => :grace)))"
        ),
        {
            "org": org,
            "domain": domain,
            "grace": get_settings().files_unsynced_grace_seconds,
            **SUBTREE_DEPTH_BIND,
        },
    )
    findings.extend(
        FsckFinding(code=HOLDER_FACET_WITHOUT_LEASE, kind="node", ref_id=str(row[0]))
        for row in rows
    )

    # A replay record is scoped to the org, the principal and the route, never
    # to a domain — the request it answers for need not have touched a drive at
    # all. A pass over any domain therefore reports the org's expired records,
    # which is right: the janitor that drops them is org-wide too.
    rows = await repo.session.execute(
        text(
            "SELECT key, route, principal_id FROM file_idempotency_keys "
            "WHERE org_team_id = :org AND expires_at <= now()"
        ),
        {"org": org},
    )
    findings.extend(
        FsckFinding(
            code=IDEMPOTENCY_KEY_EXPIRED,
            kind="idempotency_key",
            ref_id=str(row[0]),
            detail={"key": str(row[0]), "route": str(row[1]), "principal_id": str(row[2])},
        )
        for row in rows
    )
    return findings


# -- the safe repairs ----------------------------------------------------


async def _repair(repo: FilesRepo, finding: FsckFinding) -> bool:
    """Fix one safe finding. Never a delete, never a row that names bytes."""
    async with repo.transaction():
        if finding.code == DIR_STATS_DRIFT:
            recomputed = finding.detail["recomputed"]
            result = await repo.session.execute(
                text(
                    "UPDATE file_dir_stats SET bytes = :bytes, files = :files, "
                    "direct_children = :kids, updated_at = now() "
                    "WHERE node_id = :node AND org_team_id = :org RETURNING node_id"
                ),
                {
                    "bytes": recomputed[0],
                    "files": recomputed[1],
                    "kids": recomputed[2],
                    "node": finding.ref_id,
                    "org": str(repo.scope.org_team_id),
                },
            )
            return len(result.all()) == 1
        if finding.code == NODE_FLAG_WITHOUT_OP:
            # Only ``acl_rewriting`` is safe to clear, and only together with
            # the body the flag invalidated: a reader falls through to the
            # chain while the flag stands and goes straight back to ``acl_id``
            # once it is gone, so clearing the flag alone would restore the
            # interned union of ancestors this node has left. Dropping both
            # leaves the chain authoritative until the rewrite re-interns. A
            # ``moving`` node is mid-tree-surgery and only its operation knows
            # which batches committed, so it stays flagged and reported.
            result = await repo.session.execute(
                text(
                    "UPDATE file_nodes SET state = 'live', acl_id = NULL "
                    "WHERE id = :node AND org_team_id = :org AND state = 'acl_rewriting' "
                    "RETURNING id"
                ),
                {"node": finding.ref_id, "org": str(repo.scope.org_team_id)},
            )
            return len(result.all()) == 1
        if finding.code == COMMITTING_STALE:
            # Back to ``uploading`` so the completion worker re-drives it. The
            # staged bytes are untouched: re-queueing is the whole repair.
            result = await repo.session.execute(
                text(
                    "UPDATE file_upload_sessions SET state = 'uploading' "
                    "WHERE id = :session AND org_team_id = :org AND state = 'committing' "
                    "RETURNING id"
                ),
                {"session": finding.ref_id, "org": str(repo.scope.org_team_id)},
            )
            return len(result.all()) == 1
    return False


async def _quarantine(repo: FilesRepo, finding: FsckFinding) -> None:
    """Put a report-only finding in front of an operator. Ids only."""
    async with repo.transaction():
        await repo.session.execute(
            text(
                "INSERT INTO file_quarantine "
                "(id, org_team_id, kind, ref_id, reason, attempts, detail) "
                "VALUES (gen_random_uuid(), :org, :kind, :ref, :reason, 0, "
                "CAST(:detail AS jsonb))"
            ),
            {
                "org": str(repo.scope.org_team_id),
                "kind": finding.kind,
                "ref": finding.ref_id,
                "reason": finding.code,
                "detail": json.dumps(finding.detail, sort_keys=True, default=str),
            },
        )


__all__ = [
    "BAD_HEAD_POINTER",
    "CHECKS",
    "COMMITTING_DEADLINE",
    "COMMITTING_STALE",
    "DANGLING_REFERENCE",
    "DELETED_PAST_WINDOW",
    "DIR_STATS_DRIFT",
    "DIR_STATS_SAMPLE",
    "EXPIRED_LEASE_LIVE_SESSIONS",
    "HEARTBEAT_DEADLINE",
    "HOLDER_FACET_WITHOUT_LEASE",
    "HOLD_SUM_MISMATCH",
    "IDEMPOTENCY_KEY_EXPIRED",
    "INCOMING_GRACE",
    "INCOMING_PAST_TTL",
    "INLINE_SIZE_MISMATCH",
    "NEVER_QUARANTINED",
    "NODE_FLAG_DEADLINE",
    "NODE_FLAG_WITHOUT_OP",
    "OBJECT_OUTSIDE_PREFIX",
    "OBJECT_SIZE_MISMATCH",
    "OP_WITHOUT_HEARTBEAT",
    "ORPHAN_OBJECT",
    "PURGE_GRACE",
    "REPAIRABLE",
    "TRASHED_PAST_PURGE",
    "FsckFinding",
    "FsckReport",
    "run_fsck",
]
