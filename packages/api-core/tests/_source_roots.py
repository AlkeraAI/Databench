"""The shipped Python source roots a source ratchet walks.

Every top-level package directly under ``apps/<app>/`` and
``packages/<package>/``, test and Alembic trees aside: the open tree's own
packages, and in a composed checkout the product's too, with no list to keep
in step with either.
"""

from __future__ import annotations

from pathlib import Path

#: Package-shaped directories that ship no product code.
NOT_SHIPPED = frozenset({"tests", "scripts", "alembic", "conftest_layers"})


def shipped_source_roots(repo_root: Path) -> list[str]:
    roots = {
        init.parent.relative_to(repo_root).as_posix()
        for base in ("apps", "packages")
        for init in (repo_root / base).glob("*/*/__init__.py")
        if init.parent.name not in NOT_SHIPPED
    }
    return sorted(roots)
