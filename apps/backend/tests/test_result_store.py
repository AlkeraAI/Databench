"""The cloud result store: the inline/spill boundary, paging across pages, and
a CSV export a spreadsheet reads as text.

The two things that are easy to get wrong and expensive to get wrong late: the
boundary between inline and spilled (a payload that lands on the wrong side is
either a bloated row or a lost result), and the page arithmetic when a read
window straddles two spill pages. Both are pinned with real payloads against
the real database rather than a mocked store.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import ObjectPayloadRow, User, WorkspaceObject
from alkera_core.schemas.objects import BlobHandle, ResultBlobEnvelope, ResultSpec
from backend.services.objects import object_service, result_store
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin


async def _admin(db: AsyncSession, org: OrgWithAdmin) -> User:
    user = await db.get(User, org.admin_id)
    assert user is not None
    return user


async def _result(
    db: AsyncSession, owner: User, *, columns: list[dict[str, str]] | None = None
) -> WorkspaceObject:
    spec = ResultSpec()
    obj, _ = await object_service.create_object(
        db,
        owner=owner,
        type="result",
        title="Orders",
        spec=spec.model_dump(mode="json"),
        org_id=owner.home_org_team_id,
    )
    if columns is not None:
        obj.spec = {**obj.spec, "columns": columns}
    await db.commit()
    return obj


def _rows_envelope(count: int, *, width: int = 1) -> ResultBlobEnvelope:
    rows: list[list[Any]] = [[f"r{index}-{'x' * width}", index] for index in range(count)]
    return ResultBlobEnvelope(kind="rows", columns=["label", "n"], rows=rows, total=count)


def _handle(envelope: ResultBlobEnvelope) -> BlobHandle:
    return BlobHandle(sha256="a" * 64, size=result_store.envelope_bytes(envelope))


async def _store(
    db: AsyncSession, obj: WorkspaceObject, envelope: ResultBlobEnvelope
) -> dict[str, Any]:
    placement = await result_store.store_payload(
        db, obj=obj, envelope=envelope, handle=_handle(envelope)
    )
    obj.spec = {**obj.spec, **placement}
    await db.commit()
    return placement


async def _spill_pages(object_id: UUID) -> list[ObjectPayloadRow]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(ObjectPayloadRow)
            .where(ObjectPayloadRow.object_id == object_id)
            .order_by(ObjectPayloadRow.page)
        )
        return list(rows.scalars().all())


# ---------------------------------------------------------------------------
# The inline / spill boundary
# ---------------------------------------------------------------------------


def _envelope_of_size(target: int) -> ResultBlobEnvelope:
    """An envelope whose stored size straddles ``target`` as closely as one
    row allows, so the boundary test is about the boundary and not about a
    payload that happens to be far from it."""
    envelope = _rows_envelope(0)
    filler = 200
    while result_store.envelope_bytes(envelope) < target:
        envelope = ResultBlobEnvelope(
            kind="rows",
            columns=["label", "n"],
            rows=[*envelope.rows, ["x" * filler, len(envelope.rows)]],
            total=len(envelope.rows) + 1,
        )
    return envelope


async def test_a_payload_under_the_inline_limit_rides_in_the_spec(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    admin = await _admin(real_session, org_admin)
    obj = await _result(real_session, admin)
    envelope = _envelope_of_size(63 * 1024)
    assert result_store.envelope_bytes(envelope) < result_store.INLINE_LIMIT_BYTES
    placement = await _store(real_session, obj, envelope)
    assert placement["inline"] is not None
    assert placement["total_rows"] == len(envelope.rows)
    assert await _spill_pages(obj.id) == []


async def test_a_payload_over_the_inline_limit_is_paged_into_rows(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    admin = await _admin(real_session, org_admin)
    obj = await _result(real_session, admin)
    envelope = _envelope_of_size(65 * 1024)
    assert result_store.envelope_bytes(envelope) >= result_store.INLINE_LIMIT_BYTES
    placement = await _store(real_session, obj, envelope)
    assert placement["inline"] is None
    assert placement["payload"]["sha256"] == "a" * 64
    pages = await _spill_pages(obj.id)
    assert pages, "a spilled payload leaves at least one page"
    assert sum(len(page.page_rows) for page in pages) == len(envelope.rows)
    assert [page.page for page in pages] == list(range(len(pages)))


async def test_both_placements_read_back_identically(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The point of one reader over two placements: a caller cannot tell which
    one it got."""
    admin = await _admin(real_session, org_admin)
    small = await _result(real_session, admin)
    big = await _result(real_session, admin)
    inline_envelope = _envelope_of_size(1024)
    await _store(real_session, small, inline_envelope)
    await _store(real_session, big, _envelope_of_size(70 * 1024))
    columns, rows, total = await result_store.read_rows(real_session, obj=small, offset=0, limit=3)
    assert columns == ["label", "n"]
    assert rows == inline_envelope.rows[:3]
    assert total == inline_envelope.total
    _, big_rows, big_total = await result_store.read_rows(real_session, obj=big, offset=0, limit=3)
    assert len(big_rows) == 3
    assert big_total > total


# ---------------------------------------------------------------------------
# Paging
# ---------------------------------------------------------------------------


async def test_a_read_window_straddling_two_spill_pages_returns_the_right_rows(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The arithmetic that breaks silently: a window that starts inside one
    page and ends inside the next."""
    monkeypatch.setattr(result_store, "ROWS_PER_PAGE", 10)
    monkeypatch.setattr(result_store, "INLINE_LIMIT_BYTES", 1)
    admin = await _admin(real_session, org_admin)
    obj = await _result(real_session, admin)
    envelope = _rows_envelope(35)
    await _store(real_session, obj, envelope)
    assert len(await _spill_pages(obj.id)) == 4
    _, rows, total = await result_store.read_rows(real_session, obj=obj, offset=8, limit=5)
    assert rows == envelope.rows[8:13]
    assert total == 35


@pytest.mark.parametrize(
    ("offset", "limit", "expected"),
    [
        pytest.param(0, 5, (0, 5), id="first_page"),
        pytest.param(30, 5, (30, 35), id="last_page_exactly"),
        pytest.param(33, 5, (33, 35), id="last_page_short"),
        pytest.param(35, 5, (35, 35), id="offset_at_the_end"),
        pytest.param(200, 5, (35, 35), id="offset_past_the_end"),
    ],
)
async def test_spill_paging_boundaries(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    offset: int,
    limit: int,
    expected: tuple[int, int],
) -> None:
    monkeypatch.setattr(result_store, "ROWS_PER_PAGE", 10)
    monkeypatch.setattr(result_store, "INLINE_LIMIT_BYTES", 1)
    admin = await _admin(real_session, org_admin)
    obj = await _result(real_session, admin)
    envelope = _rows_envelope(35)
    await _store(real_session, obj, envelope)
    _, rows, total = await result_store.read_rows(real_session, obj=obj, offset=offset, limit=limit)
    start, end = expected
    assert rows == envelope.rows[start:end]
    assert total == 35


async def test_a_re_upload_replaces_every_page_rather_than_layering(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retried promote must not leave half of an older payload behind."""
    monkeypatch.setattr(result_store, "ROWS_PER_PAGE", 10)
    monkeypatch.setattr(result_store, "INLINE_LIMIT_BYTES", 1)
    admin = await _admin(real_session, org_admin)
    obj = await _result(real_session, admin)
    await _store(real_session, obj, _rows_envelope(35))
    await _store(real_session, obj, _rows_envelope(12))
    pages = await _spill_pages(obj.id)
    assert sum(len(page.page_rows) for page in pages) == 12


async def test_a_payload_over_the_row_ceiling_is_refused(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(result_store, "MAX_TOTAL_ROWS", 5)
    admin = await _admin(real_session, org_admin)
    obj = await _result(real_session, admin)
    with pytest.raises(result_store.PayloadTooLargeError, match="at most 5 rows"):
        await result_store.store_payload(
            db=real_session,
            obj=obj,
            envelope=_rows_envelope(6),
            handle=BlobHandle(sha256="a" * 64, size=1),
        )
    await real_session.rollback()


async def test_a_text_payload_over_the_inline_limit_is_refused(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Text has no paged form; a promote of one that large is a mistake, not a
    thing to store half of."""
    admin = await _admin(real_session, org_admin)
    obj = await _result(real_session, admin)
    huge = ResultBlobEnvelope(kind="text", text="x" * (80 * 1024), total=80 * 1024)
    with pytest.raises(result_store.PayloadTooLargeError, match="no paged form"):
        await result_store.store_payload(
            db=real_session, obj=obj, envelope=huge, handle=_handle(huge)
        )
    await real_session.rollback()


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        pytest.param("=1+1", "'=1+1", id="equals"),
        pytest.param("+SUM(A1)", "'+SUM(A1)", id="plus"),
        pytest.param("-SUM(A1)", "'-SUM(A1)", id="minus"),
        pytest.param("@SUM(A1)", "'@SUM(A1)", id="at"),
        pytest.param("=cmd|'/c calc'!A1", "'=cmd|'/c calc'!A1", id="dde_payload"),
        pytest.param("\tSUM(1)", "'\tSUM(1)", id="tab"),
        pytest.param("\r=1", "'\r=1", id="carriage_return"),
        pytest.param("acme", "acme", id="ordinary_text_is_untouched"),
        pytest.param("a=1", "a=1", id="equals_not_at_the_start"),
        pytest.param(None, "", id="null_becomes_empty"),
        pytest.param(0, "0", id="zero_is_not_falsy_here"),
        pytest.param(-5, "-5", id="a_negative_int_is_a_number_not_a_formula"),
        pytest.param(-5.25, "-5.25", id="a_negative_float_is_a_number"),
        pytest.param("-5", "-5", id="a_negative_number_written_as_text_is_a_number"),
        pytest.param("-1.5e-3", "-1.5e-3", id="scientific_notation_is_a_number"),
        pytest.param("+41", "+41", id="an_explicitly_signed_positive_is_a_number"),
        pytest.param("-.5", "-.5", id="a_leading_dot_decimal_is_a_number"),
        pytest.param("-cmd|'/c calc'!A1", "'-cmd|'/c calc'!A1", id="minus_dde_is_still_escaped"),
        pytest.param("+cmd|'/c calc'!A1", "'+cmd|'/c calc'!A1", id="plus_dde_is_still_escaped"),
        pytest.param("-5-cmd", "'-5-cmd", id="a_number_prefix_is_not_a_number"),
        pytest.param("-", "'-", id="a_bare_minus_is_not_a_number"),
        pytest.param("-1e", "'-1e", id="a_truncated_exponent_is_not_a_number"),
        pytest.param("\t-5", "'\t-5", id="a_control_lead_is_escaped_even_before_a_number"),
    ],
)
def test_formula_leading_cells_are_escaped(cell: object, expected: str) -> None:
    assert result_store.escape_csv_cell(cell) == expected


def test_csv_is_a_golden_document() -> None:
    body = result_store.to_csv(
        ["day", "note"],
        [["2026-09-01", '=HYPERLINK("http://evil")'], ["2026-09-02", 'quote " and, comma']],
    )
    assert body == (
        "day,note\r\n"
        '2026-09-01,"\'=HYPERLINK(""http://evil"")"\r\n'
        '2026-09-02,"quote "" and, comma"\r\n'
    )


async def _export(obj: WorkspaceObject, session: AsyncSession) -> str:
    """The whole export document, joined back up by the test.

    The service hands it over a page at a time — that is the point of it — so
    a test that wants to read the document whole is the one place putting the
    pages back together belongs.
    """
    return "".join([chunk async for chunk in result_store.stream_csv(session, obj=obj)])


async def test_export_reads_every_spill_page_in_order(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(result_store, "ROWS_PER_PAGE", 10)
    monkeypatch.setattr(result_store, "INLINE_LIMIT_BYTES", 1)
    admin = await _admin(real_session, org_admin)
    obj = await _result(real_session, admin)
    envelope = _rows_envelope(25)
    await _store(real_session, obj, envelope)
    body = await _export(obj, real_session)
    lines = body.strip().split("\r\n")
    assert lines[0] == "label,n"
    assert len(lines) == 26
    assert lines[1].startswith("r0-")
    assert lines[-1].startswith("r24-")


async def test_export_uses_the_authors_column_labels(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Renaming a column is a stated requirement, and the export is where it
    has to be visible or the rename means nothing."""
    admin = await _admin(real_session, org_admin)
    obj = await _result(
        real_session,
        admin,
        columns=[{"name": "label", "label": "Customer"}, {"name": "n", "label": ""}],
    )
    await _store(real_session, obj, _rows_envelope(1))
    body = await _export(obj, real_session)
    assert body.split("\r\n")[0] == "Customer,n"
