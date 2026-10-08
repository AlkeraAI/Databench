"""A box capability is spelled once: as a :class:`BoxCapability` member.

The backend gates features on what a box reports, and the box reports from the
same enum. A capability written as a bare string on either side drifts from
the other the day one of them renames it, and the gate then silently refuses
every box. So no string literal equal to a capability's value may appear in
shipped source outside ``box_contract.py`` (generated clients aside: they
restate the API's field names).

Some of those values are also ordinary words (``"workspaces"`` is a router
tag and a directory name, ``"gpu"`` a compute class). Each such homonym is
listed below with its count and the reason it is not a capability. The list
may only shrink: a new literal fails, and so does an entry that no longer
matches the tree.
"""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

import pytest
from _source_roots import shipped_source_roots
from alkera_core.compute.box_contract import BoxCapability

REPO_ROOT = Path(__file__).resolve().parents[4]
OWNER = "packages/api-core/alkera_core/compute/box_contract.py"

#: ``(path, value) -> (count, reason)``: literals that equal a capability value
#: but do not mean the capability.
HOMONYMS: dict[tuple[str, str], tuple[int, str]] = {
    ("apps/backend/backend/api/routes/workspaces/workspaces.py", "workspaces"): (
        1,
        "the OpenAPI tag of the workspaces routers",
    ),
    ("apps/backend/backend/api/routes/workspaces/machine.py", "workspaces"): (
        1,
        "the OpenAPI tag of the workspace machine router",
    ),
    ("apps/backend/backend/api/routes/chats/wake.py", "workspaces"): (
        1,
        "the OpenAPI tag of the workspace wake router",
    ),
    ("apps/backend/backend/api/routes/connections/held.py", "workspaces"): (
        1,
        "the OpenAPI tag of the held connections router",
    ),
    ("apps/backend/backend/services/compute/ssh_machines.py", "gpu"): (
        2,
        "a machine type's compute class, not what a box reports",
    ),
    ("packages/api-core/alkera_core/compute/ssh/provider.py", "gpu"): (
        1,
        "a machine type's compute class, not what a box reports",
    ),
    ("apps/backend/backend/api/routes/chats/read_state.py", "workspaces"): (
        1,
        "the OpenAPI tag of the workspace read state router",
    ),
    ("packages/api-core/alkera_core/files/providers/registry.py", "workspaces"): (
        1,
        "the web route segment a workspace object opens on",
    ),
    ("apps/cli/alkera_cli/cloud/custody_layout.py", "workspaces"): (
        1,
        "the name of the directory a box keeps workspace folders in",
    ),
    ("apps/backend/backend/services/compute/offerings.py", "gpu"): (
        1,
        "the key of an offering's GPU description",
    ),
    ("apps/cli/alkera_cli/supervisor/service.py", "org_workers"): (
        1,
        "the supervisor status field that counts running org workers",
    ),
    ("packages/api-core/alkera_core/compute/ec2.py", "gpu"): (
        1,
        "a machine type's compute class, not what a box reports",
    ),
    ("packages/api-core/alkera_core/compute/localdev.py", "gpu"): (
        1,
        "a machine type's compute class, not what a box reports",
    ),
    ("packages/api-core/alkera_core/compute/runpod.py", "gpu"): (
        2,
        "a machine type's compute class, not what a box reports",
    ),
    ("packages/api-core/alkera_core/status/machine.py", "gpu"): (
        2,
        "the reason code of a machine status that waits for a free GPU",
    ),
}


def scan_roots(repo_root: Path) -> list[str]:
    """The shipped source roots."""
    return shipped_source_roots(repo_root)


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def capability_literals(
    repo_root: Path, roots: Iterable[str], values: frozenset[str]
) -> Counter[tuple[str, str]]:
    """Every string literal equal to a capability value, by file, outside the owner."""
    found: Counter[tuple[str, str]] = Counter()
    for root in roots:
        for path in sorted((repo_root / root).rglob("*.py")):
            rel = path.relative_to(repo_root).as_posix()
            if rel == OWNER or "/_marimo/" in rel or "/_generated/" in rel:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            docstrings = _docstring_ids(tree)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and node.value in values
                    and id(node) not in docstrings
                ):
                    found[(rel, node.value)] += 1
    return found


def violations(found: Counter[tuple[str, str]]) -> tuple[list[str], list[str]]:
    """``(new, stale)``: literals beyond the homonym list, and list entries the
    tree no longer has."""
    new = [
        f"{path}: {value!r} x{count}"
        for (path, value), count in sorted(found.items())
        if count > HOMONYMS.get((path, value), (0, ""))[0]
    ]
    stale = [
        f"{path}: {value!r} (listed x{listed}, found x{found.get((path, value), 0)})"
        for (path, value), (listed, _reason) in sorted(HOMONYMS.items())
        if found.get((path, value), 0) < listed
    ]
    return new, stale


VALUES = frozenset(member.value for member in BoxCapability)


def test_capabilities_are_spelled_only_in_the_contract() -> None:
    new, stale = violations(capability_literals(REPO_ROOT, scan_roots(REPO_ROOT), VALUES))
    assert not new, (
        "a box capability is spelled as a string; use BoxCapability from "
        "alkera_core.compute.box_contract:\n  " + "\n  ".join(new)
    )
    assert not stale, "remove these homonym entries, the tree no longer has them:\n  " + (
        "\n  ".join(stale)
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            'CAPS = ("model_switch_v1",)\n', ["pkg/mod.py: 'model_switch_v1' x1"], id="tuple"
        ),
        pytest.param(
            'def f(c):\n    return "org_workers" in c\n',
            ["pkg/mod.py: 'org_workers' x1"],
            id="membership",
        ),
        pytest.param('"""Names model_switch_v1 in prose."""\n', [], id="docstring-is-prose"),
        pytest.param('X = "model_switch"\n', [], id="near-miss"),
    ],
)
def test_the_scan_catches_a_planted_literal(
    tmp_path: Path, source: str, expected: list[str]
) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text(source, encoding="utf-8")
    new, _stale = violations(capability_literals(tmp_path, ["pkg"], VALUES))
    assert new == expected


def test_a_homonym_that_left_the_tree_is_stale(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("X = 1\n", encoding="utf-8")
    _new, stale = violations(capability_literals(tmp_path, ["pkg"], VALUES))
    assert len(stale) == len(HOMONYMS)


# --------------------------------------------------------------------------- #
# Capabilities a box may only claim from what it found out about itself
# --------------------------------------------------------------------------- #

#: Where the box's own code lives.
BOX_ROOT = "apps/cli/alkera_cli"
#: ``capability -> the one function that may name it on the box``. The server
#: places a second org on a box that names ``org_isolation``, so the box says
#: it only from the report of the probe that tried each mechanism, never from
#: a list a build carries.
#: The registry that says which part of the box claims each capability: it
#: names every member as a declaration (its own test pins the probed ones to
#: the probe), not a claim.
REGISTRY = "box_capabilities.py"
PROBED_HOMES: dict[BoxCapability, str] = {
    BoxCapability.ORG_ISOLATION: "IsolationReport.capabilities",
}


def _enclosing(tree: ast.AST) -> dict[int, str]:
    """``id(node) -> "Class.function"`` (or ``"function"``, or ``"<module>"``)
    for every node, by the innermost function (else class) that holds it."""
    names: dict[int, str] = {}

    def visit(node: ast.AST, classes: tuple[str, ...], function: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, (*classes, child.name), None)
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                visit(child, classes, ".".join((*classes, child.name)))
            else:
                names[id(child)] = function or ".".join(classes) or "<module>"
                visit(child, classes, function)

    visit(tree, (), None)
    return names


def probed_claims(repo_root: Path, box_root: str) -> dict[BoxCapability, list[tuple[str, str]]]:
    """``capability -> [(file, enclosing function)]`` for every place the
    box's code names a probed capability."""
    members = {capability.name: capability for capability in PROBED_HOMES}
    found: dict[BoxCapability, list[tuple[str, str]]] = {c: [] for c in PROBED_HOMES}
    for path in sorted((repo_root / box_root).rglob("*.py")):
        rel = path.relative_to(repo_root).as_posix()
        if rel == f"{box_root}/{REGISTRY}":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        enclosing = _enclosing(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr in members
                and isinstance(node.value, ast.Name)
                and node.value.id == "BoxCapability"
            ):
                found[members[node.attr]].append((rel, enclosing[id(node)]))
    return found


def misplaced_claims(found: dict[BoxCapability, list[tuple[str, str]]]) -> list[str]:
    """Every probed capability named outside its one home, or in more than one."""
    wrong: list[str] = []
    for capability, places in sorted(found.items()):
        home = PROBED_HOMES[capability]
        wrong.extend(
            f"{rel}: {capability.name} is named in {function}, not {home}"
            for rel, function in places
            if function != home
        )
        homes = sorted({rel for rel, function in places if function == home})
        if len(homes) > 1:
            wrong.append(f"{capability.name} has {len(homes)} homes: {', '.join(homes)}")
    return wrong


def test_a_probed_capability_is_claimed_only_by_its_probe() -> None:
    assert misplaced_claims(probed_claims(REPO_ROOT, BOX_ROOT)) == []


_HOME = (
    "class IsolationReport:\n"
    "    def capabilities(self):\n"
    "        return (BoxCapability.ORG_ISOLATION,) if self.namespaced else ()\n"
)


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        pytest.param({"probe.py": _HOME}, [], id="the-probe-report"),
        pytest.param({"probe.py": "X = 1\n"}, [], id="nobody-claims-it"),
        pytest.param(
            {"claims.py": "CLAIMS = (BoxCapability.ORG_ISOLATION,)\n"},
            ["box/claims.py: ORG_ISOLATION is named in <module>, not IsolationReport.capabilities"],
            id="a-list-the-build-carries",
        ),
        pytest.param(
            {"beat.py": "def heartbeat():\n    return [BoxCapability.ORG_ISOLATION]\n"},
            ["box/beat.py: ORG_ISOLATION is named in heartbeat, not IsolationReport.capabilities"],
            id="another-function",
        ),
        pytest.param(
            {
                "other.py": "class Other:\n    def capabilities(self):\n"
                "        return (BoxCapability.ORG_ISOLATION,)\n"
            },
            [
                "box/other.py: ORG_ISOLATION is named in Other.capabilities, "
                "not IsolationReport.capabilities"
            ],
            id="another-class",
        ),
        pytest.param(
            {
                "probe.py": "class IsolationReport:\n    def capabilities(self):\n"
                "        return ()\n    def heartbeat(self):\n"
                "        return [BoxCapability.ORG_ISOLATION]\n"
            },
            [
                "box/probe.py: ORG_ISOLATION is named in IsolationReport.heartbeat, "
                "not IsolationReport.capabilities"
            ],
            id="another-method-of-the-report",
        ),
        pytest.param(
            {"probe.py": _HOME, "copy.py": _HOME},
            ["ORG_ISOLATION has 2 homes: box/copy.py, box/probe.py"],
            id="a-second-report",
        ),
    ],
)
def test_a_planted_claim_outside_the_probe_is_caught(
    tmp_path: Path, sources: dict[str, str], expected: list[str]
) -> None:
    (tmp_path / "box").mkdir()
    for name, source in sources.items():
        (tmp_path / "box" / name).write_text(source, encoding="utf-8")
    assert misplaced_claims(probed_claims(tmp_path, "box")) == expected
