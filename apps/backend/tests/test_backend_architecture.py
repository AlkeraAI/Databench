"""Architecture gates for ``apps/backend/backend``.

Each rule is checked against the real tree, and each allowlist records the
violations the tree had when the gate landed. An allowlist may only shrink: a
new violation fails, and so does an entry that no longer violates, so the list
is tightened in the change that fixes it. Each rule also has a decoy test that
plants a violating module in a temporary tree and proves the scan catches it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from _backend_architecture_scan import (
    BACKEND,
    authz_session_endings,
    cross_domain_submodule_imports,
    fastapi_imports_in_services,
    function_level_backend_imports,
    integrity_error_catches,
    module_lengths,
    private_imports,
    recorded_checks_in_loops,
    route_rollbacks,
    route_to_route_imports,
    sql_in_routes,
    string_resolved_imports,
    writes_before_first_enforce,
)

pytestmark = pytest.mark.xdist_group("backend_architecture")

#: The open modules' entries. A test layer adds its own modules' entries
#: through the root conftest's ``architecture_allowlist`` fixture.
OPEN_ALLOWLIST = json.loads(
    (Path(__file__).parent / "fixtures" / "backend_architecture_allowlist.json").read_text(
        encoding="utf-8"
    )
)

#: The longest a module may grow before it has to be split.
MAX_MODULE_LINES = 1500

#: Services that still import FastAPI or Starlette, and why each one stays.
FASTAPI_IN_SERVICES: Mapping[str, str] = {
    "backend.services.abuse.bans": (
        "raises RequestValidationError from a ban check; moving it would change the "
        "order the route's validation and ban refusal are raised in"
    ),
    "backend.services.chats.spares": (
        "claims a warm spare through enforce() with the Request and raises HTTPException "
        "inside the claim; a split would reorder the refusal and the claim"
    ),
    "backend.services.files.home": (
        "takes BackgroundTasks as a parameter type; replacing it needs a protocol, "
        "which is new code rather than a move"
    ),
    "backend.services.files.operations_runner": (
        "takes BackgroundTasks as a parameter type; replacing it needs a protocol, "
        "which is new code rather than a move"
    ),
    "backend.services.identity.lockout": (
        "builds the HTTPException a locked or wrong factor answers with; the callers "
        "raise it at different points, so converting it would change raise ordering"
    ),
    "backend.services.realtime.runtime": "a transport adapter for the websocket",
    "backend.services.realtime.session": "a transport adapter for the websocket",
}

#: Route modules that still import another route module, and why.
ROUTE_TO_ROUTE: Mapping[tuple[str, str], str] = {}

#: Functions that make a recorded single-resource check inside a loop, and why
#: each one is a run of access attempts rather than a listing.
RECORDED_CHECKS_IN_LOOPS: Mapping[str, str] = {
    "backend.api.routes.chats.chats::_message_attachments": (
        "each attachment a message names is re-read for the send; one refused "
        "node refuses the message"
    ),
    "backend.api.routes.files.bulk::_decide_each": (
        "a queued batch decides each item it will apply; each refusal is that item's result"
    ),
    "backend.api.routes.files.bulk::_decide_one": (
        "one bulk item names up to three nodes, each its own access attempt"
    ),
    "backend.api.routes.files.items::create_tree": (
        "a path walk decides each folder it enters or writes into, then writes"
    ),
    "backend.api.routes.files.leases::heartbeat_leases": (
        "each lease beat is its own write, in a transaction of its own"
    ),
    "backend.api.routes.files.trash::empty_trash": (
        "each trashed root is decided for DELETE and purged; a refusal skips that root"
    ),
    "backend.services.files.archive::_member": (
        "decides through the pure engine by default and records no row; skips "
        "are reported in the archive's own errors"
    ),
    "backend.services.files.decided::_file_decisions": (
        "files the rows one background decision produced; it decides nothing itself"
    ),
    "backend.services.files.guards::_refuse_unreadable": (
        "a copy's source is refused at the first hidden node the batched read "
        "could not admit; the refusal is the copy's answer"
    ),
    "backend.services.notebooks.socket::recheck": (
        "re-decides each channel a socket holds and drops the ones the person "
        "may no longer read; a socket holds a handful"
    ),
}


@pytest.fixture(scope="module")
def allowlist(
    architecture_allowlist: Callable[[str, Mapping[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    own = {**OPEN_ALLOWLIST, "fastapi_in_services": FASTAPI_IN_SERVICES}
    return architecture_allowlist("backend_architecture", own)


def _pairs(allowlist: Mapping[str, Any], key: str) -> set[tuple[str, str]]:
    return {(a, b) for a, b in allowlist[key]}


def _assert_exact(found: set[object], allowed: set[object], what: str) -> None:
    new = sorted(found - allowed, key=str)
    gone = sorted(allowed - found, key=str)
    assert not new, f"new {what}: {new}"
    assert not gone, f"{what} fixed; remove them from the allowlist: {gone}"


def _assert_ratchet(found: Mapping[str, int], allowed: Mapping[str, int], what: str) -> None:
    grew = {m: (n, allowed.get(m, 0)) for m, n in found.items() if n > allowed.get(m, 0)}
    shrank = {m: (found.get(m, 0), n) for m, n in allowed.items() if found.get(m, 0) < n}
    assert not grew, f"{what} grew (found, allowed): {grew}"
    assert not shrank, f"{what} shrank; lower the allowlist to (found, allowed): {shrank}"


def test_services_import_no_fastapi(allowlist: dict[str, Any]) -> None:
    _assert_exact(
        set(fastapi_imports_in_services()),
        set(allowlist["fastapi_in_services"]),
        "FastAPI imports in services",
    )


def test_no_route_module_imports_another() -> None:
    _assert_exact(set(route_to_route_imports()), set(ROUTE_TO_ROUTE), "route-to-route imports")


def test_no_cross_domain_import_of_a_service_submodule(allowlist: dict[str, Any]) -> None:
    _assert_exact(
        set(cross_domain_submodule_imports()),
        _pairs(allowlist, "cross_domain_submodule_imports"),
        "cross-domain service submodule imports",
    )


def test_no_cross_module_private_import(allowlist: dict[str, Any]) -> None:
    _assert_exact(set(private_imports()), _pairs(allowlist, "private_imports"), "private imports")


def test_sql_construction_in_routes_only_shrinks(allowlist: dict[str, Any]) -> None:
    _assert_ratchet(sql_in_routes(), allowlist["sql_in_routes"], "SQL built in routes")


def test_function_level_backend_imports_only_shrink(allowlist: dict[str, Any]) -> None:
    _assert_ratchet(
        function_level_backend_imports(),
        allowlist["function_level_backend_imports"],
        "function-level backend imports",
    )


def test_modules_stay_under_the_size_budget(allowlist: dict[str, Any]) -> None:
    over = {m: n for m, n in module_lengths().items() if n > MAX_MODULE_LINES}
    _assert_ratchet(over, allowlist["module_lengths"], "modules over the size budget")


def test_no_route_writes_before_its_first_recorded_check() -> None:
    """A refusal is rolled back by the request's own unit of work, never by the
    recorder, so a write made before the check is safe only while the route
    lets the refusal propagate. None does today; keep it that way."""
    assert writes_before_first_enforce() == []


def test_recording_a_decision_never_ends_the_callers_session() -> None:
    assert authz_session_endings() == []


def test_recorded_checks_in_loops_are_the_reviewed_ones() -> None:
    _assert_exact(
        set(recorded_checks_in_loops()),
        set(RECORDED_CHECKS_IN_LOOPS),
        "recorded single-resource checks in a loop (decide a batch through decide_many)",
    )


def test_rollbacks_in_routes_only_shrink(allowlist: dict[str, Any]) -> None:
    _assert_ratchet(route_rollbacks(), allowlist["route_rollbacks"], "rollbacks in routes")


def test_the_scan_reads_the_real_tree() -> None:
    assert (BACKEND / "app_factory.py").is_file()
    assert "backend.app_factory" in module_lengths()


# --- decoys: each scan catches a planted violation ---------------------------


def _plant(root: Path, files: Mapping[str, str]) -> Path:
    backend = root / "backend"
    for rel, body in files.items():
        path = backend / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        for parent in path.relative_to(backend).parents:
            init = backend / parent / "__init__.py"
            if not init.exists():
                init.write_text("", encoding="utf-8")
    return backend


def test_a_service_importing_fastapi_is_caught(tmp_path: Path) -> None:
    backend = _plant(
        tmp_path,
        {
            "services/org/teams.py": "from fastapi import HTTPException\n",
            "services/org/memberships.py": "import starlette.requests\n",
            "services/org/settings.py": "import json\n",
            "api/routes/org/teams.py": "from fastapi import APIRouter\n",
        },
    )
    assert fastapi_imports_in_services(backend) == {
        "backend.services.org.teams",
        "backend.services.org.memberships",
    }


def test_a_route_importing_another_route_is_caught(tmp_path: Path) -> None:
    backend = _plant(
        tmp_path,
        {
            "api/routes/org/teams.py": "from backend.api.routes.widgets.org import x\n",
            "api/routes/widgets/org.py": "x = 1\n",
            "api/routes/files/items.py": "from backend.api.routes.files.sharing import y\n",
            "api/routes/files/sharing.py": "y = 1\n",
            "api/routes/gadgets/gadgets.py": "from backend.api.routes import files\n",
        },
    )
    assert route_to_route_imports(backend) == {
        ("backend.api.routes.org.teams", "backend.api.routes.widgets.org"),
        ("backend.api.routes.gadgets.gadgets", "backend.api.routes.files"),
    }


def test_a_cross_domain_submodule_import_is_caught(tmp_path: Path) -> None:
    backend = _plant(
        tmp_path,
        {
            "services/org/teams.py": "x = 1\n",
            "services/org/memberships.py": "from backend.services.org.teams import x\n",
            "services/widgets/service.py": (
                "from backend.services.org import teams\nfrom backend.services.org import x\n"
            ),
            "api/routes/org/teams.py": "import backend.services.org.teams\n",
        },
    )
    assert cross_domain_submodule_imports(backend) == {
        ("backend.services.widgets.service", "backend.services.org.teams"),
        ("backend.api.routes.org.teams", "backend.services.org.teams"),
    }


def test_a_private_import_is_caught(tmp_path: Path) -> None:
    backend = _plant(
        tmp_path,
        {
            "services/org/teams.py": "_x = 1\nfrom backend.services.org.teams import _x\n",
            "services/org/settings.py": (
                "from backend.services.org.teams import _x, y\n"
                "from backend.services.org.teams import __doc__\n"
            ),
        },
    )
    assert private_imports(backend) == {
        ("backend.services.org.settings", "backend.services.org.teams._x")
    }


def test_sql_built_in_a_route_is_counted(tmp_path: Path) -> None:
    backend = _plant(
        tmp_path,
        {
            "api/routes/org/teams.py": (
                "from sqlalchemy import select, text\n"
                "from sqlalchemy import delete as sql_delete\n"
                "def f(d):\n"
                "    select(1); text('x'); sql_delete(2); d.update({})\n"
            ),
            "services/org/teams.py": "from sqlalchemy import select\nselect(1)\n",
        },
    )
    assert dict(sql_in_routes(backend)) == {"backend.api.routes.org.teams": 3}


def test_a_function_level_backend_import_is_counted(tmp_path: Path) -> None:
    backend = _plant(
        tmp_path,
        {
            "services/org/teams.py": (
                "import json\n"
                "def f():\n"
                "    from backend.services.org import settings\n"
                "    import json\n"
                "    if True:\n"
                "        import backend.services.org.settings\n"
            ),
            "services/org/settings.py": "x = 1\n",
        },
    )
    assert dict(function_level_backend_imports(backend)) == {"backend.services.org.teams": 2}


def test_module_length_is_measured(tmp_path: Path) -> None:
    backend = _plant(tmp_path, {"services/org/teams.py": "x = 1\n" * 1501})
    assert module_lengths(backend)["backend.services.org.teams"] == 1501


@pytest.mark.parametrize(
    ("found", "allowed", "message"),
    [
        pytest.param({"m": 3}, {"m": 2}, "grew", id="grew"),
        pytest.param({"m": 1}, {"m": 2}, "shrank", id="shrank"),
        pytest.param({"n": 1}, {}, "grew", id="new-module"),
    ],
)
def test_the_ratchet_refuses_growth_and_a_stale_ceiling(
    found: dict[str, int], allowed: dict[str, int], message: str
) -> None:
    with pytest.raises(AssertionError, match=message):
        _assert_ratchet(found, allowed, "x")


def test_the_ratchet_accepts_the_recorded_figures() -> None:
    _assert_ratchet({"m": 2}, {"m": 2}, "x")


@pytest.mark.parametrize(
    ("found", "allowed", "message"),
    [
        pytest.param({"a", "b"}, {"a"}, "new", id="new-violation"),
        pytest.param({"a"}, {"a", "b"}, "fixed", id="stale-entry"),
    ],
)
def test_an_allowlist_refuses_new_and_stale_entries(
    found: set[object], allowed: set[object], message: str
) -> None:
    with pytest.raises(AssertionError, match=message):
        _assert_exact(found, allowed, "x")


def test_no_backend_module_imports_by_a_computed_string() -> None:
    """The CLI binary bundles backend modules by following literal imports, so a
    module imported through a computed string is missing from the binary and
    fails only there (a lazily exported name did exactly that once)."""
    assert string_resolved_imports() == []


def test_a_computed_string_import_is_caught(tmp_path: Path) -> None:
    backend = _plant(
        tmp_path,
        {
            "services/org/__init__.py": (
                "from importlib import import_module\n"
                "def __getattr__(name):\n"
                "    return import_module(OWNERS[name])\n"
            ),
            "services/org/teams.py": "import importlib\nimportlib.import_module('json')\n",
        },
    )
    assert string_resolved_imports(backend) == ["backend.services.org"]


def test_a_package_owner_loader_is_not_a_function_level_import(tmp_path: Path) -> None:
    backend = _plant(
        tmp_path,
        {
            "services/org/__init__.py": (
                "def _owner_module(owner):\n"
                "    from backend.services.org import teams\n"
                "    return teams\n"
                "def other():\n"
                "    from backend.services.org import teams\n"
            ),
            "services/org/teams.py": "x = 1\n",
        },
    )
    assert dict(function_level_backend_imports(backend)) == {"backend.services.org": 1}


def test_a_write_before_the_first_recorded_check_is_caught(tmp_path: Path) -> None:
    backend = _plant(
        tmp_path,
        {
            "api/routes/org/teams.py": (
                "@router.post('/a')\n"
                "async def writes_first(db):\n"
                "    db.add(row)\n"
                "    await enforce(request, db, ctx, action, resource, attrs)\n"
                "@router.post('/b')\n"
                "async def statement_first(files):\n"
                "    await files.repo.session.execute(update(T).values(x=1))\n"
                "    await authorized(request, db, repo, ctx, node, action)\n"
                "@router.post('/c')\n"
                "async def checks_first(db, seen):\n"
                "    seen.add(1)\n"
                "    await enforce(request, db, ctx, action, resource, attrs)\n"
                "    db.add(row)\n"
            ),
        },
    )
    assert writes_before_first_enforce(backend) == [
        "backend.api.routes.org.teams::statement_first",
        "backend.api.routes.org.teams::writes_first",
    ]


def test_a_recorder_that_ends_the_callers_session_is_caught(tmp_path: Path) -> None:
    backend = _plant(
        tmp_path,
        {
            "authz/enforce.py": (
                "async def record_deny(self, db, event):\n"
                "    await db.rollback()\n"
                "async def record_allow(self, db, event):\n"
                "    db.expire_all()\n"
                "async def own(event):\n"
                "    async with Local() as session:\n"
                "        await session.commit()\n"
            ),
        },
    )
    assert authz_session_endings(backend) == [
        "backend.authz.enforce::record_allow:expire_all",
        "backend.authz.enforce::record_deny:rollback",
    ]


def test_a_recorded_check_in_a_loop_is_caught(tmp_path: Path) -> None:
    backend = _plant(
        tmp_path,
        {
            "api/routes/org/teams.py": (
                "from alkera_core.authz import authorize as pure\n"
                "async def per_row(rows):\n"
                "    for row in rows:\n"
                "        await enforce(request, db, ctx, action, row, attrs)\n"
                "async def comprehension(ids):\n"
                "    return [await _decide_one(i) for i in ids]\n"
                "async def batched(ids):\n"
                "    for page in pages:\n"
                "        await decide_many(request, db, ctx, action, page)\n"
                "def pure_engine(rows):\n"
                "    return [pure(ctx, action, r, {}) for r in rows]\n"
            ),
            "services/org/teams.py": (
                "async def recheck(self):\n    while True:\n        await self.decide(x)\n"
            ),
        },
    )
    assert recorded_checks_in_loops(backend) == [
        "backend.api.routes.org.teams::comprehension",
        "backend.api.routes.org.teams::per_row",
        "backend.services.org.teams::recheck",
    ]


def test_a_rollback_in_a_route_is_counted(tmp_path: Path) -> None:
    backend = _plant(
        tmp_path,
        {
            "api/routes/org/teams.py": "async def f(db):\n    await db.rollback()\n",
            "services/org/teams.py": "async def g(db):\n    await db.rollback()\n",
        },
    )
    assert dict(route_rollbacks(backend)) == {"backend.api.routes.org.teams": 1}


# --- database refusals are translated once -----------------------------------


def test_integrity_error_catches_only_shrink(allowlist: dict[str, Any]) -> None:
    """Each catch left is a site that does more than translate (a retry, a
    typed domain error a caller branches on, an upsert race); replace one with
    a registered constraint and lower its count."""
    _assert_ratchet(
        integrity_error_catches(), allowlist["integrity_error_catches"], "except IntegrityError"
    )


def test_an_integrity_error_catch_is_counted(tmp_path: Path) -> None:
    backend = _plant(
        tmp_path,
        {
            "services/org/teams.py": (
                "from sqlalchemy import exc\n"
                "from sqlalchemy.exc import IntegrityError\n"
                "def f():\n"
                "    try:\n"
                "        pass\n"
                "    except IntegrityError:\n"
                "        pass\n"
                "    except (ValueError, exc.IntegrityError):\n"
                "        pass\n"
                "    except ValueError:\n"
                "        pass\n"
            ),
        },
    )
    core = tmp_path / "core" / "alkera_core"
    (core / "db").mkdir(parents=True)
    (core / "__init__.py").write_text("", encoding="utf-8")
    (core / "db" / "__init__.py").write_text("", encoding="utf-8")
    owner = (
        "from sqlalchemy.exc import IntegrityError\n"
        "try:\n    pass\nexcept IntegrityError:\n    pass\n"
    )
    (core / "db" / "errors.py").write_text(owner, encoding="utf-8")
    (core / "db" / "other.py").write_text(owner, encoding="utf-8")
    assert dict(integrity_error_catches((backend, core))) == {
        "backend.services.org.teams": 2,
        "alkera_core.db.other": 1,
    }
