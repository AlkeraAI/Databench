"""A machine's name comes from the person who named it or from one picker.

An org's live machines have unique names, so a name the server chooses for a
machine nobody named must step around the ones already there. The dedicated box
assignment named every machine it made "Machine 1" and failed the insert for an
org that already had one. This scan holds every place a machine row is made to
one of two sources for its name: a request's own ``name`` (a person chose it;
the unique index answers a clash with ``name_taken``), or
``free_machine_name``, the one picker that steps around taken names.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
ROOTS = (
    REPO / "apps" / "backend" / "backend",
    REPO / "apps" / "worker" / "worker",
    REPO / "packages" / "api-core" / "alkera_core",
)

#: The calls that make a machine row and take its name.
MAKERS = frozenset({"OrgMachine", "create_org_machine"})

#: Request bodies a route hands a service: their ``name`` is the caller's.
REQUEST_BODIES = frozenset({"body", "payload"})

#: The one picker for a name the server chooses.
PICKER = "free_machine_name"

#: The function that is itself the maker, passing its own ``name`` through.
OWNER = "create_org_machine"


def _called(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _name_from_an_allowed_source(value: ast.expr, enclosing: str) -> bool:
    if isinstance(value, ast.Await):
        value = value.value
    if isinstance(value, ast.Call) and _called(value) == PICKER:
        return True
    if (
        isinstance(value, ast.Attribute)
        and value.attr == "name"
        and isinstance(value.value, ast.Name)
        and value.value.id in REQUEST_BODIES
    ):
        return True
    return enclosing == OWNER and isinstance(value, ast.Name) and value.id == "name"


def machine_names_minted_elsewhere(roots: tuple[Path, ...] = ROOTS) -> list[str]:
    """Every call that makes a machine row with a name from anywhere else."""
    found: list[str] = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for fn in ast.walk(tree):
                if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                for node in ast.walk(fn):
                    if not (isinstance(node, ast.Call) and _called(node) in MAKERS):
                        continue
                    name = next((k.value for k in node.keywords if k.arg == "name"), None)
                    if name is None or not _name_from_an_allowed_source(name, fn.name):
                        found.append(f"{path.relative_to(root.parent)}:{node.lineno}")
    return sorted(set(found))


def test_every_machine_name_is_the_callers_or_the_pickers() -> None:
    assert machine_names_minted_elsewhere() == []


def test_the_scan_sees_the_real_makers() -> None:
    """A scan that found no maker at all would pass having proven nothing."""
    makers = 0
    for root in ROOTS:
        for path in root.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call) and _called(node) in MAKERS:
                    makers += 1
    assert makers >= 4


def test_a_name_minted_anywhere_else_is_caught(tmp_path: Path) -> None:
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "makers.py").write_text(
        "async def assign(db, body, alloc):\n"
        "    OrgMachine(name='Machine 1')\n"
        "    OrgMachine(name=f'{alloc.name} box')\n"
        "    await create_org_machine(db, name=alloc.name)\n"
        "    OrgMachine(org_team_id=1)\n"
        "    OrgMachine(name=await free_machine_name(db, org_id=1, wanted='Machine 1'))\n"
        "    await create_org_machine(db, name=body.name)\n"
        "async def create_org_machine(db, *, name):\n"
        "    return OrgMachine(name=name)\n"
        "async def other(db, *, name):\n"
        "    return OrgMachine(name=name)\n",
        encoding="utf-8",
    )
    assert machine_names_minted_elsewhere((root,)) == [
        "pkg/makers.py:11",
        "pkg/makers.py:2",
        "pkg/makers.py:3",
        "pkg/makers.py:4",
        "pkg/makers.py:5",
    ]
