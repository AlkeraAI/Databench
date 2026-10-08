"""The outbox fence must never replace the error that happened inside it.

``_reading_the_outbox`` drops to the login role for the outbox join and takes
the Files role back afterwards. When the body fails, that "take it back"
statement runs on a transaction the failure has already aborted — Postgres
answers every further statement with ``25P02`` — and because the statement runs
from ``__aexit__`` its error *replaces* the one that actually happened. The
caller then sees ``InFailedSQLTransactionError`` and has no way to learn what
went wrong; that is exactly how the eight-writer soak lost its first error.
"""

from __future__ import annotations

import pytest
from alkera_core.files.delta import _reading_the_outbox
from alkera_core.files.repo import APP_ROLE, FilesRepo
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.asyncio

#: ``division_by_zero`` — what the armed statement inside the fence really is.
DIVISION_BY_ZERO = "22012"
#: ``in_failed_sql_transaction`` — what the cleanup statement answers once the
#: body has poisoned the transaction, and what used to surface instead.
IN_FAILED_TRANSACTION = "25P02"


def _sqlstate(error: BaseException) -> str:
    return str(getattr(getattr(error, "orig", None), "sqlstate", ""))


async def test_a_failing_statement_inside_the_fence_surfaces_its_own_error(
    repo: FilesRepo,
) -> None:
    """The body's error reaches the caller, not the cleanup's."""
    with pytest.raises(DBAPIError) as caught:
        async with repo.transaction():
            async with _reading_the_outbox(repo):
                await repo.session.execute(text("SELECT 1 / 0"))

    state = _sqlstate(caught.value)
    assert state != IN_FAILED_TRANSACTION, (
        "the fence's cleanup masked the body's error with its own: "
        f"{type(caught.value.orig).__name__}"
    )
    assert state == DIVISION_BY_ZERO, f"unexpected sqlstate {state!r} from {caught.value!r}"


async def test_the_fence_takes_the_files_role_back_on_the_happy_path(repo: FilesRepo) -> None:
    """A body that succeeds leaves the caller under the Files role again."""
    async with repo.transaction():
        async with _reading_the_outbox(repo):
            assert (await repo.session.execute(text("SELECT current_user"))).scalar_one() != (
                APP_ROLE
            ), "the fence never dropped to the login role"
        after = (await repo.session.execute(text("SELECT current_user"))).scalar_one()
    assert after == APP_ROLE, f"the fence left the session as {after!r}"


async def test_a_python_failure_inside_the_fence_still_takes_the_files_role_back(
    repo: FilesRepo,
) -> None:
    """A body that fails without poisoning the transaction is not left widened.

    Skipping the cleanup outright would hand the rest of the transaction the
    login role — no RLS, every grant — so the restore is still attempted
    whenever it can run; it just may never replace the body's error.
    """
    async with repo.transaction():
        with pytest.raises(ValueError, match="nothing to do with the database"):
            async with _reading_the_outbox(repo):
                raise ValueError("nothing to do with the database")
        still = (await repo.session.execute(text("SELECT current_user"))).scalar_one()
        assert still == APP_ROLE, f"the fence left the session as {still!r}"
