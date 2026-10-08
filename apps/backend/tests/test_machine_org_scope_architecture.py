"""A box's org is the org of what it acts on, never its credential's.

A shared box speaks on a machine credential minted in its OPERATOR's org and
acts on chats, drives and workspaces of the customer orgs it serves. Code that
scopes a query by the principal's own org (``ctx.org_id``, ``principal.org_id``,
``OrgScope(org_team_id=ctx.org_id)``) answers a box about the operator's rows:
it finds nothing and says not found, or worse, it answers with the operator's
data. The org a box acts in comes from the resource it was admitted to: the
Files node's drive through ``backend.services.files.node_scope``, or the row's
own ``org_team_id`` once the policy admitted the box to it.

So every read of a principal's own org in the backend's services, dependencies
and routes is counted per module, and a count may only shrink. Today's counts
are reads on a person's door, on the credential's own standing (a box's claim,
heartbeat and logs are about the box itself), or of a decision's fallback org;
a new one is the resource's org instead. A read that must stay the principal's
own is never a raised count: it is a named exception in ``EXPLAINED``, keyed by
module and function with its reason, which excuses exactly one read in that
function. Both lists may only shrink, and an entry whose read is gone fails.

``current_org`` (``CurrentOrg``) is the dependency spelling of the same read.
No route a box can reach on its machine credential may declare it: a box asking
"my org's settings" would be answered about its operator. Today every route
that declares it sits behind a person's gate, and the second gate below keeps
it that way.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import pytest
from backend.auth.dependencies import (
    current_org,
    current_user,
    require_browser_session,
    require_verified_or_grace,
)
from fastapi import APIRouter, Depends
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from tests.conftest import fastapi_app

pytestmark = [pytest.mark.spread]

BACKEND = Path(__file__).resolve().parents[1] / "backend"

#: Where a box's requests run: every service, dependency and route module.
SCANNED = ("services", "api/deps", "api/routes")

#: The names a principal travels under, and the attribute every holder of one
#: exposes it as (``files.ctx``, ``reader.ctx``, ``self.ctx``).
PRINCIPAL_NAMES = frozenset({"ctx", "principal"})
PRINCIPAL_ATTRIBUTE = "ctx"

#: Reads of a principal's own org per open module when the gate landed. May only
#: shrink. A test layer adds its own modules' counts under ``reads`` through the
#: root conftest's ``architecture_allowlist`` fixture.
ALLOWED: Mapping[str, int] = {
    "backend.services.chats.duplicate": 1,
    "backend.services.chats.send_admission": 1,
    "backend.services.chats.spares": 10,
    "backend.services.chats.templates": 2,
    "backend.services.compute.grants": 7,
    "backend.services.compute.machines": 1,
    "backend.services.compute.org_admission": 2,
    "backend.services.compute.org_machine_access": 4,
    "backend.services.compute.org_machine_buying": 4,
    "backend.services.compute.placement": 6,
    "backend.services.compute.service": 1,
    "backend.services.files.context": 4,
    "backend.services.files.decided": 1,
    "backend.services.files.facts": 3,
    "backend.services.notebooks.callers": 1,
    "backend.services.org.teams": 2,
    "backend.services.realtime.session": 2,
    "backend.services.realtime.tickets": 1,
    "backend.services.sharing.access": 8,
    "backend.api.deps.chat_placement": 1,
    "backend.api.deps.connection_leases": 2,
    "backend.api.routes.audit.org": 2,
    "backend.api.routes.chats.chats": 12,
    "backend.api.routes.chats.templates": 2,
    "backend.api.routes.compute.box_logs": 1,
    "backend.api.routes.compute.compute": 5,
    "backend.api.routes.compute.machines": 12,
    "backend.api.routes.connections.team_connections": 15,
    "backend.api.routes.files.content_serve": 1,
    "backend.api.routes.files.items": 3,
    "backend.api.routes.objects.objects": 5,
    "backend.api.routes.org.memberships": 1,
    "backend.api.routes.realtime.events": 2,
    "backend.api.routes.realtime.ws": 1,
    "backend.api.routes.workspaces.workspaces": 9,
}


@dataclass(frozen=True)
class ReadSite:
    """Where a read sits: its module and its enclosing function's dotted name
    (``Class.method``, ``outer.inner``; ``<module>`` at module level)."""

    module: str
    function: str


#: Named exceptions: reads of a principal's own org that are reviewed and stay.
#: Each entry excuses exactly one read in that function, so a second read there
#: still counts. May only shrink; an entry with no read left to excuse fails.
EXPLAINED: Mapping[ReadSite, str] = {
    # Reached from ``_machine_drive``: a pool box stands on its credential alone
    # in the org it was minted in, and no rows of that org are read on it.
    ReadSite("backend.services.files.context", "_stands_in"): (
        "a machine's own operator org, used only to identify the machine, "
        "never to scope tenant data"
    ),
}


def _is_principal(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id in PRINCIPAL_NAMES
    return isinstance(node, ast.Attribute) and node.attr == PRINCIPAL_ATTRIBUTE


def _is_read(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "org_id" and _is_principal(node.value)


def _reads_in(node: ast.AST, scope: tuple[str, ...]) -> list[str]:
    """The enclosing function of every read under ``node``."""
    found: list[str] = []
    for child in ast.iter_child_nodes(node):
        inner = scope
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            inner = (*scope, child.name)
        if _is_read(child):
            found.append(".".join(scope) or "<module>")
        found.extend(_reads_in(child, inner))
    return found


def principal_org_read_sites(root: Path = BACKEND) -> list[ReadSite]:
    """Every ``<principal>.org_id`` read under ``root``'s scanned trees."""
    found: list[ReadSite] = []
    for sub in SCANNED:
        for path in sorted((root / sub).rglob("*.py")):
            module = path.relative_to(root).with_suffix("").as_posix().replace("/", ".")
            tree = ast.parse(path.read_text(encoding="utf-8"))
            found.extend(ReadSite(f"backend.{module}", fn) for fn in _reads_in(tree, ()))
    return found


def counted_reads(
    sites: list[ReadSite], explained: Mapping[ReadSite, str]
) -> tuple[dict[str, int], set[ReadSite]]:
    """Reads per module once each named exception has excused its one read,
    and the exceptions that found no read to excuse."""
    unexcused = set(explained)
    found: dict[str, int] = {}
    for site in sites:
        if site in unexcused:
            unexcused.discard(site)
            continue
        found[site.module] = found.get(site.module, 0) + 1
    return found, unexcused


def principal_org_reads(
    root: Path = BACKEND, explained: Mapping[ReadSite, str] | None = None
) -> dict[str, int]:
    """Unexplained ``<principal>.org_id`` reads per module under ``root``."""
    found, _ = counted_reads(principal_org_read_sites(root), explained or {})
    return found


def _assert_ratchet(found: Mapping[str, int], allowed: Mapping[str, int]) -> None:
    grew = {m: (n, allowed.get(m, 0)) for m, n in found.items() if n > allowed.get(m, 0)}
    shrank = {m: (found.get(m, 0), n) for m, n in allowed.items() if found.get(m, 0) < n}
    assert not grew, (
        "a principal's own org is read in more places (found, allowed). A box acts "
        "in the org of the resource it names: take it from node_scope or the row's "
        f"org_team_id, or name it in EXPLAINED with its reason: {grew}"
    )
    assert not shrank, f"reads removed; lower the allowlist to (found, allowed): {shrank}"


def test_reads_of_a_principals_own_org_only_shrink(
    architecture_allowlist: Callable[[str, Mapping[str, Any]], dict[str, Any]],
) -> None:
    allowed = architecture_allowlist("machine_org_scope", {"reads": ALLOWED})["reads"]
    _assert_ratchet(principal_org_reads(explained=EXPLAINED), allowed)


def test_every_named_exception_still_excuses_a_read() -> None:
    _, stale = counted_reads(principal_org_read_sites(), EXPLAINED)
    assert not stale, f"the read is gone; remove the named exception: {stale}"


def test_the_scan_reads_the_real_tree() -> None:
    found = principal_org_reads()
    assert found["backend.services.sharing.access"] >= 1
    assert sum(found.values()) > 50


def _plant(root: Path, files: Mapping[str, str]) -> Path:
    for relative, body in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("x = ctx.org_id\n", id="ctx"),
        pytest.param("x = principal.org_id\n", id="principal"),
        pytest.param("x = files.ctx.org_id\n", id="a-holders-ctx"),
        pytest.param(
            "repo = FilesRepo(db, OrgScope(org_team_id=self.ctx.org_id))\n", id="org-scope"
        ),
    ],
)
def test_a_new_read_is_caught(tmp_path: Path, source: str) -> None:
    root = _plant(tmp_path, {"services/files/fresh.py": source, "api/deps/clean.py": "x = 1\n"})
    found = principal_org_reads(root)
    assert found == {"backend.services.files.fresh": 1}
    with pytest.raises(AssertionError, match="more places"):
        _assert_ratchet(found, {})


def test_the_resources_org_is_not_counted(tmp_path: Path) -> None:
    root = _plant(
        tmp_path,
        {
            "services/files/fine.py": (
                "a = drive.org_team_id\nb = chat.org_team_id\nc = target.org_id\n"
                "d = ctx.acting_principal.org_id\n"
            )
        },
    )
    assert principal_org_reads(root) == {}


def test_a_stale_ceiling_is_refused() -> None:
    with pytest.raises(AssertionError, match="lower the allowlist"):
        _assert_ratchet({"backend.services.x": 1}, {"backend.services.x": 2})


BOXY = ReadSite("backend.services.files.boxy", "Box._mine")


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(
            "class Box:\n    def _mine(self):\n        return self.ctx.org_id\n",
            {},
            id="the-excused-read",
        ),
        pytest.param(
            "class Box:\n    def _mine(self):\n        return self.ctx.org_id or self.ctx.org_id\n",
            {"backend.services.files.boxy": 1},
            id="a-second-read-in-the-same-function",
        ),
        pytest.param(
            "class Box:\n    def _mine(self):\n        return self.ctx.org_id\n"
            "    def _yours(self):\n        return self.ctx.org_id\n",
            {"backend.services.files.boxy": 1},
            id="a-read-in-another-function",
        ),
        pytest.param(
            "def _mine(ctx):\n    return ctx.org_id\n",
            {"backend.services.files.boxy": 1},
            id="same-name-outside-the-class",
        ),
    ],
)
def test_a_named_exception_excuses_one_read_in_its_function_only(
    tmp_path: Path, body: str, expected: dict[str, int]
) -> None:
    root = _plant(tmp_path, {"services/files/boxy.py": body})
    assert principal_org_reads(root, {BOXY: "the box's own org"}) == expected


def test_a_named_exception_with_no_read_left_is_stale(tmp_path: Path) -> None:
    root = _plant(
        tmp_path, {"services/files/boxy.py": "class Box:\n    def _mine(self):\n        return 1\n"}
    )
    _, stale = counted_reads(principal_org_read_sites(root), {BOXY: "the box's own org"})
    assert stale == {BOXY}


CONTEXT = "backend.services.files.context"


def _plant_context(root: Path, extra: str = "") -> Path:
    source = (BACKEND / "services/files/context.py").read_text(encoding="utf-8")
    return _plant(root, {"services/files/context.py": source + extra})


def test_files_context_as_it_stands_passes_its_ceiling(tmp_path: Path) -> None:
    found = principal_org_reads(_plant_context(tmp_path), EXPLAINED)
    _assert_ratchet(found, {CONTEXT: ALLOWED[CONTEXT]})


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param(
            "\n\ndef _fresh(principal):\n    return principal.org_id\n", id="new-function"
        ),
        pytest.param(
            "\n\nasync def _stands_in(db, principal, org_id):\n"
            "    return org_id == principal.org_id\n",
            id="second-read-under-the-excused-name",
        ),
    ],
)
def test_a_new_unexplained_read_in_files_context_fails(tmp_path: Path, extra: str) -> None:
    found = principal_org_reads(_plant_context(tmp_path, extra), EXPLAINED)
    with pytest.raises(AssertionError, match="more places"):
        _assert_ratchet(found, {CONTEXT: ALLOWED[CONTEXT]})


#: Dependencies only a person's credential passes: a machine credential is
#: refused by each before the route runs.
PERSON_ONLY = frozenset({current_user, require_verified_or_grace, require_browser_session})


def _calls(dependant: Dependant) -> set[object]:
    found: set[object] = set()
    for sub in dependant.dependencies:
        if sub.call is not None:
            found.add(sub.call)
        found |= _calls(sub)
    return found


def machine_routes_naming_the_request_org(routes: list[object]) -> set[tuple[str, str]]:
    """``(methods, path)`` of every route a box on its machine credential can
    reach (no person's gate, no session-only principal) that still declares
    ``current_org``."""
    found: set[tuple[str, str]] = set()
    for route in routes:
        if not isinstance(route, APIRoute):
            continue
        calls = _calls(route.dependant)
        person_only = bool(calls & PERSON_ONLY)
        if current_org in calls and not person_only:
            found.add((",".join(sorted(route.methods)), route.path))
    return found


def test_no_route_a_box_reaches_names_the_request_org() -> None:
    assert machine_routes_naming_the_request_org(list(fastapi_app.routes)) == set()


def test_a_box_route_naming_the_request_org_is_caught() -> None:
    router = APIRouter()

    @router.get("/box-door")
    async def box_door(org: Annotated[UUID, Depends(current_org)]) -> None:
        del org

    @router.get("/person-door", dependencies=[Depends(require_verified_or_grace)])
    async def person_door(org: Annotated[UUID, Depends(current_org)]) -> None:
        del org

    assert machine_routes_naming_the_request_org(list(router.routes)) == {("GET", "/box-door")}
