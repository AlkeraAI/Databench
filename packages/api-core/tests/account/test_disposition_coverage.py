"""Every column that names a person has a written disposition.

The registry in ``alkera_core.account.dispositions`` is the contract erasure is
held to. These cases walk the live ORM metadata, so a table added tomorrow with
a foreign key to ``users.id`` (or a column named like a person) fails here until
its author says what erasure does to it, instead of quietly surviving every
account deletion.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys

import alkera_core.models  # noqa: F401 - registers every table on the metadata
import pytest
from alkera_core.account import dispositions
from alkera_core.account.dispositions import Disposition, Kind
from alkera_core.db.base import Base
from sqlalchemy import Column

#: A column whose name says it holds a person's id.
_PERSON_SHAPED = re.compile(
    r"(^|_)(user_id|user|actor|actor_id|principal_id|holder_principal|holder_principal_id"
    r"|delegating_user|acting_principal|lease_holder)$|_by$|_by_id$|_user_id$|user"
)


def _columns() -> list[tuple[str, Column[object]]]:
    return [(t.name, c) for t in Base.metadata.tables.values() for c in t.columns]


def _references_users(column: Column[object]) -> bool:
    return any(fk.target_fullname == "users.id" for fk in column.foreign_keys)


def _fk_columns() -> list[str]:
    return sorted(f"{t}.{c.name}" for t, c in _columns() if _references_users(c))


def _person_shaped_columns() -> list[str]:
    return sorted(
        f"{t}.{c.name}"
        for t, c in _columns()
        if t != "users" and not _references_users(c) and _PERSON_SHAPED.search(c.name)
    )


REGISTRY = dispositions.registry()
NOT_A_PERSON = dispositions.not_a_person()


@pytest.mark.parametrize("column", _fk_columns())
def test_every_foreign_key_to_users_has_a_disposition(column: str) -> None:
    table, name = column.split(".")
    assert (table, name) in REGISTRY, (
        f"{column} references users.id but alkera_core.account.dispositions says nothing "
        "about what erasing an account does to it"
    )


@pytest.mark.parametrize("column", _person_shaped_columns())
def test_every_person_shaped_column_is_decided(column: str) -> None:
    table, name = column.split(".")
    assert (table, name) in REGISTRY or (table, name) in NOT_A_PERSON, (
        f"{column} reads like it holds a person; give it a disposition or list it in "
        "NOT_A_PERSON with what it holds instead"
    )


_OPEN_WALK = """
import json
import alkera_core.models
from alkera_core.db.base import Base

columns = [(t.name, c) for t in Base.metadata.tables.values() for c in t.columns]
print(json.dumps({
    "fk": sorted(
        f"{t}.{c.name}"
        for t, c in columns
        if any(fk.target_fullname == "users.id" for fk in c.foreign_keys)
    ),
    "all": sorted(f"{t}.{c.name}" for t, c in columns),
}))
"""


def test_the_walk_sees_the_tables_it_guards() -> None:
    """Guard the guards: the walk must find the known shapes, or an empty
    metadata would pass every case above vacuously. Counted on the open models
    alone, in a fresh interpreter, so the bound holds whichever other test
    files loaded private models into this process first."""
    result = subprocess.run(
        [sys.executable, "-c", _OPEN_WALK], capture_output=True, text=True, check=True
    )
    walk = json.loads(result.stdout.strip().splitlines()[-1])
    assert "workspace_objects.owner_user_id" in walk["fk"]
    assert "compute_allocations.user_id" in walk["fk"]
    assert "file_nodes.created_by" in walk["all"]
    assert len(walk["fk"]) >= 45
    # Every one of them is decided in this process too.
    assert set(walk["fk"]) <= set(_fk_columns())


@pytest.mark.parametrize(
    ("table", "column"), sorted(REGISTRY), ids=[f"{t}.{c}" for t, c in sorted(REGISTRY)]
)
def test_every_disposition_names_a_real_column(table: str, column: str) -> None:
    found = Base.metadata.tables.get(table)
    assert found is not None, f"no table {table}"
    assert column in found.columns, f"no column {table}.{column}"


@pytest.mark.parametrize(("table", "column"), sorted(NOT_A_PERSON))
def test_every_not_a_person_entry_names_a_real_column(table: str, column: str) -> None:
    found = Base.metadata.tables.get(table)
    assert found is not None and column in found.columns
    assert (table, column) not in REGISTRY


@pytest.mark.parametrize(
    "disposition",
    sorted(REGISTRY.values(), key=lambda d: d.key),
    ids=lambda d: d.key,
)
def test_every_disposition_is_applied_or_kept_with_a_reason(disposition: Disposition) -> None:
    assert disposition.why.strip()
    if disposition.kind is Kind.KEEP:
        assert disposition.step is None
    else:
        assert disposition.step is not None or disposition.stage


def test_a_kept_column_cannot_carry_a_step() -> None:
    with pytest.raises(ValueError, match="no step"):
        dispositions.register(
            Disposition(
                "users_x", "y", Kind.KEEP, "why", step=dispositions.delete_rows("users_x", "y")
            )
        )


def test_an_applied_column_needs_a_step_or_a_stage() -> None:
    with pytest.raises(ValueError, match="needs a step or a stage"):
        dispositions.register(Disposition("users_x", "y", Kind.ERASE, "why"))


def test_a_column_is_decided_once() -> None:
    table, column = next(iter(REGISTRY))
    with pytest.raises(ValueError, match="already has a disposition"):
        dispositions.register(Disposition(table, column, Kind.KEEP, "again"))


def test_money_and_audit_are_kept_or_scrubbed_never_erased() -> None:
    """The retention the law requires: ledger and audit rows outlive the person."""
    assert REGISTRY[("compute_allocations", "user_id")].kind is Kind.KEEP
    for key in (("org_audit_events", "actor_id"), ("audit_logs", "actor_id")):
        assert REGISTRY[key].kind is Kind.SCRUB


def test_credentials_are_erased_before_anything_else() -> None:
    """The session credentials sit on the first rung, and nothing on a later
    rung runs before one of them. Which other tables share that rung (a sign-in
    link, a private domain's credential) is the registry's to say."""
    ordered = list(dispositions.stepped())
    credential_tables = {"auth_tokens", "auth_refresh_tokens", "personal_access_tokens"}
    first_rung = ordered[0].order
    last_credential = max(i for i, d in enumerate(ordered) if d.table in credential_tables)
    assert {REGISTRY[(table, "user_id")].order for table in credential_tables} == {first_rung}
    assert all(d.order == first_rung for d in ordered[: last_credential + 1])
