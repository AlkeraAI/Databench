"""No role name outside the package that owns the ladder.

A role is an internal fact about how :mod:`alkera_core.files.authz` ranks
callers. The moment a route, a service or a button branches on ``"writer"`` the
ladder stops being data: adding a rung means editing every branch, and the
platform RBAC engine can no longer replace the ladder without a sweep through
the product. This module is the grep that keeps that from happening — it scans
Python with :mod:`ast` (a role name is a *string constant*, wherever it hides)
and TypeScript with a comparison-shaped regex, so a role passed straight through
to the UI as a label is allowed and a role a component *branches on* is not.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

#: Every rung of the ladder, as spelled on the wire and in the database.
ROLE_NAMES: Final[frozenset[str]] = frozenset({"reader", "commenter", "writer", "manager", "owner"})

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[5]
FILES_PACKAGE: Final[Path] = REPO_ROOT / "packages/api-core/alkera_core/files"
FILES_SCHEMAS: Final[Path] = REPO_ROOT / "packages/api-core/alkera_core/schemas/files"
BACKEND: Final[Path] = REPO_ROOT / "apps/backend/backend"
WEB_SRC: Final[Path] = REPO_ROOT / "apps/web/src"

#: The one place outside Files that spells a role word for a reason that has
#: nothing to do with permissions: the weak-password blocklist, where "manager"
#: is a common password, not a rung. Exempted by exact path so a *new* backend
#: file cannot inherit the exemption.
BACKEND_EXEMPT: Final[frozenset[Path]] = frozenset({BACKEND / "auth" / "password_policy.py"})

_ROLE_ALTERNATION: Final[str] = "|".join(sorted(ROLE_NAMES))

#: A TypeScript role *branch*: an equality against a role literal, or a
#: ``case "writer":`` arm. Rendering ``{share.role}`` is untouched — passing a
#: role through to a label is the allowed use, deciding on it is not.
_TS_BRANCH: Final[re.Pattern[str]] = re.compile(
    rf"""(?:(?:===|!==|==|!=)\s*(['"`])(?P<a>{_ROLE_ALTERNATION})\1)"""
    rf"""|(?:(['"`])(?P<b>{_ROLE_ALTERNATION})\3\s*(?:===|!==|==|!=))"""
    rf"""|(?:\bcase\s+(['"`])(?P<c>{_ROLE_ALTERNATION})\5\s*:)"""
)


def _python_files(root: Path) -> Iterator[Path]:
    if not root.exists():
        return
    yield from sorted(root.rglob("*.py"))


def role_literals_in_python(source: str) -> set[str]:
    """Every role name that appears as a string constant in ``source``.

    An AST walk rather than a regex: a role hidden in a dict key, a default
    argument or a tuple is still a role literal, while a role word inside a
    comment or a docstring is not — docstrings are excluded explicitly, because
    prose explaining the ladder is exactly what a reader of Files should find.
    """
    tree = ast.parse(source)
    docstrings = {
        node.body[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node in docstrings:
                continue
            if node.value in ROLE_NAMES:
                found.add(node.value)
    return found


def role_branches_in_typescript(source: str) -> set[str]:
    """Every role name a TypeScript source *branches on*."""
    found: set[str] = set()
    for match in _TS_BRANCH.finditer(source):
        for group in ("a", "b", "c"):
            hit = match.group(group)
            if hit is not None:
                found.add(hit)
    return found


def _rel(found: Path) -> str:
    """A short name for a report; ``tmp_path`` files live outside the repo."""
    return str(found.relative_to(REPO_ROOT)) if found.is_relative_to(REPO_ROOT) else found.name


def _offenders(root: Path, *, skip: frozenset[Path] = frozenset()) -> list[str]:
    offenders: list[str] = []
    for found in _python_files(root):
        if found in skip or any(found.is_relative_to(one) for one in skip if one.is_dir()):
            continue
        hits = role_literals_in_python(found.read_text(encoding="utf-8"))
        if hits:
            offenders.append(f"{_rel(found)}: {sorted(hits)}")
    return offenders


def test_the_scanner_finds_a_planted_python_role_literal(tmp_path: Path) -> None:
    """The negative twin: a file that *does* branch on a role is reported.

    Without it the three scans below would pass just as happily against a
    scanner that always returns nothing.
    """
    planted = tmp_path / "route.py"
    planted.write_text(
        '"""A docstring mentioning writer is fine."""\n'
        "def can_edit(role: str) -> bool:\n"
        '    return role == "writer"\n',
        encoding="utf-8",
    )
    assert role_literals_in_python(planted.read_text(encoding="utf-8")) == {"writer"}
    assert _offenders(tmp_path) == ["route.py: ['writer']"]


def test_the_scanner_ignores_prose_about_the_ladder(tmp_path: Path) -> None:
    """A module docstring naming every rung is documentation, not a branch."""
    planted = tmp_path / "doc.py"
    planted.write_text(
        '"""reader, commenter, writer, manager, owner — the ladder."""\n'
        "# owner is also fine in a comment\n",
        encoding="utf-8",
    )
    assert role_literals_in_python(planted.read_text(encoding="utf-8")) == set()


def test_no_role_literal_under_files_outside_authz() -> None:
    """The tree, the ACL cache and the namespace never spell a rung."""
    offenders = _offenders(FILES_PACKAGE, skip=frozenset({FILES_PACKAGE / "authz"}))
    assert offenders == [], f"role literals outside files/authz: {offenders}"


def test_no_role_literal_in_files_schemas_outside_the_sharing_shapes() -> None:
    """Only the two schemas that carry a role on the wire may name one."""
    allowed = frozenset({FILES_SCHEMAS / "sharing.py", FILES_SCHEMAS / "principal.py"})
    offenders = _offenders(FILES_SCHEMAS, skip=allowed)
    assert offenders == [], f"role literals in schemas/files: {offenders}"


def test_no_role_literal_in_the_backend() -> None:
    """No route, service or policy in the backend branches on a rung."""
    offenders = _offenders(BACKEND, skip=BACKEND_EXEMPT)
    assert offenders == [], f"role literals in the backend: {offenders}"


def test_the_backend_exemption_still_names_a_real_file() -> None:
    """An exemption that outlives its file would silently widen the gate."""
    for exempt in BACKEND_EXEMPT:
        assert exempt.exists(), f"stale exemption: {exempt}"


def test_the_typescript_scanner_finds_a_planted_role_branch(tmp_path: Path) -> None:
    """The negative twin for the web scan, including the ``case`` arm."""
    planted = tmp_path / "Share.tsx"
    planted.write_text(
        "export function label(role: string) {\n"
        "  switch (role) {\n"
        '    case "manager":\n'
        '      return "Manager";\n'
        "  }\n"
        '  if (role === "writer") return "Editor";\n'
        "  return role;\n"
        "}\n",
        encoding="utf-8",
    )
    assert role_branches_in_typescript(planted.read_text(encoding="utf-8")) == {
        "manager",
        "writer",
    }


def test_the_typescript_scanner_allows_pass_through_display(tmp_path: Path) -> None:
    """Rendering the role the server sent is display, not a decision."""
    planted = tmp_path / "Row.tsx"
    planted.write_text(
        'const ROLE_LABELS = { writer: "Editor" };\n'
        "export const Row = ({ role }) => <span>{role}</span>;\n",
        encoding="utf-8",
    )
    assert role_branches_in_typescript(planted.read_text(encoding="utf-8")) == set()


@pytest.mark.parametrize("suffix", [".ts", ".tsx"], ids=["ts", "tsx"])
def test_no_role_branch_in_the_web_app(suffix: str) -> None:
    """The portal decides on capabilities; it never sees a rung to branch on."""
    offenders: list[str] = []
    for found in sorted(WEB_SRC.rglob(f"*{suffix}")):
        hits = role_branches_in_typescript(found.read_text(encoding="utf-8"))
        if hits:
            offenders.append(f"{found.relative_to(REPO_ROOT)}: {sorted(hits)}")
    assert offenders == [], f"role branches in apps/web: {offenders}"
