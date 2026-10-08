"""The two Files test trees must stay collectible in ONE pytest session.

Neither `apps/backend/tests/` nor `packages/api-core/tests/` carries an
`__init__.py` (three `tests` packages would clash), so pytest's default prepend
import mode imports every module under them by **bare basename** off a sys.path
entry. Two files that share a basename across the two trees therefore compete
for one entry in `sys.modules`: whichever is imported first wins, and the second
is either skipped or — worse — silently answers with the first one's contents.

That is not hypothetical. It is F-127: the api-core Files tests spelled
`from tests.files.conftest import FilesFactory`, which resolved to
`apps/backend/tests/files/conftest.py` once both trees were collected together
(`tests.files` is a namespace package and merges both portions), and the gate
run aborted with 53 collection errors. F-173 is the same hazard one step later:
the collision stays invisible until somebody happens to collect both trees in
one session, and by then it is a wall of unrelated errors.

So three rules, checked against the trees as they are on disk rather than an
allowlist, so a new module joins the gate the day it lands:

1. No `test_*.py` basename exists in both trees.
2. No helper module a test imports by bare name (`from _oracle import probe`)
   exists in both trees — those resolve through sys.path exactly like the test
   modules do.
3. No module in either tree reaches a conftest through an importable name:
   neither the bare `conftest` nor the namespace-merged `tests.files.conftest`
   names one file. A conftest's exports are reached as fixtures, or from a
   repo-unique helper module (`_kit/factory.py`).

Rule 3 is green on the api-core tree and still has a named residue on the
backend one (`KNOWN_CONFTEST_IMPORTERS`), so it is enforced there as a ratchet:
a module not on that list fails immediately, and the list is only ever allowed
to shrink.
"""

from __future__ import annotations

import ast
from pathlib import Path

#: Repo root: this file is packages/api-core/tests/files/<this>.py.
REPO_ROOT = Path(__file__).resolve().parents[4]

#: The two Files test trees, by the rootdir-relative path a reader would type.
TREES: tuple[str, ...] = (
    "packages/api-core/tests/files",
    "apps/backend/tests/files",
)

#: Conftest spellings that name a file only by luck of sys.path ordering.
FORBIDDEN_CONFTEST_IMPORTS: frozenset[str] = frozenset({"conftest", "tests.files.conftest"})

#: The backend Files modules that still spell `from tests.files.conftest import
#: ...`. They resolve to their own tree today only because apps/backend reaches
#: sys.path before packages/api-core; the api-core half of exactly this shape is
#: what produced F-127's 53 collection errors. Rewriting them belongs to the
#: backend-test-imports lane, so until then the rule is a one-way ratchet: a
#: module NOT on this list is a hard failure, and the list may only shrink.
KNOWN_CONFTEST_IMPORTERS: frozenset[str] = frozenset(
    {
        "test_files_conflicts_routes.py",
        "test_files_content_routes.py",
        "test_files_delta_routes.py",
        "test_files_filtered_before_pagination.py",
        "test_files_ids_reveal_nothing.py",
        "test_files_operations_routes.py",
        "test_files_sharing_routes.py",
        "test_files_statement_counts.py",
        "test_files_trash_routes.py",
        "test_files_uploads_routes.py",
        "test_files_versions_routes.py",
    }
)


def _tree(relative: str) -> Path:
    root = REPO_ROOT / relative
    assert root.is_dir(), f"{relative} is not a directory — the discovery is pointing at nothing"
    return root


def _modules(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def _imported_module_names(source: str) -> set[str]:
    """Every module name an import statement in `source` names.

    Relative imports (`from . import x`) are excluded: they resolve inside a
    real package and never race for a sys.path entry.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


def _helper_basenames(root: Path) -> set[str]:
    """Non-test, non-conftest modules in a tree, by the stem an import would use."""
    return {
        source.stem
        for source in _modules(root)
        if not source.name.startswith("test_") and source.name not in ("conftest.py", "__init__.py")
    }


def bare_name_helper_imports(root: Path) -> set[str]:
    """Helper modules this tree imports by bare name, so through sys.path.

    A name is only counted when the tree actually holds a module of that stem —
    `import statistics` is the stdlib, `from _oracle import probe` is the file
    next door, and only the second one can be shadowed by the other tree.
    """
    helpers = _helper_basenames(root)
    reached: set[str] = set()
    for source in _modules(root):
        for name in _imported_module_names(source.read_text(encoding="utf-8")):
            head = name.split(".")[0]
            if head in helpers:
                reached.add(head)
    return reached


def forbidden_conftest_importers(root: Path) -> list[str]:
    """Modules in `root` that import a conftest by an ambiguous module name."""
    offenders: list[str] = []
    for source in _modules(root):
        named = _imported_module_names(source.read_text(encoding="utf-8"))
        if named & FORBIDDEN_CONFTEST_IMPORTS:
            offenders.append(str(source.relative_to(root)))
    return sorted(offenders)


def test_no_test_module_basename_exists_in_both_files_trees() -> None:
    """Rule 1 — the F-173 shape, checked by name rather than by collection."""
    first, second = (_tree(relative) for relative in TREES)
    left = {source.name for source in _modules(first) if source.name.startswith("test_")}
    right = {source.name for source in _modules(second) if source.name.startswith("test_")}

    assert left and right, "one of the two trees has no test modules — the discovery is broken"
    shared = sorted(left & right)
    assert shared == [], (
        f"these basenames exist under both {TREES[0]} and {TREES[1]}: {shared}. Neither tree is a "
        "package, so pytest imports both by bare basename and one silently shadows the other; "
        "rename one to something repo-unique (e.g. name the app in it)"
    )


def test_no_bare_name_helper_module_exists_in_both_files_trees() -> None:
    """Rule 2 — the same hazard for the helpers a conftest or a test reaches for."""
    first, second = (_tree(relative) for relative in TREES)
    left = bare_name_helper_imports(first)
    right = bare_name_helper_imports(second)

    assert left and right, "neither tree imports a helper by bare name — the discovery is broken"
    shared = sorted(left & right)
    assert shared == [], (
        f"these helper modules are imported by bare name and exist in both trees: {shared}. "
        "Give one a repo-unique name, or move it under a package like "
        "packages/api-core/tests/files/_kit/"
    )


def test_no_module_in_either_tree_imports_a_conftest_by_module_name() -> None:
    """Rule 3 — the F-127 shape itself.

    `tests.files` is a namespace package with a portion in each tree, so
    `tests.files.conftest` names whichever portion sys.path happens to reach
    first; the bare `conftest` is worse still. Shared shapes belong in a
    repo-unique helper module that both trees import by its own name.
    """
    offenders = {relative: forbidden_conftest_importers(_tree(relative)) for relative in TREES}
    unwaived = {
        relative: sorted(set(found) - KNOWN_CONFTEST_IMPORTERS)
        for relative, found in offenders.items()
    }

    assert unwaived == {relative: [] for relative in TREES}, (
        "these modules import a conftest by a name that only resolves by luck of sys.path "
        f"ordering (F-127): {unwaived}. Take the shapes as fixtures, or from _kit/"
    )
    assert offenders["packages/api-core/tests/files"] == [], (
        "the api-core tree is the one F-127 actually broke and it must stay at zero — no waiver "
        "applies here"
    )
    assert set(offenders["apps/backend/tests/files"]) <= KNOWN_CONFTEST_IMPORTERS, (
        "the waiver is a ratchet: it may shrink, never grow"
    )


def test_the_basename_rule_reports_a_collision_it_is_shown(tmp_path: Path) -> None:
    """The negative twin: the comparison must fail on a planted duplicate.

    Without this the two rules above would keep passing if the discovery ever
    stopped finding files at all.
    """
    left = tmp_path / "left"
    right = tmp_path / "right"
    for tree in (left, right):
        tree.mkdir()
        (tree / "test_files_shared_name.py").write_text("def test_x() -> None: ...\n")
    (left / "test_files_only_here.py").write_text("def test_y() -> None: ...\n")

    left_names = {source.name for source in _modules(left)}
    right_names = {source.name for source in _modules(right)}

    assert sorted(left_names & right_names) == ["test_files_shared_name.py"]
    assert "test_files_only_here.py" not in right_names


def test_the_helper_rule_only_counts_a_name_the_tree_really_holds(tmp_path: Path) -> None:
    """The negative twin for rule 2: a stdlib import must not read as a helper.

    Both trees import `statistics`; if a bare import counted on its own, rule 2
    would report every stdlib module as a collision and mean nothing.
    """
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "_oracle.py").write_text("def probe() -> None: ...\n")
    (tree / "test_a.py").write_text("import statistics\nfrom _oracle import probe\n")

    assert bare_name_helper_imports(tree) == {"_oracle"}


def test_the_conftest_rule_reports_each_forbidden_spelling(tmp_path: Path) -> None:
    """The negative twin for rule 3, one planted module per forbidden spelling."""
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "test_a.py").write_text("import conftest\n")
    (planted / "test_b.py").write_text("from tests.files.conftest import Fixtures\n")
    (planted / "test_c.py").write_text("import tests.files.conftest as c\n")
    (planted / "test_clean.py").write_text(
        "from _kit.factory import FilesFactory\nfrom . import conftest\n"
    )

    offenders = forbidden_conftest_importers(planted)

    assert offenders == ["test_a.py", "test_b.py", "test_c.py"], (
        "the rule must catch every ambiguous spelling and forgive the repo-unique one"
    )
