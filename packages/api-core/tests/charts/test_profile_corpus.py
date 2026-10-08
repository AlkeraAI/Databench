"""The shared chart corpus, walked by the real validator.

Every file under ``fixtures/charts/profile/`` is a case, discovered rather than
listed, so a fixture added for the renderer is a case here too. ``valid`` and
``altair`` must be admitted; ``invalid`` must be refused at its recorded path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from alkera_core.charts import BOUND, HOSTED, INLINE, ChartSpecError, validate

CORPUS = Path(__file__).resolve().parents[1] / "fixtures" / "charts" / "profile"
POLICIES = {"bound": BOUND, "inline": INLINE, "hosted": HOSTED}


def _cases(kind: str) -> list[Any]:
    return [
        pytest.param(json.loads(path.read_text()), id=path.stem)
        for path in sorted((CORPUS / kind).glob("*.json"))
    ]


def test_the_corpus_is_present_so_the_parametrized_cases_cannot_pass_vacuously() -> None:
    assert len(_cases("valid")) >= 25
    assert len(_cases("altair")) >= 15
    assert len(_cases("invalid")) >= 20


@pytest.mark.parametrize("spec", _cases("valid"))
def test_a_profile_spec_is_admitted_unchanged(spec: dict[str, Any]) -> None:
    chart = validate(spec)
    persisted = chart.persisted()
    assert persisted.pop("$schema").startswith("https://vega.github.io/schema/vega-lite/v6")
    assert persisted == spec


@pytest.mark.parametrize("spec", _cases("altair"))
def test_altair_six_output_is_admitted_as_altair_wrote_it(spec: dict[str, Any]) -> None:
    chart = validate(spec)
    assert chart.persisted() == spec
    assert chart.persisted()["$schema"].endswith("v6.4.1.json")


@pytest.mark.parametrize("case", _cases("invalid"))
def test_a_spec_outside_the_profile_is_refused_at_its_path(case: dict[str, Any]) -> None:
    with pytest.raises(ChartSpecError) as refused:
        validate(case["spec"], policy=POLICIES[case["policy"]])
    assert refused.value.path == case["path"]


@pytest.mark.parametrize("case", _cases("invalid"))
def test_a_refusal_never_echoes_a_value_from_the_spec(case: dict[str, Any]) -> None:
    with pytest.raises(ChartSpecError) as refused:
        validate(case["spec"], policy=POLICIES[case["policy"]])
    message = str(refused.value)
    for needle in ("evil.example", "datum.", "now()", "elsewhere", "aa'"):
        assert needle not in message
