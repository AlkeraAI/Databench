"""Every test module under the repo's testpaths imports under its own name.

The test trees carry no ``__init__.py`` at their roots (three ``tests``
packages would clash), so pytest's default prepend import mode imports a test
module by the dotted path from its nearest directory without one, which for
most files is the bare basename. Two modules with the same import name compete
for one ``sys.modules`` entry, and a whole-repo ``pytest apps packages`` stops
with an import-file mismatch: the second module is never collected, so its
tests never run in that session. A CLI test module once went uncollected this
way beside a backend test module of the same name.

The Files trees have a narrower guard of their own
(``packages/api-core/tests/files/test_files_test_tree_hygiene.py``); this one
covers every configured testpath.
"""

from __future__ import annotations

import tomllib
from collections import defaultdict
from pathlib import Path

import pytest

#: Repo root: this file is ops/scripts/tests/<this>.py.
REPO_ROOT = Path(__file__).resolve().parents[3]


def _testpaths(root: Path) -> list[Path]:
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return [root / entry for entry in config["tool"]["pytest"]["ini_options"]["testpaths"]]


def import_name(path: Path) -> str:
    """The module name prepend import mode gives ``path``: its stem, prefixed
    by every enclosing directory that is a package."""
    parts = [path.stem]
    parent = path.parent
    while (parent / "__init__.py").is_file():
        parts.insert(0, parent.name)
        parent = parent.parent
    return ".".join(parts)


def colliding_test_modules(testpaths: list[Path]) -> dict[str, list[Path]]:
    """Import names that more than one test module under ``testpaths`` shares."""
    by_name: dict[str, list[Path]] = defaultdict(list)
    for testpath in testpaths:
        for path in sorted(testpath.rglob("test_*.py")):
            by_name[import_name(path)].append(path)
    return {name: paths for name, paths in by_name.items() if len(paths) > 1}


def test_no_two_test_modules_share_an_import_name() -> None:
    collisions = colliding_test_modules(_testpaths(REPO_ROOT))
    shown = {
        name: [str(p.relative_to(REPO_ROOT)) for p in paths] for name, paths in collisions.items()
    }
    assert not shown, f"rename one module of each pair so both are collected: {shown}"


@pytest.mark.parametrize(
    ("layout", "expected"),
    [
        pytest.param(
            {"a/tests/test_x.py", "b/tests/test_x.py"},
            {"test_x": ["a/tests/test_x.py", "b/tests/test_x.py"]},
            id="same-basename-in-two-trees-collides",
        ),
        pytest.param(
            {"a/tests/sub/test_x.py", "a/tests/test_x.py"},
            {"test_x": ["a/tests/sub/test_x.py", "a/tests/test_x.py"]},
            id="a-plain-subdirectory-does-not-separate-them",
        ),
        pytest.param(
            {"a/tests/kit/__init__.py", "a/tests/kit/test_x.py", "b/tests/test_x.py"},
            {},
            id="a-package-directory-gives-its-module-a-distinct-name",
        ),
        pytest.param(
            {"a/tests/test_x.py", "b/tests/test_y.py", "b/tests/helper_x.py"},
            {},
            id="distinct-names-and-non-test-helpers-pass",
        ),
    ],
)
def test_collisions_follow_the_import_name(
    tmp_path: Path, layout: set[str], expected: dict[str, list[str]]
) -> None:
    for relative in layout:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")

    found = colliding_test_modules([tmp_path / "a" / "tests", tmp_path / "b" / "tests"])

    assert {
        name: sorted(p.relative_to(tmp_path).as_posix() for p in paths)
        for name, paths in found.items()
    } == expected
