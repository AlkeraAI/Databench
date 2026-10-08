"""The unsaved sweep's backlog figure: the sessions still holding edits not on
the drive well after their last edit, which the backlog alarm reads.

Against real Postgres and real sandbox workers: a session counts once its last
edit is :data:`~backend.services.crdt.sweeper.BACKLOG_AGE_SECONDS` old (not a
second sooner), a parked one counts as parked, a saved one never counts, the
count stays inside the orgs it is asked about, and a sweep pass says the
figure (and says nothing while there is none).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import CrdtDoc
from backend.services.crdt import sweeper
from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin
from tests.crdt.crdt_world import crdt_docs
from tests.crdt.file_world import FileWorld, file_world
from tests.crdt.test_crdt_text_peers import _Docs, _open, _tab, _type

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

AGE = sweeper.BACKLOG_AGE_SECONDS


async def _edited_at(db: AsyncSession, fw: FileWorld) -> datetime:
    row = (
        await db.execute(
            select(CrdtDoc).where(CrdtDoc.doc_type == "file", CrdtDoc.doc_id == fw.ref.doc_id)
        )
    ).scalar_one()
    await db.refresh(row)
    await db.commit()
    return row.updated_at


async def _typed(real_session: AsyncSession, org_admin: OrgWithAdmin, name: str) -> FileWorld:
    """A file whose session holds typing the drive does not have."""
    fw = await file_world(real_session, org_admin, name=name)
    async with _Docs() as docs:
        tab = _tab(8301)
        await _open(docs, real_session, fw, tab)
        await _type(docs, real_session, fw, tab, 0, "# typed\n")
    return fw


@pytest.mark.parametrize(
    ("after", "counted"),
    [
        pytest.param(AGE - 1, 0, id="a-second-short-of-the-age"),
        pytest.param(AGE, 1, id="at-the-age"),
        pytest.param(AGE + 3600, 1, id="an-hour-past-it"),
    ],
)
async def test_a_session_is_backlog_once_its_last_edit_is_old_enough(
    real_session: AsyncSession, org_admin: OrgWithAdmin, after: float, counted: int
) -> None:
    fw = await _typed(real_session, org_admin, "aged.py")
    edited = await _edited_at(real_session, fw)
    found = await sweeper.report_backlog(
        AsyncSessionLocal,
        now=edited + timedelta(seconds=after),
        orgs=frozenset({org_admin.org_id}),
    )
    assert (found.count, found.parked) == (counted, 0)
    if counted:
        assert found.oldest_seconds == pytest.approx(after, abs=1)


async def test_parked_sessions_count_as_parked_and_saved_ones_never_count(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    typed = await _typed(real_session, org_admin, "typed.py")
    parked = await _typed(real_session, org_admin, "parked.py")
    saved = await file_world(real_session, org_admin, name="saved.py")
    async with _Docs() as docs:
        await _open(docs, real_session, saved, _tab(8302))
        await sweeper.park(
            docs.session_factory, parked.ref, reason="no_writer", now=datetime.now(UTC)
        )
    later = max([await _edited_at(real_session, fw) for fw in (typed, parked, saved)]) + timedelta(
        seconds=AGE
    )
    mine = await sweeper.report_backlog(
        AsyncSessionLocal, now=later, orgs=frozenset({org_admin.org_id})
    )
    assert (mine.count, mine.parked) == (2, 1)
    async with AsyncSessionLocal() as db:
        assert await sweeper.count_unsaved(db, now=later, orgs=frozenset({org_admin.org_id})) == 2
        # Another org's figure does not include this org's sessions.
        assert await sweeper.count_unsaved(db, now=later, orgs=frozenset({org_admin.admin_id})) == 0


async def test_a_sweep_pass_says_the_backlog_and_nothing_when_there_is_none(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A parked session (not due a try, so the sweep claims nothing) whose
    last edit is past the age: the pass logs the figure the alarm reads. The
    same pass before the age logs nothing."""
    fw = await _typed(real_session, org_admin, "swept.py")
    edited = await _edited_at(real_session, fw)
    async with crdt_docs() as docs:
        docs.sweep_orgs = frozenset({org_admin.org_id})
        # Due again only long after both passes, so neither claims it.
        await sweeper.park(
            docs.session_factory, fw.ref, reason="no_writer", now=edited + timedelta(hours=2)
        )
        with capture_logs() as early:
            with freeze_time(edited + timedelta(seconds=AGE - 1), real_asyncio=True):
                await docs.sweep_unsaved()
        with capture_logs() as late:
            with freeze_time(edited + timedelta(seconds=AGE + 1), real_asyncio=True):
                await docs.sweep_unsaved()
    assert [e for e in early if e["event"] == "crdt.sessions.unsaved_backlog"] == []
    (said,) = [e for e in late if e["event"] == "crdt.sessions.unsaved_backlog"]
    assert (said["count"], said["parked"], said["log_level"]) == (1, 1, "warning")
