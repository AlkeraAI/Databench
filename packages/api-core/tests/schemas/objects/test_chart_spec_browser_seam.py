"""The chart the browser derives is the chart this server admits and stores.

Three copies of one shape used to live in three suites, each typed by hand:
the browser's `timeSeriesSpec` output, the server's allowlist walk, and the
object page's `readTimeSeriesSpec`. They agree today, and nothing held them
to it — a channel renamed on one side (the `color` channel came and went
exactly this way) turns into a 422 on save or a chart that silently stops
drawing, with every suite green.

So the shape is ONE committed artifact per direction, in
``packages/api-core/tests/fixtures/objects/seam/``:

* ``chart_spec_from_browser.json`` — what `timeSeriesSpec({x: "day", y:
  "orders"})` emits, pinned byte-for-byte by
  ``apps/web/src/tests/pages/workspace/chat/chartSpecSeam.test.ts`` and fed
  here to the real ``validate_chart_spec``;
* ``chart_spec_persisted.json`` — what ``.persisted()`` writes into a result's
  spec, pinned here and read back by the same browser test through the real
  ``readTimeSeriesSpec``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from alkera_core.schemas.objects import unbound_chart_fields, validate_chart_spec

# One xdist worker for this module: the module-scoped fixtures below are built
# once per worker, so splitting the module per test would rebuild them per worker.
pytestmark = pytest.mark.xdist_group("chart_spec_browser_seam")

REPO_ROOT = Path(__file__).resolve().parents[5]
SEAM = REPO_ROOT / "packages/api-core/tests/fixtures/objects/seam"
FROM_BROWSER = SEAM / "chart_spec_from_browser.json"
PERSISTED = SEAM / "chart_spec_persisted.json"


@pytest.fixture(scope="module")
def from_the_browser() -> dict[str, object]:
    payload: dict[str, object] = json.loads(FROM_BROWSER.read_text())
    return payload


def test_the_fixtures_exist_so_this_module_cannot_pass_vacuously() -> None:
    assert FROM_BROWSER.is_file(), f"missing {FROM_BROWSER}"
    assert PERSISTED.is_file(), f"missing {PERSISTED}"


def test_the_browsers_chart_is_admitted_by_the_allowlist(
    from_the_browser: dict[str, object],
) -> None:
    chart = validate_chart_spec(from_the_browser)
    assert chart.marks == {"line"}
    assert chart.persisted() == from_the_browser


def test_what_is_persisted_is_the_committed_artifact_the_browser_reads(
    from_the_browser: dict[str, object],
) -> None:
    """The reader's half of the seam is pinned against these bytes."""
    assert validate_chart_spec(from_the_browser).persisted() == json.loads(PERSISTED.read_text())


def test_the_chart_binds_to_the_columns_the_promote_names(
    from_the_browser: dict[str, object],
) -> None:
    """The promote route refuses a chart naming a field its columns lack; the
    browser builds the spec from the preview's own column names, so it binds."""
    persisted = validate_chart_spec(from_the_browser).persisted()
    assert unbound_chart_fields(persisted, ["day", "orders"]) == []
    assert unbound_chart_fields(persisted, ["day"]) == ["encoding.y.field"]
