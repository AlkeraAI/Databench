"""The facts the backend architecture gates read from the source tree.

Pure AST reads over ``apps/backend/backend``: nothing here imports the
modules it scans, so a gate can run without a database or settings.
"""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"

#: The SQLAlchemy constructors a route builds a statement with.
SQL_BUILDERS = frozenset({"select", "insert", "update", "delete", "text", "union", "union_all"})


@dataclass(frozen=True)
class Module:
    name: str
    path: Path
    tree: ast.Module
    lines: int


def modules(root: Path = BACKEND) -> list[Module]:
    """Every module under ``root``, a directory holding the ``backend`` package."""
    found = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(root.parent).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        text = path.read_text(encoding="utf-8")
        found.append(Module(".".join(parts), path, ast.parse(text), len(text.splitlines())))
    return found


def is_package(dotted: str, root: Path = BACKEND) -> bool:
    return (root.parent / dotted.replace(".", "/") / "__init__.py").exists()


def is_module(dotted: str, root: Path = BACKEND) -> bool:
    base = root.parent / dotted.replace(".", "/")
    return base.with_suffix(".py").exists() or (base / "__init__.py").exists()


def imported_modules(node: ast.Import | ast.ImportFrom, root: Path = BACKEND) -> list[str]:
    """Every backend module an import statement loads, submodules named in a
    ``from package import submodule`` included."""
    if isinstance(node, ast.Import):
        return [a.name for a in node.names if a.name.split(".")[0] == "backend"]
    if node.level or not node.module or node.module.split(".")[0] != "backend":
        return []
    out = [node.module]
    if is_package(node.module, root):
        out += [
            f"{node.module}.{a.name}"
            for a in node.names
            if is_module(f"{node.module}.{a.name}", root)
        ]
    return out


def all_imports(module: Module) -> list[ast.Import | ast.ImportFrom]:
    return [n for n in ast.walk(module.tree) if isinstance(n, (ast.Import, ast.ImportFrom))]


#: The function a package ``__init__`` uses to import the submodule that owns a
#: lazily exported name. Its imports are the package's own submodules, written
#: as literal imports so the compiled CLI build can follow them, not cycle dodges.
OWNER_LOADER = "_owner_module"


def function_level_imports(module: Module) -> list[ast.Import | ast.ImportFrom]:
    """Imports anywhere inside a function body, each counted once even when
    functions nest. A package ``__init__``'s owner loader is not counted."""
    seen: dict[int, ast.Import | ast.ImportFrom] = {}
    is_init = module.path.name == "__init__.py"
    for fn in ast.walk(module.tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if is_init and fn.name == OWNER_LOADER:
                continue
            for stmt in fn.body:
                for n in ast.walk(stmt):
                    if isinstance(n, (ast.Import, ast.ImportFrom)):
                        seen[id(n)] = n
    return list(seen.values())


def service_domain(dotted: str) -> str | None:
    parts = dotted.split(".")
    if parts[:2] == ["backend", "services"] and len(parts) >= 3:
        return parts[2]
    return None


def fastapi_imports_in_services(root: Path = BACKEND) -> set[str]:
    hits = set()
    for m in modules(root):
        if service_domain(m.name) is None:
            continue
        for node in all_imports(m):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            if any(n.split(".")[0] in {"fastapi", "starlette"} for n in names):
                hits.add(m.name)
    return hits


def route_to_route_imports(root: Path = BACKEND) -> set[tuple[str, str]]:
    prefix = "backend.api.routes."
    files = "backend.api.routes.files"
    hits = set()
    for m in modules(root):
        if not m.name.startswith(prefix):
            continue
        for node in all_imports(m):
            for target in imported_modules(node, root):
                if not target.startswith(prefix) or target == m.name:
                    continue
                if is_package(target, root) and not target.startswith(files):
                    continue  # a domain package's __init__ holds no route
                inside_files = m.name.startswith(files) and target.startswith(files)
                if not inside_files:
                    hits.add((m.name, target))
    return hits


def cross_domain_submodule_imports(root: Path = BACKEND) -> set[tuple[str, str]]:
    hits = set()
    for m in modules(root):
        own = service_domain(m.name)
        for node in all_imports(m):
            for target in imported_modules(node, root):
                domain = service_domain(target)
                if domain is None or domain == own:
                    continue
                if target == f"backend.services.{domain}":
                    continue  # the package's own contract
                hits.add((m.name, target))
    return hits


def private_imports(root: Path = BACKEND) -> set[tuple[str, str]]:
    hits = set()
    for m in modules(root):
        for node in all_imports(m):
            if not isinstance(node, ast.ImportFrom) or node.level or not node.module:
                continue
            if node.module.split(".")[0] != "backend" or node.module == m.name:
                continue
            for a in node.names:
                if a.name.startswith("_") and not a.name.startswith("__"):
                    hits.add((m.name, f"{node.module}.{a.name}"))
    return hits


def sql_in_routes(root: Path = BACKEND) -> Counter[str]:
    counts: Counter[str] = Counter()
    for m in modules(root):
        if not m.name.startswith("backend.api.routes."):
            continue
        for node in ast.walk(m.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if _sqlalchemy_builder(m, node.func.id):
                    counts[m.name] += 1
    return counts


def _sqlalchemy_builder(module: Module, local: str) -> bool:
    """Whether ``local`` is one of :data:`SQL_BUILDERS` imported from SQLAlchemy."""
    for node in all_imports(module):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "sqlalchemy":
            for a in node.names:
                if (a.asname or a.name) == local and a.name in SQL_BUILDERS:
                    return True
    return False


def function_level_backend_imports(root: Path = BACKEND) -> Counter[str]:
    counts: Counter[str] = Counter()
    for m in modules(root):
        for node in function_level_imports(m):
            if imported_modules(node, root):
                counts[m.name] += 1
    return counts


def module_lengths(root: Path = BACKEND) -> dict[str, int]:
    return {m.name: m.lines for m in modules(root)}


def string_resolved_imports(root: Path = BACKEND) -> list[str]:
    """Service modules that import a module by a string they compute.

    The compiled CLI build reaches into ``backend.services`` (the Files store)
    and bundles only what literal imports name, so a string import there is a
    module the binary may be missing. Route discovery under ``backend.api`` is
    out of the binary's reach and keeps its computed imports."""
    found = []
    for m in modules(root):
        if not m.name.startswith("backend.services"):
            continue
        for node in ast.walk(m.tree):
            if (
                isinstance(node, ast.Call)
                and (
                    (isinstance(node.func, ast.Name) and node.func.id == "import_module")
                    or (isinstance(node.func, ast.Attribute) and node.func.attr == "import_module")
                )
                and not (node.args and isinstance(node.args[0], ast.Constant))
            ):
                found.append(m.name)
                break
    return sorted(found)


# --- authorization decisions --------------------------------------------------

#: The calls that decide and record one resource: the platform choke point,
#: the Files glue and library entry points, and a direct denial record.
RECORDED_CHECKS = frozenset(
    {"enforce", "platform_enforce", "authorized", "authorize", "record_deny", "authorized_node"}
)

#: Name stems of the wrappers routes put around one recorded check
#: (``_decide``, ``_authorize``, ``_decide_one``, ``self.decide``...).
CHECK_STEMS = ("decide", "authoriz", "enforce")

#: Wrapper names that are one recorded check without a telling stem.
CHECK_WRAPPERS = frozenset({"_resolve"})

#: Calls that decide a whole batch: what a loop of single checks becomes.
BATCH_DECISIONS = frozenset({"decide_many", "decided_page", "decided_by_id"})

#: Modules whose ``authorize`` is the pure engine, which records nothing.
PURE_ENGINES = frozenset({"alkera_core.authz", "alkera_core.authz.engine"})

#: Session methods that write.
SESSION_WRITES = frozenset({"add", "add_all", "flush", "merge"})

#: SQLAlchemy statement builders that write.
WRITE_BUILDERS = frozenset({"insert", "update", "delete"})

#: The decorators that make a function a route.
ROUTE_VERBS = frozenset({"get", "post", "put", "patch", "delete", "api_route", "websocket"})

#: Session-ending calls a decision recorder must never make on its caller's session.
SESSION_ENDINGS = frozenset({"rollback", "commit", "expire_all", "expire"})


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]


def _is_route(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for deco in fn.decorator_list:
        target = deco.func if isinstance(deco, ast.Call) else deco
        if isinstance(target, ast.Attribute) and target.attr in ROUTE_VERBS:
            return True
    return False


def _builds_a_write(node: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in WRITE_BUILDERS
        for n in ast.walk(node)
    )


#: How a route names its database session: ``db``, ``session``, or a
#: ``.session`` attribute (``files.repo.session``).
SESSION_NAMES = frozenset({"db", "session"})


def _is_session(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id in SESSION_NAMES
    return isinstance(node, ast.Attribute) and node.attr == "session"


def _is_session_write(node: ast.Call) -> bool:
    if not (isinstance(node.func, ast.Attribute) and _is_session(node.func.value)):
        return False
    if node.func.attr in SESSION_WRITES:
        return True
    return node.func.attr == "execute" and any(_builds_a_write(arg) for arg in node.args)


def _label(module: Module, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    return f"{module.name}::{fn.name}"


def writes_before_first_enforce(root: Path = BACKEND) -> list[str]:
    """Route functions that write through a session before their first
    recorded check. A refusal raised there is rolled back by the request's own
    unit of work; one the route catches would keep the write, so each hit is
    reviewed rather than assumed safe."""
    found = []
    for m in modules(root):
        if not m.name.startswith("backend.api."):
            continue
        for fn in _functions(m.tree):
            if not _is_route(fn):
                continue
            calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
            checks = [c.lineno for c in calls if _call_name(c) in RECORDED_CHECKS]
            if not checks:
                continue
            first = min(checks)
            if any(_is_session_write(c) and c.lineno < first for c in calls):
                found.append(_label(m, fn))
    return sorted(set(found))


def authz_session_endings(root: Path = BACKEND) -> list[str]:
    """Calls in ``backend.authz`` that end, commit or expire a session that was
    passed in as a parameter. Recording a decision is an audit side effect: it
    never touches the transaction of the code being audited."""
    found = []
    for m in modules(root):
        if not m.name.startswith("backend.authz"):
            continue
        for fn in _functions(m.tree):
            params = {a.arg for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs}
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                receiver = node.func.value
                if (
                    node.func.attr in SESSION_ENDINGS
                    and isinstance(receiver, ast.Name)
                    and receiver.id in params
                ):
                    found.append(f"{_label(m, fn)}:{node.func.attr}")
    return sorted(set(found))


def _pure_names(module: Module) -> set[str]:
    """Local names bound to the pure engine's ``authorize``."""
    return {
        a.asname or a.name
        for node in all_imports(module)
        if isinstance(node, ast.ImportFrom) and node.module in PURE_ENGINES
        for a in node.names
        if a.name == "authorize"
    }


def _is_single_check(node: ast.Call, pure: set[str]) -> bool:
    name = _call_name(node)
    if name in BATCH_DECISIONS or (isinstance(node.func, ast.Name) and name in pure):
        return False
    stem = name.lstrip("_")
    return name in RECORDED_CHECKS or name in CHECK_WRAPPERS or stem.startswith(CHECK_STEMS)


def recorded_checks_in_loops(root: Path = BACKEND) -> list[str]:
    """Functions in routes and services that make a recorded single-resource
    check lexically inside a loop or a comprehension. Deciding over many
    resources is :func:`backend.authz.decide_many`'s job: one decision per
    resource, one row per batch, nothing raised per row."""
    found = []
    for m in modules(root):
        if not m.name.startswith(("backend.api.routes.", "backend.services.")):
            continue
        pure = _pure_names(m)
        for fn in _functions(m.tree):
            for loop in ast.walk(fn):
                if not isinstance(
                    loop,
                    ast.For
                    | ast.AsyncFor
                    | ast.While
                    | ast.ListComp
                    | ast.SetComp
                    | ast.DictComp
                    | ast.GeneratorExp,
                ):
                    continue
                if any(
                    isinstance(n, ast.Call) and _is_single_check(n, pure) for n in ast.walk(loop)
                ):
                    found.append(_label(m, fn))
                    break
    return sorted(set(found))


def route_rollbacks(root: Path = BACKEND) -> Counter[str]:
    """``.rollback()`` calls in route modules, per module. A rollback in a
    route expires every row the request loaded; each one is a place a later
    attribute read can fail outside the async context."""
    counts: Counter[str] = Counter()
    for m in modules(root):
        if not m.name.startswith("backend.api.routes."):
            continue
        for node in ast.walk(m.tree):
            if isinstance(node, ast.Call) and _call_name(node) == "rollback":
                counts[m.name] += 1
    return counts


#: The trees whose code can catch a database error raised by a request: the
#: backend, the model gateway and the shared core. Each maps a top-level
#: package to the directory that holds it.
REPO = BACKEND.parents[2]
INTEGRITY_SCAN_ROOTS: tuple[Path, ...] = (
    BACKEND,
    REPO / "apps" / "model-gateway" / "model_gateway",
    REPO / "packages" / "api-core" / "alkera_core",
)

#: The one module that may name ``IntegrityError`` in an ``except``: the
#: classifier every request's refusal goes through.
INTEGRITY_OWNER = "alkera_core.db.errors"


def _names_integrity_error(node: ast.expr | None) -> bool:
    if node is None:
        return False
    if isinstance(node, ast.Tuple):
        return any(_names_integrity_error(item) for item in node.elts)
    if isinstance(node, ast.Name):
        return node.id == "IntegrityError"
    if isinstance(node, ast.Attribute):
        return node.attr == "IntegrityError"
    return False


def integrity_error_catches(roots: tuple[Path, ...] = INTEGRITY_SCAN_ROOTS) -> Counter[str]:
    """``except IntegrityError`` handlers per module.

    A database refusal is translated once, by the classifier, from the
    constraint registered beside the model; a hand catch at a create is a
    second translation that drifts from it."""
    counts: Counter[str] = Counter()
    for root in roots:
        for m in modules(root):
            if m.name == INTEGRITY_OWNER:
                continue
            for node in ast.walk(m.tree):
                if isinstance(node, ast.ExceptHandler) and _names_integrity_error(node.type):
                    counts[m.name] += 1
    return counts
