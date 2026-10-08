"""The migrations registry: keyed by source major version, append only."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from alkera_notebook.format import migrations


@pytest.fixture
def registry() -> Iterator[dict[str, migrations.Migration]]:
    saved = dict(migrations._MIGRATIONS)
    yield migrations._MIGRATIONS
    migrations._MIGRATIONS.clear()
    migrations._MIGRATIONS.update(saved)


def test_format_one_has_no_migrations() -> None:
    assert dict(migrations.MIGRATIONS) == {}


def test_migrations_chain_in_order(registry: dict[str, migrations.Migration]) -> None:
    migrations.register("1")(lambda text: text + "[1->2]")
    migrations.register("2")(lambda text: text + "[2->3]")
    assert migrations.migrate("x", 1, 3) == "x[1->2][2->3]"
    assert migrations.migrate("x", 2, 3) == "x[2->3]"
    assert migrations.migrate("x", 3, 3) == "x"


def test_a_gap_in_the_chain_is_an_error(registry: dict[str, migrations.Migration]) -> None:
    migrations.register("1")(lambda text: text)
    with pytest.raises(LookupError, match="from format 2"):
        migrations.migrate("x", 1, 3)


def test_a_migration_cannot_be_replaced(registry: dict[str, migrations.Migration]) -> None:
    migrations.register("1")(lambda text: text)
    with pytest.raises(ValueError, match="already registered"):
        migrations.register("1")(lambda text: text)


@pytest.mark.parametrize("key", ["1.0", "v1", ""])
def test_keys_are_major_versions(key: str) -> None:
    with pytest.raises(ValueError, match="major version"):
        migrations.register(key)


def test_the_public_view_is_read_only() -> None:
    with pytest.raises(TypeError):
        migrations.MIGRATIONS["1"] = lambda text: text  # type: ignore[index]
