"""The supervisor is the box's trusted base: it stays small and touches no
tenant content, so it can be read in one sitting."""

from __future__ import annotations

import ast
import configparser
import subprocess
import sys
from pathlib import Path

import alkera_cli.supervisor
import pytest

PACKAGE = Path(alkera_cli.supervisor.__file__).parent
#: The supervisor's package, by its dotted name.
SUPERVISOR = "alkera_cli.supervisor"
#: The whole package's lines as measured, with every supervisor-only module
#: inside it (the import scan below holds that). It is the size the trusted
#: base really has, not a size anyone can read in one sitting: the follow-up
#: is a real split of the privileged base, the part that runs as root and
#: holds the machine credential, from the rest of the supervisor, with the cap
#: then on that part alone. Until then it only goes down. Re-measure with
#: ``cat apps/cli/alkera_cli/supervisor/*.py | wc -l``.
LINE_CAP = 3830
#: Nothing under these may be imported by the supervisor, directly or not.
FORBIDDEN = (
    "alkera_cli.harness",
    "alkera_cli.plugins",
    "alkera_cli.files",
    "alkera_cli.cloud_sync",
    "alkera_cli.cloud",
    "alkera_cli.app",
    "alkera_cli.daemon",
)


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def _forbidden(names: set[str]) -> set[str]:
    return {n for n in names if any(n == f or n.startswith(f + ".") for f in FORBIDDEN)}


def test_the_supervisor_names_nothing_that_touches_tenant_content() -> None:
    found = {p.name: _forbidden(_imports(p)) for p in PACKAGE.glob("*.py")}
    assert {name: bad for name, bad in found.items() if bad} == {}


def test_importing_the_supervisor_loads_nothing_that_touches_tenant_content() -> None:
    probe = (
        "import sys, alkera_cli.supervisor.service\n"
        f"bad = sorted(m for m in sys.modules if m.startswith({FORBIDDEN!r}))\n"
        "print('\\n'.join(bad))\n"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""


def test_the_supervisor_writes_files_without_loading_the_project_store() -> None:
    """Its state files go through the leaf ``alkera_core.atomic_io``; the
    project package would bring the chat store into the box's root process."""
    probe = (
        "import sys, alkera_cli.supervisor.service\n"
        "print('\\n'.join(sorted(m for m in sys.modules if m.startswith('alkera_core.project'))))\n"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""


def test_the_supervisor_stays_small() -> None:
    """Measured at 3624 lines. ``cat apps/cli/alkera_cli/supervisor/*.py | wc -l``
    gives the number this compares; a change that moves supervisor code in or
    out re-measures and sets the cap to what it finds, never above."""
    lines = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in PACKAGE.glob("*.py"))
    assert lines <= LINE_CAP


def test_the_import_contract_forbids_what_this_scan_forbids() -> None:
    """``make lint-imports`` holds the same line through the whole import graph,
    from ``apps/cli/.importlinter``; the two lists must not drift apart."""
    config = configparser.ConfigParser()
    config.read(PACKAGE.parents[1] / ".importlinter", encoding="utf-8")
    contract = config["importlinter:contract:cli-supervisor"]
    assert contract["source_modules"].split() == ["alkera_cli.supervisor"]
    assert set(FORBIDDEN) <= set(contract["forbidden_modules"].split())


def test_the_scan_catches_a_forbidden_import() -> None:
    assert _forbidden({"alkera_cli.cloud.mirror", "alkera_cli.supervisor.slots", "json"}) == {
        "alkera_cli.cloud.mirror"
    }


# -- what only the supervisor uses lives in the supervisor's package -----------------


def _module_name(root: Path, path: Path) -> str:
    parts = path.relative_to(root.parent).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _imported(name: str, path: Path, modules: dict[str, Path]) -> set[str]:
    """The modules of the tree that ``path`` imports, anywhere in it (a
    function's own import counts): ``from a import b`` is ``a.b`` when that is
    a module, else ``a``."""
    package = name if path.name == "__init__.py" else name.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                above = package.split(".")
                above = above[: len(above) - (node.level - 1)]
                base = ".".join([*above, base] if base else above)
            for alias in node.names:
                inner = f"{base}.{alias.name}"
                found.add(inner if inner in modules else base)
    return {module for module in found if module in modules and module != name}


def supervisor_only_modules(root: Path, package: str = SUPERVISOR) -> set[str]:
    """The modules under ``root`` (a package's directory) that sit outside
    ``package`` and are imported by nothing but it, or by nothing but modules
    that are themselves so: code that only ever runs for the supervisor. A
    module with one importer elsewhere in the tree is a shared library and is
    not named. Tests are not in the tree, so a test's import keeps nothing
    out."""
    modules = {_module_name(root, path): path for path in root.rglob("*.py")}
    importers: dict[str, set[str]] = {name: set() for name in modules}
    for name, path in modules.items():
        for target in _imported(name, path, modules):
            importers[target].add(name)
    inside = {name for name in modules if name == package or name.startswith(package + ".")}
    owned = set(inside)
    while True:
        more = {m for m, by in importers.items() if m not in owned and by and by <= owned}
        if not more:
            return owned - inside
        owned |= more


def test_a_module_only_the_supervisor_imports_lives_in_its_package() -> None:
    """The size cap counts the supervisor's package, so the package has to
    hold all of the supervisor: a module beside it that nothing else imports
    is part of the trusted base whatever directory it is in. Move it in."""
    assert supervisor_only_modules(PACKAGE.parent) == set()


def _tree(root: Path, files: dict[str, str]) -> Path:
    for name, text in {"__init__.py": "", "supervisor/__init__.py": "", **files}.items():
        path = root / "alkera_cli" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root / "alkera_cli"


@pytest.mark.parametrize(
    ("files", "named"),
    [
        pytest.param(
            {
                "supervisor/service.py": "from alkera_cli.org_log import OrgLog\n",
                "org_log.py": "",
            },
            {"alkera_cli.org_log"},
            id="a-module-beside-the-package-that-only-it-imports",
        ),
        pytest.param(
            {
                "supervisor/service.py": "def run():\n    from alkera_cli import org_log\n",
                "org_log.py": "",
            },
            {"alkera_cli.org_log"},
            id="imported-inside-a-function-by-its-parent",
        ),
        pytest.param(
            {
                "supervisor/service.py": "from ..host import log\n",
                "host/__init__.py": "",
                "host/log.py": "from . import rotate\n",
                "host/rotate.py": "",
            },
            {"alkera_cli.host.log", "alkera_cli.host.rotate"},
            id="relative-and-through-a-module-that-is-itself-supervisor-only",
        ),
        pytest.param(
            {
                "supervisor/service.py": "import alkera_cli.host.backoff\n",
                "host/__init__.py": "",
                "host/backoff.py": "",
                "cloud/__init__.py": "",
                "cloud/service.py": "from alkera_cli.host.backoff import doubled\n",
            },
            set(),
            id="a-library-something-else-imports-too-is-shared",
        ),
        pytest.param(
            {
                "supervisor/service.py": "from alkera_cli.supervisor import slots\n",
                "supervisor/slots.py": "import json\n",
                "cloud/__init__.py": "",
                "cloud/worker.py": "",
            },
            set(),
            id="the-package-itself-and-modules-nothing-imports",
        ),
    ],
)
def test_the_scan_names_what_only_the_supervisor_imports(
    tmp_path: Path, files: dict[str, str], named: set[str]
) -> None:
    assert supervisor_only_modules(_tree(tmp_path, files)) == named
