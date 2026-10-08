"""The `alkera` entry (``alkera_cli.entry``): the open entry and the product's
both go through it, and the product's extensions are installed before the
command tree is imported, so no extension point is read and frozen first."""

from __future__ import annotations

import sys
import types

import pytest
from alkera_cli import entry

pytestmark = [pytest.mark.spread]


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.delitem(sys.modules, "alkera_cli.main", raising=False)
    cli = types.ModuleType("alkera_cli.main")
    setattr(cli, "app", lambda: seen.append("app"))  # noqa: B010
    setattr(cli, "main", lambda: seen.append("console"))  # noqa: B010

    monkeypatch.setitem(sys.modules, "alkera_cli.main", cli)
    monkeypatch.setattr("alkera_cli.main", cli, raising=False)
    monkeypatch.setattr(sys, "argv", ["alkera", "health"])
    return seen


def test_the_product_installs_before_the_typer_app_runs(calls: list[str]) -> None:
    entry.main(lambda: calls.append("install"))
    assert calls == ["install", "app"]


def test_the_console_script_installs_before_the_crash_path_runs(calls: list[str]) -> None:
    entry.console_main(lambda: calls.append("install"))
    assert calls == ["install", "console"]


@pytest.fixture
def open_composition(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    """The open composition stands in for one that records its install: the
    suite's product is installed already, so the real one would collide."""
    composition = types.ModuleType("_open_composition_stand_in")
    setattr(composition, "install", lambda: calls.append("open install"))  # noqa: B010
    monkeypatch.setitem(sys.modules, composition.__name__, composition)
    monkeypatch.setattr(entry, "OPEN_COMPOSITION", f"{composition.__name__}:install")


@pytest.mark.usefixtures("open_composition")
def test_the_open_console_script_installs_the_open_composition_first(calls: list[str]) -> None:
    entry.console_main()
    assert calls == ["open install", "console"]


@pytest.mark.usefixtures("open_composition")
def test_the_open_python_m_installs_the_open_composition_first(calls: list[str]) -> None:
    entry.main()
    assert calls == ["open install", "app"]


@pytest.mark.usefixtures("open_composition")
def test_an_installer_given_replaces_the_open_composition(calls: list[str]) -> None:
    entry.main(lambda: calls.append("install"))
    assert calls == ["install", "app"]
