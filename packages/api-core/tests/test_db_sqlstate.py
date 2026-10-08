"""The one SQLSTATE reader and the unique-violation verdict callers build on it.

Driven by real Postgres errors: the point is where the driver puts the code
once SQLAlchemy has wrapped it, which a hand-built exception would only guess.
"""

from __future__ import annotations

import uuid

import pytest
from alkera_core.db import session as db_session
from alkera_core.db.errors import UNIQUE_VIOLATION, sqlstate_of
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError


async def _raise(*statements: str) -> DBAPIError:
    """The error the last of ``statements`` raises, run in one transaction."""
    async with db_session.AsyncSessionLocal() as db:
        try:
            for sql in statements:
                await db.execute(text(sql))
        except DBAPIError as exc:
            await db.rollback()
            return exc
    raise AssertionError(f"{statements!r} did not fail")


async def test_a_wrapped_unique_violation_reads_as_unique_violation() -> None:
    table = f"tmp_sqlstate_{uuid.uuid4().hex[:8]}"
    exc = await _raise(
        f"CREATE TEMP TABLE {table} (k int PRIMARY KEY)",
        f"INSERT INTO {table} VALUES (1)",
        f"INSERT INTO {table} VALUES (1)",
    )
    assert isinstance(exc, IntegrityError)
    assert sqlstate_of(exc) == UNIQUE_VIOLATION


async def test_a_non_unique_integrity_error_is_not_a_unique_violation() -> None:
    """A NOT NULL failure is an IntegrityError too; a caller that treats a
    duplicate as "already done" must not swallow it."""
    table = f"tmp_sqlstate_{uuid.uuid4().hex[:8]}"
    exc = await _raise(
        f"CREATE TEMP TABLE {table} (k int NOT NULL)", f"INSERT INTO {table} VALUES (NULL)"
    )
    assert isinstance(exc, IntegrityError)
    assert sqlstate_of(exc) == "23502"


async def test_the_code_is_found_through_a_re_raise() -> None:
    exc = await _raise("SELECT 1 / 0")
    try:
        raise RuntimeError("wrapped by a caller") from exc
    except RuntimeError as outer:
        assert sqlstate_of(outer) == "22012"


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(ValueError("no code"), id="plain-exception"),
        pytest.param(type("Blank", (Exception,), {"sqlstate": ""})(), id="empty-code"),
    ],
)
def test_an_exception_without_a_code_reads_as_none(exc: BaseException) -> None:
    assert sqlstate_of(exc) is None


def test_a_pgcode_only_driver_error_is_read() -> None:
    """Some wrappers keep only ``pgcode``; the reader accepts either spelling."""
    driver = type("LegacyDriverError", (Exception,), {"pgcode": UNIQUE_VIOLATION})()
    assert sqlstate_of(DBAPIError("INSERT", None, driver)) == UNIQUE_VIOLATION


def test_a_cause_cycle_terminates() -> None:
    first, second = ValueError("a"), ValueError("b")
    first.__cause__, second.__cause__ = second, first
    assert sqlstate_of(first) is None
