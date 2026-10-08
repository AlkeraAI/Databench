"""Chart export through the real route and the real renderer (vl-convert).

Nothing here mocks the renderer: an SVG must carry the geometry of the mark it
was asked for and the theme's own colors, a PNG must be a PNG whose size moves
with ``scale``, and the network wall must hold even for a spec that skipped
validation.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

import pytest
from alkera_core.charts import CHART_TOKENS
from alkera_core.observability.security_headers import API_CSP
from backend.services import chart_export
from httpx import AsyncClient
from tests.conftest import OrgWithAdmin, login

CORPUS = Path(__file__).resolve().parents[3] / "packages/api-core/tests/fixtures/charts/profile"
URL = "/api/v1/charts/export"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

ROWS = [
    {"region": "north", "units": 12},
    {"region": "south", "units": 22},
    {"region": "east", "units": 17},
]
BAR = {
    "data": {"values": ROWS},
    "mark": "bar",
    "encoding": {
        "x": {"field": "region", "type": "nominal"},
        "y": {"field": "units", "type": "quantitative"},
    },
}


def _fixture(name: str) -> dict[str, Any]:
    spec: dict[str, Any] = json.loads((CORPUS / "valid" / f"{name}.json").read_text())
    return spec


def _png_size(png: bytes) -> tuple[int, int]:
    width, height = struct.unpack(">II", png[16:24])
    return width, height


async def _post(client: AsyncClient, org_admin: OrgWithAdmin, **body: Any) -> Any:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    return await client.post(URL, json=body)


@pytest.mark.parametrize(
    ("fixture", "vega_mark"),
    [
        pytest.param("line_multi_series", "mark-line", id="line"),
        pytest.param("bar_grouped", "mark-rect", id="bar"),
        pytest.param("arc_donut", "mark-arc", id="arc"),
        pytest.param("rect_heatmap", "mark-rect", id="heatmap"),
        pytest.param("errorband_stdev", "mark-area", id="errorband"),
        pytest.param("boxplot", "mark-rule", id="boxplot"),
        pytest.param("text_labels", "mark-text", id="text"),
        pytest.param("facet_columns", "mark-rect", id="facet"),
        pytest.param("regression_overlay", "mark-line", id="regression"),
    ],
)
async def test_an_svg_export_draws_the_marks_its_spec_names(
    client: AsyncClient, org_admin: OrgWithAdmin, fixture: str, vega_mark: str
) -> None:
    response = await _post(client, org_admin, spec=_fixture(fixture), format="svg")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("image/svg+xml")
    svg = response.text
    assert svg.lstrip().startswith("<svg")
    assert vega_mark in svg
    assert "<path" in svg


async def test_an_export_draws_a_spec_whose_filter_calculate_and_condition_are_expressions(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    spec = {
        **BAR,
        "transform": [
            {"calculate": "datum.units * 2", "as": "double"},
            {"filter": "(datum.double > 30)"},
        ],
        "encoding": {
            "x": {"field": "region", "type": "nominal"},
            "y": {"field": "double", "type": "quantitative"},
            "color": {
                "condition": {"test": "datum.region === 'south'", "value": "#c0392b"},
                "value": "#7f8c8d",
            },
        },
    }
    response = await _post(client, org_admin, spec=spec, format="svg")
    assert response.status_code == 200, response.text
    svg = response.text.lower()
    # 22 * 2 and 17 * 2 pass the filter, 12 * 2 does not; south wears the condition.
    assert svg.count('aria-roledescription="bar"') == 2
    assert "region: south; double: 44" in svg
    assert "region: east; double: 34" in svg
    assert "region: north" not in svg
    assert "#c0392b" in svg
    assert "#7f8c8d" in svg


@pytest.mark.parametrize("scheme", ["light", "dark"])
async def test_an_export_wears_the_schemes_own_palette_and_surface(
    client: AsyncClient, org_admin: OrgWithAdmin, scheme: str
) -> None:
    other = "dark" if scheme == "light" else "light"
    response = await _post(client, org_admin, spec=BAR, format="svg", scheme=scheme)
    assert response.status_code == 200, response.text
    svg = response.text.lower()
    assert CHART_TOKENS[scheme]["category"][0] in svg
    assert CHART_TOKENS[other]["category"][0] not in svg
    assert CHART_TOKENS[scheme]["background"] in svg


async def test_an_svg_export_is_a_scriptless_attachment(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    response = await _post(client, org_admin, spec=BAR, format="svg")
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment")
    assert response.headers["x-content-type-options"] == "nosniff"
    # The API's own policy covers it: no source of any kind, so an SVG opened
    # directly runs no script and fetches nothing, and nothing may frame it.
    csp = response.headers["content-security-policy"]
    assert csp == API_CSP
    assert "default-src 'none'" in csp
    assert "script-src" not in csp


async def test_a_png_export_is_a_png_whose_size_follows_its_scale(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    one = await _post(client, org_admin, spec=BAR, format="png", scale=1)
    two = await client.post(URL, json={"spec": BAR, "format": "png", "scale": 2})
    assert one.status_code == two.status_code == 200
    assert one.headers["content-type"] == "image/png"
    assert one.content.startswith(PNG_SIGNATURE)
    assert two.content.startswith(PNG_SIGNATURE)
    (w1, h1), (w2, h2) = _png_size(one.content), _png_size(two.content)
    assert w1 > 0 and h1 > 0
    assert (w2, h2) == (w1 * 2, h1 * 2)


@pytest.mark.parametrize(
    ("spec", "path"),
    [
        pytest.param(
            {"data": {"url": "https://evil.example/rows.json"}, "mark": "point"},
            "data",
            id="remote_data",
        ),
        pytest.param(
            {
                "data": {"values": ROWS},
                "transform": [{"calculate": "datum.units.constructor", "as": "evil_double"}],
                "mark": "point",
            },
            "transform[0].calculate",
            id="calculate_reaching_a_prototype",
        ),
        pytest.param(
            {
                "data": {"values": ROWS},
                "mark": "bar",
                "encoding": {"x": {"field": "secret_column", "type": "nominal"}},
            },
            "encoding.x.field",
            id="unbound_field",
        ),
    ],
)
async def test_a_spec_outside_the_profile_is_refused_naming_the_path_never_the_value(
    client: AsyncClient, org_admin: OrgWithAdmin, spec: dict[str, Any], path: str
) -> None:
    response = await _post(client, org_admin, spec=spec, format="svg")
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "chart_spec_refused"
    assert error["details"]["path"] == path
    for value in ("evil.example", "datum.units", "evil_double", "secret_column"):
        assert value not in response.text


@pytest.mark.parametrize("scale", [0.5, 4.5])
async def test_a_scale_outside_the_bounds_is_refused(
    client: AsyncClient, org_admin: OrgWithAdmin, scale: float
) -> None:
    response = await _post(client, org_admin, spec=BAR, format="png", scale=scale)
    assert response.status_code == 422


async def test_an_export_needs_a_credential(client: AsyncClient) -> None:
    response = await client.post(URL, json={"spec": BAR, "format": "svg"})
    assert response.status_code == 401


def test_the_renderer_refuses_to_fetch_even_a_spec_that_skipped_validation() -> None:
    """The second wall on its own: vl-convert fetches any URL by default, and
    ``allowed_base_urls=[]`` is what stops it. Remove it and this test sees a
    fetch attempt (or a rendered chart) instead of the refusal."""
    unvalidated = {
        "data": {"url": "https://example.com/rows.json"},
        "mark": "point",
        "encoding": {"x": {"field": "a", "type": "quantitative"}},
    }
    with pytest.raises(Exception, match="External data url not allowed"):
        chart_export.render_unvalidated(unvalidated, "svg", "light", 1.0)


def test_the_service_refuses_a_scale_outside_the_bounds_before_rendering() -> None:
    with pytest.raises(ValueError, match="scale"):
        chart_export.render(BAR, "png", "light", chart_export.MAX_SCALE + 1)


_PNG_IN_A_FRESH_PROCESS = """
import sys
import vl_convert as vlc
from alkera_core.charts import FONT_DIRECTORY, scheme_config
from backend.services import chart_export

spec = {
    "title": "Weekly orders by region",
    "data": {"values": [{"region": "North", "orders": 3}, {"region": "South", "orders": 7}]},
    "mark": "bar",
    "encoding": {
        "x": {"field": "region", "type": "nominal"},
        "y": {"field": "orders", "type": "quantitative"},
    },
}
how = sys.argv[1]
if how == "export":
    png = chart_export.render_unvalidated(spec, "png", "light", 1.0)
else:
    if how == "theme-face":
        vlc.register_font_directory(str(FONT_DIRECTORY))
    png = vlc.vegalite_to_png(
        spec,
        vl_version=chart_export.VEGA_LITE_VERSION,
        config=scheme_config("light"),
        scale=1.0,
        allowed_base_urls=[],
    )
sys.stdout.buffer.write(png)
"""


def _png_in_a_fresh_process(how: str) -> bytes:
    """A render in its own interpreter: the renderer's font database is
    process-wide, so a face registered once is there for every later render
    in the same process and could not be told apart from a fallback."""
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, "-c", _PNG_IN_A_FRESH_PROCESS, how],
        capture_output=True,
        check=True,
        timeout=120,
    )
    return done.stdout


def test_an_export_is_set_in_the_themes_typeface_not_the_renderers_fallback() -> None:
    """The theme names Hanken Grotesk, which the renderer does not ship: an
    export drew its labels in the fallback sans, so the downloaded image and
    the one an agent reads did not match the chart on screen."""
    exported = _png_in_a_fresh_process("export")
    assert exported == _png_in_a_fresh_process("theme-face")
    assert exported != _png_in_a_fresh_process("fallback")
