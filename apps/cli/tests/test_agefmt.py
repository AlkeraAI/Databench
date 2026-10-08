"""Relative-age formatting for model-facing context results."""

from __future__ import annotations

import pytest
from alkera_cli.agefmt import format_age_ago

_T0 = 1_000_000.0  # an arbitrary "then" anchor


@pytest.mark.parametrize(
    ("now", "then", "expected"),
    [
        # Unstamped / legacy item → empty so the caller can omit it.
        pytest.param(_T0, 0.0, "", id="then-zero"),
        pytest.param(_T0, -5.0, "", id="then-negative"),
        # Sub-minute (and clock skew where now < then) → "just now".
        pytest.param(_T0, _T0, "just now", id="delta-zero"),
        pytest.param(_T0 + 30, _T0, "just now", id="30s"),
        pytest.param(_T0 + 59, _T0, "just now", id="59s-boundary"),
        pytest.param(_T0 - 1000, _T0, "just now", id="negative-delta-clock-skew"),
        # Minutes.
        pytest.param(_T0 + 60, _T0, "1m ago", id="60s-exact"),
        pytest.param(_T0 + 600, _T0, "10m ago", id="10m-example"),
        pytest.param(_T0 + 3599, _T0, "59m ago", id="just-under-1h"),
        # Hours + minutes.
        pytest.param(_T0 + 3600, _T0, "1h0m ago", id="1h-exact"),
        pytest.param(_T0 + 6180, _T0, "1h43m ago", id="1h43m-example"),
        pytest.param(_T0 + 86399, _T0, "23h59m ago", id="just-under-1d"),
        # Days + hours + minutes.
        pytest.param(_T0 + 86400, _T0, "1d0h0m ago", id="1d-exact"),
        pytest.param(_T0 + 99720, _T0, "1d3h42m ago", id="1d3h42m-example"),
        pytest.param(_T0 + 8 * 86400 + 13, _T0, "8d0h0m ago", id="8d"),
    ],
)
def test_format_age_ago(now: float, then: float, expected: str) -> None:
    assert format_age_ago(now, then) == expected


def test_format_age_ago_floors_to_the_minute() -> None:
    # 1m59s still reads "1m ago" (floor, not round) — no surprise jump to 2m.
    assert format_age_ago(_T0 + 119, _T0) == "1m ago"
