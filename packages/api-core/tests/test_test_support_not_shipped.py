"""The test fakes live in ``alkera_test_support`` and never ship.

The root workspace installs that package through its dev dependency group only
and the app images sync with ``--no-dev``, so a shipped module that imported a
fake would fail at import in production. Three rules keep it that way:

- no module of a shipped package imports ``alkera_test_support`` (the shipped
  packages are read from each workspace member's wheel config, so a new member
  is covered without editing this file);
- no shipped workspace member, and not the root's ``[project]`` dependencies,
  depends on ``alkera-test-support``;
- no module under ``alkera_core`` defines a class named ``Fake*`` or ``Faulty*``:
  a new fake goes into ``alkera_test_support``.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TEST_SUPPORT = "alkera_test_support"
TEST_SUPPORT_DIST = "alkera-test-support"
#: Workspace members that never reach an app image or a release binary.
UNSHIPPED_MEMBERS = frozenset({"packages/test-support", "experiments"})
FAKE_CLASS = re.compile(r"^(Fake|Faulty)[A-Z_]")
#: Fakes that still sit in alkera_core: ``FakeClock`` is the Files clock seam's
#: test double, exported from ``alkera_core.files`` and imported by over a
#: hundred test modules. Each entry must still exist, so a move shrinks this.
FAKE_CLASS_EXEMPT = frozenset({"alkera_core.files.clock.FakeClock"})


def _workspace_members() -> list[str]:
    root = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    members: list[str] = root["tool"]["uv"]["workspace"]["members"]
    return members


def _shipped_package_dirs() -> list[Path]:
    dirs: list[Path] = []
    for member in _workspace_members():
        if member in UNSHIPPED_MEMBERS:
            continue
        config = tomllib.loads((REPO_ROOT / member / "pyproject.toml").read_text(encoding="utf-8"))
        packages: list[str] = config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
        dirs.extend(REPO_ROOT / member / package for package in packages)
    return dirs


def _module_name(path: Path, package_dir: Path) -> str:
    parts = list(path.relative_to(package_dir.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def imports_of_test_support(source: str) -> list[str]:
    """Every import in ``source`` that names the test-support package, anywhere
    in the file (function bodies and ``TYPE_CHECKING`` blocks included)."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        else:
            continue
        found.extend(
            name for name in names if name == TEST_SUPPORT or name.startswith(f"{TEST_SUPPORT}.")
        )
    return found


def fake_classes(source: str) -> list[str]:
    """The names of every ``Fake*`` / ``Faulty*`` class ``source`` defines."""
    return [
        node.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ClassDef) and FAKE_CLASS.match(node.name)
    ]


def _depends_on_test_support(requirements: list[str]) -> bool:
    return any(
        re.split(r"[\s\[<>=!~;]", requirement, maxsplit=1)[0] == TEST_SUPPORT_DIST
        for requirement in requirements
    )


# --- the scanners catch what they are for ----------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("import alkera_test_support\n", ["alkera_test_support"], id="bare-import"),
        pytest.param(
            "import alkera_test_support.compute.fake_box as f\n",
            ["alkera_test_support.compute.fake_box"],
            id="dotted-import",
        ),
        pytest.param(
            "from alkera_test_support.files.fake_store import FakeStore\n",
            ["alkera_test_support.files.fake_store"],
            id="from-import",
        ),
        pytest.param(
            "def f() -> None:\n    from alkera_test_support import billing\n",
            ["alkera_test_support"],
            id="function-local",
        ),
        pytest.param(
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n    from alkera_test_support.files import faulty_store\n",
            ["alkera_test_support.files"],
            id="type-checking-block",
        ),
        pytest.param("import alkera_test_supportive\n", [], id="longer-name-is-not-it"),
        pytest.param("from alkera_core.config import settings\n", [], id="unrelated"),
        pytest.param("from . import alkera_test_support\n", [], id="relative-import"),
    ],
)
def test_the_import_scanner_finds_every_import_form(source: str, expected: list[str]) -> None:
    assert imports_of_test_support(source) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("class FakeDocker:\n    pass\n", ["FakeDocker"], id="fake"),
        pytest.param("class FaultyStore:\n    pass\n", ["FaultyStore"], id="faulty"),
        pytest.param(
            "def f() -> None:\n    class FakeInner:\n        pass\n", ["FakeInner"], id="nested"
        ),
        pytest.param("class Fault:\n    pass\n", [], id="fault-value-type"),
        pytest.param("class Faker:\n    pass\n", [], id="lowercase-continuation"),
        pytest.param("class StripeFake:\n    pass\n", [], id="suffix-only"),
    ],
)
def test_the_class_scanner_matches_only_the_fake_prefixes(source: str, expected: list[str]) -> None:
    assert fake_classes(source) == expected


@pytest.mark.parametrize(
    ("requirements", "expected"),
    [
        pytest.param(["alkera-test-support"], True, id="bare"),
        pytest.param(["alkera-test-support>=0"], True, id="specifier"),
        pytest.param(["alkera-test-support[x]"], True, id="extra"),
        pytest.param(["alkera-test-support ; python_version>'3'"], True, id="marker"),
        pytest.param(["alkera-test-supporter"], False, id="longer-name"),
        pytest.param(["alkera-core", "httpx>=0.27"], False, id="others"),
    ],
)
def test_the_dependency_check_matches_the_distribution_name(
    requirements: list[str], expected: bool
) -> None:
    assert _depends_on_test_support(requirements) is expected


# --- the tree --------------------------------------------------------------------


def test_the_shipped_packages_cover_every_app_and_shared_package() -> None:
    names = {path.name for path in _shipped_package_dirs()}
    assert {"alkera_core", "backend", "worker", "alkera_cli", "model_gateway"} <= names
    assert TEST_SUPPORT not in names


def test_no_shipped_module_imports_the_test_support_package() -> None:
    offenders = [
        f"{path.relative_to(REPO_ROOT)}: {name}"
        for package_dir in _shipped_package_dirs()
        for path in sorted(package_dir.rglob("*.py"))
        if "__pycache__" not in path.parts
        for name in imports_of_test_support(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, "\n".join(offenders)


def test_no_shipped_member_depends_on_the_test_support_package() -> None:
    root = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    offenders = (
        ["pyproject.toml [project]"]
        if _depends_on_test_support(root["project"].get("dependencies", []))
        else []
    )
    for member in _workspace_members():
        if member in UNSHIPPED_MEMBERS:
            continue
        project = tomllib.loads(
            (REPO_ROOT / member / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        groups = {"dependencies": project.get("dependencies", [])}
        groups.update(project.get("optional-dependencies", {}))
        offenders.extend(
            f"{member} {group}" for group, reqs in groups.items() if _depends_on_test_support(reqs)
        )
    assert not offenders, offenders


def test_alkera_core_defines_no_fake_classes() -> None:
    package_dir = REPO_ROOT / "packages" / "api-core" / "alkera_core"
    found = {
        f"{_module_name(path, package_dir)}.{name}"
        for path in sorted(package_dir.rglob("*.py"))
        if "__pycache__" not in path.parts
        for name in fake_classes(path.read_text(encoding="utf-8"))
    }
    assert found - FAKE_CLASS_EXEMPT == set(), "move these into alkera_test_support"
    assert FAKE_CLASS_EXEMPT <= found, "an exempt fake moved: drop it from FAKE_CLASS_EXEMPT"
