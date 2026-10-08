"""Format migrations: pure functions that lift a file from one major version.

A migration is keyed by the major version it reads (``"1"``) and returns the
text of the next major version. Readers apply the chain before parsing a file
older than :data:`~alkera_notebook.format.ir.FORMAT_VERSION`. Entries are never
removed: they are the record of how the format evolved. Format 1.0 is the
first version, so the registry is empty.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType

Migration = Callable[[str], str]

_MIGRATIONS: dict[str, Migration] = {}
MIGRATIONS: Mapping[str, Migration] = MappingProxyType(_MIGRATIONS)


def register(source_major: str) -> Callable[[Migration], Migration]:
    """Register the migration that reads major version ``source_major``."""
    if not source_major.isdigit():
        raise ValueError(f"a migration is keyed by a major version, got {source_major!r}")

    def decorate(migration: Migration) -> Migration:
        if source_major in _MIGRATIONS:
            raise ValueError(f"a migration from format {source_major} is already registered")
        _MIGRATIONS[source_major] = migration
        return migration

    return decorate


def migrate(text: str, source_major: int, target_major: int) -> str:
    """Apply the registered migrations from ``source_major`` up to ``target_major``."""
    for major in range(source_major, target_major):
        migration = _MIGRATIONS.get(str(major))
        if migration is None:
            raise LookupError(f"no migration from format {major}")
        text = migration(text)
    return text
