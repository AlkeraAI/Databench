"""The one figure an operator can move on the request boundary scan.

The scan holds a JSON body in memory to check it, before routing and before any
credential is checked, on every request of both apps. How much it will hold is
therefore a deployment decision — a memory bound on one side, the coverage of
the routes that persist the most text on the other — so it is a setting rather
than a constant nobody can reach, and a value that breaks either side of that
trade is refused at boot instead of discovered under load.
"""

from __future__ import annotations

import pytest
from _settings_env import seal_settings_env
from alkera_core.config import Settings
from pydantic import ValidationError


@pytest.fixture(autouse=True)
def _only_what_this_test_sets(monkeypatch: pytest.MonkeyPatch) -> None:
    """A shell that sourced the dev config would otherwise reach a `Settings()`
    built here and decide the case for it."""
    seal_settings_env(monkeypatch)


def test_the_shipped_default_is_the_figure_the_scan_was_written_against() -> None:
    """Read off the model field, not the live settings object, so a developer's
    own `.env` cannot make this pass while the shipped default has drifted."""
    assert Settings.model_fields["request_scan_max_body_bytes"].default == 2 * 1024 * 1024


@pytest.mark.parametrize(
    ("raw", "accepted"),
    [
        pytest.param("65536", True, id="the-floor"),
        pytest.param("65535", False, id="one-below-the-floor"),
        pytest.param("4194304", True, id="a-deployment-with-large-documents"),
        pytest.param("33554432", True, id="the-ceiling"),
        pytest.param("33554433", False, id="one-above-the-ceiling"),
        pytest.param("0", False, id="zero-turns-the-scan-off-silently"),
        pytest.param("-1", False, id="negative"),
    ],
)
def test_the_ceiling_is_refused_outside_the_range_it_has_to_sit_in(
    monkeypatch: pytest.MonkeyPatch, raw: str, accepted: bool
) -> None:
    """Below the floor an ordinary request body stops being scanned at all —
    the boundary would be on, reporting nothing. Above the ceiling one
    unauthenticated request makes every process hold 32 MiB before a credential
    has been looked at."""
    monkeypatch.setenv("REQUEST_SCAN_MAX_BODY_BYTES", raw)
    if accepted:
        assert Settings().request_scan_max_body_bytes == int(raw)
    else:
        with pytest.raises(ValidationError, match="REQUEST_SCAN_MAX_BODY_BYTES"):
            Settings()
