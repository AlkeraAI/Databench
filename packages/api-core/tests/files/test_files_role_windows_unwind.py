"""No role window may replace the database error it is unwinding.

Every window that steps out of the Files role for a statement or two steps back
in afterwards. When the body failed on the database the transaction is already
aborted, and the step back in answers ``25P02`` — which, raised from the
window's exit, replaced the real error. During the Sep 29 2026 staging stall a
statement timeout (``57014``, a coded, retryable 503) reached clients as an
unattributable 500 this way. Driven with a real statement timeout on a real
Postgres, through every window that exists.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

import pytest
from alkera_core.files import acl, delta, objects_bridge
from alkera_core.files.repo import APP_ROLE, FilesRepo
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.asyncio

QUERY_CANCELED = "57014"
IN_FAILED_TRANSACTION = "25P02"

Window = Callable[[FilesRepo], AbstractAsyncContextManager[None]]

WINDOWS = [
    pytest.param(acl.as_login_role, id="acl-login-role"),
    pytest.param(objects_bridge._writing_a_grant, id="bridge-writing-a-grant"),
    pytest.param(objects_bridge._recording_the_reference, id="bridge-recording-the-reference"),
    pytest.param(delta._reading_the_outbox, id="delta-reading-the-outbox"),
]


def _sqlstate(error: BaseException) -> str:
    return str(getattr(getattr(error, "orig", None), "sqlstate", ""))


@pytest.mark.parametrize("window", WINDOWS)
async def test_a_statement_timeout_inside_the_window_surfaces_as_itself(
    repo: FilesRepo, window: Window
) -> None:
    with pytest.raises(DBAPIError) as caught:
        async with repo.transaction():
            async with window(repo):
                await repo.session.execute(text("SET LOCAL statement_timeout = '50ms'"))
                await repo.session.execute(text("SELECT pg_sleep(2)"))

    state = _sqlstate(caught.value)
    assert state != IN_FAILED_TRANSACTION, "the window's restore masked the body's error"
    assert state == QUERY_CANCELED, f"unexpected sqlstate {state!r} from {caught.value!r}"


@pytest.mark.parametrize("window", WINDOWS)
async def test_a_python_failure_inside_the_window_still_takes_the_files_role_back(
    repo: FilesRepo, window: Window
) -> None:
    """A failure that left the transaction usable must not leave the rest of the
    unit of work as the login role — no RLS, every grant."""
    async with repo.transaction():
        with pytest.raises(ValueError, match="not the database"):
            async with window(repo):
                raise ValueError("not the database")
        role = (await repo.session.execute(text("SELECT current_user"))).scalar_one()
        assert role == APP_ROLE, f"the window left the session as {role!r}"


@pytest.mark.parametrize("window", WINDOWS)
async def test_the_window_takes_the_files_role_back_on_the_happy_path(
    repo: FilesRepo, window: Window
) -> None:
    async with repo.transaction():
        async with window(repo):
            inside = (await repo.session.execute(text("SELECT current_user"))).scalar_one()
            assert inside != APP_ROLE
        after = (await repo.session.execute(text("SELECT current_user"))).scalar_one()
    assert after == APP_ROLE
