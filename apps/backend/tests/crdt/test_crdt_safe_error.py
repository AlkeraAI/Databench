"""What a CRDT log line may say about a failure: its type and a database error's
SQLSTATE, never the text (which carries the statement's parameters, people's
drafts on this lane)."""

from __future__ import annotations

import pytest
from alkera_core.db import session as db_session
from backend.services.crdt.errors import CrdtError, safe_error
from sqlalchemy import bindparam, text
from sqlalchemy.exc import DBAPIError

DRAFT = "my private draft text"


async def test_a_database_error_reports_its_sqlstate_and_never_its_parameters() -> None:
    async with db_session.AsyncSessionLocal() as db:
        with pytest.raises(DBAPIError) as caught:
            await db.execute(
                text("SELECT CAST(CAST(:draft AS text) AS integer)").bindparams(bindparam("draft")),
                {"draft": DRAFT},
            )
        await db.rollback()
    assert DRAFT in str(caught.value), "the driver's own text does carry the parameter"
    line = safe_error(caught.value)
    assert line == f"{type(caught.value).__name__}[22P02]"
    assert DRAFT not in line


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        pytest.param(
            CrdtError("too_large", reason="update"),
            "CrdtError[too_large:update]",
            id="lane-with-reason",
        ),
        pytest.param(CrdtError("stale_epoch"), "CrdtError[stale_epoch]", id="lane-without-reason"),
        pytest.param(ValueError(DRAFT), "ValueError", id="other-exception"),
    ],
)
def test_a_non_database_error_reports_only_what_is_safe(exc: BaseException, expected: str) -> None:
    assert safe_error(exc) == expected
