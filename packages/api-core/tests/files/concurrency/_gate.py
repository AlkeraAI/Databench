"""The two-session interleaving harness the lock-order modules share.

A concurrency claim about Files is a claim about two Postgres backends and one
row, so a test needs three things no library seam provides: a way to park one
session *while it holds a lock* (a statement gate on that session's own DBAPI
connection), a way to know the other session is *genuinely queued* behind it
(an ungranted lock for its backend in ``pg_locks``, never a sleep), and a way to
tell the deadlock detector firing apart from a documented Files refusal.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.util import await_only

DEADLOCK = "40P01"
"""The SQLSTATE the Postgres deadlock detector raises. Seeing it is a failure."""

#: The documented order. A session's row-lock statements must be non-decreasing
#: in this rank; ``file_versions`` is last because a version is only ever locked
#: once its node already is.
LOCK_RANK = {"file_drives": 0, "file_nodes": 1, "file_versions": 2}

_LOCKING = re.compile(r"\bFOR\s+(UPDATE|SHARE|NO\s+KEY\s+UPDATE)\b", re.IGNORECASE)
_FROM = re.compile(r"\bFROM\s+(file_[a-z_]+)", re.IGNORECASE)

BLOCKED_TIMEOUT = 10.0
"""How long we wait for Postgres to report the second operation blocked."""

BLOCKED_POLL_SECONDS = 0.005
"""How long to wait between two asks of ``pg_locks``.

That view is a reading of the WHOLE lock manager, not of the two rows this test
cares about: answering it takes every lock partition in shared mode and walks
one fast-path array per backend, so its cost follows the server's connection
budget and every other backend's lock acquisition queues behind it. Asked with
no pause at all — which is what yielding to the event loop and going straight
round again amounted to — one module issued about nine and a half thousand of
them per run, and on CI every xdist worker does that at once against one
server. Five milliseconds is orders of magnitude below the time either side
needs to reach a row lock, so nothing observed here changes, and it cuts the
asks by a factor of a hundred.
"""


def _sqlstate(exc: BaseException) -> str | None:
    original = getattr(exc, "orig", None)
    state = getattr(original, "sqlstate", None)
    return state if state is not None else getattr(original, "pgcode", None)


def _mentions_deadlock(exc: BaseException) -> bool:
    seen: BaseException | None = exc
    while seen is not None:
        if _sqlstate(seen) == DEADLOCK or DEADLOCK in str(seen):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


class StatementGate:
    """Records one session's row-lock statements and can park it after the first.

    Installed on the session's own DBAPI connection, so the park happens inside
    the transaction with the lock already held — the state a pair test needs,
    and one no library checkpoint provides for every operation (``create`` and
    ``rename`` have none).
    """

    def __init__(self, session: AsyncSession, *, park_on: re.Pattern[str] | None = None) -> None:
        bind = session.bind
        assert bind is not None
        sync = bind.sync_connection  # type: ignore[union-attr]
        assert sync is not None
        self._sync = sync
        self.tables: list[str] = []
        self.parked = asyncio.Event()
        self._release: asyncio.Event | None = None
        self._armed = False
        #: Park after the statement this matches instead of after a row lock.
        #: A rename's own write is not a ``FOR UPDATE``, so the one test that
        #: needs a session parked *holding the row it just renamed* names that
        #: statement rather than waiting for a lock that never comes.
        self._park_on = park_on
        event.listen(self._sync, "after_cursor_execute", self._after)

    def close(self) -> None:
        event.remove(self._sync, "after_cursor_execute", self._after)
        self.release()

    def arm(self) -> None:
        """Park this session right after its next row-lock statement."""
        self._release = asyncio.Event()
        self._armed = True

    def release(self) -> None:
        if self._release is not None:
            self._release.set()

    def _after(
        self,
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        locking = bool(_LOCKING.search(statement))
        match = _FROM.search(statement) if locking else None
        if locking and match is not None:
            self.tables.append(match.group(1).lower())
        if self._park_on is None:
            if not (locking and match is not None):
                return
        elif not self._park_on.search(statement):
            return
        if not self._armed:
            return
        self._armed = False
        release = self._release
        assert release is not None
        self.parked.set()
        await_only(release.wait())

    def ranks(self) -> list[int]:
        """The rank of each table's FIRST lock, in the order they were taken.

        Only the first matters: a transaction that already holds a row's
        ``FOR UPDATE`` cannot block on asking for it again, so a composite
        operation that re-enters ``lock_chain`` (resolving a conflict with
        ``both`` locks the chain, then ``Namespace.create`` locks it again to
        place the sibling) is not an inversion. Taking a table for the first
        time below one already held still is, which is the bug this checks for.
        """
        seen: dict[str, int] = {}
        for table in self.tables:
            if table in LOCK_RANK and table not in seen:
                seen[table] = LOCK_RANK[table]
        return list(seen.values())


async def _backend_pid(session: Any) -> int:
    return int((await session.execute(text("SELECT pg_backend_pid()"))).scalar_one())


async def _wait_blocked(observer: Any, pid: int, other: asyncio.Task[Any]) -> bool:
    """Return once Postgres reports `pid` waiting on a lock, or `other` finished.

    A condition wait against the server's own view, never a sleep to let one
    side win: either the second operation is genuinely queued behind the first
    or it never wanted the same row. The pause between two asks is
    :data:`BLOCKED_POLL_SECONDS` and is about what the ask costs the server, not
    about giving either side time — the answer is a fact when it is read.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + BLOCKED_TIMEOUT
    while loop.time() < deadline:
        blocked = (
            await observer.execute(
                text("SELECT count(*) FROM pg_locks WHERE pid = :pid AND NOT granted"),
                {"pid": pid},
            )
        ).scalar_one()
        if int(blocked) > 0:
            return True
        if other.done():
            return False
        await asyncio.sleep(BLOCKED_POLL_SECONDS)
    return False


__all__ = [
    "BLOCKED_POLL_SECONDS",
    "BLOCKED_TIMEOUT",
    "DEADLOCK",
    "LOCK_RANK",
    "StatementGate",
    "_backend_pid",
    "_mentions_deadlock",
    "_wait_blocked",
]
