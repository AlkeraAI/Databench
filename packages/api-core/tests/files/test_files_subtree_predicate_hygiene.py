"""One module owns the ltree ancestry operators, and this proves it.

Every subtree query goes through ``FilesRepo.subtree_predicate`` (or
its raw-SQL twin ``subtree_sql``) so the predicate and the expression index that
serves it cannot drift apart. That is a rule about code that is easy to keep on
the day it is written and easy to lose a month later: a new statement spells
``path_ids <@ CAST(:path AS ltree)`` by hand, the planner drops to a sequential
scan over every node in the org, and nothing goes red — the answers are still
correct, just linear.

So the rule is enforced mechanically. ``<@`` and ``@>`` may appear only in
:mod:`alkera_core.files.repo`, where the builders live; anywhere else under
``alkera_core/files/`` they are a defect. Docstrings and comments are exempt —
they are where the operators are *explained*, and a prose ban would only teach
people to stop writing the explanation.
"""

from __future__ import annotations

import ast
import io
import tokenize
import uuid
from pathlib import Path

import pytest
from alkera_core.files import SUBTREE_DEPTH_BIND, ancestor_chain_predicate, subtree_sql
from alkera_core.files import repo as repo_module
from alkera_core.models.files.tree import FileNode
from sqlalchemy import and_, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

#: The package the rule covers.
FILES_PACKAGE = Path(repo_module.__file__).parent

#: The ltree ancestry operators. ``@>`` is ``<@`` with its operands swapped, so
#: banning one without the other would ban nothing.
OPERATORS = ("<@", "@>")

#: The only module allowed to name them: the one that builds the predicates
#: every other module asks for.
OWNER = "repo.py"


def _docstring_lines(tree: ast.Module) -> set[int]:
    """Every line occupied by a module, class or function docstring."""
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


def _offences(source: str) -> list[tuple[int, str]]:
    """``(line, token)`` for every ltree operator outside prose in ``source``."""
    docstrings = _docstring_lines(ast.parse(source))
    found: list[tuple[int, str]] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT or token.start[0] in docstrings:
            continue
        if any(operator in token.string for operator in OPERATORS):
            found.append((token.start[0], token.string.strip()))
    return found


def _modules() -> list[Path]:
    return sorted(path for path in FILES_PACKAGE.rglob("*.py") if path.name != OWNER)


@pytest.mark.parametrize("module", _modules(), ids=lambda path: str(path.name))
def test_no_module_but_the_repo_spells_an_ltree_ancestry_operator(module: Path) -> None:
    """A subtree scan written by hand is a sequential scan nobody notices."""
    offences = _offences(module.read_text())
    assert offences == [], (
        f"{module.relative_to(FILES_PACKAGE)} spells an ltree ancestry operator at "
        f"{[line for line, _ in offences]}; build the predicate with "
        "FilesRepo.subtree_predicate (ORM) or subtree_sql (raw SQL) instead"
    )


def test_the_owner_module_is_actually_where_the_builders_live() -> None:
    """The exemption is not a hole: ``repo.py`` is exempt because it builds them.

    If the builders ever move, the parametrized test above would keep exempting
    a module that no longer earns it — so the exemption is pinned to the fact
    that makes it true.
    """
    owner = (FILES_PACKAGE / OWNER).read_text()
    assert "def subtree_sql(" in owner
    assert "def _subtree_predicate_for(" in owner
    assert "def ancestor_chain_predicate(" in owner


def test_the_raw_sql_twin_spells_both_conjuncts_of_the_orm_predicate() -> None:
    """The two spellings are one rule, so they must say the same thing."""
    fragment = subtree_sql("n.path_ids", "CAST(:path AS ltree)")
    depth = next(iter(SUBTREE_DEPTH_BIND))
    assert (
        f"subpath(n.path_ids, 0, :{depth}) <@ subpath(CAST(:path AS ltree), 0, :{depth})"
        in fragment
    ), "the index-served conjunct is missing"
    assert "n.path_ids <@ CAST(:path AS ltree)" in fragment, "the exact conjunct is missing"
    assert SUBTREE_DEPTH_BIND[depth] == repo_module.SUBTREE_INDEX_DEPTH


def test_every_statement_carrying_the_fragment_also_binds_its_depth() -> None:
    """A fragment without its bind is a ``ProgrammingError`` at run time.

    The fragment names ``:subtree_depth``; the value comes from
    ``SUBTREE_DEPTH_BIND``. A module that spreads one without the other only
    fails when that statement runs, which for a sweeper or a resume can be a
    long way from the change that broke it.
    """
    missing = []
    for module in _modules():
        source = module.read_text()
        if "subtree_sql(" in source and "SUBTREE_DEPTH_BIND" not in source:
            missing.append(module.name)
    assert missing == [], f"{missing} build the subtree fragment but never bind its depth"


#: Which converted module asks the subtree question in which shape. The pin is
#: per module because the two shapes plan differently — a bind the planner can
#: fold into an index condition, versus a join whose right-hand side is another
#: row of the same table — and a module that switched shape without anyone
#: noticing would be the drift this whole file exists to catch.
PATH_ADDRESSED = ("trash.py", "namespace.py", "large_move.py", "copy.py", "leases.py")
COLUMN_ADDRESSED = ("stats.py", "fsck.py", "sweepers.py")


def _path_addressed_sql() -> str:
    """The shape a module uses when the root arrives as a bind."""
    return "SELECT n.id FROM file_nodes AS n WHERE " + subtree_sql(
        "n.path_ids", "CAST(:path AS ltree)"
    )


def _column_addressed_sql() -> str:
    """The shape a module uses when the root is another row of the same table."""
    return (
        "SELECT n.id FROM file_nodes AS a JOIN file_nodes AS n ON "
        + subtree_sql("n.path_ids", "a.path_ids")
        + " WHERE a.id = CAST(:anchor AS uuid)"
    )


async def _plan(session: AsyncSession, sql: str, params: dict[str, object]) -> str:
    """The plan Postgres picks for ``sql`` with the sequential fallback disabled.

    A test database is small enough that a sequential scan wins on cost whatever
    the index says, so the alternative is turned off: what is proven is that the
    index CAN serve the fragment, which is exactly what a hand-spelled predicate
    loses.
    """
    await session.begin()
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    rows = (await session.execute(text(f"EXPLAIN {sql}"), params)).scalars().all()
    await session.rollback()
    return "\n".join(str(line) for line in rows)


@pytest.mark.parametrize("module", PATH_ADDRESSED)
async def test_the_path_addressed_subtree_scan_is_index_served(
    module: str, files_session: AsyncSession
) -> None:
    """Every module that scans a subtree from a path reaches the subtree index."""
    plan = await _plan(files_session, _path_addressed_sql(), {"path": "a.b", **SUBTREE_DEPTH_BIND})
    assert "ix_file_nodes_path_ids" in plan, (module, plan)
    assert "Index" in plan, (module, plan)


@pytest.mark.parametrize("module", COLUMN_ADDRESSED)
async def test_the_column_addressed_subtree_join_is_index_served(
    module: str, files_session: AsyncSession
) -> None:
    """Every module that folds a subtree against another row reaches it too."""
    plan = await _plan(
        files_session, _column_addressed_sql(), {"anchor": str(uuid.uuid4()), **SUBTREE_DEPTH_BIND}
    )
    assert "ix_file_nodes_path_ids" in plan, (module, plan)
    assert "Index" in plan, (module, plan)


def _ancestor_chain_sql() -> str:
    """The join ``readable_ids`` runs: one anchor row, its whole chain.

    Compiled from the ORM builder rather than retyped, so a predicate that stops
    naming the indexed expression is caught here and not in a latency graph.
    """
    anchor = aliased(FileNode, name="anchor")
    statement = (
        select(FileNode.id)
        .select_from(anchor)
        .join(
            FileNode,
            and_(FileNode.drive_id == anchor.drive_id, ancestor_chain_predicate(anchor)),
        )
        .where(anchor.id == uuid.UUID(int=0))
    )
    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


async def test_the_ancestor_chain_join_is_index_served(files_session: AsyncSession) -> None:
    """Asking for a node's ancestors reaches the subtree index too.

    It is the same ltree question as a subtree scan with the operands swapped,
    and the same thing goes wrong when only the exact test is spelled: no index
    can serve it, so Postgres answers by reading every node that shares the
    anchor's drive — once per anchor. A page of items resolved this way is
    quadratic in the size of the drive, which is invisible on a test tree and
    minutes on a real one.
    """
    plan = await _plan(files_session, _ancestor_chain_sql(), {})
    assert "ix_file_nodes_path_ids" in plan, plan


def test_the_ancestor_chain_predicate_spells_both_conjuncts() -> None:
    """The index-served conjunct and the exact one, like every other spelling."""
    anchor = aliased(FileNode, name="anchor")
    rendered = str(
        ancestor_chain_predicate(anchor).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    depth = repo_module.SUBTREE_INDEX_DEPTH
    assert (
        f"subpath(file_nodes.path_ids, 0, {depth}) @> subpath(anchor.path_ids, 0, {depth})"
        in rendered
    ), rendered
    assert "file_nodes.path_ids @> anchor.path_ids" in rendered, rendered
