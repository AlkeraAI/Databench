"""Public model-gateway selection and reasoning-display contracts."""

from __future__ import annotations

import pytest
from alkera_core.gateway import DISPLAY_SUMMARIZED, thinking_display_for_effort


@pytest.mark.parametrize(
    ("effort", "display"),
    [
        pytest.param(None, None, id="absent-off"),
        pytest.param("none", None, id="none-off"),
        pytest.param(" NoNe ", None, id="none-is-trimmed-and-case-insensitive"),
        pytest.param("low", DISPLAY_SUMMARIZED, id="low-on"),
        pytest.param("medium", DISPLAY_SUMMARIZED, id="medium-on"),
        pytest.param("high", DISPLAY_SUMMARIZED, id="high-on"),
    ],
)
def test_thinking_display_is_derived_only_from_effective_effort(
    effort: str | None, display: str | None
) -> None:
    """Only the effective ``none`` level disables surfaced reasoning."""
    assert thinking_display_for_effort(effort) == display
