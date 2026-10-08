"""The facts the tenancy gates read from the source tree.

A request's org comes from its credential and from nowhere else. These scans
find the code that reaches for an org some other way, so the gates in
``test_tenancy_architecture.py`` can refuse any of it:

* **user-org reads** — ``.org_team_id`` read off a user-like object (``user``,
  ``caller``, ``owner``, ``self.user``, ``User.org_team_id`` …). Each one assumes
  a person has exactly one org. None may exist.
* **unscoped membership queries** — a function that filters
  ``TeamMembership.user_id`` without also constraining
  ``TeamMembership.org_team_id``: "every team this person is in", across all of
  their orgs. None may exist.
* **encoder calls** — who calls the session, CLI and gateway token encoders.
  Only the mint helper and the token module itself may.

Pure AST reads: nothing here imports what it scans. The route-table gate (no
client-supplied org) needs the real app and lives in the test module.

Known blind spot: the scans match names, not types. A user object bound to a
name outside :data:`USER_RECEIVERS`, or ``TeamMembership`` reached through an
alias, is not counted; the receiver list is the set the codebase actually uses.
"""

from __future__ import annotations

import ast
import json
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

#: The source trees the gates read, as (directory relative to the repo, the
#: top-level package name the directory holds).
SCOPES: tuple[tuple[str, str], ...] = (
    ("apps/backend/backend", "backend"),
    ("apps/worker/worker", "worker"),
    ("apps/model-gateway/model_gateway", "model_gateway"),
    ("packages/api-core/alkera_core", "alkera_core"),
)

#: Modules the user-org scan never counts: the model that defines the column,
#: and the membership check that is the one place allowed to compare against it.
USER_ORG_EXCLUDED = frozenset({"alkera_core.models.user", "alkera_core.auth.tenancy"})

#: The modules that may read ``home_org_team_id`` (the identity's home org):
#: the model that maps it, and the tenancy module, which answers "where does a
#: sign-in that names no org land" and moves a home when an org is deleted.
HOME_ORG_READERS = frozenset({"alkera_core.models.user", "alkera_core.auth.tenancy"})

#: The names a user row is bound to across the codebase.
USER_RECEIVERS = frozenset(
    {
        "user",
        "caller",
        "admin",
        "owner",
        "member",
        "target",
        "target_user",
        "me",
        "u",
        "actor",
        "inviter",
        "existing",
        "account_owner",
        "current_user",
        "speaker",
        "account",
        "holder",
        "person",
    }
)

#: The functions that turn claims into a signed token. Calling one is minting a
#: credential, which only the mint helper may do.
ENCODERS = frozenset(
    {"encode_session_token", "encode_cli_token", "mint_gateway_token", "mint_machine_gateway_token"}
)

#: Each tenancy area of the open modules and the module prefixes it owns; the
#: longest matching prefix wins. The cross-tenant route matrix files every
#: operation, seed and parameter table under its area. A test layer adds its own
#: modules' prefixes (new areas, or more prefixes for an open one) under the
#: ``areas`` key of its ``tenancy_architecture.json``; see :func:`layer_areas`.
OPEN_AREAS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "objects_files",
        (
            "backend.services.objects",
            "backend.api.routes.objects",
            "backend.services.files",
            "backend.api.routes.files",
            "backend.services.sharing",
            "backend.content_app",
            "alkera_core.files",
            "alkera_core.objects",
        ),
    ),
    (
        "chats_compute_gate",
        (
            "backend.services.chats",
            "backend.api.routes.chats",
            "backend.services.compute",
            "backend.api.routes.compute",
            "backend.services.workspaces",
            "backend.api.routes.workspaces",
        ),
    ),
    (
        "org_connections",
        (
            "backend.services.org",
            "backend.api.routes.org",
            "backend.services.connections",
            "backend.api.routes.connections",
        ),
    ),
    (
        "realtime_slack",
        (
            "backend.services.realtime",
            "backend.api.routes.realtime",
            "backend.services.crdt",
        ),
    ),
    ("worker", ("worker",)),
    (
        "identity",
        (
            "backend.services.identity",
            "backend.api.routes.identity",
            "backend.services.audit",
            "backend.api.routes.audit",
            "backend.services.abuse",
            "backend.api.admin",
        ),
    ),
    (
        "auth",
        (
            "backend.auth",
            "backend.authz",
            "backend.services.credentials",
            "alkera_core.auth",
            "alkera_core.authz",
        ),
    ),
    ("gateway", ("model_gateway",)),
    ("api_core", ("alkera_core",)),
)
#: Where a distribution's test layer sits (the root conftest loads it as
#: plugins). The open tree ships none, so the open areas stand alone there.
LAYER_DIR = Path(__file__).resolve().parents[3] / "conftest_layers"


def layer_areas(layer_dir: Path = LAYER_DIR) -> dict[str, list[str]]:
    """The areas a test layer adds, by name. Read at import, not through the
    ``pytest_alkera_architecture_allowlist`` hook, because the cross-tenant
    route matrix files its operations by area when it is imported."""
    path = layer_dir / "tenancy_architecture.json"
    if not path.is_file():
        return {}
    areas: dict[str, list[str]] = json.loads(path.read_text(encoding="utf-8")).get("areas", {})
    return areas


def merge_areas(
    own: Iterable[tuple[str, tuple[str, ...]]], extra: dict[str, list[str]]
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """``own`` with ``extra``'s prefixes added: to the open area of the same
    name, or as a new area after the open ones."""
    merged = {name: tuple(prefixes) for name, prefixes in own}
    for name, prefixes in extra.items():
        merged[name] = (*merged.get(name, ()), *prefixes)
    return tuple(merged.items())


AREAS = merge_areas(OPEN_AREAS, layer_areas())
OTHER_AREA = "other"
AREA_NAMES: tuple[str, ...] = (*(name for name, _ in AREAS), OTHER_AREA)


def area_of(module: str, areas: Iterable[tuple[str, tuple[str, ...]]] = AREAS) -> str:
    """The tenancy area that owns ``module``: the area of the longest prefix
    that matches it as a whole package name."""
    best, best_len = OTHER_AREA, -1
    for name, prefixes in areas:
        for p in prefixes:
            if (module == p or module.startswith(f"{p}.")) and len(p) > best_len:
                best, best_len = name, len(p)
    return best


def iter_modules(
    scopes: Iterable[tuple[Path, str]] | None = None,
) -> Iterator[tuple[str, ast.Module]]:
    """``(dotted name, parsed tree)`` for every module in the scopes, which
    default to :data:`SCOPES` under the repo."""
    roots = scopes if scopes is not None else ((REPO / rel, pkg) for rel, pkg in SCOPES)
    for root, package in roots:
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            parts = [package, *path.relative_to(root).with_suffix("").parts]
            if parts[-1] == "__init__":
                parts = parts[:-1]
            yield ".".join(parts), ast.parse(path.read_text(encoding="utf-8"))


def _is_user_org_read(node: ast.Attribute) -> bool:
    if node.attr != "org_team_id":
        return False
    value = node.value
    if isinstance(value, ast.Name):
        return value.id in USER_RECEIVERS or value.id == "User"
    return (
        isinstance(value, ast.Attribute)
        and value.attr == "user"
        and isinstance(value.value, ast.Name)
        and value.value.id == "self"
    )


def user_org_reads(scopes: Iterable[tuple[Path, str]] | None = None) -> dict[str, int]:
    """Per module, how many times ``.org_team_id`` is read off a user."""
    counts: Counter[str] = Counter()
    for name, tree in iter_modules(scopes):
        if name in USER_ORG_EXCLUDED:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and _is_user_org_read(node):
                counts[name] += 1
    return dict(counts)


def home_org_reads(scopes: Iterable[tuple[Path, str]] | None = None) -> dict[str, int]:
    """Per module outside :data:`HOME_ORG_READERS`, how many times
    ``.home_org_team_id`` is read, whatever it is read off. A keyword argument
    that sets it on a new row is a write and is not counted."""
    counts: Counter[str] = Counter()
    for name, tree in iter_modules(scopes):
        if name in HOME_ORG_READERS:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "home_org_team_id":
                counts[name] += 1
    return dict(counts)


def _own_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """Every node inside ``scope`` that does not belong to a function nested in
    it: a nested function is a scope of its own."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))


def _membership_columns(scope: ast.AST) -> set[str]:
    return {
        node.attr
        for node in _own_nodes(scope)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "TeamMembership"
    }


def unscoped_membership_queries(
    scopes: Iterable[tuple[Path, str]] | None = None,
) -> dict[str, int]:
    """Per module, how many functions (module-level code counts as one) name
    ``TeamMembership.user_id`` without also naming ``TeamMembership.org_team_id``."""
    counts: Counter[str] = Counter()
    for name, tree in iter_modules(scopes):
        scopes_in_module: list[ast.AST] = [tree]
        scopes_in_module += [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        ]
        for scope in scopes_in_module:
            columns = _membership_columns(scope)
            if "user_id" in columns and "org_team_id" not in columns:
                counts[name] += 1
    return dict(counts)


def encoder_callers(scopes: Iterable[tuple[Path, str]] | None = None) -> set[str]:
    """The modules that call a token encoder (:data:`ENCODERS`)."""
    found: set[str] = set()
    for name, tree in iter_modules(scopes):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if called in ENCODERS:
                found.add(name)
    return found
