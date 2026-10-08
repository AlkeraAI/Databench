"""The memory probes must run on the platform they are measuring.

Two Files proofs measure a fresh interpreter's peak resident set, and both used
to open with ``import resource`` — a module that does not exist on Windows. The
child would end with ``ModuleNotFoundError`` before it measured anything, and
the assertion that failed would be the one about its exit status, which says
nothing about memory. So the branch lives in one place, every branch of it is
driven from here, and both probes are checked for the top-level import that
would sink them again.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from _peak_rss import PEAK_RSS_SOURCE, peak_rss

WINDOWS_PEAK = 123 * 1024 * 1024


def _fake_psutil(peak: int) -> types.ModuleType:
    """A stand-in for the module only the Windows branch imports."""
    module = types.ModuleType("psutil")

    class _Info:
        peak_wset = peak

    class _Process:
        def memory_info(self) -> Any:
            return _Info()

    module.Process = _Process  # type: ignore[attr-defined]
    return module


def test_the_windows_branch_answers_without_the_resource_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``import resource`` raises where the branch must still produce a number.

    ``sys.modules["resource"] = None`` is the documented way to make an import
    of it fail, so this is the Windows condition itself rather than a mock of
    it: a branch that fell through to ``getrusage`` cannot pass.
    """
    monkeypatch.setitem(sys.modules, "resource", None)
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil(WINDOWS_PEAK))

    assert peak_rss("win32") == WINDOWS_PEAK


def test_the_helper_imports_at_all_where_resource_does_not_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hoisting the import back to module scope breaks Windows at import time.

    The helper is re-imported from scratch with ``resource`` unavailable, which
    is the only way to catch a top-level import: once a module is in
    ``sys.modules`` its imports have already run.
    """
    monkeypatch.setitem(sys.modules, "resource", None)
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil(WINDOWS_PEAK))
    monkeypatch.delitem(sys.modules, "_peak_rss", raising=False)

    reloaded = importlib.import_module("_peak_rss")

    assert reloaded.peak_rss("win32") == WINDOWS_PEAK


def test_a_posix_platform_reads_its_own_high_water_mark() -> None:
    """The branch this machine is on returns a plausible, positive byte count.

    Bytes, not kibibytes: the ceilings both probes assert are in bytes, so a
    branch that returned Linux's ``ru_maxrss`` unit would silently pass a
    thousand-fold overshoot.
    """
    measured = peak_rss(sys.platform)

    assert measured > 4 * 1024 * 1024, f"{measured} bytes is not a live interpreter's peak"
    assert measured < 64 * 1024**3


def _load(relative: str, name: str) -> types.ModuleType:
    """Import a probe-carrying test module by path, under a name of its own.

    By path because the perf modules sit in a directory pytest only puts on
    ``sys.path`` when it collects it, and under a private name so this never
    shadows the copy pytest itself imported.
    """
    path = Path(__file__).parent / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with _on_path(path.parent):
        spec.loader.exec_module(module)
    return module


@contextmanager
def _on_path(directory: Path) -> Iterator[None]:
    sys.path.insert(0, str(directory))
    try:
        yield
    finally:
        sys.path.remove(str(directory))


@pytest.mark.parametrize(
    ("relative", "constant"),
    [
        pytest.param("test_files_download_zip.py", "_MEMORY_PROBE", id="zip-stream"),
        pytest.param("perf/test_files_perf_search_bulk.py", "_RSS_PROBE", id="range-read"),
    ],
)
def test_neither_probe_imports_a_posix_only_module_at_top_level(
    relative: str, constant: str
) -> None:
    """The probes are source text, so their imports are read out of the text.

    A top-level ``import resource`` is exactly the regression that made the
    Windows shard fail with nothing attributable, and it is invisible to every
    other check in the suite: the string compiles fine on this machine and the
    child that chokes on it only exists on a runner.
    """
    module = _load(relative, "_probe_source_" + constant.strip("_").lower())
    probe: str = getattr(module, constant)
    top_level = {
        alias.name.split(".")[0]
        for node in ast.parse(probe).body
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.parse(probe).body
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert not top_level & {"resource", "fcntl", "psutil", "pwd", "grp", "termios"}, (
        f"{relative} imports a platform-specific module before it branches: {top_level}"
    )
    assert PEAK_RSS_SOURCE in probe, "the probe measures memory some other way than the helper"
