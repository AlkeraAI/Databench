"""The Alkera chart theme: design tokens mapped onto a Vega config.

One mapping, two implementations held together by a committed fixture: this
module (used by server-side export, where there is no stylesheet to read) and
``chartConfig`` in ``packages/ui/src/charts/theme.ts`` (used by the renderer,
which reads the live CSS tokens of whatever surface it sits on: the portal,
the content frame, a VS Code theme).
``packages/api-core/tests/fixtures/charts/theme/*.json`` is this module's
output; the TypeScript test feeds the same tokens through ``chartConfig`` and
must produce the same object.

The categorical palette is the validated eight-slot order (adjacent-pair CVD
separation >= 8 OKLab x100 and a normal-vision floor >= 15 in both schemes,
checked against Alkera's card surfaces), assigned in fixed order and never
cycled. Sequential is one hue light to dark; diverging is two hues around a
neutral grey.

Standard library only, so the ``alkera`` client could carry it too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

#: The faces the theme's ``font`` names, as files a renderer with no
#: stylesheet registers (Hanken Grotesk, regular and semibold, latin subset,
#: under the SIL Open Font License in ``fonts/OFL.txt``). The portal draws the
#: same faces from ``@fontsource/hanken-grotesk``.
FONT_DIRECTORY = Path(__file__).parent / "fonts"

#: The token values per scheme, mirroring ``packages/ui/src/theme/tokens.css``
#: for the surface and text roles (the UI test pins that they agree).
CHART_TOKENS: dict[str, dict[str, Any]] = {
    "light": {
        "background": "#ffffff",
        "text": "#1b1a17",
        "muted": "#5f5b53",
        "grid": "rgba(60, 52, 40, 0.14)",
        "domain": "rgba(60, 52, 40, 0.26)",
        "font": '"Hanken Grotesk", ui-sans-serif, system-ui, sans-serif',
        "category": [
            "#2a78d6",
            "#eb6834",
            "#1baf7a",
            "#eda100",
            "#e87ba4",
            "#008300",
            "#4a3aa7",
            "#e34948",
        ],
        "ramp": [
            "#cde2fb",
            "#9ec5f4",
            "#6da7ec",
            "#3987e5",
            "#256abf",
            "#184f95",
            "#0d366b",
        ],
        "diverging": [
            "#184f95",
            "#3987e5",
            "#9ec5f4",
            "#f0efec",
            "#f2aca9",
            "#e34948",
            "#a3272a",
        ],
    },
    "dark": {
        "background": "#25231e",
        "text": "#ece9e2",
        "muted": "#a39d92",
        "grid": "rgba(231, 226, 216, 0.11)",
        "domain": "rgba(231, 226, 216, 0.2)",
        "font": '"Hanken Grotesk", ui-sans-serif, system-ui, sans-serif',
        "category": [
            "#3987e5",
            "#d95926",
            "#199e70",
            "#c98500",
            "#d55181",
            "#008300",
            "#9085e9",
            "#e66767",
        ],
        "ramp": [
            "#0d366b",
            "#184f95",
            "#256abf",
            "#3987e5",
            "#6da7ec",
            "#9ec5f4",
            "#cde2fb",
        ],
        "diverging": [
            "#9ec5f4",
            "#3987e5",
            "#1c5cab",
            "#383835",
            "#a3272a",
            "#e66767",
            "#f2aca9",
        ],
    },
}

SCHEMES = ("light", "dark")


def chart_config(tokens: dict[str, Any]) -> dict[str, Any]:
    """The Vega config for one scheme's tokens.

    Marks follow the house specs: 2px lines, 8px markers, a 2px surface gap
    between fills, rounded bar ends, recessive grid and axes, text in text
    tokens (never a series color).
    """
    font = tokens["font"]
    text = tokens["text"]
    muted = tokens["muted"]
    label = {"labelColor": muted, "labelFont": font, "labelFontSize": 11}
    heading = {"titleColor": text, "titleFont": font, "titleFontSize": 11, "titleFontWeight": 600}
    return {
        "background": tokens["background"],
        "font": font,
        "padding": 8,
        "view": {"stroke": None},
        "title": {
            "color": text,
            "subtitleColor": muted,
            "font": font,
            "subtitleFont": font,
            "fontSize": 14,
            "fontWeight": 600,
            "anchor": "start",
            "offset": 8,
        },
        "axis": {
            **label,
            **heading,
            "domainColor": tokens["domain"],
            "tickColor": tokens["domain"],
            "gridColor": tokens["grid"],
            "titlePadding": 8,
            "labelPadding": 4,
        },
        "axisBand": {"grid": False},
        "legend": {**label, **heading, "symbolType": "circle", "symbolSize": 64},
        "header": {**label, **heading},
        "range": {
            "category": list(tokens["category"]),
            "ordinal": list(tokens["ramp"][1:]),
            "ramp": list(tokens["ramp"]),
            "heatmap": list(tokens["ramp"]),
            "diverging": list(tokens["diverging"]),
        },
        "mark": {"color": tokens["category"][0]},
        "line": {"strokeWidth": 2},
        "trail": {"size": 2},
        "point": {"size": 64, "filled": True},
        "circle": {"size": 64},
        "square": {"size": 64},
        "bar": {"binSpacing": 2, "cornerRadiusEnd": 2},
        "rect": {"binSpacing": 2},
        "arc": {"stroke": tokens["background"], "strokeWidth": 2},
        "area": {"opacity": 0.85},
        "rule": {"color": muted},
        "text": {"color": text, "font": font, "fontSize": 11},
        "tick": {"thickness": 2},
    }


def scheme_config(scheme: str) -> dict[str, Any]:
    """The config for ``"light"`` or ``"dark"``."""
    if scheme not in CHART_TOKENS:
        raise ValueError(f"scheme must be one of {list(SCHEMES)}")
    return chart_config(CHART_TOKENS[scheme])


__all__ = ["CHART_TOKENS", "SCHEMES", "chart_config", "scheme_config"]
