"""A saved result's chart: the Alkera chart profile under the bound data policy.

The persisted definition is a declarative spec (marks, encodings, transforms)
that carries **no URL loading, and no expression beyond the profile's bounded
row expressions**; anything outside the profile is refused at write time rather
than rendered. The profile itself,
its registries and its guarantees live in :mod:`alkera_core.charts.profile`;
this module is the objects-shaped entry point onto it.

A result's chart never carries rows. It names the result's column KEYS and the
reader binds the result's rows to it, so ``data`` and ``datasets`` are not part
of its vocabulary (the :data:`~alkera_core.charts.profile.BOUND` policy) and a
field neither a column nor a transform provides is reported by
:func:`unbound_chart_fields`.

Reads never re-validate: an allowlist is strict where the data enters and
permissive where it leaves, so a spec written by a future validator still
renders.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from alkera_core.charts.profile import (
    BOUND,
    VEGA_LITE_V5_SCHEMA,
    VEGA_LITE_V6_SCHEMA,
    ChartSpecError,
    ValidatedChart,
    validate,
)


def validate_chart_spec(spec: object) -> ValidatedChart:
    """``spec`` admitted as a saved result's chart, or :class:`ChartSpecError`."""
    return validate(spec, policy=BOUND)


def unbound_chart_fields(spec: Mapping[str, Any], keys: Collection[str]) -> list[str]:
    """The paths (``encoding.x.field``) of fields ``spec`` reads that are not
    one of ``keys`` (the column KEYS of the result the chart is stored on) and
    that no transform in the spec creates.

    A chart names keys, never labels, so a renamed column cannot detach it;
    a field outside the keys is a chart that would render nothing, and the
    writer refuses it naming the path (never the value, which is the
    customer's column name). A spec the profile does not admit binds nothing
    and is reported as bound: the profile walk, not this check, decides
    whether such a spec is admitted at all.
    """
    try:
        chart = validate(spec, policy=BOUND)
    except ChartSpecError:
        return []
    return chart.unbound_fields(keys)


__all__ = [
    "VEGA_LITE_V5_SCHEMA",
    "VEGA_LITE_V6_SCHEMA",
    "ChartSpecError",
    "ValidatedChart",
    "unbound_chart_fields",
    "validate_chart_spec",
]
