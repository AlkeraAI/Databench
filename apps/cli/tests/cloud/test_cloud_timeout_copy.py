"""The statement-timeout note is read in a browser as often as in an editor
now that one daemon serves both: it must name the setting and who changes it,
never one client's menu path."""

from __future__ import annotations

import inspect

import pytest
from alkera_cli.plugins.plugin_base.sql import timeout


@pytest.fixture
def note(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv(timeout.ENV_VAR, "120")
    rendered = timeout.statement_timeout_note(
        RuntimeError("canceling statement due to statement timeout")
    )
    assert rendered is not None
    return rendered


def test_the_note_never_points_at_an_editor_only_surface(note: str) -> None:
    assert "vs code" not in note.lower()
    assert "vscode" not in note.lower()


def test_the_note_names_the_setting_and_the_person_who_can_raise_it(note: str) -> None:
    assert "Statement timeout" in note
    assert "Ask an admin to raise the query timeout for this workspace" in note
    assert "2 minutes" in note  # the current value still rides along


def test_no_sibling_in_the_sql_package_mentions_the_editor_by_name() -> None:
    """A grep over the module source, so a future copy edit cannot bring the
    phrase back through a neighbouring string."""
    source = inspect.getsource(timeout)
    assert "VS Code" not in source
