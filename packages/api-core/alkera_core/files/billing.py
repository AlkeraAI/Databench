"""What an org is charged for, and what it actually costs us.

Two numbers, deliberately computed from different rows and never conflated:

``billable_bytes`` is **logical**: the sum, over the content hashes distinct
*within the org*, of each one's size — head versions, retained versions, trashed
nodes and conflicted copies alike, inline bytes included. It is a function of the
org's own rows and nothing else. Two members who upload the same dataset pay for
it once and the admin can see why; two *orgs* that upload the same dataset each
pay in full. That is not a missed optimization, it is the only shape that is
safe: a bill has to be explainable from the customer's own listing and stable
under repacking and chunker changes, and a number that moved when a stranger
uploaded or deleted something would be a cross-tenant side channel — a byte
count is no more a capability than a hash is.

``physical_bytes`` is the cost of goods: distinct store objects behind a dedup
domain, after cross-org dedup. The gap between the two is the platform's margin,
and it is shown only in the platform view — never on a customer's bill, never in
their quota bar.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, cast

from sqlalchemy import Table, func, select

from alkera_core.files.errors import NotFound
from alkera_core.files.ids import DomainId, DriveId
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.versions import FileVersion

_VERSIONS: Final[Table] = cast(Table, FileVersion.__table__)


@dataclass(frozen=True, slots=True)
class BillableUsage:
    """The org's logical bytes behind one drive, with the terms that made it."""

    drive_id: DriveId
    #: What the customer is billed for and what the quota bar shows.
    logical_bytes: int
    #: How many distinct content hashes the org holds — the "why" behind the
    #: number when an admin asks why two copies cost one.
    distinct_contents: int


async def billable_bytes(repo: FilesRepo, drive_id: DriveId) -> BillableUsage:
    """The org's billable bytes, in one query over the org's own version rows.

    Dedup is by ``content_hash`` across the whole org rather than per drive, so
    a second drive holding the same bytes does not double a bill; the drive is
    named because that is what a quota bar is drawn for, and it is validated so
    a caller cannot bill an org for a drive it does not own.
    """
    drive = await repo.drive(drive_id)
    if drive is None:
        raise NotFound()
    # One statement, grouped by content hash: the org's rows collapse to one
    # row per distinct content and the sizes are added on this side. Two
    # queries would let a concurrent write land between them and bill a total
    # that existed at no instant.
    scoped = await repo.execute_scoped(
        select(_VERSIONS.c.content_hash, func.max(_VERSIONS.c.size_bytes)).group_by(
            _VERSIONS.c.content_hash
        )
    )
    rows = scoped.all()
    return BillableUsage(
        drive_id=drive_id,
        logical_bytes=sum(int(size) for _, size in rows),
        distinct_contents=len(rows),
    )


async def physical_bytes(repo: FilesRepo, domain_id: DomainId) -> int:
    """Distinct store objects behind ``domain_id`` — the platform view only.

    Cross-org dedup lives here and only here. This number must never reach a
    customer surface: it moves when another tenant uploads identical bytes, and
    a number that moves with a stranger's data is a side channel.
    """
    domain = await repo.domain(domain_id)
    if domain is None:
        raise NotFound()
    result = await repo.execute_scoped(
        select(_VERSIONS.c.store_key, func.max(_VERSIONS.c.size_bytes))
        .where(_VERSIONS.c.store_key.is_not(None))
        .group_by(_VERSIONS.c.store_key)
    )
    return sum(int(size) for _, size in result.all())


__all__ = ["BillableUsage", "billable_bytes", "physical_bytes"]
