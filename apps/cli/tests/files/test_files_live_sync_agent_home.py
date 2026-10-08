"""The agent's own ``.cache/`` and ``.local/`` never leave the box.

A box runs a chat's commands with ``HOME`` at the working directory, so every
tool that keeps a cache under ``~`` grows them there. The watcher must not wake
on them and the tree report must not carry them; a same-named file deeper in
the chat's work, or a name that only starts the same way, still travels.
"""

from __future__ import annotations

import pytest
from alkera_cli.files.live_sync import _tree_safe, live_watch_filter
from alkera_cli.files.tree_watch import Change


@pytest.mark.parametrize(
    "relative",
    [
        pytest.param(".cache", id="cache"),
        pytest.param(".cache/pip/http/abc", id="cache-deep"),
        pytest.param(".local", id="local"),
        pytest.param(".local/bin/tool", id="local-deep"),
    ],
)
def test_the_agents_home_directories_are_not_reported(relative: str) -> None:
    assert not _tree_safe(relative)


@pytest.mark.parametrize("leaf", [".cache", ".local"])
def test_the_watcher_does_not_wake_on_them(leaf: str) -> None:
    assert not live_watch_filter(Change.added, f"/work/{leaf}")


@pytest.mark.parametrize(
    "relative",
    [
        pytest.param(".cachefile", id="prefix-only"),
        pytest.param("project/.cache", id="nested"),
        pytest.param("notes.local", id="suffix"),
    ],
)
def test_names_that_only_look_alike_still_travel(relative: str) -> None:
    assert _tree_safe(relative)
