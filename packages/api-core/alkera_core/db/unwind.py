"""Put a transaction-scoped setting back without ever replacing the error that
is being unwound.

Several windows step out of the Files role (``tenant_session.stepped_out``) for a
statement or two and step back in afterwards. When the body fails with a
database error, Postgres has already aborted the transaction: the step back in
answers ``25P02`` (``in_failed_sql_transaction``), or — after a failed flush —
SQLAlchemy refuses the session outright with ``PendingRollbackError``. Raised
from a ``finally`` or an ``except`` that error *replaces* the real one, so a
statement timeout that should reach the client as a coded, retryable 503
arrives as an unattributable 500.

The rule every window follows, spelled once here:

* a body that succeeded is restored, and a failed restore is raised — it is the
  only error there is;
* a body cancelled is never followed by a statement (the connection may have one
  in flight, and awaiting here is only cancelled again) — the rollback that
  ends the request reverts the transaction-scoped setting;
* a body that failed with a database error is not followed by one either: the
  transaction is dead, and the caller's rollback reverts the setting;
* any other failure (a Python error, a refusal raised as an HTTP exception) left
  the transaction usable, so the restore is still attempted — skipping it would
  hand the rest of the unit of work the wider role — but a restore that fails
  is swallowed, and the body's error is what propagates.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress

from sqlalchemy.exc import DBAPIError, PendingRollbackError

Restore = Callable[[], Awaitable[object]]


def transaction_is_dead(error: BaseException) -> bool:
    """Whether ``error`` leaves no statement runnable on the session's
    transaction until it is rolled back."""
    return isinstance(error, DBAPIError | PendingRollbackError | asyncio.CancelledError)


async def restore_while_unwinding(error: BaseException, restore: Restore) -> None:
    """Best-effort ``restore`` while ``error`` propagates; never raises."""
    if transaction_is_dead(error):
        return
    with suppress(Exception):
        await restore()


@asynccontextmanager
async def then_restore(restore: Restore) -> AsyncIterator[None]:
    """Run the body, then ``restore`` — without letting the restore decide the
    outcome of a body that failed."""
    try:
        yield
    except BaseException as error:
        await restore_while_unwinding(error, restore)
        raise
    await restore()


__all__ = ["restore_while_unwinding", "then_restore", "transaction_is_dead"]
