"""Alkera charts from Python: ``alkera.chart``.

::

    import alkera

    alkera.chart(df).line(x="day", y="units", color="region").tooltip()
    alkera.chart(df).histogram("latency_ms", bins=40)
    alkera.chart(df).point(x="price", y="units").zoom()
    alkera.chart(df).bar(x="region", y="sum(units)").title("Units by region")
    alkera.chart(altair_chart)            # an Altair chart, as Altair wrote it
    alkera.chart({"mark": "bar", ...})    # a Vega-Lite dict
    alkera.chart("sales.csv").bar(x="region", y="sum(units)")   # CSV, read into rows

Every chart is a Vega-Lite v6 spec admitted by the Alkera chart profile: a
notebook shows it, an agent returns it, the platform renders and exports it,
and none of them ever fetches or evaluates anything to do so. Data comes from
a pandas, Polars or pyarrow table, a list of row dicts, a dict of columns, or
CSV (text, a path or an open file, read here into rows: a chart never carries
CSV); rows travel inside the spec, sampled past ``max_rows`` (a chart that
aggregates is refused instead of sampled).

This package depends on the Python standard library only and runs on Python
3.8 and later, so it loads into any environment without touching its packages.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Mapping
from typing import Any

from alkera.chart import _builder
from alkera.chart._builder import DEFAULT_MAX_ROWS, Bin, Chart, ChartError, Value, bin, value
from alkera.chart._profile import CHART_MIME, INLINE, PROFILE, PROFILE_VERSION, ChartSpecError
from alkera.chart._profile import validate as _validate

#: Keys that make a mapping a Vega-Lite spec rather than a dict of columns.
_SPEC_KEYS = ("$schema", "mark", "layer", "hconcat", "vconcat", "concat", "facet")


def _is_spec(data: Mapping[str, Any]) -> bool:
    return any(key in data for key in _SPEC_KEYS) and not all(
        isinstance(v, (list, tuple)) for v in data.values()
    )


def from_spec(spec: Mapping[str, Any]) -> Chart:
    """A chart from a Vega-Lite dict, kept as written (validated on output)."""
    return Chart(_spec=dict(spec))


def validate(spec: Mapping[str, Any]) -> dict[str, Any]:
    """``spec`` as the profile admits it, or :class:`ChartError`."""
    try:
        return _validate(spec, policy=INLINE).persisted()
    except ChartSpecError as exc:
        raise ChartError(str(exc), exc.path) from None


def profile() -> dict[str, Any]:
    """What the profile admits: marks, channels, transforms, limits."""
    return PROFILE.summary()


class _ChartEntry:
    """``alkera.chart``: call it with data (or a spec) to start a chart; its
    attributes are the rest of the API."""

    Chart = Chart
    ChartError = ChartError
    Value = Value
    Bin = Bin
    MIME = CHART_MIME
    PROFILE_VERSION = PROFILE_VERSION
    DEFAULT_MAX_ROWS = DEFAULT_MAX_ROWS

    def __call__(self, data: object = None) -> _builder.Chart:
        if type(data).__module__.split(".")[0] == "altair" and hasattr(data, "to_dict"):
            return from_spec(data.to_dict())
        if isinstance(data, Mapping) and _is_spec(data):
            return from_spec(data)
        return Chart(data)

    @staticmethod
    def value(constant: object) -> _builder.Value:
        """A constant for a channel: ``color=alkera.chart.value("#1c7064")``."""
        return value(constant)

    @staticmethod
    def bin(field: str, **params: Any) -> _builder.Bin:
        """A binned field: ``x=alkera.chart.bin("price", maxbins=30)``."""
        return bin(field, **params)

    @staticmethod
    def from_spec(spec: Mapping[str, Any]) -> _builder.Chart:
        return from_spec(spec)

    @staticmethod
    def validate(spec: Mapping[str, Any]) -> dict[str, Any]:
        return validate(spec)

    @staticmethod
    def profile() -> dict[str, Any]:
        return profile()

    def __repr__(self) -> str:
        return "<alkera.chart: call with a table, a Vega-Lite dict or an Altair chart>"


chart = _ChartEntry()

#: ``alkera.chart.MIME``, the same name the callable entry carries.
MIME = CHART_MIME


class _CallableChartModule(types.ModuleType):
    """``alkera`` binds ``alkera.chart`` lazily, and Python binds the
    submodule over that name whenever ``alkera.chart.*`` is imported first;
    making the module callable keeps ``alkera.chart(df)`` working in both
    orders."""

    def __call__(self, data: object = None) -> Chart:
        return chart(data)


sys.modules[__name__].__class__ = _CallableChartModule

__all__ = [
    "CHART_MIME",
    "DEFAULT_MAX_ROWS",
    "MIME",
    "Bin",
    "Chart",
    "ChartError",
    "Value",
    "bin",
    "chart",
    "from_spec",
    "profile",
    "validate",
    "value",
]
