"""A foreign key between two models needs a relationship, or the flush order is luck.

SQLAlchemy's unit of work writes the rows of one flush mapper by mapper. Between
two mappers with no ``relationship()`` it does not look at foreign keys: it
orders them by module and class name. A child whose module sorts before its
parent's is inserted first and the flush fails on the foreign key; the reverse
pair deletes the parent first. Moving a model to another module, as the open
source split does, silently flips that order (it broke every test that seeded a
model and its sell price in one flush).

So every foreign key between two mapped tables whose models live in different
modules has a relationship on one side (a never-loaded ``lazy="raise"`` one is
enough), or an entry in ``fixtures/flush_order/unrelated_foreign_keys.json`` saying why it
has none. The lists hold the keys that predate this rule and may only shrink:
the open one names keys between open tables, and the product's, kept in
``fixtures/flush_order_product``, the keys that touch a private table.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.xdist_group("foreign_key_flush_order")]

FIXTURES = Path(__file__).parent / "fixtures"
#: The keys between two open tables. The open tree ships only this list.
OPEN_ALLOWLIST = FIXTURES / "flush_order" / "unrelated_foreign_keys.json"
#: The keys that touch a private table, kept with the product. Absent from the
#: open tree, where no private model loads and the walk finds none of them.
PRODUCT_ALLOWLIST = FIXTURES / "flush_order_product" / "unrelated_foreign_keys.json"

#: Runs in a fresh interpreter so the walk sees every model the deployment's
#: backend composition loads (``BACKEND_INSTALL``, the open platform's when
#: unset), whatever this test session imported first.
WALK = """
import json
import alkera_core.models
from backend.composition import install_composition

install_composition()
from alkera_core.db.base import Base
from sqlalchemy.orm import configure_mappers

configure_mappers()
mappers = {m.local_table.name: m for m in Base.registry.mappers}


def related(a, b):
    return any(r.mapper is b for r in a.relationships) or any(
        r.mapper is a for r in b.relationships
    )


unrelated = set()
for name, child in mappers.items():
    for fk in child.local_table.foreign_keys:
        parent = mappers.get(fk.column.table.name)
        if parent is None or parent is child:
            continue
        if child.class_.__module__ == parent.class_.__module__ or related(child, parent):
            continue
        unrelated.add(f"{name}.{fk.parent.name} -> {fk.column.table.name}")
print(json.dumps({"unrelated": sorted(unrelated), "tables": sorted(mappers)}))
"""


@pytest.fixture(scope="module")
def walk() -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, "-c", WALK], capture_output=True, text=True, timeout=300, check=False
    )
    assert result.returncode == 0, result.stderr[-4000:]
    found: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    return found


def _read(path: Path) -> dict[str, str]:
    """Each key a list allows and the reason its group gives."""
    groups: dict[str, dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    return {key: group["why"] for group in groups.values() for key in group["keys"]}


def _allowed() -> dict[str, str]:
    """The open list, and the product's when the product is present."""
    allowed = _read(OPEN_ALLOWLIST)
    if PRODUCT_ALLOWLIST.exists():
        allowed |= _read(PRODUCT_ALLOWLIST)
    return allowed


def test_the_walk_finds_models_and_their_keys(walk: dict[str, Any]) -> None:
    assert {"users", "teams", "workspace_objects"} <= set(walk["tables"])
    assert len(walk["tables"]) > 80


def test_every_allowed_key_carries_a_reason() -> None:
    assert all(why.strip() for why in _allowed().values())


def test_a_foreign_key_across_modules_has_a_relationship_or_a_reason(
    walk: dict[str, Any],
) -> None:
    new = sorted(set(walk["unrelated"]) - set(_allowed()))
    assert new == [], (
        "these foreign keys join models in different modules with no relationship(), so "
        "their insert and delete order rests on module names; declare a never-loaded "
        'relationship(lazy="raise") on the child'
    )


def test_the_allowlist_holds_only_keys_that_still_lack_a_relationship(
    walk: dict[str, Any],
) -> None:
    loaded = set(walk["tables"])
    stale = sorted(
        key for key in set(_allowed()) - set(walk["unrelated"]) if key.split(".", 1)[0] in loaded
    )
    assert stale == [], "these keys have a relationship now; remove them from the allowlist"


def test_the_open_list_names_no_private_table() -> None:
    """The open list ships in the open tree, so every table it names is one the
    open models alone define."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, alkera_core.models\n"
            "from alkera_core.db.base import Base\n"
            "print(json.dumps(sorted(Base.metadata.tables)))",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
    )
    open_tables = set(json.loads(result.stdout.strip().splitlines()[-1]))
    named = {
        table
        for key in _read(OPEN_ALLOWLIST)
        for table in (key.split(".", 1)[0], key.split(" -> ", 1)[1])
    }
    assert sorted(named - open_tables) == []
