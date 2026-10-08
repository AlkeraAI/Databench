"""A revision that takes something away names the expand it completes.

The previous release keeps reading and writing the database while a deploy
replaces it, so a column, table or constraint may go only after an earlier
revision moved that release off it. ``backend.migration_contract`` reads each
revision for what its ``upgrade()`` removes or tightens; this module runs it
over every revision numbered above the head the rule landed at, and over
planted revisions that break it each way.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from backend.migration_contract import (
    RevisionHeader,
    ancestors,
    contracting_operations,
    lint_versions,
    read_header,
)

pytestmark = [pytest.mark.spread]

_VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
#: The head when the rule landed: every revision numbered above it is held to it.
_RULE_AFTER = 204

_REMEDY = (
    "a previous release still reads and writes this schema while it is replaced: say "
    'which earlier revision moved it off what this one removes (contract_of = "NNNN"), '
    "or ship that expand first"
)


def _revision(
    revision: str, down: str | None, upgrade: str, *, extra: str = "", downgrade: str = "pass"
) -> str:
    lines = [
        '"""planted"""',
        "from alembic import op",
        "import sqlalchemy as sa",
        f'revision: str = "{revision}"',
        f"down_revision: str | None = {down!r}",
        *([extra] if extra else []),
        "def upgrade() -> None:",
        *(f"    {line}" for line in upgrade.splitlines()),
        "def downgrade() -> None:",
        *(f"    {line}" for line in downgrade.splitlines()),
        "",
    ]
    return "\n".join(lines)


def _plant(tmp_path: Path, **sources: str) -> Path:
    for name, source in sources.items():
        (tmp_path / f"{name}.py").write_text(source, encoding="utf-8")
    return tmp_path


_ADD = 'op.add_column("things", sa.Column("label", sa.Text(), nullable=True))'
_DROP = 'op.drop_column("things", "legacy")'


#: Revisions that take something away with no expand before them, each with
#: why it is allowed for now. Pinned exactly: a split revision leaves this list
#: and the test fails until its entry goes.
UNSPLIT: dict[str, str] = {
    # Databench ships fresh installs only. 0220 must be split into an expand
    # (the link tables and the copy) and a contract (the dropped columns)
    # before this line is promoted to staging or prod, where a rolling deploy
    # still runs 0219's code against the dropped columns.
    "0220": "moves compute funding into billing's link tables; split before promotion",
}


def test_every_revision_since_the_rule_names_its_expand() -> None:
    found = lint_versions(_VERSIONS, above=_RULE_AFTER)
    assert {name.split("_", 1)[0] for name in found} == set(UNSPLIT), found


def test_the_scan_reads_the_real_tree() -> None:
    """The reader sees real revisions: a known header, a known drop, and the
    whole chain behind the head the rule landed at."""
    head = next(_VERSIONS.glob("0204_*.py")).read_text(encoding="utf-8")
    assert read_header(head) == RevisionHeader(revision="0204", parents=("0203",), contract_of=None)
    dropped = (_VERSIONS / "0082_drop_team_connection_max_effect.py").read_text(encoding="utf-8")
    assert contracting_operations(dropped) == ["line 21: op.drop_column() takes something away"]
    parents = {
        header.revision: header.parents
        for path in _VERSIONS.glob("*.py")
        if (header := read_header(path.read_text(encoding="utf-8"))).revision
    }
    behind = ancestors("0204", parents)
    assert {"0001", "0082", "0203"} <= behind
    assert "0204" not in behind
    assert len(behind) == 203


@pytest.mark.parametrize(
    ("upgrade", "expected"),
    [
        pytest.param(_DROP, ["line 7: op.drop_column() takes something away"], id="drop-column"),
        pytest.param(
            'op.drop_table("things")', ["line 7: op.drop_table() drops a table"], id="drop-table"
        ),
        pytest.param(
            'op.drop_constraint("ck_things_size", "things")',
            ["line 7: op.drop_constraint() takes something away"],
            id="drop-constraint",
        ),
        pytest.param(
            'op.rename_table("things", "items")',
            ["line 7: op.rename_table() takes something away"],
            id="rename-table",
        ),
        pytest.param(
            'op.alter_column("things", "label", nullable=False)',
            ["line 7: op.alter_column(nullable=False) makes a column required"],
            id="set-not-null",
        ),
        pytest.param(
            'op.alter_column("things", "label", new_column_name="title")',
            ["line 7: op.alter_column(new_column_name=...) renames a column"],
            id="rename-column",
        ),
        pytest.param(
            'op.add_column("things", sa.Column("size", sa.Integer(), nullable=False))',
            ["line 7: op.add_column() adds a required column with no server default"],
            id="required-column-no-default",
        ),
        pytest.param(
            'op.execute("ALTER TABLE things DROP COLUMN IF EXISTS legacy")',
            ["line 7: SQL drops a column"],
            id="sql-drop-column",
        ),
        pytest.param(
            'op.execute("ALTER TABLE things ALTER COLUMN label SET NOT NULL")',
            ["line 7: SQL makes a column required"],
            id="sql-set-not-null",
        ),
        pytest.param(
            'op.execute("ALTER TABLE things RENAME COLUMN label TO title")',
            ["line 7: SQL renames"],
            id="sql-rename-column",
        ),
        pytest.param(
            'op.execute("ALTER TABLE things DROP CONSTRAINT ck_things_size")',
            ["line 7: SQL drops a constraint"],
            id="sql-drop-constraint",
        ),
        pytest.param(
            'op.execute("DROP TABLE IF EXISTS things")',
            ["line 7: SQL drops a table"],
            id="sql-drop-table",
        ),
        pytest.param(
            'drop_constraint_if_exists("things", "ck_things_size")',
            ["line 7: drop_constraint_if_exists() drops a constraint"],
            id="helper-drop-constraint",
        ),
        pytest.param(
            'with op.batch_alter_table("things") as batch_op:\n    batch_op.drop_column("legacy")',
            ["line 8: op.drop_column() takes something away"],
            id="batch-drop-column",
        ),
        pytest.param(
            f"{_ADD}\n{_DROP}\nop.execute(sa.text('ALTER TABLE things DROP CONSTRAINT ck_a'))",
            [
                "line 8: op.drop_column() takes something away",
                "line 9: SQL drops a constraint",
            ],
            id="several-in-source-order",
        ),
        pytest.param(
            'op.create_table("scratch")\nop.drop_table("things")',
            ["line 8: op.drop_table() drops a table"],
            id="another-table-than-the-scratch-one",
        ),
    ],
)
def test_what_takes_something_away_is_named(upgrade: str, expected: list[str]) -> None:
    assert contracting_operations(_revision("0300", "0299", upgrade)) == expected


@pytest.mark.parametrize(
    "upgrade",
    [
        pytest.param(_ADD, id="nullable-column"),
        pytest.param(
            'op.add_column("things", sa.Column("size", sa.Integer(), nullable=False, '
            'server_default="0"))',
            id="required-column-with-default",
        ),
        pytest.param('op.create_table("items", sa.Column("id", sa.Integer()))', id="new-table"),
        pytest.param('op.create_index("ix_things_label", "things", ["label"])', id="new-index"),
        pytest.param('op.alter_column("things", "label", nullable=True)', id="made-optional"),
        pytest.param(
            'op.alter_column("things", "label", server_default="x")', id="default-changed"
        ),
        pytest.param(
            'op.create_table("scratch", sa.Column("id", sa.Integer()))\nop.drop_table("scratch")',
            id="scratch-table-made-and-dropped",
        ),
        pytest.param(
            'op.execute("CREATE TEMP TABLE scratch AS SELECT 1")\n'
            'op.execute("DROP TABLE IF EXISTS scratch")',
            id="sql-scratch-table-made-and-dropped",
        ),
        pytest.param("op.execute(\"UPDATE things SET label = 'dropped column'\")", id="data-only"),
        pytest.param(
            'op.execute("ALTER TABLE things ALTER COLUMN label DROP NOT NULL")',
            id="sql-made-optional",
        ),
    ],
)
def test_what_only_adds_or_loosens_is_not(upgrade: str) -> None:
    assert contracting_operations(_revision("0300", "0299", upgrade)) == []


_ADD_SQL = 'op.execute("ALTER TABLE things ADD COLUMN IF NOT EXISTS label TEXT")'
_DEFAULT_SQL = "op.execute(\"ALTER TABLE things ALTER COLUMN label SET DEFAULT 'x'\")"
_REQUIRE_SQL = 'op.execute("ALTER TABLE things ALTER COLUMN label SET NOT NULL")'
_SCAFFOLD = 'add_check_not_valid("things", "ck_things_label_set", "label IS NOT NULL")'
_OWN_FK_SQL = (
    'op.execute("ALTER TABLE things ADD CONSTRAINT fk_things_label '
    'FOREIGN KEY (label) REFERENCES labels (id) NOT VALID")'
)


@pytest.mark.parametrize(
    "upgrade",
    [
        pytest.param(f'{_ADD}\nop.drop_column("things", "label")', id="column-added-then-dropped"),
        pytest.param(
            f'{_ADD_SQL}\nop.execute("ALTER TABLE things DROP COLUMN IF EXISTS label")',
            id="sql-column-added-then-dropped",
        ),
        pytest.param(
            'op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN label TEXT")\n'
            'op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN {_COLUMN}")',
            id="table-and-column-held-in-module-names",
        ),
        pytest.param(
            f"{_ADD_SQL}\n{_DEFAULT_SQL}\n{_REQUIRE_SQL}", id="sql-defaulted-then-required"
        ),
        pytest.param(
            "op.execute(\"ALTER TABLE things ADD COLUMN label TEXT DEFAULT 'x'\")\n" + _REQUIRE_SQL,
            id="sql-added-with-a-default-then-required",
        ),
        pytest.param(
            f'{_ADD}\nop.alter_column("things", "label", server_default="x")\n'
            'op.alter_column("things", "label", nullable=False)',
            id="defaulted-then-required",
        ),
        pytest.param(
            f'{_ADD_SQL}\n{_SCAFFOLD}\ndrop_constraint_if_exists("things", "ck_things_label_set")',
            id="scaffold-check-on-its-own-column",
        ),
        pytest.param(
            f'{_ADD_SQL}\ndrop_constraint_if_exists("things", "fk_things_label")\n{_OWN_FK_SQL}',
            id="own-constraint-cleared-before-it-is-added",
        ),
        pytest.param(
            f'{_ADD_SQL}\nop.execute("ALTER TABLE things DROP CONSTRAINT IF EXISTS '
            f'fk_things_label")\n{_OWN_FK_SQL}',
            id="sql-own-constraint-cleared-before-it-is-added",
        ),
        pytest.param(
            f'{_ADD}\nop.create_check_constraint("ck_things_label", "things", "label <> \'\'")\n'
            'op.drop_constraint("ck_things_label", "things")',
            id="op-check-on-its-own-column",
        ),
    ],
)
def test_an_upgrade_may_take_away_what_it_added_itself(upgrade: str) -> None:
    extra = '_TABLE = "things"\n_COLUMN = "label"'
    assert contracting_operations(_revision("0300", "0299", upgrade, extra=extra)) == []


@pytest.mark.parametrize(
    ("upgrade", "expected"),
    [
        pytest.param(
            f"{_ADD}\n{_DROP}",
            ["line 8: op.drop_column() takes something away"],
            id="another-column-than-the-added-one",
        ),
        pytest.param(
            f'{_ADD}\nop.drop_column("items", "label")',
            ["line 8: op.drop_column() takes something away"],
            id="the-same-column-name-on-another-table",
        ),
        pytest.param(
            f'{_ADD_SQL}\nop.execute("ALTER TABLE things DROP COLUMN label, DROP COLUMN legacy")',
            ["line 8: SQL drops a column"],
            id="sql-its-own-column-and-another-in-one-statement",
        ),
        pytest.param(
            f"{_ADD_SQL}\n{_REQUIRE_SQL}",
            ["line 8: SQL makes a column required"],
            id="sql-required-with-no-default",
        ),
        pytest.param(
            f'{_ADD}\nop.alter_column("things", "label", nullable=False)',
            ["line 8: op.alter_column(nullable=False) makes a column required"],
            id="required-with-no-default",
        ),
        pytest.param(
            f"{_DEFAULT_SQL}\n{_REQUIRE_SQL}",
            ["line 8: SQL makes a column required"],
            id="sql-defaulted-and-required-but-not-added-here",
        ),
        pytest.param(
            f'{_ADD_SQL}\n{_SCAFFOLD}\ndrop_constraint_if_exists("things", "ck_things_size")',
            ["line 9: drop_constraint_if_exists() drops a constraint"],
            id="another-constraint-than-the-scaffold",
        ),
        pytest.param(
            f'{_ADD_SQL}\n{_SCAFFOLD}\ndrop_constraint_if_exists("items", "ck_things_label_set")',
            ["line 9: drop_constraint_if_exists() drops a constraint"],
            id="the-same-constraint-name-on-another-table",
        ),
        pytest.param(
            'drop_constraint_if_exists("things", "ck_things_size")\n'
            'add_check_not_valid("things", "ck_things_size", "size > 0")',
            ["line 7: drop_constraint_if_exists() drops a constraint"],
            id="a-constraint-replaced-over-a-column-that-was-there",
        ),
        pytest.param(
            f'{_ADD_SQL}\nop.drop_constraint("uq_things_size", "things")\n'
            'op.create_unique_constraint("uq_things_size", "things", ["size"])',
            ["line 8: op.drop_constraint() takes something away"],
            id="a-key-replaced-beside-an-unrelated-new-column",
        ),
        pytest.param(
            f'{_ADD_SQL}\nop.drop_constraint("fk_things_owner", "things")\n'
            'op.create_foreign_key("fk_things_owner", "things", "label", ["owner"], ["id"])',
            ["line 8: op.drop_constraint() takes something away"],
            id="a-foreign-key-whose-target-is-named-like-the-new-column",
        ),
        pytest.param(
            'op.execute(f"ALTER TABLE {table_for()} ADD COLUMN label TEXT")\n'
            'op.execute(f"ALTER TABLE {table_for()} DROP COLUMN label")',
            ["line 8: SQL drops a column"],
            id="a-table-the-source-does-not-spell",
        ),
        pytest.param(
            'op.execute(f"ALTER TABLE things ADD COLUMN IF NOT EXISTS {column_for()} TEXT")\n'
            'op.execute(f"ALTER TABLE things DROP COLUMN IF EXISTS {column_for()}")',
            ["line 8: SQL drops a column"],
            id="a-column-the-source-does-not-spell",
        ),
        pytest.param(
            'with op.batch_alter_table("things") as batch_op:\n'
            '    batch_op.add_column(sa.Column("label", sa.Text()))\n'
            '    batch_op.drop_column("label")',
            ["line 9: op.drop_column() takes something away"],
            id="a-batch-names-no-table-on-the-call",
        ),
    ],
)
def test_adding_one_thing_excuses_taking_away_only_that_thing(
    upgrade: str, expected: list[str]
) -> None:
    assert contracting_operations(_revision("0300", "0299", upgrade)) == expected


def test_the_revisions_that_finish_their_own_additions_are_read_that_way() -> None:
    """The two shapes in the real tree: a foreign key on a new column cleared
    before it is added so a second run passes, and a new column back-filled,
    proven by a scaffold CHECK, made required and the scaffold dropped."""
    for name in ("0206_refresh_single_successor.py", "0208_idempotency_scopes.py"):
        assert contracting_operations((_VERSIONS / name).read_text(encoding="utf-8")) == []


def test_a_downgrade_may_take_away_what_its_upgrade_added() -> None:
    source = _revision("0300", "0299", _ADD, downgrade='op.drop_column("things", "label")')
    assert contracting_operations(source) == []


def test_sql_held_in_a_module_constant_is_read() -> None:
    source = _revision(
        "0300",
        "0299",
        "op.execute(_TIGHTEN)",
        extra=(
            '_TABLE = "things"\n_TIGHTEN = f"ALTER TABLE {_TABLE} ALTER COLUMN label SET NOT NULL"'
        ),
    )
    assert contracting_operations(source) == ["line 9: SQL makes a column required"]


def test_contracting_revision_needs_expand(tmp_path: Path) -> None:
    versions = _plant(
        tmp_path,
        **{
            "0300_expand": _revision("0300", "0204", _ADD),
            "0301_other": _revision("0301", "0300", _ADD),
            "0302_contract": _revision("0302", "0301", _DROP),
        },
    )
    assert lint_versions(versions, above=_RULE_AFTER) == {
        "0302_contract.py": ["line 7: op.drop_column() takes something away", _REMEDY]
    }


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        pytest.param("0300", {}, id="an-ancestor"),
        pytest.param("0301", {}, id="its-parent"),
        pytest.param(
            "0302",
            {"0302_contract.py": ['contract_of = "0302" is not a revision this one descends from']},
            id="itself",
        ),
        pytest.param(
            "0303",
            {"0302_contract.py": ['contract_of = "0303" is not a revision this one descends from']},
            id="a-later-revision",
        ),
        pytest.param(
            "0999",
            {"0302_contract.py": ['contract_of = "0999" is not a revision this one descends from']},
            id="no-such-revision",
        ),
    ],
)
def test_the_named_expand_is_a_revision_behind_it(
    tmp_path: Path, declared: str, expected: dict[str, list[str]]
) -> None:
    versions = _plant(
        tmp_path,
        **{
            "0300_expand": _revision("0300", "0204", _ADD),
            "0301_other": _revision("0301", "0300", _ADD),
            "0302_contract": _revision("0302", "0301", _DROP, extra=f'contract_of = "{declared}"'),
            "0303_later": _revision("0303", "0302", _ADD),
        },
    )
    assert lint_versions(versions, above=_RULE_AFTER) == expected


def test_an_expand_reached_through_a_merge_counts(tmp_path: Path) -> None:
    merge = _revision("0302", None, _DROP, extra='contract_of = "0300"').replace(
        "down_revision: str | None = None", 'down_revision = ("0300", "0301")'
    )
    versions = _plant(
        tmp_path,
        **{
            "0300_left": _revision("0300", "0204", _ADD),
            "0301_right": _revision("0301", "0204", _ADD),
            "0302_merge": merge,
        },
    )
    assert lint_versions(versions, above=_RULE_AFTER) == {}


def test_a_declaration_with_nothing_taken_away_is_refused(tmp_path: Path) -> None:
    versions = _plant(
        tmp_path,
        **{
            "0300_expand": _revision("0300", "0204", _ADD),
            "0301_claims": _revision("0301", "0300", _ADD, extra='contract_of = "0300"'),
        },
    )
    assert lint_versions(versions, above=_RULE_AFTER) == {
        "0301_claims.py": [
            'declares contract_of = "0300" but upgrade() takes nothing away; remove the declaration'
        ]
    }


def test_revisions_from_before_the_rule_are_left_alone(tmp_path: Path) -> None:
    versions = _plant(
        tmp_path,
        **{
            "0204_old": _revision("0204", "0203", _DROP),
            "0205_new": _revision("0205", "0204", _DROP),
        },
    )
    assert list(lint_versions(versions, above=_RULE_AFTER)) == ["0205_new.py"]
