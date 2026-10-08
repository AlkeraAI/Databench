"""A retired column stays in the table and leaves the mapper.

That is what lets a later migration drop it while tasks of this release are
still serving: a column the mapper knows is named by every insert (in the
column list, or in RETURNING when it has a server default), deferred or not.
A separate migration test proves against a real database that such a drop
is survivable.
"""

from __future__ import annotations

import alkera_core.models  # noqa: F401  registers every model on the metadata
import pytest
from alkera_core.db.base import Base
from alkera_core.db.retired import is_retired, retired_column, unmapped
from sqlalchemy import BigInteger, Column, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: Every retired column the schema holds today. A new one is added here with
#: the release that retires it, and removed with the migration that drops it.
RETIRED_TODAY = {
    ("org_settings", "gate_pr_budget_nanos"),
    ("org_settings", "gate_monthly_budget_nanos"),
    ("gate_runs", "spend_nanos"),
    ("connection_verifications", "vantage_kind"),
}


def _table_columns_by_mapping() -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """(marked retired, absent from the mapper) across every mapped table."""
    marked: set[tuple[str, str]] = set()
    absent: set[tuple[str, str]] = set()
    for mapper in Base.registry.mappers:
        table = mapper.local_table
        mapped = {column.name for column in mapper.columns}
        for column in table.columns:
            if not isinstance(column, Column):
                continue
            key = (str(table.name), column.name)
            if is_retired(column):
                marked.add(key)
            if column.name not in mapped:
                absent.add(key)
    return marked, absent


def test_the_retired_columns_are_the_ones_expected() -> None:
    marked, _ = _table_columns_by_mapping()
    assert marked == RETIRED_TODAY


def test_every_retired_column_is_out_of_its_mapper_and_nothing_else_is() -> None:
    """Marked and unmapped are the same set: a retired column left in the mapper
    is still named by every insert, and a column quietly excluded without the
    mark is one nobody will remember to drop."""
    marked, absent = _table_columns_by_mapping()
    assert absent == marked


def test_a_retired_column_stays_in_the_table_metadata() -> None:
    for table_name, column_name in RETIRED_TODAY:
        assert column_name in Base.metadata.tables[table_name].columns


def test_the_helpers_keep_a_column_in_the_table_and_out_of_the_mapper() -> None:
    class Scratch(DeclarativeBase):
        pass

    retired = (
        retired_column("old_nullable", BigInteger, nullable=True),
        retired_column("old_defaulted", String(16), nullable=False, server_default="x"),
    )

    class Thing(Scratch):
        __tablename__ = "things"
        __table_args__ = (*retired,)
        __mapper_args__ = unmapped(retired)

        id: Mapped[int] = mapped_column(primary_key=True)
        kept_nullable: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
        kept_defaulted: Mapped[str] = mapped_column(String(16), server_default="y")

    mapper = Thing.__mapper__
    assert {column.name for column in mapper.columns} == {
        "id",
        "kept_nullable",
        "kept_defaulted",
    }
    assert {column.name for column in Thing.__table__.columns} == {
        "id",
        "kept_nullable",
        "kept_defaulted",
        "old_nullable",
        "old_defaulted",
    }
    assert not hasattr(Thing, "old_nullable")
    assert not hasattr(Thing, "old_defaulted")


@pytest.mark.parametrize(
    ("nullable", "server_default", "refused"),
    [
        pytest.param(True, None, False, id="nullable"),
        pytest.param(False, "0", False, id="not-null-with-a-default"),
        pytest.param(True, "0", False, id="nullable-with-a-default"),
        pytest.param(False, None, True, id="not-null-and-nothing-to-fill-it"),
    ],
)
def test_a_retired_column_must_be_one_the_database_can_fill(
    nullable: bool, server_default: str | None, refused: bool
) -> None:
    if refused:
        with pytest.raises(ValueError, match="NOT NULL with no server default"):
            retired_column("old", BigInteger, nullable=nullable, server_default=server_default)
        return
    column = retired_column("old", BigInteger, nullable=nullable, server_default=server_default)
    assert is_retired(column)
    assert column.nullable is nullable


def test_an_ordinary_column_is_not_retired() -> None:
    assert not is_retired(Column("live", BigInteger))
