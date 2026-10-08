"""`FilesRepo` is the only place a Files table is queried — checked by an AST scan.

The typed repo is the second tenant-isolation layer, and it only holds if
nothing else reaches for a Files model directly. Grep would be defeated by
whitespace and by a name that merely contains a model's; the scan below walks
the real syntax tree of every module in the Files blast radius and flags a
`select`/`insert`/`update`/`delete` or a `session.get` whose argument is a Files
ORM class.

The scan itself is proven by a planted module under ``tmp_path`` that does
exactly what the rule forbids: a scanner that silently stopped finding things
would fail there.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from alkera_core.models import files as files_models

#: Every ORM class name under ``alkera_core.models.files`` — the set the scan
#: treats as "a Files model", derived from the package so a new table joins the
#: rule by existing rather than by someone remembering to add it here.
FILES_MODEL_NAMES: frozenset[str] = frozenset(
    name
    for name in files_models.__all__
    if isinstance(getattr(files_models, name, None), type)
    and hasattr(getattr(files_models, name), "__tablename__")
)

#: The statement constructors that read or write rows. The dialect-specific
#: `insert` is spelled `pg_insert` at every call site in this repo, so the
#: alias is listed too — a scan that only knew the core name would have read
#: `pg_insert(FileStar)` as innocent.
STATEMENT_CALLS: frozenset[str] = frozenset(
    {"select", "insert", "update", "delete", "pg_insert", "postgresql_insert"}
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
#: The one module allowed to name a Files model in a statement.
ALLOWED = _REPO_ROOT / "packages/api-core/alkera_core/files/repo.py"
SCANNED_ROOTS = (
    _REPO_ROOT / "packages/api-core/alkera_core/files",
    _REPO_ROOT / "apps/backend/backend",
    _REPO_ROOT / "apps/worker/worker",
)


@dataclass(frozen=True, slots=True)
class Violation:
    path: Path
    line: int
    call: str
    model: str

    def __str__(self) -> str:
        rel = self.path.relative_to(_REPO_ROOT)
        return f"{rel}:{self.line} {self.call}({self.model})"


def _model_argument(node: ast.expr) -> str | None:
    """The Files model name this expression names, if it names one."""
    if isinstance(node, ast.Name) and node.id in FILES_MODEL_NAMES:
        return node.id
    if isinstance(node, ast.Attribute) and node.attr in FILES_MODEL_NAMES:
        return node.attr
    return None


def _callee_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def scan_module(path: Path) -> Iterator[Violation]:
    """Yield every forbidden statement in one Python file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = _callee_name(node.func)
        if callee is None:
            continue
        if callee in STATEMENT_CALLS:
            targets = node.args
        elif callee == "get" and isinstance(node.func, ast.Attribute):
            # `session.get(FileNode, id)` reads a row without a select().
            targets = node.args[:1]
        else:
            continue
        for argument in targets:
            model = _model_argument(argument)
            if model is not None:
                yield Violation(path=path, line=node.lineno, call=callee, model=model)


def _scanned_files() -> list[Path]:
    found: list[Path] = []
    for root in SCANNED_ROOTS:
        found.extend(sorted(path for path in root.rglob("*.py") if path != ALLOWED))
    return found


def test_the_model_name_set_is_not_empty() -> None:
    """A scan over an empty model set would pass vacuously."""
    assert "FileNode" in FILES_MODEL_NAMES
    assert len(FILES_MODEL_NAMES) > 20


def test_no_files_model_is_queried_outside_the_repo() -> None:
    """The rule: every Files statement lives in ``files/repo.py``."""
    scanned = _scanned_files()
    assert scanned, "the scan found no modules — the roots moved"
    violations = [str(found) for path in scanned for found in scan_module(path)]
    assert violations == []


def test_the_repo_itself_does_query_files_models() -> None:
    """The negative twin of the rule: the allowed module really is where the
    statements are, so the scan above is not passing because nothing anywhere
    queries a Files table."""
    inside = list(scan_module(ALLOWED))
    assert {found.model for found in inside} >= {"FileNode", "FileDrive", "FileVersion"}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            "from sqlalchemy import select\n"
            "from alkera_core.models.files.tree import FileNode\n"
            "def leak(session):\n"
            "    return session.execute(select(FileNode))\n",
            [("select", "FileNode")],
            id="select-a-node",
        ),
        pytest.param(
            "from alkera_core.models.files.tree import FileNode\n"
            "async def leak(session, id):\n"
            "    return await session.get(FileNode, id)\n",
            [("get", "FileNode")],
            id="session-get",
        ),
        pytest.param(
            "from sqlalchemy import update\n"
            "from alkera_core.models import files as m\n"
            "def leak():\n"
            "    return update(m.FileNode).values(size=0)\n",
            [("update", "FileNode")],
            id="update-through-an-attribute",
        ),
        pytest.param(
            "from sqlalchemy import delete\n"
            "from alkera_core.models.files.acl import FileShare\n"
            "def leak():\n"
            "    return delete(FileShare)\n",
            [("delete", "FileShare")],
            id="delete-a-share",
        ),
        pytest.param(
            "from sqlalchemy import select\n"
            "from alkera_core.models.team import Team\n"
            "def fine():\n"
            "    return select(Team)\n",
            [],
            id="a-non-files-model-is-fine",
        ),
        pytest.param(
            "def fine(repo, id):\n    return repo.node(id)\n",
            [],
            id="going-through-the-repo-is-fine",
        ),
    ],
)
def test_the_scanner_finds_a_planted_violation(
    tmp_path: Path,
    source: str,
    expected: list[tuple[str, str]],
) -> None:
    """A module planted on disk with the forbidden shape is found, and one
    without it is not — so a green scan above means the rule holds, not that
    the scanner went blind."""
    planted = tmp_path / "planted.py"
    planted.write_text(source, encoding="utf-8")
    found = [(v.call, v.model) for v in scan_module(planted)]
    assert found == expected
