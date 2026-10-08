"""Server-side chart export: an Alkera chart profile spec to SVG or PNG.

Agents read a chart as an image and people download one; neither needs a
browser. ``vl-convert`` runs Vega and Vega-Lite inside its own runtime, and
this module is the only caller of it.

Two walls stand between a spec and the network:

1. The spec is validated against the chart profile (inline policy) before it
   reaches the renderer, so a ``url``, an expression or an image mark is
   refused with the profile's own path-naming message.
2. ``vl-convert`` is always called with ``allowed_base_urls=[]``. Its default
   is to fetch any URL a spec names; with an empty list it refuses every
   external data request, so even a spec that bypassed validation cannot make
   the server fetch.

The theme's typeface is registered with the renderer before the first
render, so an export, and the image an agent reads, is set in the same face
the reader saw rather than the renderer's fallback sans.

Rendering is CPU-bound and blocking, so it runs in a worker thread under a
process-wide limit: a burst of exports queues instead of starving the event
loop's thread pool.
"""

from __future__ import annotations

import asyncio
import functools
import importlib
import weakref
from types import ModuleType
from typing import Any, Literal

import anyio
from alkera_core.charts import FONT_DIRECTORY, INLINE, scheme_config, validate

ExportFormat = Literal["svg", "png"]
Scheme = Literal["light", "dark"]

#: The Vega-Lite version rendered: the one Altair 6 writes and the browser
#: renderer bundles, so an export draws what the reader saw.
VEGA_LITE_VERSION = "6.4"

MIN_SCALE = 1.0
MAX_SCALE = 4.0

#: How many exports render at once in this process.
MAX_CONCURRENT_EXPORTS = 2
#: One limiter per event loop: a limiter's waiters belong to the loop that made it.
_limiters: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, anyio.CapacityLimiter] = (
    weakref.WeakKeyDictionary()
)


class ChartExportUnavailableError(RuntimeError):
    """The renderer is not installed in this environment."""


def _limiter_for_loop() -> anyio.CapacityLimiter:
    loop = asyncio.get_running_loop()
    limiter = _limiters.get(loop)
    if limiter is None:
        limiter = _limiters[loop] = anyio.CapacityLimiter(MAX_CONCURRENT_EXPORTS)
    return limiter


@functools.cache
def _vl_convert() -> ModuleType:
    """The renderer, with the theme's typeface registered (once per process:
    the registration is the renderer's own process-wide font database)."""
    try:
        vlc = importlib.import_module("vl_convert")
    except ImportError as exc:  # pragma: no cover - the backend image always has it
        raise ChartExportUnavailableError(
            "chart export needs vl-convert-python, which this environment lacks"
        ) from exc
    vlc.register_font_directory(str(FONT_DIRECTORY))
    return vlc


def render_unvalidated(
    spec: dict[str, Any], fmt: ExportFormat, scheme: Scheme, scale: float
) -> bytes:
    """Render ``spec`` as is. Callers validate first; this is the second wall
    on its own, kept separate so it can be proven on its own."""
    vlc = _vl_convert()
    config = scheme_config(scheme)
    if fmt == "svg":
        svg: str = vlc.vegalite_to_svg(
            spec, vl_version=VEGA_LITE_VERSION, config=config, allowed_base_urls=[]
        )
        return svg.encode("utf-8")
    png: bytes = vlc.vegalite_to_png(
        spec, vl_version=VEGA_LITE_VERSION, config=config, scale=scale, allowed_base_urls=[]
    )
    return png


def render(spec: object, fmt: ExportFormat, scheme: Scheme = "light", scale: float = 2.0) -> bytes:
    """``spec`` rendered, or :class:`~alkera_core.charts.ChartSpecError` when
    the profile refuses it. ``scale`` applies to PNG only."""
    if not MIN_SCALE <= scale <= MAX_SCALE:
        raise ValueError(f"scale must be between {MIN_SCALE:g} and {MAX_SCALE:g}")
    chart = validate(spec, policy=INLINE)
    return render_unvalidated(chart.persisted(), fmt, scheme, scale)


async def render_async(
    spec: object, fmt: ExportFormat, scheme: Scheme = "light", scale: float = 2.0
) -> bytes:
    """:func:`render` in a worker thread, under the process-wide export limit."""
    return await anyio.to_thread.run_sync(
        render, spec, fmt, scheme, scale, limiter=_limiter_for_loop()
    )


__all__ = [
    "MAX_CONCURRENT_EXPORTS",
    "MAX_SCALE",
    "MIN_SCALE",
    "VEGA_LITE_VERSION",
    "ChartExportUnavailableError",
    "ExportFormat",
    "Scheme",
    "render",
    "render_async",
    "render_unvalidated",
]
