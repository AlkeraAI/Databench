"""Every child process starts through ``alkera_core.process``.

What a child inherits (fd 0, every open descriptor, the terminal's process
group, no tie to its parent's lifetime) fails far from the call site: a
probe that read its inherited stdin consumed an org worker's control socket
and crash-looped the worker. The spawn seam gives every child the safe
defaults, so nothing in shipped source may start a process any other way: no
``subprocess`` call, ``asyncio.create_subprocess_*``, ``anyio`` process,
``os.exec*`` / ``os.spawn*`` / ``os.fork`` / ``os.system`` / ``os.popen``,
``pty.spawn``, ``multiprocessing`` or ``ProcessPoolExecutor`` outside
``alkera_core/process.py``.

The roots scanned are every shipped package (``_source_roots``) plus the
notebook package (its vendored ``_marimo`` aside). The allowlist
holds today's remaining sites, each with a reason; it may only shrink, and an
entry the tree no longer has fails too.
"""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
from _source_roots import shipped_source_roots

REPO_ROOT = Path(__file__).resolve().parents[3]
SEAM = "packages/api-core/alkera_core/process.py"
EXTRA_ROOTS = ("packages/alkera-notebook/alkera_notebook",)
SKIPPED = ("/_marimo/", "/_generated/")

_SUBPROCESS_CALLS = frozenset(
    {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}
)
_EXACT = frozenset(
    {
        "asyncio.create_subprocess_exec",
        "asyncio.create_subprocess_shell",
        "anyio.open_process",
        "anyio.run_process",
        "os.fork",
        "os.forkpty",
        "os.system",
        "os.popen",
        "pty.spawn",
        "concurrent.futures.ProcessPoolExecutor",
        "multiprocessing.Process",
        "multiprocessing.Pool",
        "multiprocessing.get_context",
    }
)
_PREFIXES = ("os.exec", "os.spawn", "os.posix_spawn")

#: Why the notebook package's own runner and local launcher start processes
#: by hand: the package stands alone and never imports ``alkera_core``, so it
#: keeps the seam's defaults itself (stdin ``/dev/null`` or a pipe, a session
#: of its own, no stray descriptor); the box injects the seam's runner and the
#: sandboxed launcher in its place.
_STANDALONE = "the standalone notebook package keeps the seam's defaults by hand"

#: ``(path, call) -> (count, reason)``: the sites that still start a process
#: outside the seam.
ALLOWED: dict[tuple[str, str], tuple[int, str]] = {
    ("packages/alkera-notebook/alkera_notebook/envs/runner.py", "asyncio.create_subprocess_exec"): (
        1,
        _STANDALONE,
    ),
    ("packages/alkera-notebook/alkera_notebook/kernels/launch_local.py", "subprocess.Popen"): (
        1,
        _STANDALONE,
    ),
    ("packages/alkera-notebook/alkera_notebook/kernels/launch_local.py", "subprocess.run"): (
        1,
        _STANDALONE,
    ),
}


def scan_roots(repo_root: Path) -> list[str]:
    return sorted({*shipped_source_roots(repo_root), *EXTRA_ROOTS})


def _aliases(tree: ast.AST) -> dict[str, str]:
    """What each imported name stands for (``sp`` -> ``subprocess``,
    ``Popen`` -> ``subprocess.Popen``)."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names


def _dotted(node: ast.expr, names: dict[str, str]) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(names.get(node.id, node.id))
    return ".".join(reversed(parts))


def _spawns(name: str) -> bool:
    if name in _EXACT or name.startswith(_PREFIXES):
        return True
    module, _, call = name.rpartition(".")
    return module == "subprocess" and call in _SUBPROCESS_CALLS


def spawn_sites(repo_root: Path, roots: Iterable[str]) -> Counter[tuple[str, str]]:
    found: Counter[tuple[str, str]] = Counter()
    for root in roots:
        for path in sorted((repo_root / root).rglob("*.py")):
            rel = path.relative_to(repo_root).as_posix()
            if rel == SEAM or any(part in rel for part in SKIPPED):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            names = _aliases(tree)
            for expr in _spawning_positions(tree):
                name = _dotted(expr, names)
                if name is not None and _spawns(name):
                    found[(rel, name)] += 1
    return found


def _spawning_positions(tree: ast.AST) -> Iterable[ast.expr]:
    """Where a spawning callable can be used: called, or handed on to be
    called later (``asyncio.to_thread(subprocess.run, ...)``, a default
    argument, an assignment)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node.func
            yield from node.args
            yield from (keyword.value for keyword in node.keywords)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield from node.args.defaults
            yield from (d for d in node.args.kw_defaults if d is not None)
        elif isinstance(node, ast.Assign):
            yield node.value


Allowed = dict[tuple[str, str], tuple[int, str]]


def with_layers(own: Allowed, architecture_allowlist: Any) -> Allowed:
    """``own`` plus the sites a test layer adds for its own modules, keyed
    ``path::call``."""
    keyed = {f"{path}::{call}": [n, why] for (path, call), (n, why) in own.items()}
    merged = architecture_allowlist("process_seam", {"sites": keyed})["sites"]
    sites: Allowed = {}
    for key, (n, why) in merged.items():
        path, call = key.split("::", 1)
        sites[(path, call)] = (n, why)
    return sites


def violations(
    found: Counter[tuple[str, str]], allowed: Allowed = ALLOWED
) -> tuple[list[str], list[str]]:
    new = [
        f"{path}: {call} x{count}"
        for (path, call), count in sorted(found.items())
        if count > allowed.get((path, call), (0, ""))[0]
    ]
    stale = [
        f"{path}: {call} (listed x{listed}, found x{found.get((path, call), 0)})"
        for (path, call), (listed, _reason) in sorted(allowed.items())
        if found.get((path, call), 0) < listed
    ]
    return new, stale


def test_every_child_starts_through_the_seam(architecture_allowlist: Any) -> None:
    allowed = with_layers(ALLOWED, architecture_allowlist)
    new, stale = violations(spawn_sites(REPO_ROOT, scan_roots(REPO_ROOT)), allowed)
    assert not new, (
        "a process is started outside alkera_core.process; build a SpawnSpec and use "
        "spawn / spawn_async / run / run_async:\n  " + "\n  ".join(new)
    )
    assert not stale, "remove these allowlist entries, the tree no longer has them:\n  " + (
        "\n  ".join(stale)
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            "import subprocess\nsubprocess.run(['ls'])\n", ["pkg/mod.py: subprocess.run x1"]
        ),
        pytest.param(
            "import subprocess as sp\nsp.Popen(['ls'])\n",
            ["pkg/mod.py: subprocess.Popen x1"],
            id="aliased-module",
        ),
        pytest.param(
            "from subprocess import check_output\ncheck_output(['ls'])\n",
            ["pkg/mod.py: subprocess.check_output x1"],
            id="imported-name",
        ),
        pytest.param(
            "import asyncio\nasync def f():\n    await asyncio.create_subprocess_exec('ls')\n",
            ["pkg/mod.py: asyncio.create_subprocess_exec x1"],
            id="asyncio",
        ),
        pytest.param(
            "import anyio\nasync def f():\n    await anyio.open_process(['ls'])\n",
            ["pkg/mod.py: anyio.open_process x1"],
            id="anyio",
        ),
        pytest.param("import os\nos.execv('/bin/ls', ['ls'])\n", ["pkg/mod.py: os.execv x1"]),
        pytest.param("import os\nos.fork()\n", ["pkg/mod.py: os.fork x1"]),
        pytest.param(
            "from concurrent.futures import ProcessPoolExecutor\nProcessPoolExecutor()\n",
            ["pkg/mod.py: concurrent.futures.ProcessPoolExecutor x1"],
            id="process-pool",
        ),
        pytest.param(
            "import multiprocessing\nmultiprocessing.Process(target=print)\n",
            ["pkg/mod.py: multiprocessing.Process x1"],
            id="multiprocessing",
        ),
        pytest.param(
            "import asyncio, subprocess\nasync def f():\n"
            "    await asyncio.to_thread(subprocess.run, ['ls'])\n",
            ["pkg/mod.py: subprocess.run x1"],
            id="handed-to-a-thread",
        ),
        pytest.param(
            "import subprocess\ndef f(run=subprocess.run):\n    return run\n",
            ["pkg/mod.py: subprocess.run x1"],
            id="a-default-argument",
        ),
        pytest.param("import subprocess\nX = subprocess.DEVNULL\n", [], id="a-constant-is-fine"),
        pytest.param(
            "import subprocess\ndef f() -> subprocess.Popen[bytes]: ...\n",
            [],
            id="an-annotation-is-fine",
        ),
    ],
)
def test_the_scan_catches_a_planted_spawn(tmp_path: Path, source: str, expected: list[str]) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text(source, encoding="utf-8")
    new, _stale = violations(spawn_sites(tmp_path, ["pkg"]))
    assert new == expected


def test_an_allowlisted_site_that_left_the_tree_is_stale(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("X = 1\n", encoding="utf-8")
    _new, stale = violations(spawn_sites(tmp_path, ["pkg"]))
    assert len(stale) == len(ALLOWED)
