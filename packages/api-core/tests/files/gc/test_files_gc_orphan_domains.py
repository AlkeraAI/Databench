"""The collector that sweeps the bytes of a tenant nobody can see any more.

The reachability sweep is driven per org from the drive rows, so it can only
ever visit domains the deployment still names. These cases pin the pass for the
ones it cannot: what it takes, what it refuses to take, and the two bounds that
keep a bucket-wide walk from becoming an unbounded one.

Every case runs twice — against a real filesystem driver rooted above the
domain prefixes (real listings, real mtimes, real renames) and against an
in-memory store whose write times the case sets directly. The first proves the
collector works a real driver correctly; the second proves nothing here depends
on a directory tree, and lets the age rule be driven to instants a file's mtime
cannot reach.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from _files_gc_factory import AdminOnlyFactory
from alkera_core.files.clock import FakeClock
from alkera_core.files.gc import (
    DELETED_WINDOW,
    ORPHAN_GRACE,
    REFUSED_KNOWN_EMPTY,
    REFUSED_KNOWN_UNREADABLE,
    REFUSED_MASS_COLLECT,
    REFUSED_NO_DEPLOYMENT,
    REFUSED_ROWS_OLDER_THAN_MARKER,
    DomainCollector,
    GcRefused,
    KnownRows,
)
from alkera_core.files.ownership import marker_body, marker_key
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.files.store.protocol import ListPage, ObjectInfo

NOW = datetime.now(UTC)
"""Anchored to the real clock on purpose: a real store stamps a moved object
with the wall clock it is running on (``os.utime`` with no times), so an
object parked by a sweep is dated now however far in the past the sweep was
told it was running. A frozen NOW would make every parked object read as
written in the future and no window would ever open.

Only the grace-side instants are derived from this, and each has an hour of
margin. The recovery window is driven from the park stamp the store itself
reports, because the gap between this module's import and a case's body is
unbounded and was once long enough to matter."""
OLD = NOW - ORPHAN_GRACE - timedelta(hours=1)
"""Old enough to collect: past the grace by an hour."""
YOUNG = NOW - timedelta(minutes=5)
"""An upload still landing: the bytes are there, the drive row is not."""

KNOWN = "11111111-1111-4111-8111-111111111111"
ORPHAN = "22222222-2222-4222-8222-222222222222"
ORPHAN_B = "33333333-3333-4333-8333-333333333333"
ANCHOR = "99999999-9999-4999-8999-999999999999"
"""A domain the database names and the bucket does not hold: it keeps the
known set non-empty, the way a deployment with any tenant at all has one,
without putting a domain in the walk."""
MINE = "deployment-mine"
THEIRS = "deployment-theirs"


# --- the two stores ---------------------------------------------------------


@dataclass
class _MemoryAdmin:
    """A bucket-wide store held in a dict, with a settable write time per key."""

    objects: dict[str, tuple[bytes, datetime]] = field(default_factory=dict)
    unknown_ages: set[str] = field(default_factory=set)
    moves: list[tuple[str, str]] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)

    def write(self, key: str, body: bytes, at: datetime) -> None:
        self.objects[key] = (body, at)

    async def list_prefix(
        self, prefix: str, *, after: str | None = None, limit: int = 1000
    ) -> ListPage:
        found = sorted(
            k for k in self.objects if k.startswith(prefix) and (after is None or k > after)
        )
        page = found[:limit]
        return ListPage(keys=page, next_after=page[-1] if len(found) > limit else None)

    async def get(self, key: str, *, range: tuple[int, int] | None = None) -> Any:
        body = self.objects[key][0]

        async def chunks() -> Any:
            yield body

        return chunks()

    async def head(self, key: str) -> ObjectInfo | None:
        row = self.objects.get(key)
        if row is None:
            return None
        return ObjectInfo(size=len(row[0]), checksum=None, etag=None, storage_class=None)

    async def move(self, src: str, dst: str) -> None:
        body, _ = self.objects.pop(src)
        # The destination is stamped with the instant the bytes became visible
        # under THIS key, exactly as the filesystem driver's ``os.utime`` does
        # — a fake that carried the source's mtime across would open the
        # recovery window before the object was ever parked.
        self.objects[dst] = (body, datetime.now(UTC))
        self.moves.append((src, dst))

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)
        self.deletes.append(key)

    async def written_at(self, key: str) -> datetime | None:
        if key in self.unknown_ages:
            return None
        row = self.objects.get(key)
        return None if row is None else row[1]


class _Bucket:
    """One store under test, with the two things a case needs to say."""

    def __init__(self, store: Any, root: Path | None) -> None:
        self.store = store
        self._root = root

    @property
    def factory(self) -> AdminOnlyFactory:
        return AdminOnlyFactory(self.store)

    def write(
        self, key: str, body: bytes = b"bytes", *, at: datetime, owner: str | None = MINE
    ) -> None:
        """Put an object at ``key`` as if it had been written at ``at``.

        The domain it lands under is stamped as ``owner``'s, the way the API
        stamps a domain it creates; ``owner=None`` leaves the prefix unmarked,
        which is what bytes older than the marker look like.
        """
        self._put(key, body, at)
        domain = key.split("/")[1] if key.startswith("domains/") else ""
        try:
            uuid.UUID(domain)
        except ValueError:
            return
        if owner is not None:
            self._put(marker_key(domain), marker_body(owner, written_at=at), at)

    def _put(self, key: str, body: bytes, at: datetime) -> None:
        if self._root is None:
            self.store.write(key, body, at)
            return
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        stamp = at.timestamp()
        import os

        os.utime(path, (stamp, stamp))

    async def keys(self) -> list[str]:
        """Every object in the bucket, the ownership markers aside."""
        return sorted(k for k in await self.all_keys() if not k.endswith("/meta/owner.json"))

    async def all_keys(self) -> list[str]:
        page = await self.store.list_prefix("domains/", after=None, limit=10_000)
        return sorted(page.keys)

    async def written_at(self, key: str) -> datetime | None:
        """When the store says the bytes under ``key`` became visible.

        The recovery window is measured against this and nothing else, so a
        case about the window has to ask the store rather than assume.
        """
        stamped: datetime | None = await self.store.written_at(key)
        return stamped


@pytest.fixture(params=["filesystem", "memory"])
def bucket(request: pytest.FixtureRequest, tmp_path: Path) -> _Bucket:
    if request.param == "memory":
        return _Bucket(_MemoryAdmin(), None)
    root = tmp_path / "bucket"
    root.mkdir()
    return _Bucket(FilesystemStore(root, clock=lambda: NOW, layout="bucket"), root)


def collector(bucket: _Bucket, known: set[str], **kwargs: Any) -> DomainCollector:
    """A collector for deployment ``MINE`` whose database names ``known``.

    ``ANCHOR`` rides along so the known set is never empty, and the breaker is
    opened wide: the cases that are about either one say so themselves.
    """
    return raw_collector(bucket, known | {ANCHOR}, **{"max_orphan_fraction": 1.0, **kwargs})


def raw_collector(
    bucket: _Bucket, known: set[str], *, newest_row_at: datetime | None = NOW, **kwargs: Any
) -> DomainCollector:
    """``newest_row_at`` defaults to NOW: a database at least as new as every
    marker a case writes (they are all at OLD or YOUNG), which is what the
    bucket's own database looks like. The restore cases move it back."""

    async def _known() -> KnownRows:
        return KnownRows(ids=frozenset(known), newest_at=newest_row_at)

    options = {"deployment_id": MINE, **kwargs}
    return DomainCollector(bucket.factory, FakeClock(NOW), known_domains=_known, **options)


# --- what makes it refuse the whole pass ------------------------------------


async def test_an_empty_known_set_over_a_bucket_that_holds_domains_is_refused(
    bucket: _Bucket,
) -> None:
    """The answer a database that cannot see its own rows gives is the empty
    set. Read literally it means "every domain is an orphan", so it is never
    read literally: the pass refuses and not one object moves."""
    bucket.write(f"domains/{ORPHAN}/objects/aa", at=OLD)
    bucket.write(f"domains/{ORPHAN_B}/objects/bb", at=OLD)
    before = await bucket.all_keys()

    report = await raw_collector(bucket, set(), max_orphan_fraction=1.0).sweep(NOW)

    assert report.verdict == REFUSED_KNOWN_EMPTY
    assert report.refused
    assert report.moved == () and report.erased == ()
    assert await bucket.all_keys() == before


async def test_an_empty_known_set_over_an_empty_bucket_is_simply_nothing_to_do(
    bucket: _Bucket,
) -> None:
    """A deployment nobody has used yet is not an error."""
    report = await raw_collector(bucket, set()).sweep(NOW)

    assert report.verdict == "ok"
    assert report.scanned == ()


async def test_a_known_domain_source_that_cannot_vouch_for_itself_refuses_the_pass(
    bucket: _Bucket,
) -> None:
    """The source raises rather than answer; the pass reports the code it was
    given and completes, so a retry policy never runs a refusal again."""
    bucket.write(f"domains/{ORPHAN}/objects/aa", at=OLD)

    async def _blind() -> KnownRows:
        raise GcRefused(REFUSED_KNOWN_UNREADABLE, "the login does not bypass row security")

    report = await DomainCollector(
        bucket.factory, FakeClock(NOW), known_domains=_blind, deployment_id=MINE
    ).sweep(NOW)

    assert report.verdict == REFUSED_KNOWN_UNREADABLE
    assert f"domains/{ORPHAN}/objects/aa" in await bucket.keys()


async def test_a_deployment_that_cannot_name_itself_collects_nothing(bucket: _Bucket) -> None:
    bucket.write(f"domains/{ORPHAN}/objects/aa", at=OLD)

    report = await collector(bucket, set(), deployment_id=None).sweep(NOW)

    assert report.verdict == REFUSED_NO_DEPLOYMENT
    assert f"domains/{ORPHAN}/objects/aa" in await bucket.keys()


# --- whose bytes they are ---------------------------------------------------


async def test_a_prefix_another_deployment_stamped_is_reported_and_never_touched(
    bucket: _Bucket,
) -> None:
    """Two deployments on one bucket: this database has never heard of the
    other's domain, and that is exactly why it must not collect it. The
    neighbour stamped by THIS deployment is the control — it is collected, so
    a pass that simply stopped collecting cannot pass."""
    bucket.write(f"domains/{ORPHAN}/objects/aa", at=OLD, owner=THEIRS)
    bucket.write(f"domains/{ORPHAN_B}/objects/bb", at=OLD, owner=MINE)

    report = await collector(bucket, set()).sweep(NOW)

    assert report.foreign == (ORPHAN,)
    assert report.orphaned == (ORPHAN_B,)
    assert report.moved == (f"domains/{ORPHAN_B}/objects/bb",)
    assert f"domains/{ORPHAN}/objects/aa" in await bucket.keys()


async def test_an_unstamped_prefix_is_reported_until_somebody_adopts_it(
    bucket: _Bucket,
) -> None:
    """Bytes older than the marker belong to nobody this pass can prove. The
    same prefix becomes collectable the moment it carries this deployment's
    stamp, which is the whole of what adopting one means."""
    bucket.write(f"domains/{ORPHAN}/objects/aa", at=OLD, owner=None)

    before = await collector(bucket, set()).sweep(NOW)
    assert before.unstamped == (ORPHAN,)
    assert before.orphaned == () and before.moved == ()
    assert f"domains/{ORPHAN}/objects/aa" in await bucket.keys()

    bucket.write(f"domains/{ORPHAN}/objects/aa", at=OLD, owner=MINE)
    after = await collector(bucket, set()).sweep(NOW)
    assert after.moved == (f"domains/{ORPHAN}/objects/aa",)


async def test_a_marker_that_is_not_a_marker_reads_as_unstamped(bucket: _Bucket) -> None:
    bucket.write(f"domains/{ORPHAN}/objects/aa", at=OLD, owner=None)
    bucket.write(marker_key(ORPHAN), b"not json", at=OLD, owner=None)

    report = await collector(bucket, set()).sweep(NOW)

    assert report.unstamped == (ORPHAN,)
    assert report.moved == ()


async def test_the_ownership_marker_is_never_parked_with_the_domains_bytes(
    bucket: _Bucket,
) -> None:
    """The marker is as old as the domain, so age alone would take it — and the
    next pass would then read the prefix as nobody's and never expire it."""
    bucket.write(f"domains/{ORPHAN}/objects/aa", at=OLD)

    await collector(bucket, set()).sweep(NOW)

    assert marker_key(ORPHAN) in await bucket.all_keys()


# --- a database older than its bucket ---------------------------------------


async def test_an_orphan_stamped_after_the_databases_newest_row_refuses_the_run(
    bucket: _Bucket,
) -> None:
    """A database restored from a snapshot carries the owner's id, so every
    domain the bucket gained since reads as its own orphan. That domain's
    marker is newer than any row the snapshot holds -- and that is the tell.
    The whole run is refused, and the earlier, legitimately collectable orphan
    on the same page is left alone too: a database that cannot be trusted
    about one domain cannot be trusted about the rest."""
    bucket.write(f"domains/{ORPHAN}/objects/aa", at=OLD)
    bucket.write(f"domains/{ORPHAN_B}/objects/bb", at=YOUNG - timedelta(days=2))
    before = await bucket.all_keys()

    # The snapshot's newest domain row predates ORPHAN_B's marker (YOUNG - 2d)
    # but not ORPHAN's (OLD, more than a day back).
    report = await raw_collector(
        bucket, {ANCHOR}, newest_row_at=YOUNG - timedelta(days=3), max_orphan_fraction=1.0
    ).sweep(NOW)

    assert report.verdict == REFUSED_ROWS_OLDER_THAN_MARKER
    assert report.moved == () and report.erased == ()
    assert await bucket.all_keys() == before


async def test_the_restore_guard_is_not_something_the_operator_can_override(
    bucket: _Bucket,
) -> None:
    bucket.write(f"domains/{ORPHAN}/objects/aa", at=OLD)

    report = await raw_collector(bucket, {ANCHOR}, newest_row_at=OLD - timedelta(days=1)).sweep(
        NOW, allow_mass_collect=True
    )

    assert report.verdict == REFUSED_ROWS_OLDER_THAN_MARKER
    assert f"domains/{ORPHAN}/objects/aa" in await bucket.keys()


@pytest.mark.parametrize(
    "newest_row_at",
    [
        pytest.param(None, id="a-database-with-no-domain-rows"),
        pytest.param(OLD - timedelta(seconds=1), id="rows-a-second-older-than-the-marker"),
    ],
)
async def test_a_database_that_cannot_prove_it_saw_the_domain_refuses(
    bucket: _Bucket, newest_row_at: datetime | None
) -> None:
    bucket.write(f"domains/{ORPHAN}/objects/aa", at=OLD)

    report = await raw_collector(bucket, {ANCHOR}, newest_row_at=newest_row_at).sweep(NOW)

    assert report.verdict == REFUSED_ROWS_OLDER_THAN_MARKER


async def test_a_marker_as_old_as_the_newest_row_is_this_databases_own(bucket: _Bucket) -> None:
    """The boundary from the other side: a marker written no later than the
    newest row is a domain this database made, and it is collected."""
    bucket.write(f"domains/{ORPHAN}/objects/aa", at=OLD)

    report = await collector(bucket, set(), newest_row_at=OLD).sweep(NOW)

    assert report.verdict == "ok"
    assert report.moved == (f"domains/{ORPHAN}/objects/aa",)


async def test_a_marker_with_no_readable_time_reads_as_newer_than_the_rows(
    bucket: _Bucket,
) -> None:
    bucket.write(f"domains/{ORPHAN}/objects/aa", at=OLD, owner=None)
    bucket.write(
        marker_key(ORPHAN), b'{"v": 1, "deployment_id": "deployment-mine"}', at=OLD, owner=None
    )

    report = await raw_collector(bucket, {ANCHOR}).sweep(NOW)

    assert report.verdict == REFUSED_ROWS_OLDER_THAN_MARKER


async def test_a_foreign_or_unstamped_domain_never_trips_the_restore_guard(
    bucket: _Bucket,
) -> None:
    """The guard reads only markers this deployment would act on: another
    deployment's newer marker is its business, and no marker says nothing."""
    bucket.write(f"domains/{ORPHAN}/objects/aa", at=OLD, owner=THEIRS)
    bucket.write(f"domains/{ORPHAN_B}/objects/bb", at=OLD, owner=None)

    report = await raw_collector(bucket, {ANCHOR}, newest_row_at=OLD - timedelta(days=9)).sweep(NOW)

    assert report.verdict == "ok"
    assert report.foreign == (ORPHAN,) and report.unstamped == (ORPHAN_B,)


# --- the breaker ------------------------------------------------------------


def _ten_domains() -> list[str]:
    return [f"{n:08d}-0000-4000-8000-000000000000" for n in range(1, 11)]


@pytest.mark.parametrize(
    ("orphans", "refused"),
    [
        pytest.param(1, False, id="one-in-ten-is-at-the-threshold"),
        pytest.param(2, True, id="two-in-ten-is-over-it"),
    ],
)
async def test_the_breaker_refuses_a_page_whose_orphan_share_is_over_the_threshold(
    bucket: _Bucket, orphans: int, refused: bool
) -> None:
    """A tenth of the deployment reading as gone is the threshold; the page is
    judged whole, before its first move, so a refusal leaves nothing half done."""
    domains = _ten_domains()
    for domain in domains:
        bucket.write(f"domains/{domain}/objects/aa", at=OLD)
    known = set(domains[orphans:])
    before = await bucket.all_keys()

    report = await raw_collector(bucket, known, max_orphan_fraction=0.1).sweep(NOW)

    assert report.refused is refused
    if refused:
        assert report.verdict == REFUSED_MASS_COLLECT
        assert report.orphaned == tuple(domains[:orphans])
        assert report.moved == ()
        assert await bucket.all_keys() == before
    else:
        assert report.moved == (f"domains/{domains[0]}/objects/aa",)


async def test_the_operator_can_let_one_run_past_the_breaker(bucket: _Bucket) -> None:
    domains = _ten_domains()
    for domain in domains:
        bucket.write(f"domains/{domain}/objects/aa", at=OLD)
    known = set(domains[5:])

    report = await raw_collector(bucket, known, max_orphan_fraction=0.1).sweep(
        NOW, allow_mass_collect=True
    )

    assert report.verdict == "ok"
    assert len(report.moved) == 5


async def test_the_override_does_not_open_any_other_refusal(bucket: _Bucket) -> None:
    """``allow_mass_collect`` answers the breaker and nothing else: an empty
    known set is still refused with it set."""
    bucket.write(f"domains/{ORPHAN}/objects/aa", at=OLD)

    report = await raw_collector(bucket, set()).sweep(NOW, allow_mass_collect=True)

    assert report.verdict == REFUSED_KNOWN_EMPTY
    assert f"domains/{ORPHAN}/objects/aa" in await bucket.keys()


@pytest.mark.parametrize("fraction", [-0.01, 1.01])
async def test_a_threshold_that_is_not_a_fraction_is_refused(
    bucket: _Bucket, fraction: float
) -> None:
    with pytest.raises(ValueError, match="max_orphan_fraction"):
        raw_collector(bucket, {ANCHOR}, max_orphan_fraction=fraction)


# --- what it refuses to take ------------------------------------------------


async def test_a_domain_a_drive_row_still_names_is_never_collected(bucket: _Bucket) -> None:
    """The reachability sweep owns a live tenant's objects — it knows their
    roots. This pass must not touch them even though they are ancient."""
    bucket.write(f"domains/{KNOWN}/objects/aa", at=OLD)
    bucket.write(f"domains/{KNOWN}/incoming/{uuid.uuid4()}/part-1", at=OLD)

    report = await collector(bucket, {KNOWN}).sweep(NOW)

    assert report.scanned == (KNOWN,)
    assert report.orphaned == ()
    assert report.moved == ()
    assert await bucket.keys() == sorted(
        k for k in await bucket.keys() if k.startswith(f"domains/{KNOWN}/")
    )
    assert f"domains/{KNOWN}/objects/aa" in await bucket.keys()


async def test_an_object_inside_the_grace_is_kept_even_in_an_orphan_domain(
    bucket: _Bucket,
) -> None:
    """Bytes land before the drive row that names their domain commits. The
    grace is the only thing standing between that upload and this sweep."""
    bucket.write(f"domains/{ORPHAN}/objects/young", at=YOUNG)

    report = await collector(bucket, set()).sweep(NOW)

    assert report.orphaned == (ORPHAN,)
    assert report.moved == ()
    assert report.kept_young == 1
    assert f"domains/{ORPHAN}/objects/young" in await bucket.keys()


async def test_an_object_whose_age_the_store_will_not_say_is_kept() -> None:
    """A store that cannot date an object protects it. The age rule may only
    ever keep bytes — an unknown age is never read as 'old enough'."""
    store = _MemoryAdmin()
    place = _Bucket(store, None)
    place.write(f"domains/{ORPHAN}/objects/dateless", b"x", at=OLD)
    store.unknown_ages.add(f"domains/{ORPHAN}/objects/dateless")

    report = await collector(place, set()).sweep(NOW)

    assert report.moved == ()
    assert report.kept_young == 1
    assert f"domains/{ORPHAN}/objects/dateless" in store.objects


async def test_a_key_under_domains_that_names_no_domain_is_stepped_over(
    bucket: _Bucket,
) -> None:
    """A stray key is not a tenant. The walk steps past it rather than guessing
    at it — and keeps going, so one piece of litter cannot hide the orphans
    behind it in key order."""
    bucket.write("domains/not-a-uuid/stray", at=OLD)
    bucket.write(f"domains/{ORPHAN}/objects/aa", at=OLD)

    report = await collector(bucket, set()).sweep(NOW)

    assert report.scanned == (ORPHAN,)
    assert "domains/not-a-uuid/stray" in await bucket.keys()
    assert report.moved == (f"domains/{ORPHAN}/objects/aa",)


# --- what it takes ----------------------------------------------------------


async def test_an_orphan_domains_aged_objects_are_parked_for_their_week(
    bucket: _Bucket,
) -> None:
    """Phase one: the bytes move under ``deleted/`` and are still there. A
    mistake is a move back, not a restore from backup."""
    bucket.write(f"domains/{ORPHAN}/objects/aa", b"0123456789", at=OLD)
    bucket.write(f"domains/{ORPHAN}/incoming/{ORPHAN_B}/part-1", b"abc", at=OLD)

    report = await collector(bucket, set()).sweep(NOW)

    assert report.orphaned == (ORPHAN,)
    assert set(report.moved) == {
        f"domains/{ORPHAN}/objects/aa",
        f"domains/{ORPHAN}/incoming/{ORPHAN_B}/part-1",
    }
    assert report.bytes_moved == 13
    assert report.erased == ()
    assert set(await bucket.keys()) == {
        f"domains/{ORPHAN}/deleted/objects/aa",
        f"domains/{ORPHAN}/deleted/incoming/{ORPHAN_B}/part-1",
    }


async def test_a_parked_object_is_erased_only_once_the_window_has_passed(
    bucket: _Bucket,
) -> None:
    """Phase two, and the proof that the week is real, pinned on both sides of
    the boundary: a second before the window is up the object is still there,
    a second after it is gone.

    The window is measured from the instant the object was PARKED, and a real
    store stamps that itself when it publishes the destination key — so the
    case reads the stamp back and drives the clock relative to it. Deriving
    the instants from this module's own ``NOW`` instead made the outcome
    depend on how long after the import the body happened to run: a run that
    started more than the margin later parked the object past its own cutoff
    and erased nothing.
    """
    bucket.write(f"domains/{ORPHAN}/objects/aa", at=OLD)
    await collector(bucket, set()).sweep(NOW)
    parked = f"domains/{ORPHAN}/deleted/objects/aa"
    parked_at = await bucket.written_at(parked)
    assert parked_at is not None, "the store has to date the object it just parked"

    a_second_early = await collector(bucket, set()).sweep(
        parked_at + DELETED_WINDOW - timedelta(seconds=1)
    )
    assert a_second_early.erased == ()
    assert await bucket.keys() == [parked]

    a_second_late = await collector(bucket, set()).sweep(
        parked_at + DELETED_WINDOW + timedelta(seconds=1)
    )
    assert a_second_late.erased == (parked,)
    assert await bucket.keys() == []


async def test_a_second_pass_has_nothing_left_to_park(bucket: _Bucket) -> None:
    """Idempotent by construction: a parked object is no longer under the
    prefix the walk reads, so the pass is safe to re-run at any cadence."""
    bucket.write(f"domains/{ORPHAN}/objects/aa", at=OLD)
    first = await collector(bucket, set()).sweep(NOW)

    second = await collector(bucket, set()).sweep(NOW + timedelta(minutes=1))

    assert len(first.moved) == 1
    assert second.moved == ()
    assert second.orphaned == (ORPHAN,)


async def test_a_domain_that_lost_its_drive_row_becomes_collectable(
    bucket: _Bucket,
) -> None:
    """The whole point, stated as a transition: the same bytes are protected
    while a row names the domain and collected once none does."""
    bucket.write(f"domains/{ORPHAN}/objects/aa", at=OLD)

    protected = await collector(bucket, {ORPHAN}).sweep(NOW)
    collected = await collector(bucket, set()).sweep(NOW)

    assert protected.moved == ()
    assert collected.moved == (f"domains/{ORPHAN}/objects/aa",)


# --- the bounds -------------------------------------------------------------


async def test_the_domain_budget_bounds_a_pass_and_its_cursor_resumes_it(
    bucket: _Bucket,
) -> None:
    """A bucket-wide walk under a daily schedule must never be one unbounded
    activity. A page covers its budget, names where it stopped, and the next
    page starts AFTER that domain — never re-walking it, never skipping one."""
    bucket.write(f"domains/{ORPHAN}/objects/aa", at=OLD)
    bucket.write(f"domains/{ORPHAN_B}/objects/bb", at=OLD)

    first = await collector(bucket, set()).sweep(NOW, domain_budget=1)
    assert first.scanned == (ORPHAN,)
    assert first.cursor == ORPHAN
    assert first.moved == (f"domains/{ORPHAN}/objects/aa",)

    second = await collector(bucket, set()).sweep(NOW, domain_budget=1, after=first.cursor)
    assert second.scanned == (ORPHAN_B,)
    assert second.moved == (f"domains/{ORPHAN_B}/objects/bb",)

    third = await collector(bucket, set()).sweep(NOW, domain_budget=1, after=second.cursor)
    assert third.scanned == ()
    assert third.cursor is None


async def test_the_object_budget_bounds_one_domains_pass(bucket: _Bucket) -> None:
    """One enormous orphan must not hold the activity open for the whole
    bucket. The rest are simply still there for the next pass."""
    for i in range(5):
        bucket.write(f"domains/{ORPHAN}/objects/{i:02d}", at=OLD)

    first = await collector(bucket, set()).sweep(NOW, object_budget=2)
    assert len(first.moved) == 2

    second = await collector(bucket, set()).sweep(NOW, object_budget=2)
    third = await collector(bucket, set()).sweep(NOW, object_budget=2)
    assert len(second.moved) == 2
    assert len(third.moved) == 1
    assert sorted(await bucket.keys()) == sorted(
        f"domains/{ORPHAN}/deleted/objects/{i:02d}" for i in range(5)
    )


@pytest.mark.parametrize("budget", [0, -1])
async def test_a_domain_budget_below_one_is_refused(bucket: _Bucket, budget: int) -> None:
    """A budget of zero would report 'the bucket is done' having visited
    nothing, and the cursor would never advance."""
    with pytest.raises(ValueError, match="domain_budget"):
        await collector(bucket, set()).sweep(NOW, domain_budget=budget)


# --- the dry run ------------------------------------------------------------


async def test_a_dry_run_reports_what_it_would_take_and_takes_nothing(
    bucket: _Bucket,
) -> None:
    """The operator's first command against a bucket they are unsure about."""
    bucket.write(f"domains/{ORPHAN}/objects/aa", b"0123456789", at=OLD)
    bucket.write(f"domains/{ORPHAN}/objects/young", at=YOUNG)

    report = await collector(bucket, set()).sweep(NOW, dry_run=True)

    assert report.moved == (f"domains/{ORPHAN}/objects/aa",)
    assert report.bytes_moved == 10
    assert report.kept_young == 1
    assert set(await bucket.keys()) == {
        f"domains/{ORPHAN}/objects/aa",
        f"domains/{ORPHAN}/objects/young",
    }


async def test_a_custom_grace_moves_the_line_the_objects_are_judged_against(
    bucket: _Bucket,
) -> None:
    """The grace is a dial, not a constant baked into the walk: the same object
    is kept under the default and collected under a shorter one."""
    bucket.write(f"domains/{ORPHAN}/objects/aa", at=YOUNG)

    kept = await collector(bucket, set()).sweep(NOW, dry_run=True)
    taken = await collector(bucket, set(), grace=timedelta(minutes=1)).sweep(NOW, dry_run=True)

    assert kept.moved == ()
    assert taken.moved == (f"domains/{ORPHAN}/objects/aa",)
