"""The sweeper that settles a dedup domain's outstanding ownership marker.

Making a domain stamps its prefix with ``meta/owner.json`` on the request that
made it, under a deadline short enough that an unreachable store costs the
request nothing much — which means the statement is sometimes left owed. Owed
is safe (the collector will not touch a prefix it cannot see its own name on)
and expensive (nothing under that prefix is ever collected), so something has
to come back for it.

What is proven here is the sweeper's own decisions: which domains it asks the
store about, in what order, how many, and when it records the answer. The store
itself is the injected boundary — the worker's own implementation, against real
bytes, is pinned in ``apps/worker/tests/test_files_owner_marker_repair.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files.repo import FilesRepo
from alkera_core.files.sweepers import (
    JANITOR_ORDER,
    MarkerVerdict,
    OwnerMarkers,
    SweepDeps,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import FilesOrg

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


class RecordingStore:
    """The store, as the sweeper sees it: asked about a domain, it answers
    whose that prefix is.

    It records what it was asked so a case can pin WHICH domains the sweeper
    brought to it — the half of the contract the database cannot show.
    """

    def __init__(
        self,
        *,
        answer: MarkerVerdict = "ours",
        verdicts: dict[uuid.UUID, MarkerVerdict] | None = None,
    ) -> None:
        self.answer = answer
        self.verdicts = verdicts or {}
        self.asked: list[tuple[uuid.UUID, datetime]] = []

    async def __call__(self, domain_id: uuid.UUID, created_at: datetime) -> MarkerVerdict:
        self.asked.append((domain_id, created_at))
        return self.verdicts.get(domain_id, self.answer)


async def _owed(session: AsyncSession, org_team_id: uuid.UUID) -> set[uuid.UUID]:
    """The org's domains the question is still open for — neither ours nor
    known to be somebody else's, which is exactly the sweeper's own predicate."""
    rows = (
        await session.execute(
            text(
                "SELECT id FROM dedup_domains WHERE org_team_id = :org "
                "AND owner_marked_at IS NULL AND owner_marker_conflict_at IS NULL"
            ),
            {"org": org_team_id},
        )
    ).scalars()
    return {uuid.UUID(str(row)) for row in rows}


async def _conflict_at(session: AsyncSession, domain_id: uuid.UUID) -> datetime | None:
    return (
        await session.execute(
            text("SELECT owner_marker_conflict_at FROM dedup_domains WHERE id = :id"),
            {"id": domain_id},
        )
    ).scalar_one()


async def _marked_at(session: AsyncSession, domain_id: uuid.UUID) -> datetime | None:
    return (
        await session.execute(
            text("SELECT owner_marked_at FROM dedup_domains WHERE id = :id"),
            {"id": domain_id},
        )
    ).scalar_one()


async def _make_domains(session: AsyncSession, org: FilesOrg, count: int) -> list[uuid.UUID]:
    """``count`` domains for the org, oldest first, each one's marker owed."""
    store_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO file_stores (id, driver, bucket, endpoint, region, "
            "capabilities, transfer_modes) "
            "VALUES (:id, 'filesystem', '', :endpoint, '', '{}'::jsonb, ARRAY['single'])"
        ),
        {"id": store_id, "endpoint": f"/tmp/files-test/{store_id.hex}"},
    )
    made: list[uuid.UUID] = []
    for index in range(count):
        domain_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO dedup_domains "
                "(id, org_team_id, region, store_id, chunker_seed, hmac_key_id, created_at) "
                "VALUES (:id, :org, :region, :store, ''::bytea, '', :created)"
            ),
            {
                "id": domain_id,
                "org": org.org_team_id,
                "region": f"r{index}",
                "store": store_id,
                "created": NOW - timedelta(days=count - index),
            },
        )
        made.append(domain_id)
    await session.flush()
    return made


def _deps(session: AsyncSession, org: FilesOrg, **kwargs: Any) -> SweepDeps:
    return SweepDeps(repo=FilesRepo(session, org.scope), ctx=_ctx(org), **kwargs)


async def test_the_sweeper_records_every_domain_the_store_confirmed(
    files_session: AsyncSession, files_org: FilesOrg
) -> None:
    """The whole point: a marker the creating request could not write is
    written later, and the row then says so."""
    owed = await _make_domains(files_session, files_org, 3)
    store = RecordingStore()

    outcome = await OwnerMarkers(_deps(files_session, files_org, stamp_domain=store)).run(NOW)

    assert outcome.swept == 3
    assert {domain for domain, _ in store.asked} == set(owed)
    assert await _owed(files_session, files_org.org_team_id) == set()
    assert await _marked_at(files_session, owed[0]) == NOW


async def test_a_domain_already_marked_is_never_asked_about_again(
    files_session: AsyncSession, files_org: FilesOrg
) -> None:
    """The column is what stops the pass costing one store read per domain per
    five minutes for the life of the deployment. A settled domain must not
    reach the store at all."""
    owed = await _make_domains(files_session, files_org, 2)
    settled, still_owed = owed
    await files_session.execute(
        text("UPDATE dedup_domains SET owner_marked_at = :at WHERE id = :id"),
        {"at": NOW - timedelta(days=30), "id": settled},
    )
    store = RecordingStore()

    outcome = await OwnerMarkers(_deps(files_session, files_org, stamp_domain=store)).run(NOW)

    assert [domain for domain, _ in store.asked] == [still_owed]
    assert outcome.scanned == 1
    assert await _marked_at(files_session, settled) == NOW - timedelta(days=30)


async def test_a_store_that_would_not_answer_leaves_its_domain_owed(
    files_session: AsyncSession, files_org: FilesOrg
) -> None:
    """A store that is still refusing has said nothing. Recording that as
    either answer would be a lie the collector acts on, so the only thing the
    pass may do is come back."""
    owed = await _make_domains(files_session, files_org, 2)
    silent, ours = owed
    store = RecordingStore(verdicts={silent: "unknown"})

    outcome = await OwnerMarkers(_deps(files_session, files_org, stamp_domain=store)).run(NOW)

    assert outcome.swept == 1
    assert outcome.scanned == 2
    assert await _owed(files_session, files_org.org_team_id) == {silent}
    assert await _marked_at(files_session, ours) == NOW
    assert await _conflict_at(files_session, silent) is None


async def test_a_prefix_another_deployment_owns_is_recorded_and_never_asked_about_again(
    files_session: AsyncSession, files_org: FilesOrg
) -> None:
    """The state the first column could not hold. A marker is never
    overwritten, so a prefix somebody else stamped will not become ours however
    many times we ask — and a deployment sharing a bucket has a whole database
    of them. Left owed, every one is a store read every five minutes forever."""
    owed = await _make_domains(files_session, files_org, 2)
    theirs, ours = owed
    store = RecordingStore(verdicts={theirs: "foreign"})

    first = await OwnerMarkers(_deps(files_session, files_org, stamp_domain=store)).run(NOW)

    assert first.swept == 2
    assert await _conflict_at(files_session, theirs) == NOW
    assert await _marked_at(files_session, theirs) is None
    assert await _marked_at(files_session, ours) == NOW
    later = RecordingStore()
    again = await OwnerMarkers(_deps(files_session, files_org, stamp_domain=later)).run(NOW)
    assert later.asked == []
    assert again.scanned == 0


async def test_the_pass_is_budgeted_and_says_it_has_more_to_do(
    files_session: AsyncSession, files_org: FilesOrg
) -> None:
    """Like every sweeper: a deployment that has been writing into a dead store
    for a week has a backlog, and one pass must not try to clear all of it."""
    owed = await _make_domains(files_session, files_org, 3)
    store = RecordingStore()

    first = await OwnerMarkers(_deps(files_session, files_org, stamp_domain=store)).run(
        NOW, budget=2
    )

    assert [domain for domain, _ in store.asked] == owed[:2]
    assert first.cursor == "more"
    second = await OwnerMarkers(_deps(files_session, files_org, stamp_domain=store)).run(
        NOW, budget=2
    )
    assert second.cursor is None
    assert await _owed(files_session, files_org.org_team_id) == set()


async def test_a_page_the_store_cannot_answer_for_is_not_the_only_page_ever_visited(
    files_session: AsyncSession, files_org: FilesOrg
) -> None:
    """The head-of-line case, and the reason the resume position is a column.

    A domain the store would not answer for STAYS owed — that is correct — so a
    pass that always starts at the oldest of them re-reads the same page every
    tick and never reaches the rows behind it. Recording the attempt is what
    sends that page to the back. Nothing is handed between the two passes here:
    a fresh sweeper, as the next tick builds, resumes purely off the rows.
    """
    owed = await _make_domains(files_session, files_org, 4)
    store = RecordingStore(verdicts=dict.fromkeys(owed[:2], "unknown"))

    first = await OwnerMarkers(_deps(files_session, files_org, stamp_domain=store)).run(
        NOW, budget=2
    )
    assert first.swept == 0
    assert [domain for domain, _ in store.asked] == owed[:2]

    later = RecordingStore()
    second = await OwnerMarkers(_deps(files_session, files_org, stamp_domain=later)).run(
        NOW + timedelta(minutes=5), budget=2
    )

    assert [domain for domain, _ in later.asked] == owed[2:]
    assert second.swept == 2
    assert await _owed(files_session, files_org.org_team_id) == set(owed[:2])


async def test_a_domain_nobody_has_asked_about_goes_ahead_of_one_already_tried(
    files_session: AsyncSession, files_org: FilesOrg
) -> None:
    """The asymmetric half of the ordering. A domain made TODAY, which no pass
    has seen, must be settled before this deployment tries again on one it
    could not settle last tick — the new one is the one a person is waiting on,
    and an unreachable store is what the other one is waiting for."""
    tried, fresh = await _make_domains(files_session, files_org, 2)
    await files_session.execute(
        text("UPDATE dedup_domains SET owner_marker_attempted_at = :at WHERE id = :id"),
        {"at": NOW - timedelta(minutes=5), "id": tried},
    )
    store = RecordingStore()

    await OwnerMarkers(_deps(files_session, files_org, stamp_domain=store)).run(NOW, budget=1)

    assert [domain for domain, _ in store.asked] == [fresh]


async def test_the_first_pass_after_the_column_lands_settles_a_whole_deployment(
    files_session: AsyncSession, files_org: FilesOrg
) -> None:
    """The deploy moment. Every domain that predates the column reads as owed,
    so the first ticks ask the store about all of them — and for a deployment
    whose prefixes are already marked, every one of those answers ``ours`` from
    a READ. One pass per org settles them, bounded by the budget, asking the
    store exactly once per domain and never twice."""
    owed = await _make_domains(files_session, files_org, 5)
    store = RecordingStore(answer="ours")

    first = await OwnerMarkers(_deps(files_session, files_org, stamp_domain=store)).run(
        NOW, budget=3
    )
    second = await OwnerMarkers(_deps(files_session, files_org, stamp_domain=store)).run(
        NOW, budget=3
    )

    assert len(store.asked) == len(owed)
    assert [domain for domain, _ in store.asked] == owed
    assert first.scanned == 3 and second.scanned == 2
    assert await _owed(files_session, files_org.org_team_id) == set()
    settled = RecordingStore()
    await OwnerMarkers(_deps(files_session, files_org, stamp_domain=settled)).run(NOW)
    assert settled.asked == []


async def test_the_marker_is_dated_as_the_domain_was_made(
    files_session: AsyncSession, files_org: FilesOrg
) -> None:
    """The marker's own time is what a restore guard compares against this
    database's newest domain row. A marker written a week late but dated today
    would read as newer than the bytes it covers, so the sweeper hands the
    store the ROW's creation, not the clock."""
    await _make_domains(files_session, files_org, 1)
    store = RecordingStore()

    await OwnerMarkers(_deps(files_session, files_org, stamp_domain=store)).run(NOW)

    (_, created_at) = store.asked[0]
    assert created_at == NOW - timedelta(days=1)


async def test_an_unwired_deployment_says_so_instead_of_reporting_a_clean_pass(
    files_session: AsyncSession, files_org: FilesOrg
) -> None:
    """A deployment that serves no store has no way to write a marker. Reporting
    zero swept would read as "nothing owed", which is the opposite of true."""
    await _make_domains(files_session, files_org, 1)

    outcome = await OwnerMarkers(_deps(files_session, files_org)).run(NOW)

    assert outcome.skipped is True
    assert outcome.reason == "no store to write the marker with"
    assert len(await _owed(files_session, files_org.org_team_id)) == 1


async def test_the_marker_pass_is_part_of_every_janitor_tick() -> None:
    """Where it has to be to settle anything at all. The position within the
    order is not pinned: no sweeper reads the marker in-process — the collector
    does, from another job — so ordering it is efficiency, not correctness, and
    a pin on the index would fail any harmless reshuffle."""
    assert "owner_markers" in [sweeper.name for sweeper in JANITOR_ORDER]
