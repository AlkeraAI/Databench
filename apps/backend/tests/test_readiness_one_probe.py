"""Gate: the backend and the gateway answer ``/health/ready`` through the one
shared probe in ``alkera_core.readiness``.

The gateway's readiness was once a copy of the backend's, and the copy had
neither the canary nor the store check nor a bounded reset. So the rule is
structural: each app has one readiness route, that route calls ``probe``, and
no app module marks the task ready on the latch itself (only the shared probe
decides that every check passed). The scan reads the real trees; the decoys
prove it catches what it is for.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spread]

REPO = Path(__file__).resolve().parents[3]
APPS = {
    "backend": REPO / "apps" / "backend" / "backend",
    "gateway": REPO / "apps" / "model-gateway" / "model_gateway",
}


def _route_path(fn: ast.AsyncFunctionDef | ast.FunctionDef, prefix: str) -> str | None:
    for deco in fn.decorator_list:
        if not (isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute)):
            continue
        if deco.func.attr != "get" or not deco.args:
            continue
        first = deco.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return prefix + first.value
    return None


def _router_prefix(tree: ast.Module) -> str:
    """The ``APIRouter(prefix=...)`` a module declares, else no prefix."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "APIRouter":
            for kw in node.keywords:
                if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                    return str(kw.value.value)
    return ""


def _calls(fn: ast.AST, name: str) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            func = node.func
            called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if called == name:
                return True
    return False


def _marks_ready(tree: ast.AST) -> bool:
    """A ``<something>latch.ready()`` call: marking the task ready by hand."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "ready"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id.endswith("latch")
        ):
            return True
    return False


def scan(root: Path) -> tuple[list[str], list[str]]:
    """``(readiness routes and whether each calls probe, modules marking ready)``.

    The first list holds ``"<file>::<fn> probe"`` or ``"<file>::<fn> own"``.
    """
    routes: list[str] = []
    marking: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        label = str(path.relative_to(root))
        if _marks_ready(tree):
            marking.append(label)
        prefix = _router_prefix(tree)
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            if _route_path(fn, prefix) != "/health/ready":
                continue
            routes.append(f"{label}::{fn.name} {'probe' if _calls(fn, 'probe') else 'own'}")
    return routes, marking


@pytest.mark.parametrize("app", sorted(APPS))
def test_each_app_answers_readiness_through_the_shared_probe(app: str) -> None:
    routes, marking = scan(APPS[app])
    assert len(routes) == 1, f"{app} must have exactly one /health/ready route: {routes}"
    assert routes[0].endswith(" probe"), f"{app} answers readiness on its own: {routes}"
    assert marking == [], f"{app} marks the task ready outside the shared probe: {marking}"


def _plant(root: Path, body: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "health.py").write_text(body, encoding="utf-8")
    return root


def test_the_scan_catches_a_readiness_route_of_its_own(tmp_path: Path) -> None:
    root = _plant(
        tmp_path / "app",
        "router = APIRouter(prefix='/health')\n"
        "@router.get('/ready')\n"
        "async def ready(db):\n"
        "    await db.execute(text('SELECT 1'))\n"
        "    probe_latch.ready()\n"
        "    return JSONResponse(status_code=200, content={})\n",
    )
    routes, marking = scan(root)
    assert routes == ["health.py::ready own"]
    assert marking == ["health.py"]


def test_the_scan_admits_a_route_on_the_shared_probe(tmp_path: Path) -> None:
    root = _plant(
        tmp_path / "app",
        "@application.get('/health/ready')\n"
        "async def ready():\n"
        "    return (await probe(READINESS, db, byok=False)).response()\n",
    )
    assert scan(root) == (["health.py::ready probe"], [])
