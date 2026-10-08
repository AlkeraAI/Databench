"""Every revision after the rule declares which chain it joins at the split.

The checks read revision source, so they run on planted text as well as the
real chain. Which side each real table is on comes from the open/private
manifest, in ``test_migration_side_tables.py``.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from backend import migration_side
from backend.migration_side import read_revision, side_problems
from mako.template import Template

_BACKEND = Path(__file__).resolve().parents[1]


def _script_directory() -> ScriptDirectory:
    config = Config(str(_BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND / "alembic"))
    return ScriptDirectory.from_config(config)


def _revision(body: str, *, revision: str = "0300", side: str | None = "open") -> str:
    header = f'revision: str = "{revision}"\ndown_revision: str | None = "0299"\n'
    declared = f'SIDE: str = "{side}"\n' if side is not None else ""
    return header + declared + textwrap.dedent(body)


TABLES = {"users": "open", "teams": "open", "billing_accounts": "private", "gate_runs": "private"}


# --- the real chain ---------------------------------------------------------------


def test_every_revision_after_the_rule_declares_a_side() -> None:
    script = _script_directory()
    missing = sorted(
        f"{rev.revision}: {problem}"
        for rev in script.walk_revisions()
        if migration_side.needs_side(rev.revision)
        for problem in side_problems(read_revision(Path(rev.path).read_text(encoding="utf-8")), {})
    )
    assert not missing, "\n".join(missing)


def test_the_last_unsided_revision_is_in_the_chain() -> None:
    """The cut point names a real revision, so the rule cannot start past the
    head by a typo and check nothing."""
    script = _script_directory()
    assert script.get_revision(migration_side.LAST_UNSIDED_REVISION) is not None


def test_the_template_makes_a_new_revision_choose_a_side() -> None:
    """``make migrate-create`` writes a placeholder the check refuses, so a new
    revision cannot pass until someone picks its side."""
    template = Template(filename=str(_BACKEND / "alembic" / "script.py.mako"))
    rendered = template.render(
        message="m",
        up_revision="0300",
        down_revision="0299",
        branch_labels=None,
        depends_on=None,
        create_date="2026-10-06",
        imports="",
        upgrades="",
        downgrades="",
        comma=", ".join,
    )
    facts = read_revision(rendered)
    assert facts.side is not None
    assert side_problems(facts, {}) != []


# --- reading a revision ------------------------------------------------------------


@pytest.mark.parametrize(
    ("revision", "needed"),
    [
        pytest.param("0220", False, id="the-last-unsided"),
        pytest.param("0001", False, id="the-first"),
        pytest.param("0221", True, id="the-next"),
        pytest.param("1000", True, id="far-past"),
    ],
)
def test_which_revisions_need_a_side(revision: str, needed: bool) -> None:
    assert migration_side.needs_side(revision) is needed


@pytest.mark.parametrize(
    ("source", "side"),
    [
        pytest.param('SIDE: str = "open"\n', "open", id="annotated"),
        pytest.param('SIDE = "private"\n', "private", id="plain"),
        pytest.param('def f():\n    SIDE = "open"\n', None, id="not-at-module-level"),
        pytest.param("", None, id="absent"),
    ],
)
def test_the_side_is_read_from_a_module_level_assignment(source: str, side: str | None) -> None:
    assert read_revision(source).side == side


@pytest.mark.parametrize(
    ("call", "table"),
    [
        pytest.param('op.create_table("t", sa.Column("id"))', "t", id="create_table"),
        pytest.param('op.drop_table("t")', "t", id="drop_table"),
        pytest.param('op.add_column("t", sa.Column("x"))', "t", id="add_column"),
        pytest.param('op.alter_column("t", "x", nullable=True)', "t", id="alter_column"),
        pytest.param('op.drop_column(table_name="t", column_name="x")', "t", id="keyword"),
        pytest.param('op.create_index("ix", "t", ["x"])', "t", id="create_index"),
        pytest.param('op.drop_index("ix", table_name="t")', "t", id="drop_index"),
        pytest.param('op.create_foreign_key("fk", "t", "users", ["u"], ["id"])', "t", id="fk"),
        pytest.param('op.drop_constraint("ck", "t")', "t", id="drop_constraint"),
        pytest.param('with op.batch_alter_table("t") as b:\n    pass', "t", id="batch"),
        pytest.param('add_check_not_valid("t", "ck", "x > 0")', "t", id="safety-check"),
        pytest.param('create_index_concurrently("ix", "t", "x")', "t", id="safety-index"),
        pytest.param('in_batches("t", "id", "UPDATE t SET x = 1")', "t", id="safety-batches"),
    ],
)
def test_the_table_a_call_changes_is_found(call: str, table: str) -> None:
    facts = read_revision(f"def upgrade():\n{textwrap.indent(call, '    ')}\n")
    assert facts.changed_tables == {table}


def test_a_foreign_key_target_is_not_a_changed_table() -> None:
    facts = read_revision('op.create_foreign_key("fk", "t", "users", ["u"], ["id"])\n')
    assert "users" not in facts.changed_tables


def test_a_rename_changes_both_names() -> None:
    assert read_revision('op.rename_table("old", "new")\n').changed_tables == {"old", "new"}


# --- the verdict ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "side"),
    [
        pytest.param('op.add_column("users", sa.Column("x"))\n', "open", id="open-changes-open"),
        pytest.param(
            'op.create_table("open_new", sa.Column("u", sa.ForeignKey("users.id")))\n',
            "open",
            id="open-creates-a-new-table",
        ),
        pytest.param(
            'op.add_column("billing_accounts", sa.Column("x"))\n',
            "private",
            id="private-changes-private",
        ),
        pytest.param(
            'op.create_table("gate_extra", sa.Column("u", sa.ForeignKey("users.id")))\n',
            "private",
            id="private-points-into-open",
        ),
        pytest.param(
            'op.execute("UPDATE billing_accounts b SET x = 1 FROM users u WHERE u.id = b.u")\n',
            "private",
            id="private-sql-reads-open",
        ),
        pytest.param(
            '"""Unlike billing_accounts, this touches users only."""\n'
            'op.add_column("users", sa.Column("x"))\n',
            "open",
            id="a-docstring-is-not-code",
        ),
    ],
)
def test_a_one_sided_revision_passes(body: str, side: str) -> None:
    assert side_problems(read_revision(_revision(body, side=side)), TABLES) == []


@pytest.mark.parametrize(
    ("body", "side", "problem"),
    [
        pytest.param(
            'op.add_column("billing_accounts", sa.Column("x"))\n',
            "open",
            "an open revision changes the private table 'billing_accounts'",
            id="open-changes-private",
        ),
        pytest.param(
            'op.add_column("users", sa.Column("x"))\n',
            "private",
            "a private revision changes the open table 'users'",
            id="private-changes-open",
        ),
        pytest.param(
            'op.execute("DELETE FROM gate_runs WHERE true")\n',
            "open",
            "an open revision names the private table 'gate_runs'",
            id="open-sql-names-private",
        ),
        pytest.param(
            'op.execute(f"UPDATE public.gate_runs SET x = {X}")\n',
            "open",
            "an open revision names the private table 'gate_runs'",
            id="open-f-string-names-private",
        ),
        pytest.param(
            'op.add_column("users", sa.Column("b", sa.ForeignKey("billing_accounts.id")))\n',
            "open",
            "an open revision names the private table 'billing_accounts'",
            id="open-points-into-private",
        ),
    ],
)
def test_a_revision_that_reaches_the_other_side_is_refused(
    body: str, side: str, problem: str
) -> None:
    assert side_problems(read_revision(_revision(body, side=side)), TABLES) == [problem]


@pytest.mark.parametrize(
    ("side", "message"),
    [
        pytest.param(None, "declares no SIDE", id="missing"),
        pytest.param("both", "SIDE is 'both'", id="unknown-value"),
        pytest.param("Open", "SIDE is 'Open'", id="case-matters"),
    ],
)
def test_a_missing_or_unknown_side_is_refused(side: str | None, message: str) -> None:
    problems = side_problems(read_revision(_revision("", side=side)), TABLES)
    assert len(problems) == 1
    assert problems[0].startswith(message)
