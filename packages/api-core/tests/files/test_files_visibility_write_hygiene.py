"""One module writes ``trashed_at``, and this proves it.

A node leaves the listing when ``trashed_at`` is set. A write that sets it
without the trash op beside it hides the node with nothing to bring it back
from: it is not in the trash view, a restore cannot name it, and the history
says nothing about who or why. That is exactly how a lapsed lease once hid 731
files whose only copy was on the box that held them.

So every write of the column goes through :mod:`alkera_core.files.trash`, which
writes the op in the same statement: ``Trash`` for a person's or an object's
deletion and its restore, and ``trash_left_behind_sql`` for a sweeper that has
to trash rows inside a statement of its own. Anywhere else in the server code,
spelling ``trashed_at =`` in SQL, ``.values(trashed_at=...)`` or
``node.trashed_at = ...`` is a defect. Docstrings and comments are exempt:
they are where the rule is explained.

The allowlist is the owner and nothing else. It may only shrink.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest
from alkera_core.files import trash as trash_module

#: The repository root, found from this file rather than the working directory.
ROOT = Path(__file__).resolve().parents[4]
#: The server code the rule covers: the shared library, the HTTP app and the
#: background worker. Tests are not covered; they plant states on purpose.
SCANNED = (
    ROOT / "packages" / "api-core" / "alkera_core",
    ROOT / "apps" / "backend" / "backend",
    ROOT / "apps" / "worker" / "worker",
)
#: The one module allowed to write the column.
OWNER = Path(trash_module.__file__).resolve()

COLUMN = "trashed_at"
#: An assignment to the column inside SQL text: ``SET trashed_at = ...``.
#: ``==`` is not an assignment, and a comparison against NULL is spelled
#: ``IS NULL`` in every statement this codebase writes.
SQL_WRITE = re.compile(rf"\b{COLUMN}\s*=(?!=)")
#: Token types that carry literal text. ``FSTRING_MIDDLE`` is how Python 3.12+
#: tokenizes the literal parts of an f-string.
_TEXT_TOKENS = frozenset({tokenize.STRING, getattr(tokenize, "FSTRING_MIDDLE", tokenize.STRING)})


def _docstring_lines(tree: ast.Module) -> set[int]:
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = node.body
        if not body or not isinstance(body[0], ast.Expr):
            continue
        first = body[0].value
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return lines


def _sql_writes(source: str, tree: ast.Module) -> list[int]:
    docstrings = _docstring_lines(tree)
    found: list[int] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type not in _TEXT_TOKENS or token.start[0] in docstrings:
            continue
        for match in SQL_WRITE.finditer(token.string):
            found.append(token.start[0] + token.string.count("\n", 0, match.start()))
    return found


def _python_writes(tree: ast.Module) -> list[int]:
    """``x.trashed_at = ...`` and ``.values(trashed_at=...)``."""
    found: list[int] = []
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AugAssign | ast.AnnAssign):
            targets = [node.target]
        found.extend(
            target.lineno
            for target in targets
            if isinstance(target, ast.Attribute) and target.attr == COLUMN
        )
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "values"
        ):
            found.extend(kw.value.lineno for kw in node.keywords if kw.arg == COLUMN)
    return found


def offences(source: str) -> list[int]:
    """Every line of ``source`` that writes ``trashed_at``, sorted."""
    tree = ast.parse(source)
    return sorted(set(_sql_writes(source, tree) + _python_writes(tree)))


def _modules() -> list[Path]:
    return sorted(
        path.resolve()
        for package in SCANNED
        for path in package.rglob("*.py")
        if path.resolve() != OWNER
    )


def test_no_module_but_the_trash_writes_trashed_at() -> None:
    """A node hidden without its op is a node nobody can bring back."""
    found = {
        str(module.relative_to(ROOT)): lines
        for module in _modules()
        if (lines := offences(module.read_text(encoding="utf-8")))
    }
    assert found == {}, (
        f"{COLUMN} is written outside {OWNER.relative_to(ROOT)}: {found}. Trash through "
        "alkera_core.files.trash (Trash, or trash_left_behind_sql inside a sweeper's "
        "statement) so the node gets a trash op that lists and restores it"
    )


def test_the_scan_covers_the_server_code() -> None:
    """A path that moved would make the scan pass over nothing."""
    scanned = _modules()
    assert len(scanned) > 500
    assert ROOT / "packages" / "api-core" / "alkera_core" / "files" / "sweepers.py" in scanned


def test_the_owner_is_where_the_trash_and_its_op_are_written() -> None:
    """The exemption is earned: the owner writes the column, and writes it
    beside the op every time, so a move of the writes out of it fails here
    instead of leaving an exemption nobody needs."""
    source = OWNER.read_text(encoding="utf-8")
    assert offences(source), "the owner no longer writes the column; drop the exemption"
    assert "def trash_left_behind_sql(" in source
    assert "INSERT INTO file_trash_ops" in source
    for write in re.finditer(r"SET trashed_at = (?!NULL)", source):
        statement = source[write.start() : write.start() + 200]
        assert "trash_op_id" in statement, "a trash in the owner sets no op beside it"


@pytest.mark.parametrize(
    "source",
    [
        pytest.param('Q = "UPDATE file_nodes SET trashed_at = now() WHERE id = :id"\n', id="sql"),
        pytest.param(
            'Q = f"""\nUPDATE file_nodes\n  SET etag = 1, trashed_at = :now\n"""\n',
            id="f-string-over-lines",
        ),
        pytest.param('Q = "UPDATE file_nodes SET trashed_at=NULL"\n', id="an-unhide-too"),
        pytest.param("node.trashed_at = when\n", id="orm-attribute"),
        pytest.param("stmt = update(FileNode).values(trashed_at=when)\n", id="orm-values"),
    ],
)
def test_the_scan_catches_each_shape_of_a_write(source: str) -> None:
    assert offences(source) != []


@pytest.mark.parametrize(
    "source",
    [
        pytest.param('Q = "SELECT 1 FROM file_nodes WHERE trashed_at IS NULL"\n', id="a-read"),
        pytest.param('"""We never set trashed_at = now() by hand."""\n', id="a-docstring"),
        pytest.param("# SET trashed_at = now() is the owner's\nx = 1\n", id="a-comment"),
        pytest.param("same = node.trashed_at == other\n", id="a-comparison"),
        pytest.param("snap = Snapshot(trashed_at=None)\n", id="a-schema-field"),
    ],
)
def test_the_scan_leaves_reads_and_prose_alone(source: str) -> None:
    assert offences(source) == []
