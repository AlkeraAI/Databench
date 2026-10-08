"""Count the SQL statements a block of test code runs, and only those.

The backend suite runs a worker's tests on one session-scoped event loop
against one process-wide engine, so work an earlier test left running -- a
debounced announcement, a mail dispatch, a sweeper -- executes on the same
engine while a later test measures. A listener on the whole engine charges
that work to whatever block is open at the time, and the longer the block, the
likelier the miscount.

:func:`counting` records only statements run in its own context: the code the
block awaits, every connection it opens and every task it spawns (a task copies
its creator's context; SQLAlchemy's async bridge runs each statement in a
greenlet that inherits its caller's context, so the listener sees it).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine

#: The list the innermost :func:`counting` block records into, in the context
#: that block runs in.
_measuring: ContextVar[list[str] | None] = ContextVar("statement_log_measuring", default=None)


def _engines_of(engine: Engine | None) -> list[Engine]:
    """The engines a request's work runs on. A denial's decision row is
    written through a pool of its own, never the request's, and it is work the
    refused request caused: a block measured on the app's shared engine counts
    the decision engine beside it, or a refusal would look cheaper than it is."""
    from alkera_core.db.session import decision_engine
    from alkera_core.db.session import engine as shared

    if engine is None or engine is shared.sync_engine:
        return [shared.sync_engine, decision_engine.sync_engine]
    return [engine]


@contextmanager
def counting(engine: Engine | None = None) -> Iterator[list[str]]:
    """Record every statement this block runs on ``engine`` (default: the app's
    shared engine, with the decision engine beside it), as sent to the server.

    ``before_cursor_execute`` fires once per statement actually sent, so the
    list is the work the block caused rather than the ORM calls it made.
    """
    engines = _engines_of(engine)
    seen: list[str] = []
    token = _measuring.set(seen)

    def record(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if _measuring.get() is seen:
            seen.append(statement)

    for each in engines:
        event.listen(each, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        for each in engines:
            event.remove(each, "before_cursor_execute", record)
        _measuring.reset(token)


__all__ = ["counting"]
