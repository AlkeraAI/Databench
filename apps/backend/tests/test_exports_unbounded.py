"""Exports carry the whole thing, and cost one page of memory to do it.

Both CSV exports used to build their document in memory, and both then had to
stand a cap in front of it — ten thousand rows on the audit trail, the whole
result materialised twice for a promoted table. A cap on an export is a
silently incomplete answer, and it was load-bearing only because of how the
document was built.

So the property pinned here is one property, twice: every row that matches
comes out, and the memory the export takes does not grow with how many that
is. The second half is measured with :mod:`tracemalloc` around the consumption
of the stream, against a bound far below what one materialised copy of the
same document would cost — a route that went back to building the whole thing
fails it by an order of magnitude, not by a hair.
"""

from __future__ import annotations

import tracemalloc
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from alkera_core.authz import SCOPE_PRIVATE
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import ObjectPayloadRow, OrgAuditEvent, WorkspaceObject
from backend.services.audit import org_audit as org_audit_service
from backend.services.objects import result_store
from httpx import AsyncClient
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, login, make_org_enterprise

pytestmark = pytest.mark.asyncio

#: More rows than the audit export's old ten-thousand cap, so a cap that came
#: back would cut this trail rather than pass by luck.
AUDIT_ROWS = 10_500

#: Spill pages for the result export, at the store's own page size. 250 pages
#: is a quarter-million rows — past the 200,000 a promote will accept, so no
#: figure anywhere in the write path is silently bounding the read path.
RESULT_PAGES = 250

#: What a streamed export is allowed to hold at once, generously. One page of
#: a result is a thousand small rows (~100 KB) and one audit batch is a
#: thousand events; materialising either document costs tens of megabytes of
#: Python lists before the string is even built.
PEAK_BYTES_CEILING = 12 * 1024 * 1024


async def _seed_audit(org_id: uuid.UUID, count: int) -> None:
    """``count`` audit rows for the org, written straight to the table.

    Not through ``record``: the hash chain takes a per-org advisory lock per
    row and this needs ten thousand of them. What is under test is the read,
    and the read does not care how the row got there.
    """
    start = datetime.now(UTC) - timedelta(days=30)
    async with AsyncSessionLocal() as session:
        await session.execute(
            insert(OrgAuditEvent),
            [
                {
                    "id": uuid.uuid4(),
                    "org_team_id": org_id,
                    "actor_email": f"seed{index}@example.com",
                    "action": "export.seed",
                    "target": f"row-{index}",
                    "detail": {"index": index},
                    "created_at": start + timedelta(seconds=index),
                }
                for index in range(count)
            ],
        )
        await session.commit()


async def test_the_audit_export_walks_past_the_old_cap(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """Every matching event, newest first, each one exactly once.

    The walk seeks on ``(created_at, id)`` rather than counting an offset, so
    this is also where a seek that skipped or repeated a row at a batch
    boundary would show: the ids are compared as a set against the table, and
    the timestamps as a descending sequence.
    """
    await _seed_audit(org_admin.org_id, AUDIT_ROWS)
    seen: list[OrgAuditEvent] = []
    async for event in org_audit_service.iter_all(real_session, org_id=org_admin.org_id, batch=250):
        seen.append(event)

    stamps = [event.created_at for event in seen]
    assert stamps == sorted(stamps, reverse=True)
    ids = [event.id for event in seen]
    assert len(set(ids)) == len(ids), "a batch boundary handed a row out twice"
    held = (
        await real_session.execute(
            select(OrgAuditEvent.id).where(OrgAuditEvent.org_team_id == org_admin.org_id)
        )
    ).scalars()
    assert set(ids) == set(held)
    assert len(ids) >= AUDIT_ROWS


async def test_the_audit_export_route_streams_every_row(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The download is the whole trail, and the server never holds it.

    Read off the wire a chunk at a time, which is the only way to tell a
    response that was produced as it was sent from one that was built and then
    handed over: a materialising route peaks at the size of the document
    before the first byte leaves.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    await make_org_enterprise(org_admin.org_id)
    await _seed_audit(org_admin.org_id, AUDIT_ROWS)

    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        lines = 0
        tail = ""
        async with client.stream("GET", "/api/v1/org/audit-events/export.csv") as response:
            assert response.status_code == 200
            async for chunk in response.aiter_text():
                tail += chunk
                lines += tail.count("\n")
                tail = tail.rsplit("\n", 1)[-1]
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert lines >= AUDIT_ROWS + 1, "the export stopped short of the rows the org holds"
    assert peak < PEAK_BYTES_CEILING, (
        f"the export held {peak} bytes at once; it is building the document rather than sending it"
    )


async def _spilled_result(
    session: AsyncSession, org_id: uuid.UUID, owner_id: uuid.UUID
) -> uuid.UUID:
    """A ready result whose payload is ``RESULT_PAGES`` spill pages.

    The pages are written as the store writes them, so the export reads the
    same shape a real promote produces — only the route in is skipped, because
    a quarter of a million rows will not fit through one upload request.
    """
    object_id = uuid.uuid4()
    total = RESULT_PAGES * result_store.ROWS_PER_PAGE
    session.add(
        WorkspaceObject(
            id=object_id,
            logical_id=f"export-{object_id}",
            namespace="default",
            org_team_id=org_id,
            owner_user_id=owner_id,
            type="result",
            title="A quarter of a million rows",
            status="ready",
            visibility_scope=SCOPE_PRIVATE,
            spec={
                "schema_version": "1.3.0",
                "envelope_columns": ["n", "note"],
                "total_rows": total,
                "inline": None,
                "payload": {
                    "schema_version": "1.0.0",
                    "sha256": "0" * 64,
                    "size": total,
                    "media_type": "application/json",
                },
            },
        )
    )
    await session.flush()
    for page in range(RESULT_PAGES):
        first = page * result_store.ROWS_PER_PAGE
        session.add(
            ObjectPayloadRow(
                object_id=object_id,
                page=page,
                sha256="0" * 64,
                size=total,
                media_type="application/json",
                page_rows=[
                    [index, f"row {index}"]
                    for index in range(first, first + result_store.ROWS_PER_PAGE)
                ],
            )
        )
    await session.commit()
    return object_id


async def test_the_result_export_streams_a_quarter_million_rows(
    org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """Every spilled row reaches the document, one page of memory at a time.

    The old reader put every row in one list and the whole document in one
    string before the response existed. At this size those two copies are tens
    of megabytes; a page at a time is a hundred kilobytes, and the count says
    nothing was dropped on the way.
    """
    object_id = await _spilled_result(real_session, org_admin.org_id, org_admin.admin_id)
    obj = (
        await real_session.execute(select(WorkspaceObject).where(WorkspaceObject.id == object_id))
    ).scalar_one()

    header, first = "", True
    rows = 0
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        tail = ""
        async for chunk in result_store.stream_csv(real_session, obj=obj):
            tail += chunk
            while "\r\n" in tail:
                line, tail = tail.split("\r\n", 1)
                if first:
                    header, first = line, False
                else:
                    rows += 1
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert header == "n,note"
    assert rows == RESULT_PAGES * result_store.ROWS_PER_PAGE
    assert peak < PEAK_BYTES_CEILING, (
        f"the export held {peak} bytes at once; it is materialising the result"
    )


async def test_the_result_export_route_hands_back_every_row(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """Through the real route, with the byte-order mark and no cap.

    The route is where the session's lifetime matters: the document is
    produced while the response is being sent, so a session closed at the end
    of the handler would truncate the download rather than fail it — which is
    what the row count is here to catch.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    object_id = await _spilled_result(real_session, org_admin.org_id, org_admin.admin_id)

    lines, tail, leading = 0, "", b""
    async with client.stream("GET", f"/api/v1/objects/{object_id}/export.csv") as response:
        assert response.status_code == 200
        async for chunk in response.aiter_bytes():
            if not leading:
                leading = chunk[:3]
            text = chunk.decode("utf-8", "ignore")
            tail += text
            lines += tail.count("\r\n")
            tail = tail.rsplit("\r\n", 1)[-1]

    assert leading == b"\xef\xbb\xbf", "the utf-8 byte-order mark still leads the file"
    assert lines == RESULT_PAGES * result_store.ROWS_PER_PAGE + 1
