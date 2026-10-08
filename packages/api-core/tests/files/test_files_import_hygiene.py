"""Import direction for `alkera_core.files`.

Two rules, forever:

1. **The library is the product, not a leaf of an app.** `alkera_core.files`
   must import nothing from `apps/*` (`backend`, `worker`, `alkera_cli`) and
   nothing from `alkera_core.project` (the CLI's per-workspace store, a
   different storage model entirely). Backend and worker import the library;
   never the other way round.
2. **Object-store drivers stay inside `files/store/`.** A backend serving
   metadata, or a CLI computing a hash, must not pay for `boto3` — importing
   any module outside `files/store/` may not pull `boto3`/`botocore` into
   `sys.modules`.

Both are asserted in a SUBPROCESS with a fresh interpreter: this pytest process
has imported half the world (backend conftest included), so its own
`sys.modules` proves nothing.
"""

from __future__ import annotations

import ast
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

#: Every case answers its question in a fresh interpreter of its own (or reads
#: the source tree), so nothing here is carried from one case to the next and the
#: hundred-odd subprocess probes can run on as many workers as there are.
pytestmark = [pytest.mark.spread]

#: The library's source tree, used both to discover modules and to read what
#: each one offers callers.
_FILES_ROOT = Path(__file__).resolve().parents[2] / "alkera_core" / "files"

#: Module prefixes `alkera_core.files` may never pull in. `alkera_core.project`
#: is the CLI's per-workspace `.alkera/` store; textual is the TUI stack that
#: rides in on any accidental `alkera_cli` import.
FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "backend",
    "worker",
    "alkera_cli",
    "alkera_core.project",
    "textual",
)

#: The S3 client stacks. Allowed under `alkera_core.files.store`, nowhere else.
DRIVER_PREFIXES: tuple[str, ...] = ("boto3", "botocore", "aioboto3", "aiobotocore")

_WALK_AND_IMPORT = """
import importlib, json, pkgutil, sys
import alkera_core.files as pkg

names = ["alkera_core.files"]
for info in pkgutil.walk_packages(pkg.__path__, prefix="alkera_core.files."):
    names.append(info.name)
for name in names:
    importlib.import_module(name)
print(json.dumps({"imported": names, "modules": sorted(sys.modules)}))
"""

_IMPORT_ONE = """
import importlib, json, sys
importlib.import_module({name!r})
print(json.dumps({{"modules": sorted(sys.modules)}}))
"""


def _offenders(modules: list[str], prefixes: tuple[str, ...]) -> list[str]:
    return [m for m in modules if any(m == p or m.startswith(p + ".") for p in prefixes)]


def _probe(code: str, *, cwd: Path | None = None) -> dict[str, list[str]]:
    """Run `code` in a fresh interpreter and return its reported sys.modules."""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=None if cwd is None else str(cwd),
        timeout=120,
    )
    assert proc.returncode == 0, f"probe failed:\n{proc.stdout}\n{proc.stderr}"
    result: dict[str, list[str]] = json.loads(proc.stdout.strip().splitlines()[-1])
    return result


def _submodules() -> list[str]:
    """Every module under `alkera_core.files`, discovered on disk.

    Discovered rather than listed so a new module joins the gate the moment it
    lands — an allowlist would have to be edited by the very change it guards.
    """
    root = Path(__file__).resolve().parents[2] / "alkera_core" / "files"
    names: list[str] = []
    for source in sorted(root.rglob("*.py")):
        relative = source.relative_to(root).with_suffix("")
        parts = [part for part in relative.parts if part != "__init__"]
        names.append(".".join(["alkera_core", "files", *parts]))
    return names


#: The only first-party packages `alkera_core.files` may reach for. An
#: allowlist, not a denylist: the point of the rule is that the library stays a
#: leaf of `alkera_core`, and a denylist only ever bans what somebody already
#: thought of.
ALLOWED_ALKERA_PREFIXES: tuple[str, ...] = (
    "alkera_core.files",
    "alkera_core.authz",
    "alkera_core.events",
    "alkera_core.config",
    "alkera_core.versioning",
    "alkera_core.db",
    "alkera_core.models",
    # A leaf of its own (math, re, typing): the one place the product spells a
    # byte figure, so a quota refusal says "2.3 GB" the way every other surface does.
    "alkera_core.units",
    # A leaf of its own (typing): the names of a chat's records, which the
    # drive marks at birth and the CLI's fence and watcher refuse — spelled
    # once below both, because the library may not reach the CLI's store.
    "alkera_core.chat_records",
    # The shared exactly-once claim (db and models only): Files' replay records
    # are its first user, and the other domains that take a retried request
    # claim through the same core rather than a copy of it.
    "alkera_core.idempotency",
)

#: Every first-party distribution in the workspace; a name under one of these
#: that the allowlist does not name is an offender.
FIRST_PARTY_ROOTS: tuple[str, ...] = (
    "alkera_core",
    "alkera_sdk",
    "backend",
    "worker",
    "alkera_cli",
)


def _alkera_offenders(modules: list[str]) -> list[str]:
    """First-party modules outside the allowlist.

    `alkera_core` itself is the namespace the library lives in; every other
    dotted name has to be one of the six the criterion names, or below one.
    """
    offenders: list[str] = []
    for name in modules:
        if not any(name == root or name.startswith(root + ".") for root in FIRST_PARTY_ROOTS):
            continue
        if name == "alkera_core":
            continue
        if any(
            name == allowed or name.startswith(allowed + ".") for allowed in ALLOWED_ALKERA_PREFIXES
        ):
            continue
        offenders.append(name)
    return offenders


def imported_names(source: str) -> set[str]:
    """Every module name an `import`/`from ... import` statement in `source` names.

    A ``from a.b import c`` is recorded both as ``a.b`` and as ``a.b.c``: the
    statement alone cannot tell a submodule from an attribute, and the caller
    only wants to know what this file reaches for by hand.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _direct_imports(root: Path) -> set[str]:
    """Every module `alkera_core.files` itself names in an import statement."""
    found: set[str] = set()
    for source in sorted(root.rglob("*.py")):
        found |= imported_names(source.read_text(encoding="utf-8"))
    return found


def direct_first_party_offenders(direct: set[str]) -> list[str]:
    """The unlisted first-party modules a source reaches for *by hand*.

    The runtime probe cannot answer the rule alone: `alkera_core.config` is an
    allowed package and it imports `alkera_core.compute.liveness`, so
    `alkera_core.compute` lands in the `sys.modules` of anything that reads a
    setting. That is the allowed package's dependency, not Files'. The criterion
    is about what *this* library reaches for, so a name that only ever arrives
    through an allowed package is not Files' violation — while an ``import
    alkera_core.compute`` written inside Files still is, which the twin below
    pins.
    """
    return sorted(_alkera_offenders(sorted(direct)))


def test_the_package_reaches_only_the_allowed_alkera_packages() -> None:
    """The rule as an allowlist: anything first-party and unlisted is an offender."""
    result = _probe(_WALK_AND_IMPORT)

    assert len(result["imported"]) > 1, "walk_packages found no submodules — the probe is broken"
    reached = set(_alkera_offenders(result["modules"]))
    offenders = [
        name
        for name in direct_first_party_offenders(_direct_imports(_FILES_ROOT))
        if name in reached
    ]
    assert offenders == [], (
        "alkera_core.files may import only "
        "alkera_core.{authz,events,config,versioning,db,models,units}; "
        f"it pulled: {offenders}"
    )


def test_a_direct_import_of_an_unlisted_first_party_package_is_still_an_offender() -> None:
    """The transitive clause's negative twin.

    `alkera_core.compute` reaches `sys.modules` transitively today, through
    `alkera_core.config`, and the clause above forgives exactly that. It must not
    forgive a Files module that imports it itself — otherwise the allowance would
    have retired the rule rather than narrowed it.
    """
    planted = "import alkera_core.compute.liveness\nfrom alkera_core.temporal import contract\n"

    offenders = direct_first_party_offenders(imported_names(planted))

    assert "alkera_core.compute.liveness" in offenders
    assert "alkera_core.temporal" in offenders


def test_the_transitive_clause_forgives_something_files_really_does_not_import() -> None:
    """The clause is load-bearing, and it is not what makes the rule pass.

    An allowed package really does pull an unlisted first-party module in, and
    no Files module names any of those by hand — so the day the forgiveness
    stops being needed, this case says so and the clause can go.
    """
    result = _probe(
        "import json, sys\nimport alkera_core.config\n"
        "print(json.dumps({'modules': sorted(sys.modules)}))\n"
    )

    reached = set(_alkera_offenders(result["modules"]))

    assert reached, "an allowed package no longer pulls anything unlisted"
    assert not (reached & set(direct_first_party_offenders(_direct_imports(_FILES_ROOT)))), (
        "Files imports one of these by hand — the transitive clause is not what makes it pass"
    )


def test_the_allowlist_catches_a_first_party_import_no_denylist_names(tmp_path: Path) -> None:
    """The negative twin, and the reason the rule is an allowlist.

    `alkera_core.temporal` is first-party, real, and named by no denylist — a
    denylist-shaped probe reports it clean. The allowlist must not.
    """
    (tmp_path / "_files_allowlist_decoy.py").write_text(
        "import alkera_core.temporal.contract\n", encoding="utf-8"
    )
    code = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(tmp_path)!r})\n"
        "import _files_allowlist_decoy\n"
        "print(json.dumps({'modules': sorted(sys.modules)}))\n"
    )

    result = _probe(code)

    assert _offenders(result["modules"], FORBIDDEN_PREFIXES) == [], (
        "the decoy imports nothing the denylist names — that is the point of the case"
    )
    assert _alkera_offenders(result["modules"]) != [], (
        "the allowlist cannot see a first-party import outside the allowed packages"
    )


def test_the_whole_package_imports_no_app_and_no_project_store() -> None:
    result = _probe(_WALK_AND_IMPORT)

    assert len(result["imported"]) > 1, "walk_packages found no submodules — the probe is broken"
    offenders = _offenders(result["modules"], FORBIDDEN_PREFIXES)
    assert offenders == [], (
        f"alkera_core.files must not import apps or the CLI project store; it pulled: {offenders}"
    )


def test_the_probe_itself_catches_a_forbidden_import(tmp_path: Path) -> None:
    """The negative twin: a module that DOES import `backend` must be caught.

    Without this, a probe that silently imported nothing would pass the test
    above forever.
    """
    (tmp_path / "_files_hygiene_decoy.py").write_text("import backend\n", encoding="utf-8")
    repo_root = Path(__file__).resolve().parents[4]
    code = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(repo_root / 'apps' / 'backend')!r})\n"
        f"sys.path.insert(0, {str(tmp_path)!r})\n"
        "import _files_hygiene_decoy\n"
        "print(json.dumps({'modules': sorted(sys.modules)}))\n"
    )

    result = _probe(code)

    assert _offenders(result["modules"], FORBIDDEN_PREFIXES) != [], (
        "the probe cannot detect a forbidden import — every hygiene assertion above is vacuous"
    )


@pytest.mark.parametrize("module", [pytest.param(name, id=name) for name in _submodules()])
def test_only_the_store_package_may_pull_an_s3_client(module: str) -> None:
    if module.startswith("alkera_core.files.store"):
        pytest.skip("drivers live here by design")

    result = _probe(_IMPORT_ONE.format(name=module))

    offenders = _offenders(result["modules"], DRIVER_PREFIXES)
    assert offenders == [], (
        f"{module} must import no S3 client (drivers belong under files/store/); it pulled: "
        f"{offenders}"
    )


def _public_names(source: Path) -> list[str]:
    """The names a module offers callers, read from its source rather than its namespace.

    `__all__` wins where a module declares one; otherwise every public top-level
    definition counts. Reading the AST — not `vars(module)` — is what keeps an
    imported helper (`asyncio`, `UTC`) out of the answer: an import is not a
    definition, and no `__module__` heuristic has to be trusted.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        ):
            listed = ast.literal_eval(node.value)
            return [str(name) for name in listed]

    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            names.append(node.name)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
        elif isinstance(node, ast.Assign):
            names.extend(target.id for target in node.targets if isinstance(target, ast.Name))
    return [name for name in names if not name.startswith("_")]


@pytest.mark.parametrize(
    "module",
    [
        pytest.param(source.stem, id=source.stem)
        for source in sorted((_FILES_ROOT).glob("*.py"))
        if source.stem != "__init__"
    ],
)
def test_every_top_level_module_is_re_exported_by_the_barrel(module: str) -> None:
    """`from alkera_core.files import X` reaches every seam a top-level module offers.

    Callers import the package, not its modules, so a name that lands in
    `names.py` but never in the barrel is a seam nobody outside this package can
    reach. `store/` and `sync/` are subpackages with their own barrels and are
    imported by path (`alkera_core.files.store.filesystem`), so they are out.
    """
    barrel = importlib.import_module("alkera_core.files")
    expected = _public_names(_FILES_ROOT / f"{module}.py")

    assert expected, f"{module}.py exposes nothing public — the discovery is broken"
    missing = [name for name in expected if name not in getattr(barrel, "__all__", ())]
    assert missing == [], (
        f"alkera_core.files.{module} exposes {missing}, which the package barrel does not "
        f"re-export; add them to the imports and to __all__"
    )
    unresolved = [name for name in expected if not hasattr(barrel, name)]
    assert unresolved == [], f"__all__ lists {unresolved} but the barrel does not bind them"


def test_no_public_name_is_defined_by_two_top_level_modules() -> None:
    """A flat barrel binds one meaning per name, so two must not compete for one.

    Before this rule, ``DEFAULT_BATCH`` was the ACL rewrite's 5,000 in ``acl``
    and the stats fold's 10,000 in ``stats``; the barrel bound one and every
    caller who imported it from the package silently got the other module's
    number. Names are module-qualified at their source instead
    (``ACL_REWRITE_BATCH``, ``STATS_FOLD_BATCH``), which is a rule a reader can
    apply without knowing what the barrel happens to import today.

    Two modules offering the *same object* — ``conflicts`` re-exporting
    ``names.NAME_MAX_BYTES`` — is a re-export, not a collision, and stays legal.
    """
    barrel = importlib.import_module("alkera_core.files")
    owners: dict[str, list[str]] = {}
    for source in sorted(_FILES_ROOT.glob("*.py")):
        if source.stem == "__init__":
            continue
        for name in _public_names(source):
            owners.setdefault(name, []).append(source.stem)

    collisions: list[str] = []
    for name, modules in sorted(owners.items()):
        if len(modules) < 2:
            continue
        bound = {
            id(getattr(importlib.import_module(f"alkera_core.files.{module}"), name))
            for module in modules
        }
        if len(bound) > 1:
            collisions.append(f"{name}: {modules}")

    assert collisions == [], (
        "these names mean different things in two modules, and the package barrel can "
        f"bind only one: {collisions}"
    )
    assert getattr(barrel, "__all__", None), "the barrel declares no __all__"
    assert len(barrel.__all__) == len(set(barrel.__all__)), "__all__ repeats a name"


def test_the_collision_check_would_fail_on_two_different_meanings(tmp_path: Path) -> None:
    """The negative twin: the check must separate a real collision from a re-export.

    Two planted modules, one pair sharing an object and one pair disagreeing on
    the value — the disagreement is what has to be reported, or the rule above
    would pass on the very shape it exists to forbid.
    """
    shared = tmp_path / "shared.py"
    shared.write_text("LIMIT = 1000\n", encoding="utf-8")
    reexport = tmp_path / "reexport.py"
    reexport.write_text("from shared import LIMIT\n\n__all__ = ['LIMIT']\n", encoding="utf-8")
    rival = tmp_path / "rival.py"
    rival.write_text("LIMIT = 5000\n", encoding="utf-8")

    sys.path.insert(0, str(tmp_path))
    try:
        modules = {name: importlib.import_module(name) for name in ("shared", "reexport", "rival")}
    finally:
        sys.path.remove(str(tmp_path))

    same = {id(modules["shared"].LIMIT), id(modules["reexport"].LIMIT)}
    different = {id(modules["shared"].LIMIT), id(modules["rival"].LIMIT)}

    assert len(same) == 1, "a re-export must read as one meaning"
    assert len(different) > 1, "two different values must read as a collision"
