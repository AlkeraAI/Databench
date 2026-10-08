"""Friendly harness names resolve to the slug a chat manifest stores."""

from __future__ import annotations

import pytest
from alkera_cli.harness.registry import (
    CLAUDE_HARNESS,
    HARNESS_CHOICES,
    OPENCODE_HARNESS,
    resolve_harness_type,
)


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        pytest.param("alkera", "agent", id="advertised-default"),
        pytest.param("opencode", "agent", id="opencode-names-the-default"),
        pytest.param("  OpenCode ", "agent", id="case-and-space-free"),
        pytest.param("agent", "agent", id="slug-itself"),
        pytest.param("claude", "claude-agent", id="claude"),
        pytest.param("claude-code", "claude-agent", id="claude-code"),
        pytest.param("cc", "claude-agent", id="cc"),
        pytest.param("claude-agent", "claude-agent", id="claude-slug"),
    ],
)
def test_a_known_name_resolves_to_its_stored_slug(name: str, slug: str) -> None:
    assert resolve_harness_type(name) == slug


@pytest.mark.parametrize("name", ["codex", "", "oc", "open-code"])
def test_an_unknown_name_resolves_to_nothing(name: str) -> None:
    assert resolve_harness_type(name) is None


def test_every_advertised_choice_resolves() -> None:
    assert {resolve_harness_type(c) for c in HARNESS_CHOICES} == {OPENCODE_HARNESS, CLAUDE_HARNESS}
