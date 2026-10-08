"""Live modules that write one shared endpoint run in that endpoint's xdist group.

The ``live (local services)`` job runs ``-n <workers> --dist loadgroup``, and the root
conftest groups each module by its own path, so two modules writing the same system
run concurrently on different workers. A stock Trino has one ``memory`` catalog and
the lineage seed introspects all of it: when test_trino_live.py dropped its schema
between test_gate_pipeline_trino_live.py's baseline and head, the gate reported
TABLE_DROPPED and ``blocked`` a recompile that should pass. The unit of isolation is
the endpoint, so every live module that reaches it must name the same group.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_TESTS_ROOT = Path(__file__).resolve().parents[1]

#: The env var a live module reads to reach a shared endpoint -> the xdist group
#: every such module must declare. A new shared endpoint is one more row.
_SHARED_ENDPOINTS = {"TRINO_HOST": "trino-memory-catalog"}


def _module_marks(tree: ast.Module) -> list[ast.expr]:
    """The expressions of the module-level ``pytestmark`` (a single mark or a list)."""
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
        ):
            value = node.value
            return list(value.elts) if isinstance(value, ast.List | ast.Tuple) else [value]
    return []


def _is_live(marks: list[ast.expr]) -> bool:
    return any(ast.unparse(m).split(".")[-1] == "live" for m in marks)


def _declared_group(marks: list[ast.expr]) -> str | None:
    for mark in marks:
        if not isinstance(mark, ast.Call) or ast.unparse(mark.func).split(".")[-1] != (
            "xdist_group"
        ):
            continue
        args = [*mark.args, *(k.value for k in mark.keywords if k.arg == "name")]
        if args and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
            return args[0].value
    return None


def _live_modules_reaching(env_var: str) -> dict[str, str | None]:
    found: dict[str, str | None] = {}
    for path in sorted(_TESTS_ROOT.rglob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        if f'"{env_var}"' not in source:
            continue
        marks = _module_marks(ast.parse(source))
        if _is_live(marks):
            found[path.relative_to(_TESTS_ROOT).as_posix()] = _declared_group(marks)
    return found


@pytest.mark.parametrize(
    ("env_var", "group"),
    [pytest.param(k, v, id=k) for k, v in _SHARED_ENDPOINTS.items()],
)
def test_every_live_module_on_a_shared_endpoint_names_its_group(env_var: str, group: str) -> None:
    modules = _live_modules_reaching(env_var)
    # The two files that raced are the reason this guard exists; if the walk stops
    # finding them it is checking nothing.
    assert {"e2e/test_trino_live.py", "e2e/test_gate_pipeline_trino_live.py"} <= set(modules)
    wrong = {name: declared for name, declared in modules.items() if declared != group}
    assert not wrong, (
        f"live modules reaching {env_var} must declare "
        f'pytest.mark.xdist_group("{group}") so they never run concurrently: {wrong}'
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("import pytest\npytestmark = pytest.mark.live\n", None, id="live-only"),
        pytest.param(
            'import pytest\npytestmark = [pytest.mark.live, pytest.mark.xdist_group("g")]\n',
            "g",
            id="list",
        ),
        pytest.param(
            'import pytest\npytestmark = [pytest.mark.live, pytest.mark.xdist_group(name="k")]\n',
            "k",
            id="keyword",
        ),
        pytest.param(
            'import pytest\n@pytest.mark.xdist_group("t")\ndef test_a(): ...\n',
            None,
            id="per-test-mark-does-not-group-the-module",
        ),
    ],
)
def test_the_declared_group_is_read_from_the_module_pytestmark(
    source: str, expected: str | None
) -> None:
    assert _declared_group(_module_marks(ast.parse(source))) == expected
