"""One module relates a chat's workspace to a workspace folder, and this proves it.

A chat names its workspace by the workspace OBJECT's id. A workspace folder is
a Files node with an id of its own. Code that reads the chat's side by hand
and holds it against a folder it found by hand compares two id spaces that
never match, and every answer is "not in this workspace". Nothing goes red on
the day it is written, because both values are uuids and the comparison type
checks.

So two shapes are refused everywhere but
:mod:`alkera_core.files.workspace_identity`:

* reading ``workspace_id`` off a spec as a bare dict key in Python. The typed
  ``ChatSpec`` read and the SQL column expression (``spec["workspace_id"]
  .astext``) are different things and stay allowed;
* a function that both picks a workspace folder out by its subtype and names
  a chat's workspace. That function is deciding "is this chat in the workspace
  holding this node", which is :func:`chat_works_at`'s answer to give.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest
from alkera_core.files import workspace_identity

REPO_ROOT = Path(workspace_identity.__file__).resolve().parents[4]

#: The module that may read either side.
OWNER = Path(workspace_identity.__file__).resolve()

#: The key a chat's spec names its workspace by.
KEY = "workspace_id"

#: Directories whose Python is not product source.
SKIPPED_PARTS = frozenset(
    {"tests", "_generated", "versions", "node_modules", ".venv", "dist", "build", "vendor"}
)

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _sources(root: Path) -> list[Path]:
    found: list[Path] = []
    for top in ("apps", "packages"):
        for path in sorted((root / top).rglob("*.py")):
            if SKIPPED_PARTS.isdisjoint(path.relative_to(root).parts):
                found.append(path)
    return found


def _is_key(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value == KEY


def _names_a_spec(node: ast.expr) -> bool:
    """Whether the expression a key is read off is a spec: a name or an
    attribute spelled ``spec`` anywhere inside it (``chat.spec``, ``spec``,
    ``(chat.spec or {})``)."""
    for inner in ast.walk(node):
        if isinstance(inner, ast.Name) and "spec" in inner.id.lower():
            return True
        if isinstance(inner, ast.Attribute) and "spec" in inner.attr.lower():
            return True
    return False


def _sql_column_reads(tree: ast.AST) -> set[int]:
    """The ids of subscripts used as ``<column>["workspace_id"].astext``: a
    SQL expression the database evaluates, not a value Python compares."""
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr in {"astext", "as_string"}
        and isinstance(node.value, ast.Subscript)
    }


def bare_spec_reads(source: str) -> list[int]:
    """The lines of ``source`` that read ``workspace_id`` off a spec as a
    bare dict key."""
    tree = ast.parse(source)
    in_sql = _sql_column_reads(tree)
    lines: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Load)
            and _is_key(node.slice)
            and id(node) not in in_sql
            and _names_a_spec(node.value)
        ):
            lines.append(node.lineno)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and _is_key(node.args[0])
            and _names_a_spec(node.func.value)
        ):
            lines.append(node.lineno)
    return sorted(lines)


def _is_a_mapped_class(node: ast.expr) -> bool:
    """``FileNode.subtype == ...`` is a query predicate the database answers
    with rows; a node in hand is a lowercase name."""
    return isinstance(node, ast.Name) and node.id[:1].isupper()


def _picks_a_workspace_folder(function: ast.AST) -> bool:
    """Whether the function tests the ``subtype`` of a node in hand against
    the workspace type."""
    for node in ast.walk(function):
        if not isinstance(node, ast.Compare):
            continue
        sides = [node.left, *node.comparators]
        reads_subtype = any(
            isinstance(side, ast.Attribute)
            and side.attr == "subtype"
            and not _is_a_mapped_class(side.value)
            for side in sides
        )
        names_workspace = any(
            (isinstance(inner, ast.Name) and inner.id == "WORKSPACE_TYPE")
            or (isinstance(inner, ast.Constant) and inner.value == "workspace")
            for side in sides
            for inner in ast.walk(side)
        )
        if reads_subtype and names_workspace:
            return True
    return False


def _names_a_chat_workspace(function: ast.AST) -> bool:
    for node in ast.walk(function):
        if isinstance(node, ast.Attribute) and node.attr == KEY:
            return True
        if isinstance(node, ast.Name) and node.id in {KEY, "chat_workspace_id"}:
            return True
        if _is_key(node) if isinstance(node, ast.expr) else False:
            return True
    return False


def _functions(tree: ast.AST) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    for node in ast.walk(tree):
        if isinstance(node, _FUNCTIONS):
            yield node


def hand_rolled_placements(source: str) -> list[str]:
    """The functions of ``source`` that find a workspace folder by its
    subtype and name a chat's workspace beside it."""
    return sorted(
        function.name
        for function in _functions(ast.parse(source))
        if _picks_a_workspace_folder(function) and _names_a_chat_workspace(function)
    )


def offences(root: Path, owner: Path) -> list[str]:
    found: list[str] = []
    for path in _sources(root):
        if path.resolve() == owner:
            continue
        source = path.read_text(encoding="utf-8")
        where = path.relative_to(root).as_posix()
        found.extend(f"{where}:{line} bare spec read" for line in bare_spec_reads(source))
        found.extend(f"{where}:{name} places a chat" for name in hand_rolled_placements(source))
    return found


def test_only_the_owner_relates_a_chat_to_a_workspace_folder() -> None:
    assert offences(REPO_ROOT, OWNER) == []


def test_the_scan_reads_the_real_tree() -> None:
    scanned = {path.relative_to(REPO_ROOT).as_posix() for path in _sources(REPO_ROOT)}
    assert "apps/backend/backend/services/notebooks/callers.py" in scanned
    assert "packages/api-core/alkera_core/compute/workspace_move.py" in scanned
    assert "packages/api-core/alkera_core/files/workspace_identity.py" in scanned
    assert not any("/tests/" in name for name in scanned)


def test_the_owner_itself_would_be_caught_elsewhere() -> None:
    """The rules are not vacuous: the owner does the very thing they refuse."""
    assert hand_rolled_placements(OWNER.read_text(encoding="utf-8")) == []
    planted = (
        OWNER.read_text(encoding="utf-8")
        + "\n\ndef both(chat, path):\n"
        + "    folder = workspace_folder_on(path)\n"
        + "    return [n for n in path if n.subtype == WORKSPACE_TYPE], chat_workspace_id(chat)\n"
    )
    assert hand_rolled_placements(planted) == ["both"]


#: The check that refused every agent edit in a workspace notebook: the
#: chat's workspace object id held against the workspace folder's node id.
_THE_REFUSING_CHECK = """
async def agent_chat_in_workspace(db, target, chat_id):
    chain = [*target.allowed.chain, target.node]
    workspace = None
    for node in reversed(chain):
        if node.subtype == WORKSPACE_TYPE:
            workspace = node
            break
    chat = await db.get(WorkspaceObject, chat_id)
    spec = chat.spec if isinstance(chat.spec, dict) else {}
    return str(spec.get("workspace_id") or "") == str(workspace.id)
"""


@pytest.mark.parametrize(
    ("source", "lines"),
    [
        pytest.param('x = (chat.spec or {}).get("workspace_id")\n', [1], id="get-off-chat-spec"),
        pytest.param('x = spec.get("workspace_id", "")\n', [1], id="get-off-a-local-spec"),
        pytest.param('\nx = chat.spec["workspace_id"]\n', [2], id="subscript"),
        pytest.param(_THE_REFUSING_CHECK, [11], id="the-refusing-check"),
        pytest.param(
            'named = WorkspaceObject.spec["workspace_id"].astext\n', [], id="sql-column-expression"
        ),
        pytest.param(
            "x = ChatSpec.model_validate(chat.spec or {}).workspace_id\n", [], id="typed-read"
        ),
        pytest.param('key = UUID(row["workspace_id"])\n', [], id="a-result-row-is-not-a-spec"),
        pytest.param('x = chat.spec.get("title")\n', [], id="another-key"),
        pytest.param('spec["workspace_id"] = str(workspace_id)\n', [], id="a-write"),
    ],
)
def test_a_bare_spec_read_is_found(source: str, lines: list[int]) -> None:
    assert bare_spec_reads(source) == lines


_TYPED_BUT_HAND_ROLLED = """
def in_workspace(chat, chain):
    for node in chain:
        if node.subtype == WORKSPACE_TYPE:
            return ChatSpec.model_validate(chat.spec).workspace_id == str(node.id)
    return False
"""

_LITERAL_SUBTYPE = """
def in_workspace(chat, chain):
    folder = next(n for n in chain if n.subtype == "workspace")
    return chat_workspace_id(chat) == folder.id
"""

_ONLY_FINDS_THE_FOLDER = """
def folder_of(chain):
    return next((n for n in chain if n.subtype == WORKSPACE_TYPE), None)
"""

_ONLY_READS_THE_CHAT = """
def workspace_of(chat):
    return ChatSpec.model_validate(chat.spec).workspace_id
"""

_SQL_BIND = """
async def end_lease(db, chat):
    workspace_id = chat_workspace_id(chat)
    await db.execute(QUERY, {"workspace": str(workspace_id), "subtype": WORKSPACE_TYPE})
"""

_QUERY_PREDICATE = """
async def folders_of(repo, chats):
    wanted = [chat_workspace_id(chat) for chat in chats]
    return await repo.execute_scoped(
        select(FileNode).where(
            FileNode.target_object_id.in_(wanted), FileNode.subtype == WORKSPACE_TYPE
        )
    )
"""


@pytest.mark.parametrize(
    ("source", "functions"),
    [
        pytest.param(_THE_REFUSING_CHECK, ["agent_chat_in_workspace"], id="the-refusing-check"),
        pytest.param(_TYPED_BUT_HAND_ROLLED, ["in_workspace"], id="a-typed-read-is-no-excuse"),
        pytest.param(_LITERAL_SUBTYPE, ["in_workspace"], id="the-subtype-as-a-literal"),
        pytest.param(_ONLY_FINDS_THE_FOLDER, [], id="finding-the-folder-alone"),
        pytest.param(_ONLY_READS_THE_CHAT, [], id="reading-the-chat-alone"),
        pytest.param(_SQL_BIND, [], id="a-subtype-bound-into-sql"),
        pytest.param(_QUERY_PREDICATE, [], id="a-query-predicate-on-the-mapped-class"),
    ],
)
def test_a_hand_rolled_placement_is_found(source: str, functions: list[str]) -> None:
    assert hand_rolled_placements(source) == functions


def test_an_offence_in_a_planted_tree_is_reported_with_its_place(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "backend" / "backend" / "services" / "x.py"
    module.parent.mkdir(parents=True)
    module.write_text(_THE_REFUSING_CHECK, encoding="utf-8")
    test_module = tmp_path / "apps" / "backend" / "tests" / "test_x.py"
    test_module.parent.mkdir(parents=True)
    test_module.write_text(_THE_REFUSING_CHECK, encoding="utf-8")
    (tmp_path / "packages").mkdir()
    assert offences(tmp_path, OWNER) == [
        "apps/backend/backend/services/x.py:11 bare spec read",
        "apps/backend/backend/services/x.py:agent_chat_in_workspace places a chat",
    ]
