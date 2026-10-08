"""The theme fixtures are this module's output, so the renderer's parity test
compares against what export actually uses.

Regenerate with ``uv run python packages/api-core/tests/fixtures/charts/generate.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from alkera_core.charts import CHART_TOKENS, SCHEMES, chart_config, scheme_config

THEMES = Path(__file__).resolve().parents[1] / "fixtures" / "charts" / "theme"


@pytest.mark.parametrize("scheme", SCHEMES)
def test_the_committed_theme_fixture_is_the_current_output(scheme: str) -> None:
    fixture = json.loads((THEMES / f"{scheme}.json").read_text())
    assert fixture["tokens"] == CHART_TOKENS[scheme]
    assert fixture["config"] == json.loads(json.dumps(scheme_config(scheme)))


@pytest.mark.parametrize("scheme", SCHEMES)
def test_the_palette_has_eight_distinct_slots_and_text_never_wears_one(scheme: str) -> None:
    tokens = CHART_TOKENS[scheme]
    assert len(set(tokens["category"])) == 8
    config = chart_config(tokens)
    assert config["range"]["category"] == tokens["category"]
    for ink in (
        config["axis"]["labelColor"],
        config["legend"]["labelColor"],
        config["text"]["color"],
    ):
        assert ink not in tokens["category"]


def test_the_schemes_differ_where_a_surface_differs() -> None:
    light, dark = scheme_config("light"), scheme_config("dark")
    assert light["background"] != dark["background"]
    assert light["range"]["category"] != dark["range"]["category"]
    assert light["range"]["ramp"] == list(reversed(dark["range"]["ramp"]))


def test_an_unknown_scheme_is_refused() -> None:
    with pytest.raises(ValueError, match="scheme"):
        scheme_config("sepia")
