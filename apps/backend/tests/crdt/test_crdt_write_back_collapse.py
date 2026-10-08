"""The write-back collapse against a real live session.

The drive's janitor bounds the versions a co-edited file's write backs leave
behind. A session merging an outside change looks back through the file's
latest write backs for the version the change was made on, including one it
landed and never recorded; the collapse must never take that base out from
under it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from alkera_core.files.ids import OrgScope
from alkera_core.files.repo import FilesRepo
from alkera_core.files.sweepers import WRITE_BACK_POSITIONS_KEPT, SweepDeps, WriteBackCollapse
from backend.services.crdt.registry import WRITTEN_POSITIONS
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin
from tests.crdt.crdt_world import Peer
from tests.crdt.file_world import SOURCE, acting, drive_text, file_world, outside_write
from tests.crdt.test_crdt_file_docs import (
    _open,
    _sent_by_a_box,
    _write,
    _written_back,
    crdt_docs,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]


async def test_the_collapse_keeps_every_write_back_a_merge_looks_back_through() -> None:
    """The session looks back through its file's newest write backs; the
    collapse keeps at least that many of the session's epoch."""
    assert WRITTEN_POSITIONS <= WRITE_BACK_POSITIONS_KEPT


async def test_a_collapse_pass_leaves_an_unrecorded_write_back_a_merge_can_start_from(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A box's agent read the file several write backs ago and wrote it
    after. The session lost its own record of those write backs (a write
    back cut off before it was recorded), so the base is found only on the
    version rows. A collapse pass in between must leave that row, or the
    change is merged from the wrong version and the person's lines read as
    removed or added twice."""
    fw = await file_world(real_session, org_admin)
    tab = Peer(5000, container="content")
    async with crdt_docs() as docs:
        await _open(docs, real_session, fw, fw.world.owner, tab)
        lines = [f"# line {n}\n" for n in range(12)]
        read_by_agent = ""
        for n, line in enumerate(lines):
            await _write(docs, real_session, fw, fw.world.owner, tab, tab.type(0, line), n=n + 1)
            assert await _written_back(docs, real_session, fw) == "written"
            if n == 6:
                read_by_agent = await drive_text(real_session, fw)
        await real_session.execute(
            text(
                "UPDATE crdt_docs SET source_history = '[]'::jsonb "
                "WHERE doc_type = 'file' AND doc_id = :doc"
            ),
            {"doc": fw.ref.doc_id},
        )
        await real_session.commit()

        deps = SweepDeps(
            repo=FilesRepo(real_session, OrgScope(org_team_id=fw.ref.org_id)),
            ctx=acting(fw.world.owner),
        )
        await WriteBackCollapse(deps).run(datetime.now(UTC) + timedelta(days=1))

        await outside_write(real_session, fw, (read_by_agent + "# agent\n").encode())
        await _sent_by_a_box(real_session, fw)
        settled = await docs.sessions.merge_outside(real_session, fw.ref)
        await real_session.commit()
        assert settled.outcome == "merged" and settled.applied is not None
        tab.receive(settled.applied.delta)
    assert tab.text == "".join(reversed(lines)) + SOURCE + "# agent\n"
