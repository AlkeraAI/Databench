"""The evaluation scenarios with a real model.

Runs every scenario of ``alkera_notebook.sim.eval`` through model mode against
an Anthropic-compatible messages endpoint (the Alkera gateway, or the
provider): ``ALKERA_EVAL_BASE_URL`` and ``ALKERA_EVAL_TOKEN`` (or
``ANTHROPIC_API_KEY`` for the provider), ``ALKERA_EVAL_MODEL``. Writes a JSON
report (``ALKERA_EVAL_REPORT``, default under the test's temporary directory)
and fails only on hard checks; rubric counts are reported, never asserted.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from alkera_notebook.sim.driver import SimDriver, StanceGatekeeper
from alkera_notebook.sim.eval import SCENARIOS
from alkera_notebook.sim.model import MessagesApiClient, run_episode
from alkera_notebook.sim.reference import ReferenceWorkspace
from alkera_notebook.sim.targets import ReferenceTarget

# A real model's episode of up to 30 turns outlasts the suite's default timeout.
pytestmark = [pytest.mark.live_provider, pytest.mark.timeout(600)]


def _client() -> MessagesApiClient:
    token = os.environ.get("ALKERA_EVAL_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")
    if not token:
        pytest.skip("set ALKERA_EVAL_TOKEN (gateway) or ANTHROPIC_API_KEY to run the evaluation")
    return MessagesApiClient(
        os.environ.get("ALKERA_EVAL_BASE_URL", "https://api.anthropic.com"),
        token,
        os.environ.get("ALKERA_EVAL_MODEL", "claude-sonnet-4-5"),
    )


def _write_report(directory: Path, report: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{report['scenario']}.json").write_text(
        json.dumps(report, indent=1), encoding="utf-8"
    )


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
async def test_eval_scenario(scenario: Any, tmp_path: Path) -> None:
    client = _client()
    warehouse = {"orders": pd.DataFrame({"region": ["n", "s", "n"], "amount": [1, 2, 3]})}
    target = ReferenceTarget(ReferenceWorkspace(seed=0, warehouse=warehouse))
    driver = SimDriver(target, gatekeeper=StanceGatekeeper("default"))  # type: ignore[arg-type]
    report = await run_episode(client, scenario, driver)
    _write_report(Path(os.environ.get("ALKERA_EVAL_REPORT", tmp_path / "eval")), report.as_dict())
    assert not report.violations, report.violations
    assert all(report.hard.values()), report.as_dict()
