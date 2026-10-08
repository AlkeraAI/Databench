"""The nightly scrub: re-read stored bytes and prove they still hash right.

Every hop in the write path verifies (the driver at put, the server on the head
after put, the client before a rename into place), but none of them says
anything about the byte a disk flips six months later. The scrub is the only
check that reads bytes nobody asked for.

Three properties shape it:

It never repairs
    A mismatch means one of two things is wrong — the bytes or the row — and
    the scrub cannot tell which. Guessing risks overwriting the *good* side, so
    a finding goes into ``file_quarantine`` with its reason and an operator
    decides. ``fsck --repair-safe`` has the same rule for the same reason.

It is budgeted and resumable
    A domain holds more bytes than a night. ``budget_bytes`` caps what one run
    reads and the returned cursor is where the next run starts, so consecutive
    runs cover the domain without any one of them reading it whole.

Sampling is deterministic, not random
    Which objects a given ``sample_pct`` selects is a function of the store key,
    so a resumed run selects exactly the objects the killed run would have and
    coverage is a property of the cursor rather than of luck.

Above :data:`RANGE_SAMPLE_THRESHOLD` an object is range-sampled rather than
read whole: reading a 4 GiB object to prove one byte costs the whole night's
budget. The sampled ranges are checked against the per-block digests the write
path recorded, when it recorded them; when it did not, the large object is
still held to its declared size, which is what catches a truncation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from hmac import compare_digest
from typing import Any, Final

from sqlalchemy import text

from alkera_core.files.checkpoints import Checkpoints
from alkera_core.files.gc import Janitor
from alkera_core.files.hashing import BLOCK_BYTES, StreamHasher
from alkera_core.files.ids import DomainId, OrgScope
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.keys import DOMAIN_PREFIX

#: Above this an object is range-sampled instead of read whole.
RANGE_SAMPLE_THRESHOLD: Final = 64 * 1024 * 1024

#: How many versions one page of the walk pulls.
PAGE: Final = 500

#: Where a write path may record per-block SHA-256 digests (hex) so a large
#: object's sampled ranges have something to be compared against.
BLOCK_DIGEST_KEY: Final = "block_hashes"

#: The reasons a scrub can record. Each is a quarantine ``reason``, so an
#: operator triaging the queue reads the check that refused, never prose.
MISMATCH: Final = "scrub.content_hash_mismatch"
MISSING: Final = "scrub.object_missing"
SIZE_MISMATCH: Final = "scrub.size_mismatch"
BLOCK_MISMATCH: Final = "scrub.block_mismatch"


@dataclass(frozen=True, slots=True)
class ScrubFinding:
    """One version whose stored bytes did not match its row."""

    reason: str
    version_id: str
    store_key: str
    detail: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ScrubResult:
    """What one budgeted run covered, and where the next one starts."""

    domain_id: DomainId
    checked: int
    sampled: int
    bytes_read: int
    findings: tuple[ScrubFinding, ...]
    cursor: str | None
    exhausted: bool

    @property
    def mismatches(self) -> tuple[ScrubFinding, ...]:
        """The findings, under the name the runbook uses."""
        return self.findings


def selects(store_key: str, sample_pct: int) -> bool:
    """Whether ``sample_pct`` covers ``store_key``.

    A hash of the key rather than a random draw: the same percentage always
    picks the same objects, so a run killed mid-domain resumes into the set its
    first half was already walking instead of re-rolling the sample.
    """
    if sample_pct >= 100:
        return True
    if sample_pct <= 0:
        return False
    bucket = int.from_bytes(hashlib.blake2b(store_key.encode(), digest_size=2).digest(), "big")
    return bucket % 100 < sample_pct


async def run_scrub(
    janitor: Janitor,
    *,
    org: OrgScope,
    domain_id: DomainId,
    sample_pct: int = 100,
    budget_bytes: int | None = None,
    cursor: str | None = None,
) -> ScrubResult:
    """Re-hash a sample of ``domain_id``'s objects and report what disagrees.

    ``org`` and ``domain_id`` are passed rather than looked up for the same
    reason :meth:`Janitor.sweep` takes them: the domain-to-org map is platform
    data, and reading it here would need the unscoped session the janitor
    deliberately does not own.
    """
    repo = janitor._repo_for_org(org)
    checkpoints: Checkpoints = janitor._checkpoints
    findings: list[ScrubFinding] = []
    checked = 0
    sampled = 0
    spent = 0
    after = cursor
    exhausted = False

    while True:
        async with repo.transaction():
            rows = (
                await repo.session.execute(
                    text(
                        "SELECT v.id, v.store_key, v.size_bytes, v.content_hash, v.metadata "
                        "FROM file_versions v "
                        "JOIN file_nodes n ON n.id = v.node_id "
                        "JOIN file_drives d ON d.id = n.drive_id "
                        "WHERE v.org_team_id = :org AND d.dedup_domain_id = :domain "
                        "AND v.store_key IS NOT NULL "
                        "AND (CAST(:after AS text) IS NULL OR v.store_key > CAST(:after AS text)) "
                        "ORDER BY v.store_key LIMIT :limit"
                    ),
                    {
                        "org": str(repo.scope.org_team_id),
                        "domain": str(domain_id),
                        "after": after,
                        "limit": PAGE,
                    },
                )
            ).all()
        if not rows:
            exhausted = True
            break

        previous: str | None = after
        for version_id, store_key, size_bytes, content_hash, metadata in rows:
            if not selects(store_key, sample_pct):
                checked += 1
                after = store_key
                previous = store_key
                continue
            charge = min(int(size_bytes), RANGE_SAMPLE_THRESHOLD)
            if budget_bytes is not None and spent > 0 and spent + charge > budget_bytes:
                # The budget stops *before* the object it cannot afford, and the
                # cursor stays on the last object actually read, so the next run
                # starts on this one rather than skipping it. The first object of
                # a run is always read, or a budget below one object's size would
                # never advance and the domain would never be covered.
                return ScrubResult(
                    domain_id=domain_id,
                    checked=checked,
                    sampled=sampled,
                    bytes_read=spent,
                    findings=tuple(findings),
                    cursor=previous,
                    exhausted=False,
                )
            checked += 1
            sampled += 1
            await checkpoints.reach("scrub.before_read")
            found, read = await _verify(
                janitor,
                domain_id=domain_id,
                version_id=str(version_id),
                store_key=store_key,
                size_bytes=int(size_bytes),
                content_hash=content_hash,
                metadata=dict(metadata or {}),
            )
            spent += read
            await checkpoints.reach("scrub.after_read")
            after = store_key
            previous = store_key
            if found is not None:
                findings.append(found)
                await quarantine_version(repo, found)

    return ScrubResult(
        domain_id=domain_id,
        checked=checked,
        sampled=sampled,
        bytes_read=spent,
        findings=tuple(findings),
        cursor=after,
        exhausted=exhausted,
    )


async def _verify(
    janitor: Janitor,
    *,
    domain_id: DomainId,
    version_id: str,
    store_key: str,
    size_bytes: int,
    content_hash: str,
    metadata: dict[str, Any],
) -> tuple[ScrubFinding | None, int]:
    """Read one object and decide whether it still is what the row says."""
    absolute = f"{DOMAIN_PREFIX}{domain_id}/{store_key}"
    info = await janitor._store.head(absolute)
    if info is None:
        return (
            ScrubFinding(
                reason=MISSING,
                version_id=version_id,
                store_key=store_key,
                detail={"expected_size": size_bytes},
            ),
            0,
        )
    if info.size != size_bytes:
        return (
            ScrubFinding(
                reason=SIZE_MISMATCH,
                version_id=version_id,
                store_key=store_key,
                detail={"expected_size": size_bytes, "stored_size": info.size},
            ),
            0,
        )
    if info.size > RANGE_SAMPLE_THRESHOLD:
        return await _verify_ranges(
            janitor,
            absolute=absolute,
            version_id=version_id,
            store_key=store_key,
            size=info.size,
            metadata=metadata,
        )

    hasher = StreamHasher()
    async for chunk in await janitor._store.get(absolute):
        hasher.update(chunk)
    digests = hasher.finalize()
    if digests.content_hash.hex() != content_hash:
        return (
            ScrubFinding(
                reason=MISMATCH,
                version_id=version_id,
                store_key=store_key,
                detail={"expected_hash": content_hash, "stored_hash": digests.content_hash.hex()},
            ),
            digests.size,
        )
    return None, digests.size


async def _verify_ranges(
    janitor: Janitor,
    *,
    absolute: str,
    version_id: str,
    store_key: str,
    size: int,
    metadata: dict[str, Any],
) -> tuple[ScrubFinding | None, int]:
    """Check three 4 MiB blocks of a large object against their digests.

    The size already agreed or we would not be here, so this is the check for a
    flipped byte rather than for a truncation. Without recorded per-block
    digests there is nothing honest to compare a partial read against, so the
    object stands on its size alone rather than on a hash the scrub invents.
    """
    recorded = metadata.get(BLOCK_DIGEST_KEY)
    if not isinstance(recorded, list):
        return None, 0
    blocks = (size + BLOCK_BYTES - 1) // BLOCK_BYTES
    indexes = sorted({0, blocks // 2, max(blocks - 1, 0)})
    read = 0
    for index in indexes:
        if index >= len(recorded) or not isinstance(recorded[index], str):
            continue
        lo = index * BLOCK_BYTES
        hi = min(lo + BLOCK_BYTES, size) - 1
        chunks: list[bytes] = []
        async for chunk in await janitor._store.get(absolute, range=(lo, hi)):
            chunks.append(chunk)
        payload = b"".join(chunks)
        read += len(payload)
        if not compare_digest(hashlib.sha256(payload).digest(), bytes.fromhex(recorded[index])):
            return (
                ScrubFinding(
                    reason=BLOCK_MISMATCH,
                    version_id=version_id,
                    store_key=store_key,
                    detail={"block": index},
                ),
                read,
            )
    return None, read


async def quarantine_version(repo: FilesRepo, finding: ScrubFinding) -> None:
    """Record a finding where an operator triages it. Ids only, never a name."""
    async with repo.transaction():
        await repo.session.execute(
            text(
                "INSERT INTO file_quarantine "
                "(id, org_team_id, kind, ref_id, reason, attempts, detail) "
                "VALUES (gen_random_uuid(), :org, 'version', :ref, :reason, 0, "
                "CAST(:detail AS jsonb))"
            ),
            {
                "org": str(repo.scope.org_team_id),
                "ref": finding.version_id,
                "reason": finding.reason,
                "detail": json.dumps(finding.detail, sort_keys=True),
            },
        )


__all__ = [
    "BLOCK_DIGEST_KEY",
    "BLOCK_MISMATCH",
    "MISMATCH",
    "MISSING",
    "PAGE",
    "RANGE_SAMPLE_THRESHOLD",
    "SIZE_MISMATCH",
    "ScrubFinding",
    "ScrubResult",
    "quarantine_version",
    "run_scrub",
    "selects",
]
