"""The schema head the build expects is the head of the chain it migrates with.

Deployment health compares the database's revision with
``EXPECTED_SCHEMA_HEAD``. A migration that lands without bumping it makes
every freshly migrated install report "this build expects" an older revision.
Each migration's own test only pinned ``>=``, which a forgotten bump passes.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from alkera_core.db.schema_head import EXPECTED_SCHEMA_HEAD

BACKEND = Path(__file__).resolve().parents[1]


def test_the_expected_schema_head_is_the_migration_chains_head() -> None:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    assert ScriptDirectory.from_config(config).get_heads() == [EXPECTED_SCHEMA_HEAD]
