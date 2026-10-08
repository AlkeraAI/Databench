"""Columns the database still holds and no code uses any more.

A column cannot be dropped in the release that stops using it. A deploy runs
its migration first and replaces the tasks afterwards, so tasks of the release
before keep serving on the new schema until they drain. If their mapper still
knows the column, their writes name it: a nullable column with no default is in
the INSERT's column list, and one with a server default is in its RETURNING
clause. ``deferred=True`` keeps a column out of a SELECT and out of neither of
those, so a deferred column is not safe to drop either.

So a column leaves in two releases. The first declares it here: it stays in
the table's metadata, which keeps ``alembic check`` honest about what the
database holds, and it leaves the mapper, so no statement names it. The next
release drops it, while tasks that never name it drain.

A model spells it once::

    _RETIRED = (retired_column("old", BigInteger, nullable=True),)

    class Thing(Base):
        __table_args__ = (*_RETIRED, ...)
        __mapper_args__ = unmapped(_RETIRED)

A retired column must be nullable or carry a server default, because every
insert from now on leaves it for the database to fill.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Column
from sqlalchemy.types import TypeEngine

#: The ``Column.info`` key that marks a retired column.
RETIRED = "retired"


def retired_column(
    name: str,
    type_: type[TypeEngine[Any]] | TypeEngine[Any],
    *,
    nullable: bool,
    server_default: str | None = None,
) -> Column[Any]:
    """A column kept in the table and out of the mapper until a migration drops it."""
    if not nullable and server_default is None:
        raise ValueError(
            f"retired column {name!r} is NOT NULL with no server default: "
            "no insert names it, so the database must be able to fill it"
        )
    return Column(
        name, type_, nullable=nullable, server_default=server_default, info={RETIRED: True}
    )


def unmapped(columns: Sequence[Column[Any]]) -> dict[str, Any]:
    """The ``__mapper_args__`` that keep ``columns`` out of a model's mapper."""
    return {"exclude_properties": [column.name for column in columns]}


def is_retired(column: Column[Any]) -> bool:
    return bool(column.info.get(RETIRED))
