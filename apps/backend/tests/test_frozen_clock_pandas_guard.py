"""pandas is imported before any frozen clock, because importing it under one kills
the process.

The root conftest's ``pytest_collection_finish`` imports pandas on every worker whose
collection includes a module that freezes the clock. The first test pins the hazard
itself, in a child interpreter, so the day freezegun or pandas no longer crash the guard
can go; the others pin the guard's decision.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _root_conftest(config: pytest.Config) -> types.ModuleType:
    """The repo-root ``conftest.py`` as this session registered it, from the plugin
    manager: importing it afresh would run its body, which provisions this worker's
    database, a second time."""
    wanted = REPO_ROOT / "conftest.py"
    for _name, plugin in config.pluginmanager.list_name_plugin():
        path = getattr(plugin, "__file__", None)
        if path and Path(str(path)) == wanted:
            return plugin
    raise AssertionError("the root conftest is not loaded")


@pytest.mark.skipif(importlib.util.find_spec("pandas") is None, reason="pandas is not installed")
def test_importing_pandas_under_a_frozen_clock_kills_the_interpreter() -> None:
    """The premise. A child that does it must die (a signal, or Windows' fatal
    exception code), never finish the import. If this starts passing, the guard
    in the root conftest has nothing left to guard."""
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "from freezegun import freeze_time\n"
            "with freeze_time('2026-01-01'):\n"
            "    import pandas\n"
            "print('imported')\n",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert child.returncode != 0, child.stdout + child.stderr
    assert "imported" not in child.stdout


def _module(name: str, **attrs: object) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def test_only_modules_that_bind_a_frozen_clock_are_named(request: pytest.FixtureRequest) -> None:
    conftest = _root_conftest(request.config)
    plain = _module("tests.plain", now=object())
    by_function = _module("tests.freezes", freeze_time=object())
    by_package = _module("tests.freezes_too", freezegun=object())
    assert conftest.modules_that_freeze_the_clock([plain]) == []
    assert conftest.modules_that_freeze_the_clock([by_function, plain, by_package]) == [
        "tests.freezes",
        "tests.freezes_too",
    ]


def test_a_collection_with_no_frozen_clock_imports_nothing(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    conftest = _root_conftest(request.config)

    def refuse(name: str) -> None:
        raise AssertionError(f"imported {name} for a collection that freezes no clock")

    monkeypatch.setattr(conftest.importlib, "import_module", refuse)
    assert conftest.import_pandas_ahead_of_frozen_clocks([_module("tests.plain")]) is False


@pytest.mark.skipif(importlib.util.find_spec("pandas") is None, reason="pandas is not installed")
def test_a_collection_that_freezes_the_clock_has_pandas_imported_first(
    request: pytest.FixtureRequest,
) -> None:
    conftest = _root_conftest(request.config)
    assert conftest.import_pandas_ahead_of_frozen_clocks([_module("tests.f", freeze_time=1)])
    assert "pandas" in sys.modules
