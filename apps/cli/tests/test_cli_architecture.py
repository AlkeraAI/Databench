"""Architecture gates for ``alkera_cli``.

Each rule scans the package's source with ``ast`` and compares what it finds
with an allowlist of today's violations. An allowlist may only shrink: a new
violation fails, and so does an entry that no longer violates (remove it). The
façades are ``commands/`` (Typer command groups), ``ui/`` (the terminal UI),
``daemon/`` (the JSON-RPC façade), ``app/`` (the composition root) and
``main.py``; every other package is a library.

Run this file as a script to print what each rule finds today.
"""

from __future__ import annotations

import ast
import importlib.metadata
import json
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.xdist_group("cli_architecture")

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "alkera_cli"
FACADE_PACKAGES = frozenset({"commands", "ui", "daemon", "app"})
#: The entry modules besides any ``__main__``, which is an entry wherever it sits.
ENTRY_MODULES = frozenset({"alkera_cli.main", "alkera_cli.entry"})
UI_LIBRARIES = frozenset({"typer", "click", "rich", "textual"})
PATH_OWNER = "alkera_cli.host.paths"
MAX_MODULE_LINES = 1500
MAX_CLASS_LINES = 800


# --- the scanner -----------------------------------------------------------


@dataclass(frozen=True)
class Import:
    """One name an import statement binds or loads, with where it sits."""

    target: str
    function_level: bool


def module_name(path: Path, root: Path = PACKAGE_ROOT) -> str:
    parts = list(path.relative_to(root.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def package_of(module: str) -> str:
    """The first segment under ``alkera_cli`` (``alkera_cli.main`` → ``main``)."""
    parts = module.split(".")
    return parts[1] if len(parts) > 1 else ""


def is_library(module: str) -> bool:
    if module.rsplit(".", 1)[-1] == "__main__":
        return False
    return package_of(module) not in FACADE_PACKAGES and module not in ENTRY_MODULES


def imports_in(source: str) -> list[Import]:
    """Every runtime import in ``source``. ``from a import b`` yields ``a`` and
    ``a.b``; imports under ``if TYPE_CHECKING:`` are skipped."""
    found: list[Import] = []

    def visit(node: ast.AST, function_level: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Import):
                found.extend(Import(alias.name, function_level) for alias in child.names)
            elif isinstance(child, ast.ImportFrom) and child.module and child.level == 0:
                found.append(Import(child.module, function_level))
                found.extend(
                    Import(f"{child.module}.{alias.name}", function_level) for alias in child.names
                )
            elif isinstance(child, ast.If) and "TYPE_CHECKING" in ast.unparse(child.test):
                for branch in child.orelse:
                    visit(ast.Module(body=[branch], type_ignores=[]), function_level)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                visit(child, True)
            else:
                visit(child, function_level)

    visit(ast.parse(source), False)
    return found


def call_names(source: str) -> Iterator[str]:
    """The dotted callee of every call in ``source`` (``httpx.Client``, ``load_auth``)."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute)):
            yield ast.unparse(node.func)


def alkera_dir_joins(source: str) -> int:
    """How many ``<expr> / ".alkera"`` joins ``source`` builds."""
    return sum(
        1
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Div)
        and isinstance(node.right, ast.Constant)
        and node.right.value == ".alkera"
    )


@cache
def modules() -> dict[str, str]:
    return {
        module_name(path): path.read_text(encoding="utf-8")
        for path in sorted(PACKAGE_ROOT.rglob("*.py"))
    }


# --- the rules ---------------------------------------------------------------


def ui_library_imports() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for module, source in modules().items():
        if package_of(module) in {"commands", "ui"} or module == "alkera_cli.main":
            continue
        hits = sorted({i.target.split(".")[0] for i in imports_in(source)} & UI_LIBRARIES)
        if hits:
            found[module] = hits
    return found


def facade_imports_from_libraries() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for module, source in modules().items():
        if not is_library(module):
            continue
        hits = sorted(
            {
                ".".join(i.target.split(".")[:2])
                for i in imports_in(source)
                if i.target.split(".")[:2] in (["alkera_cli", "daemon"], ["alkera_cli", "ui"])
            }
        )
        if hits:
            found[module] = hits
    return found


def contracts_outside_imports() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for module, source in modules().items():
        if package_of(module) != "contracts":
            continue
        hits = sorted(
            {
                i.target
                for i in imports_in(source)
                if i.target.startswith("alkera_cli.") and package_of(i.target) != "contracts"
            }
        )
        if hits:
            found[module] = hits
    return found


def function_level_cross_package_imports() -> dict[str, int]:
    found: dict[str, int] = {}
    known = modules()
    for module, source in known.items():
        count = sum(
            1
            for i in imports_in(source)
            if i.function_level and i.target in known and package_of(i.target) != package_of(module)
        )
        if count:
            found[module] = count
    return found


def counted_calls(matches: Callable[[str], bool], *, skip: Callable[[str], bool]) -> dict[str, int]:
    found: dict[str, int] = {}
    for module, source in modules().items():
        if skip(module):
            continue
        count = sum(1 for name in call_names(source) if matches(name))
        if count:
            found[module] = count
    return found


def hand_built_alkera_dirs() -> dict[str, int]:
    found: dict[str, int] = {}
    for module, source in modules().items():
        if module == PATH_OWNER:
            continue
        count = alkera_dir_joins(source)
        if count:
            found[module] = count
    return found


def oversized_modules() -> dict[str, int]:
    return {
        module: lines
        for module, source in modules().items()
        if (lines := len(source.splitlines())) > MAX_MODULE_LINES
    }


def oversized_classes() -> dict[str, int]:
    found: dict[str, int] = {}
    for module, source in modules().items():
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ClassDef) and node.end_lineno is not None:
                lines = node.end_lineno - node.lineno + 1
                if lines > MAX_CLASS_LINES:
                    found[f"{module}:{node.name}"] = lines
    return found


HTTPX_CLIENTS = frozenset({"httpx.Client", "httpx.AsyncClient"})
CONNECTION_STORES = frozenset({"TeamConnectionsStore", "AddedConnectionsStore"})


def _last_segment_in(names: frozenset[str]) -> Callable[[str], bool]:
    return lambda callee: callee.rsplit(".", 1)[-1] in names


def httpx_clients() -> dict[str, int]:
    return counted_calls(HTTPX_CLIENTS.__contains__, skip=lambda _module: False)


def connection_stores() -> dict[str, int]:
    return counted_calls(_last_segment_in(CONNECTION_STORES), skip=lambda _module: False)


#: Signalling a process by pid. ``os.kill`` kills instead of probing on
#: Windows, and ``os.killpg`` does not exist there; ``alkera_core.process``
#: owns both, per platform, and nothing in the CLI calls them directly.
PROCESS_SIGNALS = frozenset({"os.kill", "os.killpg", "killpg"})


def direct_process_signals(sources: dict[str, str]) -> dict[str, int]:
    found: dict[str, int] = {}
    for module, source in sources.items():
        count = sum(1 for name in call_names(source) if name in PROCESS_SIGNALS)
        if count:
            found[module] = count
    return found


#: The one module that may read the plain current sign-in: it defines the
#: compatibility shim. Every other caller resolves a profile from its chat,
#: project or command, so a switch elsewhere can never move what it acts as.
AUTH_FILE_MODULE = "alkera_cli.account.auth_file"


def bare_auth_reads(sources: dict[str, str]) -> dict[str, int]:
    """``load_auth()`` calls per module, outside the module that defines it."""
    found: dict[str, int] = {}
    for module, source in sources.items():
        if module == AUTH_FILE_MODULE:
            continue
        count = sum(1 for name in call_names(source) if name.rsplit(".", 1)[-1] == "load_auth")
        if count:
            found[module] = count
    return found


def auth_reads() -> dict[str, int]:
    return bare_auth_reads(modules())


def lineage_store_openings(source: str) -> int:
    """How many times ``source`` opens the whole lineage store: a
    ``LineageFactStore(...)`` construction, or a read of a project's
    ``.lineage_path`` (the one way to name the store's directory)."""
    count = 0
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute)):
            count += ast.unparse(node.func).rsplit(".", 1)[-1] == "LineageFactStore"
        elif isinstance(node, ast.Attribute) and node.attr == "lineage_path":
            count += 1
    return count


def whole_lineage_store_openings() -> dict[str, int]:
    found: dict[str, int] = {}
    for module, source in modules().items():
        if (count := lineage_store_openings(source)) > 0:
            found[module] = count
    return found


def shadowed_top_level_names(children: set[str], taken: set[str]) -> list[str]:
    """The direct children of ``alkera_cli`` whose name is already a top-level module.

    The compiled binary imports from ``alkera_cli/`` as a top-level root (Nuitka
    compiles ``alkera_cli/__main__.py`` as the main script), so a child named like
    a standard-library or installed module shadows it there and only there."""
    return sorted(children & taken)


def package_children() -> set[str]:
    return {
        path.stem if path.is_file() else path.name
        for path in PACKAGE_ROOT.iterdir()
        if (path.is_dir() and (path / "__init__.py").is_file())
        or (path.suffix == ".py" and path.stem != "__init__")
    } - {"__main__"}


def taken_top_level_names() -> set[str]:
    installed = set(importlib.metadata.packages_distributions()) - {"alkera_cli"}
    return set(sys.stdlib_module_names) | installed


# --- the allowlists (may only shrink) ----------------------------------------

#: Rich output that stays where it is because splitting it out would change
#: output or ordering, or would make a library import ``commands``.
ALLOWED_UI_LIBRARY_IMPORTS: dict[str, list[str]] = {
    # The daemon builds a Rich Console through the slash commands to render them.
    "alkera_cli.chat.slash": ["rich"],
    # Its only renderer caller is chat.slash, itself a Rich renderer.
    "alkera_cli.chat.usage": ["rich"],
    "alkera_cli.daemon.methods.harness": ["rich"],
}

ALLOWED_FACADE_IMPORTS_FROM_LIBRARIES: dict[str, list[str]] = {
    "alkera_cli.account.auth_watcher": ["alkera_cli.daemon"],
}

ALLOWED_HAND_BUILT_ALKERA_DIRS: dict[str, int] = {}

ALLOWED_HTTPX_CLIENTS: dict[str, int] = {
    "alkera_cli.account.device_flow": 2,
    "alkera_cli.chat.usage": 1,
    "alkera_cli.cloud.attachments": 1,
    "alkera_cli.cloud.rest": 1,
    "alkera_cli.cloud_sync.client": 1,
    "alkera_cli.files.mount": 1,
    "alkera_cli.gateway.client": 1,
    "alkera_cli.harness.adapters.opencode_http": 1,
    "alkera_cli.harness.gateway_completion": 1,
    "alkera_cli.observability.audit_report": 1,
    "alkera_cli.plugins.plugin_base.permissions.actor": 1,
}

ALLOWED_CONNECTION_STORES: dict[str, int] = {
    # The box reconcile, moved here from the schema cards.
    "alkera_cli.cloud_sync.box_sync": 2,
    "alkera_cli.cloud_sync.connections_lane": 2,
    # The data product's connection record, read by the harness through its
    # extension point (moved here from the harness runtime).
    "alkera_cli.plugins.plugin_base.harness_records": 1,
    "alkera_cli.plugins.plugin_base.registry": 2,
}

#: Bare reads of the plain current sign-in. Zero, and it stays zero.
ALLOWED_AUTH_READS: dict[str, int] = {}

#: Who may open the WHOLE lineage store. A chat reads lineage only through its
#: registry view (``ToolRegistry.lineage_store``), which on a shared box is scoped
#: to the chat's connections and owner; opening the store directly anywhere a chat
#: reaches would read every org's facts. So every opening is a writer, a
#: background pass, or a surface only the machine's own person drives.
ALLOWED_WHOLE_LINEAGE_STORE_OPENINGS: dict[str, int] = {}

ALLOWED_FUNCTION_LEVEL_IMPORTS: dict[str, int] = {
    # The entry chooses between the box supervisor and the full command tree
    # (with the product's extensions) before loading either: the root process of
    # a box must never load the tree (apps/cli/tests/supervisor/test_supervisor_entry.py),
    # so the supervisor, the open CLI and the product are all deferred. The
    # dispatch is the open entry's; the product's root defers only its install.
    "alkera_cli.entry": 3,
    "alkera_cli.account.auth_watcher": 2,
    "alkera_cli.chat.slash": 3,
    "alkera_cli.cloud.fence": 2,
    "alkera_cli.cloud.refusal": 1,
    "alkera_cli.cloud.rest": 1,
    "alkera_cli.cloud_sync.connections_lane": 1,
    "alkera_cli.cloud_sync.job": 2,
    "alkera_cli.commands.box": 3,
    "alkera_cli.daemon.methods.auth": 11,
    "alkera_cli.daemon.methods.harness": 13,
    "alkera_cli.daemon.methods.plugins": 1,
    "alkera_cli.daemon.methods.report": 1,
    "alkera_cli.daemon.server": 3,
    "alkera_cli.harness.adapters.claude_agent": 2,
    "alkera_cli.harness.adapters.opencode_http": 2,
    "alkera_cli.harness.mcp_server": 5,
    "alkera_cli.harness.runtime": 24,
    "alkera_cli.harness.runtime_scheduling": 4,
    "alkera_cli.harness.safety_judge": 3,
    "alkera_cli.harness.subagent_routing": 2,
    "alkera_cli.main": 7,
    "alkera_cli.observability.crash_report": 1,
    "alkera_cli.observability.telemetry": 2,
    "alkera_cli.plugins.plugin_base.bash_exec": 1,
    "alkera_cli.plugins.plugin_base.credential_manager": 0,
    "alkera_cli.plugins.plugin_base.permissions.bash": 2,
    "alkera_cli.plugins.plugin_base.sql.timeout": 1,
    "alkera_cli.plugins.plugin_base.sql_tools": 0,
    "alkera_cli.plugins.plugin_base.tool": 1,
}

FROZEN_MODULE_LINES: dict[str, int] = {
    "alkera_cli.cloud.folder": 1925,
    "alkera_cli.cloud.mirror": 4422,
    "alkera_cli.cloud.service": 3684,
    "alkera_cli.daemon.methods.harness": 1557,
    "alkera_cli.files.live_sync": 3719,
    "alkera_cli.files.push": 1522,
    "alkera_cli.harness.adapters.claude_agent": 1653,
    "alkera_cli.harness.adapters.opencode_http": 2701,
    # The write-and-delete classifier fix pushed it over; it splits after launch.
    "alkera_cli.plugins.plugin_base.permissions.bash": 1684,
    "alkera_cli.harness.adapters.opencode_translate": 1959,
    "alkera_cli.harness.runtime": 5257,
    "alkera_cli.plugins.plugin_base.registry": 1509,
}

FROZEN_CLASS_LINES: dict[str, int] = {
    "alkera_cli.cloud.folder:ChatFolders": 1532,
    "alkera_cli.cloud.mirror:ChatMirror": 3475,
    "alkera_cli.cloud.service:CloudMirrorService": 2880,
    "alkera_cli.files.live_sync:LiveSync": 2536,
    "alkera_cli.harness.adapters.claude_agent:ClaudeAgentAdapter": 973,
    "alkera_cli.harness.adapters.opencode_http:OpencodeHttpAdapter": 1852,
    "alkera_cli.harness.adapters.opencode_translate:OpencodeEventTranslator": 1112,
    "alkera_cli.harness.runtime:ChatSession": 2295,
    "alkera_cli.harness.runtime:HarnessRuntime": 2181,
    "alkera_cli.plugins.plugin_base.registry:PluginRegistry": 1276,
}


# --- the gates ---------------------------------------------------------------


def _assert_only_shrinks(found: dict[str, list[str]], allowed: dict[str, list[str]]) -> None:
    new = {m: v for m, v in found.items() if not set(v) <= set(allowed.get(m, []))}
    gone = {m: v for m, v in allowed.items() if not set(v) <= set(found.get(m, []))}
    assert not new, f"new violations (fix them, don't allowlist them): {new}"
    assert not gone, f"fixed — remove these from the allowlist: {gone}"


def _assert_counts_only_shrink(found: dict[str, int], allowed: dict[str, int]) -> None:
    grown = {m: n for m, n in found.items() if n > allowed.get(m, 0)}
    shrunk = {m: found.get(m, 0) for m, n in allowed.items() if found.get(m, 0) < n}
    assert not grown, f"new violations (fix them, don't allowlist them): {grown}"
    assert not shrunk, f"improved — lower these allowlist entries to: {shrunk}"


@pytest.fixture(scope="module")
def allowed(
    architecture_allowlist: Callable[[str, Mapping[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Every allowlist of this file by its constant's name, with the entries a
    test layer adds for its own modules (``box_root_modules`` adds box roots)."""
    own = {
        "ALLOWED_UI_LIBRARY_IMPORTS": ALLOWED_UI_LIBRARY_IMPORTS,
        "ALLOWED_FACADE_IMPORTS_FROM_LIBRARIES": ALLOWED_FACADE_IMPORTS_FROM_LIBRARIES,
        "ALLOWED_HAND_BUILT_ALKERA_DIRS": ALLOWED_HAND_BUILT_ALKERA_DIRS,
        "ALLOWED_HTTPX_CLIENTS": ALLOWED_HTTPX_CLIENTS,
        "ALLOWED_CONNECTION_STORES": ALLOWED_CONNECTION_STORES,
        "ALLOWED_AUTH_READS": ALLOWED_AUTH_READS,
        "ALLOWED_WHOLE_LINEAGE_STORE_OPENINGS": ALLOWED_WHOLE_LINEAGE_STORE_OPENINGS,
        "ALLOWED_FUNCTION_LEVEL_IMPORTS": ALLOWED_FUNCTION_LEVEL_IMPORTS,
        "FROZEN_MODULE_LINES": FROZEN_MODULE_LINES,
        "FROZEN_CLASS_LINES": FROZEN_CLASS_LINES,
        "ALLOWED_BOX_MODULE_STATE": ALLOWED_BOX_MODULE_STATE,
        "box_root_modules": sorted(BOX_ROOT_MODULES),
    }
    return architecture_allowlist("cli_architecture", own)


def test_ui_libraries_stay_in_the_facades(allowed: dict[str, Any]) -> None:
    _assert_only_shrinks(ui_library_imports(), allowed["ALLOWED_UI_LIBRARY_IMPORTS"])


def test_libraries_do_not_import_the_daemon_or_the_ui(allowed: dict[str, Any]) -> None:
    _assert_only_shrinks(
        facade_imports_from_libraries(), allowed["ALLOWED_FACADE_IMPORTS_FROM_LIBRARIES"]
    )


def test_contracts_import_nothing_else_from_alkera_cli() -> None:
    assert contracts_outside_imports() == {}


def test_only_the_path_owner_builds_the_alkera_dir(allowed: dict[str, Any]) -> None:
    _assert_counts_only_shrink(hand_built_alkera_dirs(), allowed["ALLOWED_HAND_BUILT_ALKERA_DIRS"])


def test_httpx_clients_are_built_only_where_they_are_today(allowed: dict[str, Any]) -> None:
    _assert_counts_only_shrink(httpx_clients(), allowed["ALLOWED_HTTPX_CLIENTS"])


def test_connection_stores_are_built_only_where_they_are_today(allowed: dict[str, Any]) -> None:
    _assert_counts_only_shrink(connection_stores(), allowed["ALLOWED_CONNECTION_STORES"])


def test_processes_are_signalled_only_through_the_cross_platform_owner() -> None:
    """A tool that times out or is cancelled must end its command on Windows
    too: every signal to a pid goes through ``alkera_core.process``."""
    assert direct_process_signals(modules()) == {}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("os.killpg(pid, signal.SIGTERM)\n", 1, id="a-group-signal"),
        pytest.param("os.kill(pid, 0)\n", 1, id="a-probe"),
        pytest.param("from os import killpg\nkillpg(p, 9)\n", 1, id="imported-bare"),
        pytest.param("kill_process_tree(pid)\nproc.kill()\n", 0, id="the-owner-and-a-handle"),
    ],
)
def test_the_process_signal_scanner(source: str, expected: int) -> None:
    found = direct_process_signals({"alkera_cli.decoy": source})
    assert found.get("alkera_cli.decoy", 0) == expected


def test_the_stored_login_is_read_from_account_or_today_s_sites(allowed: dict[str, Any]) -> None:
    _assert_counts_only_shrink(auth_reads(), allowed["ALLOWED_AUTH_READS"])


def test_only_writers_and_admin_surfaces_open_the_whole_lineage_store(
    allowed: dict[str, Any],
) -> None:
    _assert_counts_only_shrink(
        whole_lineage_store_openings(), allowed["ALLOWED_WHOLE_LINEAGE_STORE_OPENINGS"]
    )


def test_the_auth_read_scanner_catches_a_bare_read_and_spares_the_shim() -> None:
    decoy = {
        "alkera_cli.decoy.direct": "from x import load_auth\nauth = load_auth()\n",
        "alkera_cli.decoy.dotted": "auth_file.load_auth()\nauth_file.load_auth()\n",
        "alkera_cli.decoy.mentioned": "# load_auth() in a comment\nname = 'load_auth'\n",
        AUTH_FILE_MODULE: "def load_auth():\n    return load_auth()\n",
    }
    assert bare_auth_reads(decoy) == {"alkera_cli.decoy.direct": 1, "alkera_cli.decoy.dotted": 2}
    with pytest.raises(AssertionError, match="new violations"):
        _assert_counts_only_shrink(bare_auth_reads(decoy), ALLOWED_AUTH_READS)


def test_function_level_cross_package_imports_only_shrink(allowed: dict[str, Any]) -> None:
    _assert_counts_only_shrink(
        function_level_cross_package_imports(), allowed["ALLOWED_FUNCTION_LEVEL_IMPORTS"]
    )


def test_modules_stay_under_budget_or_only_shrink(allowed: dict[str, Any]) -> None:
    _assert_counts_only_shrink(oversized_modules(), allowed["FROZEN_MODULE_LINES"])


def test_classes_stay_under_budget_or_only_shrink(allowed: dict[str, Any]) -> None:
    _assert_counts_only_shrink(oversized_classes(), allowed["FROZEN_CLASS_LINES"])


def test_no_package_shadows_a_top_level_module_in_the_binary() -> None:
    assert shadowed_top_level_names(package_children(), taken_top_level_names()) == []


# --- the scanner itself ------------------------------------------------------


def test_the_shadow_check_names_a_stdlib_collision() -> None:
    taken = taken_top_level_names()
    assert "platform" in taken and "host" not in taken
    assert shadowed_top_level_names({"platform", "host", "commands"}, taken) == ["platform"]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("import rich\n", [Import("rich", False)], id="module-level"),
        pytest.param(
            "def f():\n    import rich.table\n",
            [Import("rich.table", True)],
            id="function-level",
        ),
        pytest.param(
            "from alkera_cli import ui\n",
            [Import("alkera_cli", False), Import("alkera_cli.ui", False)],
            id="from-import-names-the-submodule",
        ),
        pytest.param(
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import typer\n",
            [Import("typing", False), Import("typing.TYPE_CHECKING", False)],
            id="type-checking-only-is-skipped",
        ),
        pytest.param(
            "if TYPE_CHECKING:\n    pass\nelse:\n    import click\n",
            [Import("click", False)],
            id="the-runtime-else-branch-counts",
        ),
        pytest.param(
            "class C:\n    def m(self):\n        from a import b\n",
            [Import("a", True), Import("a.b", True)],
            id="method-level",
        ),
        pytest.param("from . import x\n", [], id="relative-import-ignored"),
    ],
)
def test_the_import_scanner(source: str, expected: list[Import]) -> None:
    assert imports_in(source) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param('p = root / ".alkera"\n', 1, id="join"),
        pytest.param('p = root / ".alkera" / "chats"\n', 1, id="join-then-subpath"),
        pytest.param('names = {".alkera", ".git"}\n', 0, id="a-name-not-a-path"),
        pytest.param('p = root / ".alkera-other"\n', 0, id="another-dir"),
    ],
)
def test_the_alkera_dir_scanner(source: str, expected: int) -> None:
    assert alkera_dir_joins(source) == expected


def test_the_call_scanner_names_dotted_and_bare_callees() -> None:
    source = "httpx.Client(t)\nload_auth()\nx.y.z()\nfactory()()\n"
    assert list(call_names(source)) == ["httpx.Client", "load_auth", "x.y.z", "factory"]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            "store = LineageFactStore(project.lineage_path)\n", 2, id="a-tool-opening-the-store"
        ),
        pytest.param("s = lineage.LineageFactStore(root)\n", 1, id="dotted-construction"),
        pytest.param("p = runtime.project.lineage_path / 'x'\n", 1, id="a-path-read"),
        pytest.param("store = ctx.registry.lineage_store\n", 0, id="the-registry-view"),
        pytest.param("def f(s: LineageFactStore) -> None: ...\n", 0, id="a-type-annotation"),
    ],
)
def test_the_lineage_store_scanner(source: str, expected: int) -> None:
    assert lineage_store_openings(source) == expected


def test_a_tool_that_opens_the_whole_store_fails_the_gate(allowed: dict[str, Any]) -> None:
    found = {
        **whole_lineage_store_openings(),
        "alkera_cli.decoy.lineage_tool": lineage_store_openings(
            "store = LineageFactStore(project.lineage_path)\n"
        ),
    }
    with pytest.raises(AssertionError, match="lineage_tool"):
        _assert_counts_only_shrink(found, allowed["ALLOWED_WHOLE_LINEAGE_STORE_OPENINGS"])


@pytest.mark.parametrize(
    ("module", "library"),
    [
        pytest.param("alkera_cli.harness.runtime", True, id="harness"),
        pytest.param("alkera_cli.ranking", True, id="top-level-module"),
        pytest.param("alkera_cli.commands.run", False, id="commands"),
        pytest.param("alkera_cli.daemon.server", False, id="daemon"),
        pytest.param("alkera_cli.app.compose", False, id="composition-root"),
        pytest.param("alkera_cli.main", False, id="entry"),
    ],
)
def test_which_modules_are_libraries(module: str, library: bool) -> None:
    assert is_library(module) is library


def test_the_ratchet_refuses_growth_and_stale_entries() -> None:
    with pytest.raises(AssertionError, match="new violations"):
        _assert_counts_only_shrink({"m": 2}, {"m": 1})
    with pytest.raises(AssertionError, match="new violations"):
        _assert_counts_only_shrink({"n": 1}, {})
    with pytest.raises(AssertionError, match="lower these"):
        _assert_counts_only_shrink({"m": 1}, {"m": 2})
    with pytest.raises(AssertionError, match="remove these"):
        _assert_only_shrinks({}, {"m": ["rich"]})
    _assert_counts_only_shrink({"m": 1}, {"m": 1})


# --- module-level mutable state the box reaches -------------------------------
#
# A box serves chats of several people, and of several orgs until each org runs
# in its own worker, from one process. State that outlives a call and is not a
# chat's own is shared by all of them: a module-level or class-level container
# that something fills, a cache decorator, a ``global`` rebinding, a module-level
# instance of a class whose methods change it. Each such site in a module the
# box can import is listed here with why it is safe, or names the leak it is;
# a new one fails until it is keyed by the chat (or the org) or justified here.

#: Where the box starts: the cloud mirror, the ``alkera box`` command, and the
#: open composition every entry installs first. A test layer adds its own
#: composition root (``box_root_modules``), which every shipped entry installs
#: first too. Every module they import, at module or function level,
#: transitively, is reachable.
BOX_ROOT_PACKAGES = frozenset({"cloud"})
BOX_ROOT_MODULES = frozenset({"alkera_cli.commands.box", "alkera_cli.app.open_product"})
CORE_ROOT = PACKAGE_ROOT.parents[2] / "packages" / "api-core" / "alkera_core"

#: Constructors of a mutable container, by last name segment.
MUTABLE_CONTAINERS = frozenset(
    {
        "dict",
        "list",
        "set",
        "defaultdict",
        "OrderedDict",
        "deque",
        "Counter",
        "ChainMap",
        "WeakValueDictionary",
        "WeakKeyDictionary",
        "WeakSet",
        "Cache",
        "LRUCache",
        "LFUCache",
        "TTLCache",
    }
)
#: Methods that change a container in place.
MUTATING_METHODS = frozenset(
    {
        "append",
        "appendleft",
        "add",
        "clear",
        "discard",
        "extend",
        "extendleft",
        "insert",
        "move_to_end",
        "pop",
        "popitem",
        "remove",
        "setdefault",
        "update",
        "__setitem__",
    }
)
CACHE_DECORATORS = frozenset({"cache", "lru_cache", "cached"})
_CONSTRUCTION = frozenset({"__init__", "__post_init__", "__new__", "__init_subclass__"})


def _callee_name(node: ast.expr) -> str | None:
    """The last name segment of what ``node`` calls (``Cache[T](...)`` → ``Cache``)."""
    func = node.func if isinstance(node, ast.Call) else node
    if isinstance(func, ast.Subscript):
        func = func.value
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else None


def _container(value: ast.expr) -> tuple[bool, bool]:
    """``(is a mutable container, starts empty)``."""
    if isinstance(value, ast.Dict):
        return True, not value.keys
    if isinstance(value, (ast.List, ast.Set)):
        return True, not value.elts
    if isinstance(value, (ast.DictComp, ast.ListComp, ast.SetComp)):
        return True, False
    if isinstance(value, ast.Call) and _callee_name(value) in MUTABLE_CONTAINERS:
        factory_only = _callee_name(value) == "defaultdict" and len(value.args) <= 1
        return True, (not value.args or factory_only) and not value.keywords
    return False, False


def _bindings(body: list[ast.stmt], *, class_level: bool) -> dict[str, ast.expr]:
    """``name -> value`` for every simple binding in ``body``, looking into
    module-level ``if``/``try`` blocks. In a class body an annotated binding is
    an instance field (a dataclass or a model copies its default) unless it is
    a ``ClassVar``."""
    found: dict[str, ast.expr] = {}
    for node in body:
        if isinstance(node, (ast.If, ast.Try)) and not class_level:
            blocks = [node.body, node.orelse, getattr(node, "finalbody", [])]
            blocks += [handler.body for handler in getattr(node, "handlers", [])]
            for block in blocks:
                found.update(_bindings(block, class_level=False))
        elif isinstance(node, ast.Assign):
            found.update((t.id, node.value) for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            if not class_level or "ClassVar" in ast.unparse(node.annotation):
                found[node.target.id] = node.value
    return found


def _changed_in_place(tree: ast.AST, owner: Callable[[ast.expr], str | None]) -> set[str]:
    """The names ``owner`` recognizes that something in ``tree`` changes in
    place: an item stored or deleted, an augmented assignment, a mutating call."""
    changed: set[str] = set()
    for node in ast.walk(tree):
        target: ast.expr | None = None
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
            target = node.value
        elif isinstance(node, ast.AugAssign):
            target = node.target
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in MUTATING_METHODS
        ):
            target = node.func.value
        if target is not None and (name := owner(target)) is not None:
            changed.add(name)
    return changed


def _self_attribute(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and (node.value.id == "self")
    )


def changes_itself(cls: ast.ClassDef) -> bool:
    """Whether a method of ``cls`` other than its construction changes the
    instance: assigns an attribute of ``self`` or changes one in place."""
    for method in cls.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if method.name in _CONSTRUCTION:
            continue
        for node in ast.walk(method):
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(_self_attribute(t) for t in targets):
                    return True
        in_place = _changed_in_place(method, lambda e: "self" if _self_attribute(e) else None)
        if in_place:
            return True
    return False


@cache
def stateful_classes() -> frozenset[str]:
    """Every class in ``alkera_cli`` and ``alkera_core`` whose instances change
    after construction, by name."""
    sources = [*modules().values()]
    sources += [path.read_text(encoding="utf-8") for path in sorted(CORE_ROOT.rglob("*.py"))]
    return frozenset(
        node.name
        for source in sources
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ClassDef) and changes_itself(node)
    )


def module_state(source: str, stateful: frozenset[str]) -> list[str]:
    """The shared mutable state ``source`` declares, one entry per site:
    ``NAME`` (a module-level container that starts empty or is changed in
    place), ``NAME = Class()`` (a module-level instance of a class that changes
    itself), ``Class.NAME`` (the same for a class attribute shared by every
    instance), ``function@decorator`` (a cache decorator) and ``global NAME``."""
    tree = ast.parse(source)
    found: set[str] = set()

    names = _bindings(tree.body, class_level=False)
    changed = _changed_in_place(tree, lambda e: e.id if isinstance(e, ast.Name) else None)
    for name, value in names.items():
        is_container, empty = _container(value)
        if is_container and (empty or name in changed):
            found.add(name)
        elif isinstance(value, ast.Call) and _callee_name(value) in stateful:
            found.add(f"{name} = {_callee_name(value)}()")

    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            found.update(f"global {name}" for name in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if (kind := _callee_name(decorator)) in CACHE_DECORATORS:
                    found.add(f"{node.name}@{kind}")
        elif isinstance(node, ast.ClassDef):
            found.update(_class_state(node))
    return sorted(found)


def _class_state(cls: ast.ClassDef) -> set[str]:
    attrs = {
        name: empty
        for name, value in _bindings(cls.body, class_level=True).items()
        for is_container, empty in [_container(value)]
        if is_container
    }
    if not attrs:
        return set()
    per_instance = {
        target.attr
        for node in ast.walk(cls)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if _self_attribute(target)
    }

    def shared(node: ast.expr) -> str | None:
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in ("self", "cls", cls.name)
            and node.attr in attrs
        ):
            return node.attr
        return None

    changed = _changed_in_place(cls, shared)
    return {
        f"{cls.name}.{name}"
        for name, empty in attrs.items()
        if name not in per_instance and (empty or name in changed)
    }


def _module_for(target: str, known: dict[str, str]) -> str | None:
    """The module an import target loads: itself, or the module a ``from``
    import names it in."""
    parts = target.split(".")
    while parts:
        if (name := ".".join(parts)) in known:
            return name
        parts.pop()
    return None


@cache
def box_reachable_modules(root_modules: frozenset[str] = BOX_ROOT_MODULES) -> frozenset[str]:
    """Every module the box can import from its roots: package ``__init__``
    modules on the way down included, function-level imports included."""
    known = modules()
    roots = [m for m in known if package_of(m) in BOX_ROOT_PACKAGES or m in root_modules]
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        module = stack.pop()
        if module in seen:
            continue
        seen.add(module)
        parts = module.split(".")
        # A namespace package (no ``__init__``) runs no code, so only real parents count.
        stack.extend(p for i in range(2, len(parts)) if (p := ".".join(parts[:i])) in known)
        for imported in imports_in(known[module]):
            if (target := _module_for(imported.target, known)) is not None:
                stack.append(target)
    return frozenset(seen & set(known))


def box_module_state(root_modules: frozenset[str] = BOX_ROOT_MODULES) -> dict[str, list[str]]:
    known, stateful = modules(), stateful_classes()
    reachable = box_reachable_modules(root_modules)
    found = {m: module_state(known[m], stateful) for m in sorted(reachable)}
    return {m: sites for m, sites in found.items() if sites}


_LOCK = "a lock per path or item, held for one operation; locks no data"
_STATIC = "a static registry filled at import from code; holds no chat data"
_PROCESS_FLAG = "a process-wide fact about the host, set once; holds no chat data"
_PER_SESSION = "keyed by session id, entered and removed by that session's own lifecycle"
_ORG_WORKERS_CLOSE = "crosses orgs only while one process serves several; per-org workers close it"
_PRODUCER_SCHEMA_MEMO = (
    "a memo keyed by the identity of the schema map one parse resolves against, which is "
    "that SQL's producer's own map, so a parse only ever reads its own producer's columns"
)

#: Each site the rule finds, and why it is safe (or the leak it is).
ALLOWED_BOX_MODULE_STATE: dict[str, dict[str, str]] = {
    "alkera_cli.account.binding": {
        "_session_profiles": "keyed by chat session id: the profile each chat itself acts as",
    },
    "alkera_cli.cloud.box_data": {
        "BOX_DATA = ExtensionPoint()": (
            "the box's data plane the product's composition registers before anything "
            "runs, frozen once read; holds no chat data"
        ),
    },
    "alkera_cli.cloud_sync.client": {
        "global _installed": "the box's one connections client, installed at start by the box",
    },
    "alkera_cli.cloud_sync.lease_cache": {
        "_epochs": "invalidation counters keyed by a cache's identity; hold no credential",
    },
    "alkera_cli.cloud_sync.shared_lease": {
        "_cache": "shared leases keyed by identity, chat, record and version",
    },
    "alkera_cli.daemon.protocol": {
        "CLIENT_REQUESTS": _STATIC,
        "METHODS": _STATIC,
        "NOTIFICATIONS": _STATIC,
    },
    "alkera_cli.daemon.server": {
        "_SHUTDOWN_HOOKS": _STATIC,
        "_STARTUP_HOOKS": _STATIC,
    },
    "alkera_cli.files.chat_fs": {
        "global _resolver": "the chat-tree identity resolver the box installs once at start",
    },
    "alkera_cli.files.folder_fence": {"_HOLDERS": _STATIC},
    "alkera_cli.files.working_copy": {"_RESOLVERS": _STATIC},
    "alkera_cli.files.working_copy_live": {
        "_BY_SERVER": "weakly keyed by the JSON-RPC server; the box serves no JSON-RPC client",
    },
    "alkera_cli.gateway.client": {
        "REMEMBERED_CATALOG = RememberedCatalog()": (
            "the last model catalog fetched, last writer wins; nothing on a box reads it, "
            "and a reader would see another chat's catalog"
        ),
    },
    "alkera_cli.harness.sandbox_kinds": {
        "_KINDS": (
            "the workspace sandbox kinds registered at start from code (an extension "
            "slot); no chat data"
        ),
    },
    "alkera_cli.harness.adapters.claude_agent": {
        "_DISALLOWED_TOOLS": "a constant tool list, extended once at import on Windows",
        "_claude_jobs": "Windows job handles that kill children with the process; no data",
    },
    "alkera_cli.harness.adapters.opencode_http": {
        "_OPENCODE_PERMISSION_ASK": "a constant permission table, adjusted once at import",
    },
    "alkera_cli.cloud_sync.job": {
        "CLOUD_SYNC_LANES = ExtensionPoint()": (
            "registered by the composition before anything runs, frozen once read; "
            "holds no chat data"
        ),
    },
    "alkera_cli.cloud_sync.team_actions": {
        "TEAM_CONNECTION_ACTIONS = ExtensionPoint()": (
            "registered by the composition before anything runs, frozen once read; "
            "holds no chat data"
        ),
    },
    "alkera_cli.cloud_sync.connections_lane": {
        "CONNECTIONS_RECONCILED = ExtensionPoint()": (
            "registered by the composition before anything runs, frozen once read; "
            "holds no chat data"
        ),
        "LEASE_INVALIDATORS = ExtensionPoint()": (
            "registered by the composition before anything runs, frozen once read; "
            "holds no chat data"
        ),
    },
    "alkera_cli.harness.extension_points": {
        "CONNECTION_RECORDS = ExtensionPoint()": (
            "the connection record the product's composition registers before anything "
            "runs, frozen once read; holds no chat data"
        ),
        "HARNESS_CONNECTION_JOBS = ExtensionPoint()": (
            "the per-connection job families the product's composition registers before "
            "anything runs, frozen once read; holds no chat data"
        ),
        "HARNESS_CONTEXT_PROVIDERS = ExtensionPoint()": (
            "the knowledge sources the product's composition registers before anything "
            "runs, frozen once read; holds no chat data"
        ),
        "HARNESS_STANDING_JOBS = ExtensionPoint()": (
            "the standing jobs the product's composition registers before anything runs, "
            "frozen once read; holds no chat data"
        ),
        "SESSION_SPEND = ExtensionPoint()": (
            "the session spend ledger the product's composition registers before anything "
            "runs, frozen once read; holds no chat data"
        ),
        "OPENED_PROJECT_STEPS = ExtensionPoint()": (
            "the opened-project steps the product's composition registers before anything "
            "runs, frozen once read; holds no chat data"
        ),
        "SHARED_PROJECT_STEPS = ExtensionPoint()": (
            "the shared-project steps the product's composition registers before anything "
            "runs, frozen once read; holds no chat data"
        ),
        "WORKSPACE_SEEDERS = ExtensionPoint()": (
            "the workspace seeder the product's composition registers before anything "
            "runs, frozen once read; holds no chat data"
        ),
    },
    "alkera_cli.harness.file_watcher": {
        "_watching": "weak set of running watchers so a supervisor can stop them all",
    },
    "alkera_cli.harness.mcp_server": {
        "global _transport_logs_quieted": _PROCESS_FLAG,
    },
    "alkera_cli.harness.prewarm": {
        "global _settled": "the one pre-warm probe of the agent binary; never handed to a chat",
        "global _task": "the one pre-warm probe of the agent binary; never handed to a chat",
    },
    "alkera_cli.harness.sandbox_probe": {
        "global _CURRENT": "the host's sandbox capability, probed once",
    },
    "alkera_cli.harness.sandbox_scope": {
        "_SCOPES": f"{_PER_SESSION}; written only by the box's workspace host",
    },
    "alkera_cli.harness.session_launches": {"_LAUNCHES": _PER_SESSION},
    "alkera_cli.harness.workspace_sandbox": {"_TEARDOWNS": _STATIC},
    "alkera_cli.host.config": {
        "get_settings@lru_cache": "takes no argument: the deployment's settings",
    },
    "alkera_cli.observability.audit_report": {
        "global _default": (
            "known leak: one audit reporter built from the box's login, attributing every "
            f"chat's decisions to the operator; {_ORG_WORKERS_CLOSE}"
        ),
    },
    "alkera_cli.observability.otel_export": {
        "global _default": (
            f"known leak: one exporter endpoint for every chat, configured by env; "
            f"{_ORG_WORKERS_CLOSE}"
        ),
        "global _default_built": "whether the one exporter was built",
    },
    "alkera_cli.plugins.plugin_base.connection_state": {
        "FAILURE_EXPLAINERS = ExtensionPoint()": (
            "registered by the composition before anything runs, frozen once read; "
            "holds no chat data"
        ),
        "_LISTENERS": _STATIC,
    },
    "alkera_cli.plugins.plugin_base.credential_manager": {
        "_EXTRA_RESOLVERS": _STATIC,
        "_EXTRA_STORERS": _STATIC,
    },
    "alkera_cli.plugins.plugin_base.sql.introspect": {
        "RELATION_AUTHORITIES = ExtensionPoint()": (
            "registered by the composition before anything runs, frozen once read; "
            "holds no chat data"
        ),
    },
    "alkera_cli.plugins.plugin_base.sql_tools": {
        "QUERY_OUTCOME_RECORDERS = ExtensionPoint()": (
            "registered by the composition before anything runs, frozen once read; "
            "holds no chat data"
        ),
    },
    "alkera_cli.plugins.plugin_base.delivery": {
        "_CONFLICT_SOURCES": "keyed by the realpath of a chat's working directory",
    },
    "alkera_cli.plugins.plugin_base.identifier_case": {
        "_dialect@cache": "keyed by a SQL dialect name; holds sqlglot dialect objects",
    },
    "alkera_cli.plugins.plugin_base.permissions.actor": {
        "_ACTOR_CACHE": (
            "keyed by sign-in, so two sign-ins never share a principal; known leak: every chat "
            f"on a box resolves the box's one login and is judged as the operator; "
            f"{_ORG_WORKERS_CLOSE}"
        ),
        "_actors": "the installed principal per sign-in key; on a box, see _ACTOR_CACHE",
        "global _auth_source": "the login reader the box installs once; see _ACTOR_CACHE",
        "global _unbound_actor": (
            "a principal installed with no sign-in to bind to (an unsigned process or a test); "
            "see _ACTOR_CACHE"
        ),
    },
    "alkera_cli.plugins.plugin_base.permissions.audit": {
        "global _observer": "the one decision observer feeding the audit reporter",
    },
    "alkera_cli.plugins.plugin_base.permissions.config": {
        "_PERMISSIONS_CACHE": (
            "parsed policy keyed by path and file stamps; it carries the box-wide "
            "permissions.local.yml whose standing answers cross chats (the canary's "
            "standing-answer cases)"
        ),
    },
    "alkera_cli.commands.extension_points": {
        "CLI_COMMANDS = ExtensionPoint()": (
            "the command mounts the product's composition registers before the command "
            "tree is built, frozen once read; holds no chat data"
        ),
        "CLI_STARTUP = ExtensionPoint()": (
            "the launch hooks the product's composition registers before the command "
            "tree is built, frozen once read; holds no chat data"
        ),
        "CLI_CRASH = ExtensionPoint()": (
            "the crash hooks the product's composition registers before the command "
            "tree is built, frozen once read; holds no chat data"
        ),
        "CLI_DEFAULT = ExtensionPoint()": (
            "the command bare `alkera` runs, registered by the product's composition "
            "before the command tree is built, frozen once read; holds no chat data"
        ),
    },
    "alkera_cli.daemon.extension_points": {
        "DAEMON_METHODS = ExtensionPoint()": (
            "the daemon method contributions the product's composition registers before "
            "the daemon loads its methods, frozen once read; holds no chat data"
        ),
    },
    "alkera_cli.notebooks.sql_slot": {
        "NOTEBOOK_SQL_PROVIDERS = ExtensionPoint()": (
            "the notebook SQL provider factories the product's composition registers "
            "before anything runs, frozen once read; holds no chat data"
        ),
    },
    "alkera_cli.plugins.plugin_base.agent_tools": {
        "AGENT_TOOLS = ExtensionPoint()": (
            "the tool sources the product's composition registers before anything runs, "
            "frozen once read; holds no chat data"
        ),
    },
    "alkera_cli.plugins.plugin_base.oauth_sign_in": {
        "OAUTH_SIGN_IN = ExtensionPoint()": (
            "the browser sign-in the product's composition registers before anything "
            "runs, frozen once read; holds no chat data"
        ),
    },
    "alkera_cli.plugins.plugin_base.registration": {
        "PLUGIN_REGISTRATION_CHECKS = ExtensionPoint()": (
            "the registration checks the product's composition registers before anything "
            "runs, frozen once read; holds no chat data"
        ),
    },
    "alkera_cli.plugins.plugin_base.plugin": {
        "CLI_PLUGINS = ExtensionPoint()": (
            "the plugin classes the product's composition registers before anything runs, "
            "frozen once read; holds no chat data"
        ),
    },
    "alkera_cli.plugins.plugin_base.tool": {
        "TOOL_FAILURE_CLASSIFIERS = ExtensionPoint()": (
            "the failure classifiers the product's composition registers before anything "
            "runs, frozen once read; holds no chat data"
        ),
    },
    "alkera_cli.plugins.plugin_base.permissions.wiring": {
        "IMPACT_MEASURES = ExtensionPoint()": (
            "the impact measure the product's composition registers before anything "
            "runs, frozen once read; holds no chat data"
        ),
    },
    "alkera_cli.plugins.plugin_base.progress": {
        "NULL_PROGRESS = ProgressReporter()": "a reporter with no sidecar path; writes nothing",
    },
    "alkera_cli.plugins.plugin_base.schema_freshness": {
        "_watchers": "one listing watcher per .alkera path",
    },
    "alkera_cli.plugins.plugin_base.sql.shared_reach": {
        "_GUARDED": "the guarded connect callables; code, no chat data",
        "_PINS": (
            "keyed by hostname: the public addresses that name was vetted to, the same answer "
            "for every org (a DNS-rebinding pin); holds no credential"
        ),
        "_TWINS": "each raw connect callable's guarded twin; code, no chat data",
        "global _INSTALLED": "whether the process-wide resolver pin is installed",
    },
    "alkera_cli.plugins.plugin_base.sql.spec": {"_READ_ONLY_ENGINES": _STATIC},
    "alkera_cli.ui.theme": {
        "global _active": "the terminal theme of an interactive session; no box output",
        "global _pushed": "the terminal theme of an interactive session; no box output",
    },
}


def test_box_reachable_module_state_is_allowlisted_with_a_reason(allowed: dict[str, Any]) -> None:
    state = allowed["ALLOWED_BOX_MODULE_STATE"]
    found = box_module_state(frozenset(allowed["box_root_modules"]))
    _assert_only_shrinks(found, {m: sorted(sites) for m, sites in state.items()})
    unexplained = {
        m: [site for site, why in sites.items() if not why.strip()] for m, sites in state.items()
    }
    assert not {m: s for m, s in unexplained.items() if s}, "every entry says why it is safe"


def test_the_box_reaches_the_harness_and_the_plugins_from_its_roots(
    allowed: dict[str, Any],
) -> None:
    reachable = box_reachable_modules(frozenset(allowed["box_root_modules"]))
    assert "alkera_cli.harness.runtime" in reachable
    assert "alkera_cli.plugins.plugin_base.tool" in reachable
    assert "alkera_cli.main" not in reachable
    assert not {m for m in reachable if m.endswith("_tui")}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("_C: dict[str, int] = {}\n", ["_C"], id="empty-container"),
        pytest.param("_C = {}\ndef f(k):\n    _C[k] = 1\n", ["_C"], id="empty-then-filled"),
        pytest.param("TABLE = {'a': 1}\n", [], id="constant-table"),
        pytest.param("TABLE = {'a': 1}\nTABLE.update(b=2)\n", ["TABLE"], id="table-changed"),
        pytest.param("S = set()\n", ["S"], id="empty-call"),
        pytest.param("D = defaultdict(list)\n", ["D"], id="defaultdict-factory-only"),
        pytest.param("W = weakref.WeakValueDictionary()\n", ["W"], id="dotted-constructor"),
        pytest.param("if X:\n    _C = []\n", ["_C"], id="inside-a-module-if"),
        pytest.param("def f():\n    c = {}\n", [], id="local-variable"),
        pytest.param("x = None\ndef f():\n    global x\n    x = 1\n", ["global x"], id="global"),
        pytest.param("@lru_cache(maxsize=8)\ndef f(k):\n    pass\n", ["f@lru_cache"], id="lru"),
        pytest.param("@functools.cache\ndef f(k):\n    pass\n", ["f@cache"], id="cache"),
        pytest.param("@cached_property\ndef f(self):\n    pass\n", [], id="per-instance"),
        pytest.param(
            "class K:\n    seen: ClassVar[set[str]] = set()\n", ["K.seen"], id="class-var"
        ),
        pytest.param(
            "class K:\n    items = []\n    def add(self, x):\n        self.items.append(x)\n",
            ["K.items"],
            id="class-list-shared-by-instances",
        ),
        pytest.param(
            "class K:\n    items = []\n    def __init__(self):\n        self.items = []\n",
            [],
            id="shadowed-per-instance",
        ),
        pytest.param("class M(BaseModel):\n    tags: list[str] = []\n", [], id="model-field"),
        pytest.param("C = Counter()\n", ["C"], id="counter"),
        pytest.param("R = Reg()\n", ["R = Reg()"], id="instance-of-a-class-that-changes-itself"),
        pytest.param("F = Frozen()\n", [], id="instance-of-an-unchanging-class"),
        pytest.param("R = Reg[int]()\n", ["R = Reg()"], id="generic-instance"),
    ],
)
def test_the_module_state_scanner(source: str, expected: list[str]) -> None:
    assert module_state(source, frozenset({"Reg"})) == expected


@pytest.mark.parametrize(
    ("source", "changes"),
    [
        pytest.param("class K:\n    def put(self, v):\n        self.v = v\n", True, id="assign"),
        pytest.param(
            "class K:\n    def put(self, k):\n        self.d[k] = 1\n", True, id="store-item"
        ),
        pytest.param(
            "class K:\n    def put(self, k):\n        self.s.add(k)\n", True, id="mutating-call"
        ),
        pytest.param(
            "class K:\n    def __init__(self):\n        self.v = 1\n", False, id="construction"
        ),
        pytest.param(
            "class K:\n    def get(self):\n        return self.v\n", False, id="read-only"
        ),
    ],
)
def test_which_classes_change_themselves(source: str, changes: bool) -> None:
    cls = ast.parse(source).body[0]
    assert isinstance(cls, ast.ClassDef)
    assert changes_itself(cls) is changes


if __name__ == "__main__":
    json.dump(
        {
            "ui": ui_library_imports(),
            "facade": facade_imports_from_libraries(),
            "contracts": contracts_outside_imports(),
            "alkera_dirs": hand_built_alkera_dirs(),
            "httpx": httpx_clients(),
            "stores": connection_stores(),
            "lineage_stores": whole_lineage_store_openings(),
            "auth": auth_reads(),
            "function_level": function_level_cross_package_imports(),
            "modules": oversized_modules(),
            "classes": oversized_classes(),
            "box_module_state": box_module_state(),
        },
        sys.stdout,
        indent=1,
        sort_keys=True,
    )
