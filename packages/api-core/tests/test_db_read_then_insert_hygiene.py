"""A row read, then inserted when it was missing, is a race nobody serialized.

The shape is ``row = <read> ... if row is None: db.add(...)``: two requests that
both read before either commits both insert, and one of them is refused as a
duplicate; when the other branch writes the row back, the later of two writers
drops what the earlier one changed. The one mechanism for it is
:func:`alkera_core.db.locking.lock_or_insert` (insert if missing, then lock and
read), or any statement that serializes the read: a row lock, an advisory lock,
or an ``ON CONFLICT`` insert.

This scan finds every function with the shape and none of those, in the
server-side packages. Today's sites are listed with why each is tolerable for
now; the list may only shrink, and a site that is gone fails until its entry is
removed.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]

SCANNED: Final[tuple[Path, ...]] = (
    REPO_ROOT / "packages/api-core/alkera_core",
    REPO_ROOT / "apps/backend/backend",
    REPO_ROOT / "apps/worker/worker",
    REPO_ROOT / "apps/model-gateway/model_gateway",
)

#: What makes a read safe to act on: a lock or a conflict-tolerant insert.
SERIALIZED_BY: Final[tuple[str, ...]] = (
    "lock_or_insert",
    "lock_rows",
    "lock_text",
    "with_for_update",
    "on_conflict",
    "advisory_xact_lock",
    "lock_namespace",
    "lock_chain",
    "lock_node",
    "lock_drive",
)

#: Today's sites, ``(module, function): why it is tolerated``. Only shrinks.
ALLOWED: Final[dict[tuple[str, str], str]] = {
    ("packages/api-core/alkera_core/readiness.py", "probe"): (
        "adds a check's name to a Python set, not a row"
    ),
    ("apps/backend/backend/seeds/compute.py", "upsert_machine_types"): (
        "development seed, run by one process"
    ),
    ("apps/backend/backend/services/connection_oauth/__init__.py", "exchange_and_store"): (
        "same race, one OAuth callback per grant; to move onto lock_or_insert"
    ),
    ("apps/backend/backend/services/notebooks/runs.py", "follow_runs"): (
        "same race, a box's run events read then inserted; one kernel's batches come "
        "from its one machine in order; to move onto lock_or_insert"
    ),
    ("apps/backend/backend/services/org/memberships.py", "add_member"): (
        "callers hold the org's team-tree lock"
    ),
    ("apps/backend/backend/services/compute/offerings.py", "carried_over_offering"): (
        "platform admin catalog edit, one operator at a time"
    ),
    ("apps/backend/backend/services/compute/offerings.py", "_set_fallback"): (
        "platform admin catalog edit, one operator at a time"
    ),
    ("apps/backend/backend/services/compute/offerings.py", "assign_dedicated"): (
        "platform admin catalog edit, one operator at a time"
    ),
    ("apps/backend/backend/services/slack/install.py", "record_install"): (
        "one OAuth callback per install; a duplicate is refused by the unique key"
    ),
    ("packages/api-core/alkera_core/billing/proxy.py", "ensure_proxy_billing_identity"): (
        "runs once per deployment at boot"
    ),
}


def _is_none_test(node: ast.If, read: set[str]) -> bool:
    test = node.test
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id in read
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Is)
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value is None
    )


def _adds(nodes: list[ast.stmt]) -> bool:
    return any(
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "add"
        for stmt in nodes
        for call in ast.walk(stmt)
    )


def _awaited_names(function: ast.AST) -> set[str]:
    """Names this function binds to something it awaited: a row it read,
    directly or through a helper."""
    names: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
            if not any(isinstance(inner, ast.Await) for inner in ast.walk(node.value)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(target.id for target in targets if isinstance(target, ast.Name))
    return names


def _serialized(function: ast.AST) -> bool:
    for node in ast.walk(function):
        if isinstance(node, ast.Attribute) and node.attr in SERIALIZED_BY:
            return True
        if isinstance(node, ast.Name) and node.id in SERIALIZED_BY:
            return True
    return False


def _sites(source: str) -> list[str]:
    """Every function in ``source`` with the shape and nothing serializing it."""
    found: list[str] = []
    tree = ast.parse(source)

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                read = _awaited_names(child)
                inserts_when_missing = any(
                    isinstance(branch, ast.If)
                    and _is_none_test(branch, read)
                    and _adds(branch.body)
                    for branch in ast.walk(child)
                )
                if inserts_when_missing and not _serialized(child):
                    found.append(f"{prefix}{child.name}")
                walk(child, f"{prefix}{child.name}.")

    walk(tree, "")
    return found


def _scan() -> set[tuple[str, str]]:
    sites: set[tuple[str, str]] = set()
    for root in SCANNED:
        for path in sorted(root.rglob("*.py")):
            module = path.relative_to(REPO_ROOT).as_posix()
            for function in _sites(path.read_text(encoding="utf-8")):
                sites.add((module, function))
    return sites


def test_no_new_read_then_insert_without_a_lock() -> None:
    new = sorted(_scan() - set(ALLOWED))
    assert not new, (
        "these read a row and insert it when missing with nothing serializing the two; "
        f"take it through lock_or_insert instead: {new}"
    )


def test_the_allowlist_only_shrinks() -> None:
    stale = sorted(set(ALLOWED) - _scan())
    assert not stale, f"no longer racy, remove from ALLOWED: {stale}"


@pytest.mark.parametrize(
    ("source", "caught"),
    [
        pytest.param(
            "async def save(db, uid):\n"
            "    row = (await db.execute(q)).scalar_one_or_none()\n"
            "    if row is None:\n"
            "        db.add(Row(uid))\n",
            True,
            id="read-then-add",
        ),
        pytest.param(
            "async def save(db, uid):\n"
            "    row = await db.get(Row, uid)\n"
            "    if row is None:\n"
            "        row = Row(uid)\n"
            "        db.add(row)\n"
            "    row.value = 1\n",
            True,
            id="get-then-add-then-write",
        ),
        pytest.param(
            "async def save(db, uid):\n"
            "    row = await _row(db, uid)\n"
            "    if row is None:\n"
            "        db.add(Row(uid))\n"
            "    else:\n"
            "        row.value = 1\n",
            True,
            id="read-through-a-helper",
        ),
        pytest.param(
            "async def save(db, uid):\n"
            "    row = (await lock_or_insert(db, rank, q, ins)).scalar_one()\n"
            "    row.value = 1\n",
            False,
            id="lock-or-insert",
        ),
        pytest.param(
            "async def save(db, uid):\n"
            "    row = (await lock_rows(db, rank, q)).scalar_one_or_none()\n"
            "    if row is None:\n"
            "        db.add(Row(uid))\n",
            False,
            id="read-under-a-lock",
        ),
        pytest.param(
            "async def save(db, uid):\n"
            "    row = (await db.execute(q)).scalar_one_or_none()\n"
            "    if row is None:\n"
            "        return None\n",
            False,
            id="read-only",
        ),
    ],
)
def test_the_scan_catches_a_planted_race(tmp_path: Path, source: str, caught: bool) -> None:
    planted = tmp_path / "planted.py"
    planted.write_text(source, encoding="utf-8")
    assert bool(_sites(planted.read_text(encoding="utf-8"))) is caught
