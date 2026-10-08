"""Postgres types the Files tree needs that SQLAlchemy does not ship.

``LTREE`` is the derived subtree index: ``file_nodes.path_ids`` holds the chain
of ancestor **id labels** (never filenames, which cannot be ltree labels), so a
subtree read is one ``path_ids <@ :prefix`` against the GiST index instead of a
recursive CTE. The type is spelled once here because both the model and the
migration have to agree on the column type exactly.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.dialects.postgresql.base import ischema_names
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import UserDefinedType


class LTREE(UserDefinedType[str]):
    """The ``ltree`` column type, carried in and out of Python as ``str``."""

    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "LTREE"

    def bind_processor(self, dialect: Dialect) -> Callable[[Any], Any] | None:
        def process(value: Any) -> Any:
            return None if value is None else str(value)

        return process

    def result_processor(self, dialect: Dialect, coltype: object) -> Callable[[Any], Any] | None:
        def process(value: Any) -> Any:
            return None if value is None else str(value)

        return process


# Reflection resolves a column's type by its Postgres type name, and an
# unregistered name only warns and falls back to NullType — which would make an
# autogenerate diff on ``path_ids`` invisible. Registering it here, beside the
# type, keeps reflection and the model on the same class.
ischema_names["ltree"] = LTREE

__all__ = ["LTREE"]
