"""What a process may use of its machine is read in one place.

A container's memory and cores are set by its cgroup, not by what
``/proc/meminfo``, ``os.cpu_count()`` or ``psutil.virtual_memory()`` report
about its host, and sizing from the host's figures oversubscribes the box
until the kernel kills it. ``alkera_core.host_resources`` resolves the
process's own cgroup; nothing else in shipped source may read those figures.

Today's readers that are not sizing decisions are listed with a reason. The
list may only shrink: a new read fails, and so does an entry the tree no
longer has.
"""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from _source_roots import shipped_source_roots

REPO_ROOT = Path(__file__).resolve().parents[3]
OWNER = "packages/api-core/alkera_core/host_resources.py"
EXTRA_ROOTS = ("packages/alkera-notebook/alkera_notebook",)

#: Calls that read the host's figures.
FORBIDDEN_CALLS = frozenset({"psutil.virtual_memory", "os.cpu_count", "os.sysconf"})
#: A path whose read is the host's figure.
FORBIDDEN_PATHS = frozenset({"/proc/meminfo"})

#: ``(path, what) -> (count, reason)``: reads that are not sizing decisions.
ALLOWED: dict[tuple[str, str], tuple[int, str]] = {
    ("packages/alkera-notebook/alkera_notebook/kernels/launch_local.py", "os.sysconf"): (
        1,
        "the page size, to turn a kernel's /proc statm pages into bytes; not a limit",
    ),
    ("packages/api-core/alkera_core/compute/ssh/probe.py", "/proc/meminfo"): (
        1,
        "a shell line run on the SSH host to report its size; reads no local figure",
    ),
}


def scan_roots(repo_root: Path) -> list[str]:
    return sorted({*shipped_source_roots(repo_root), *EXTRA_ROOTS})


def _docstrings(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _aliases(tree: ast.AST) -> dict[str, str]:
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names


def host_reads(repo_root: Path, roots: Iterable[str]) -> Counter[tuple[str, str]]:
    found: Counter[tuple[str, str]] = Counter()
    for root in roots:
        for path in sorted((repo_root / root).rglob("*.py")):
            rel = path.relative_to(repo_root).as_posix()
            if rel == OWNER or "/_marimo/" in rel or "/_generated/" in rel:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            names = _aliases(tree)
            docstrings = _docstrings(tree)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                        called = f"{names.get(func.value.id, func.value.id)}.{func.attr}"
                    elif isinstance(func, ast.Name):
                        called = names.get(func.id, func.id)
                    else:
                        continue
                    if called in FORBIDDEN_CALLS:
                        found[(rel, called)] += 1
                elif (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in docstrings
                ):
                    for forbidden in FORBIDDEN_PATHS:
                        if forbidden in node.value:
                            found[(rel, forbidden)] += 1
    return found


def violations(found: Counter[tuple[str, str]]) -> tuple[list[str], list[str]]:
    new = [
        f"{path}: {what} x{count}"
        for (path, what), count in sorted(found.items())
        if count > ALLOWED.get((path, what), (0, ""))[0]
    ]
    stale = [
        f"{path}: {what} (listed x{listed}, found x{found.get((path, what), 0)})"
        for (path, what), (listed, _reason) in sorted(ALLOWED.items())
        if found.get((path, what), 0) < listed
    ]
    return new, stale


def test_host_figures_are_read_only_by_host_resources() -> None:
    new, stale = violations(host_reads(REPO_ROOT, scan_roots(REPO_ROOT)))
    assert not new, (
        "the host's memory or cores are read outside alkera_core.host_resources; use "
        "effective_memory_bytes / effective_cpus / host_memory_in_use:\n  " + "\n  ".join(new)
    )
    assert not stale, "remove these entries, the tree no longer has them:\n  " + "\n  ".join(stale)


def test_the_scan_catches_each_kind_of_read(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text(
        '"""Reads /proc/meminfo in prose only."""\n'
        "import os\n"
        "import psutil as ps\n"
        "from os import cpu_count\n"
        "a = os.cpu_count()\n"
        "b = cpu_count()\n"
        "c = ps.virtual_memory()\n"
        'd = os.sysconf("SC_PHYS_PAGES")\n'
        'e = open("/proc/meminfo").read()\n'
        "f = os.getpid()\n",
        encoding="utf-8",
    )
    new, _stale = violations(host_reads(tmp_path, ["pkg"]))
    assert new == [
        "pkg/mod.py: /proc/meminfo x1",
        "pkg/mod.py: os.cpu_count x2",
        "pkg/mod.py: os.sysconf x1",
        "pkg/mod.py: psutil.virtual_memory x1",
    ]
