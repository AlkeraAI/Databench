"""The chart builder: a small grammar over the Alkera chart profile.

``alkera.chart(df).line(x="day", y="units", color="region")`` builds a
Vega-Lite spec that the profile admits. Each call returns a new chart (the
builder never mutates one you already hold), and the spec is validated when it
is produced, so a mistake surfaces in the cell that made it, worded the way
the server would word it, plus the context only this process can give (your
column names).
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from alkera.chart import _profile
from alkera.chart._data import TYPE_LETTERS, Table, systematic_sample, to_table

#: Rows a chart carries before it is sampled. Raise it per chart with
#: ``max_rows=`` (up to the profile's ceiling); aggregate first for more.
DEFAULT_MAX_ROWS = 5_000

#: The channels the builder accepts as keyword arguments, in Python spelling.
CHANNEL_ALIASES = {
    "x_offset": "xOffset",
    "y_offset": "yOffset",
    "x_error": "xError",
    "y_error": "yError",
    "x_error2": "xError2",
    "y_error2": "yError2",
    "stroke_dash": "strokeDash",
    "stroke_width": "strokeWidth",
    "fill_opacity": "fillOpacity",
    "stroke_opacity": "strokeOpacity",
}

#: Encoding-level statistics. Sampling the rows under one of these changes
#: the answer the chart reports, so such a chart is never sampled.
_ROW_STATISTICS = ("aggregate", "bin")
_STATISTIC_TRANSFORMS = (
    "aggregate",
    "joinaggregate",
    "window",
    "density",
    "quantile",
    "regression",
    "loess",
    "bin",
    "pivot",
)
_COMPOSITE_MARKS = ("boxplot", "errorbar", "errorband")

_SHORTHAND = re.compile(
    r"^(?:(?P<fn>[A-Za-z0-9_]+)\((?P<inner>[^()]*)\))?(?P<field>[^:()]*)(?::(?P<type>[QTONqton]))?$"
)


class ChartError(_profile.ChartSpecError):
    """A chart the profile does not admit, explained for its author."""


class Value:
    """A constant for a channel: ``color=alkera.chart.value("#1c7064")``."""

    __slots__ = ("value",)

    def __init__(self, value: object) -> None:
        self.value = value


class Bin:
    """A binned field: ``x=alkera.chart.bin("price", maxbins=30)``."""

    __slots__ = ("field", "params")

    def __init__(self, field: str, **params: Any) -> None:
        self.field = field
        self.params: dict[str, Any] = params or {}


def value(constant: object) -> Value:
    return Value(constant)


def bin(field: str, **params: Any) -> Bin:
    return Bin(field, **params)


def _snake_to_camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def _channel_name(name: str) -> str:
    return CHANNEL_ALIASES.get(name, _snake_to_camel(name))


def _rows_key(rows: list[dict[str, Any]]) -> str:
    """A dataset name from the rows' content, so a table shared by several
    views travels once."""
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()
    return f"data-{digest[:16]}"


class Chart:
    """One chart, or a composition of charts (``+`` layers, ``|`` side by
    side, ``&`` stacked). Build it with the mark methods; read it with
    :meth:`to_dict`."""

    def __init__(
        self,
        data: object = None,
        *,
        _spec: dict[str, Any] | None = None,
        _table: Table | None = None,
    ) -> None:
        self._table: Table | None = (
            _table if _table is not None else (to_table(data) if data is not None else None)
        )
        self._spec: dict[str, Any] = _spec if _spec is not None else {}
        self._children: list[Chart] = []
        self._max_rows = DEFAULT_MAX_ROWS

    # -- construction -----------------------------------------------------

    def _copy(self, **spec_updates: Any) -> Chart:
        twin = Chart(_spec=copy.deepcopy(self._spec), _table=self._table)
        twin._children = list(self._children)
        twin._max_rows = self._max_rows
        twin._spec.update(spec_updates)
        return twin

    @property
    def columns(self) -> list[str]:
        """The columns of this chart's data, in order."""
        return list(self._table.columns) if self._table is not None else []

    def _channel(self, name: str, spec: object) -> Any:
        """One channel definition from a shorthand, a :class:`Value`, a
        :class:`Bin`, a list (tooltip, detail) or a raw Vega-Lite dict."""
        if isinstance(spec, Value):
            return {"value": spec.value}
        if isinstance(spec, Bin):
            out = self._field_def(spec.field)
            out["bin"] = spec.params or True
            out["type"] = "quantitative"
            return out
        if isinstance(spec, Mapping):
            return dict(spec)
        if isinstance(spec, str):
            return self._field_def(spec)
        if isinstance(spec, Sequence) and name in ("tooltip", "detail", "order"):
            return [self._channel(name, item) for item in spec]
        raise ChartError(
            f'encoding.{name} takes a column name ("price", "sum(price)", "price:Q"), '
            "alkera.chart.value(...), alkera.chart.bin(...) or a Vega-Lite channel dict"
        )

    def _field_def(self, shorthand: str) -> dict[str, Any]:
        if self._table is not None and shorthand in self._table.kinds:
            # A real column wins over shorthand: "count(x)" may be its name.
            return {"field": _profile.escape_field(shorthand), "type": self._kind(shorthand)}
        match = _SHORTHAND.match(shorthand.strip())
        if match is None:
            return {"field": _profile.escape_field(shorthand), "type": self._kind(shorthand)}
        fn, inner, field, letter = (
            match.group("fn"),
            match.group("inner"),
            match.group("field"),
            match.group("type"),
        )
        out: dict[str, Any] = {}
        name = field
        if fn is not None:
            if field:
                # "sum(a)b" is not shorthand; treat the whole thing as a name.
                return {"field": _profile.escape_field(shorthand), "type": self._kind(shorthand)}
            name = inner
            if fn in _profile.PROFILE.aggregates:
                out["aggregate"] = fn
            elif fn in _profile.PROFILE.time_units:
                out["timeUnit"] = fn
            else:
                aggregates = ", ".join(sorted(_profile.PROFILE.aggregates))
                raise ChartError(f"{fn}() is neither an aggregate ({aggregates}) nor a time unit")
        if name:
            out["field"] = _profile.escape_field(name)
        if letter:
            out["type"] = TYPE_LETTERS[letter.upper()]
        elif "aggregate" in out:
            out["type"] = "quantitative"
        elif "timeUnit" in out:
            out["type"] = "temporal"
        elif name:
            out["type"] = self._kind(name)
        else:
            out["type"] = "quantitative"
        return out

    def _kind(self, column: str) -> str:
        if self._table is not None and column in self._table.kinds:
            return self._table.kinds[column]
        return "nominal"

    def encode(self, **channels: Any) -> Chart:
        """Add or replace encoding channels. ``None`` removes one."""
        encoding = dict(self._spec.get("encoding", {}))
        for key, spec in channels.items():
            name = _channel_name(key)
            if spec is None:
                encoding.pop(name, None)
            else:
                encoding[name] = self._channel(name, spec)
        return self._copy(encoding=encoding)

    def mark(self, mark: str, *, style: Mapping[str, Any] | None = None, **channels: Any) -> Chart:
        """Draw with ``mark``; ``style`` holds its properties (``point``,
        ``interpolate``, ``inner_radius``, a constant ``color`` ...), the
        keyword arguments are encoding channels."""
        props = {_snake_to_camel(k): v for k, v in (style or {}).items()}
        chart = self._copy(mark={"type": mark, **props} if props else mark)
        return chart.encode(**channels) if channels else chart

    def line(
        self,
        x: Any = None,
        y: Any = None,
        color: Any = None,
        *,
        style: Mapping[str, Any] | None = None,
        **channels: Any,
    ) -> Chart:
        return self.mark("line", style=style, **_xy(x, y, color, channels))

    def area(
        self,
        x: Any = None,
        y: Any = None,
        color: Any = None,
        *,
        style: Mapping[str, Any] | None = None,
        **channels: Any,
    ) -> Chart:
        return self.mark("area", style=style, **_xy(x, y, color, channels))

    def bar(
        self,
        x: Any = None,
        y: Any = None,
        color: Any = None,
        *,
        style: Mapping[str, Any] | None = None,
        **channels: Any,
    ) -> Chart:
        return self.mark("bar", style=style, **_xy(x, y, color, channels))

    def point(
        self,
        x: Any = None,
        y: Any = None,
        color: Any = None,
        *,
        style: Mapping[str, Any] | None = None,
        **channels: Any,
    ) -> Chart:
        return self.mark("point", style=style, **_xy(x, y, color, channels))

    def circle(
        self,
        x: Any = None,
        y: Any = None,
        color: Any = None,
        *,
        style: Mapping[str, Any] | None = None,
        **channels: Any,
    ) -> Chart:
        return self.mark("circle", style=style, **_xy(x, y, color, channels))

    def square(
        self,
        x: Any = None,
        y: Any = None,
        color: Any = None,
        *,
        style: Mapping[str, Any] | None = None,
        **channels: Any,
    ) -> Chart:
        return self.mark("square", style=style, **_xy(x, y, color, channels))

    def tick(
        self,
        x: Any = None,
        y: Any = None,
        color: Any = None,
        *,
        style: Mapping[str, Any] | None = None,
        **channels: Any,
    ) -> Chart:
        return self.mark("tick", style=style, **_xy(x, y, color, channels))

    def rect(
        self,
        x: Any = None,
        y: Any = None,
        color: Any = None,
        *,
        style: Mapping[str, Any] | None = None,
        **channels: Any,
    ) -> Chart:
        return self.mark("rect", style=style, **_xy(x, y, color, channels))

    def rule(
        self,
        x: Any = None,
        y: Any = None,
        color: Any = None,
        *,
        style: Mapping[str, Any] | None = None,
        **channels: Any,
    ) -> Chart:
        return self.mark("rule", style=style, **_xy(x, y, color, channels))

    def text(
        self,
        x: Any = None,
        y: Any = None,
        text: Any = None,
        *,
        style: Mapping[str, Any] | None = None,
        **channels: Any,
    ) -> Chart:
        return self.mark("text", style=style, **_xy(x, y, None, {"text": text, **channels}))

    def boxplot(
        self,
        x: Any = None,
        y: Any = None,
        color: Any = None,
        *,
        style: Mapping[str, Any] | None = None,
        **channels: Any,
    ) -> Chart:
        return self.mark("boxplot", style=style, **_xy(x, y, color, channels))

    def errorbar(
        self,
        x: Any = None,
        y: Any = None,
        *,
        extent: str = "ci",
        style: Mapping[str, Any] | None = None,
        **channels: Any,
    ) -> Chart:
        return self.mark(
            "errorbar", style={"extent": extent, **(style or {})}, **_xy(x, y, None, channels)
        )

    def errorband(
        self,
        x: Any = None,
        y: Any = None,
        *,
        extent: str = "ci",
        style: Mapping[str, Any] | None = None,
        **channels: Any,
    ) -> Chart:
        return self.mark(
            "errorband", style={"extent": extent, **(style or {})}, **_xy(x, y, None, channels)
        )

    def arc(
        self,
        theta: Any = None,
        color: Any = None,
        *,
        style: Mapping[str, Any] | None = None,
        **channels: Any,
    ) -> Chart:
        return self.mark(
            "arc", style=style, **_drop_none({"theta": theta, "color": color, **channels})
        )

    def pie(self, theta: Any, color: Any, *, donut: bool = False) -> Chart:
        """A pie (or donut) of ``theta`` split by ``color``."""
        return self.arc(theta, color, style={"inner_radius": 50} if donut else None)

    def histogram(self, x: Any, *, bins: int = 30, color: Any = None) -> Chart:
        """Counts of ``x`` in up to ``bins`` buckets."""
        return self.bar(x=Bin(x, maxbins=bins), y="count()", color=color)

    def heatmap(self, x: Any, y: Any, color: Any) -> Chart:
        return self.rect(x=x, y=y, color=color)

    # -- annotations --------------------------------------------------------

    def title(self, text: str, subtitle: str | None = None) -> Chart:
        return self._copy(title={"text": text, "subtitle": subtitle} if subtitle else text)

    def describe(self, text: str) -> Chart:
        """The chart's description, read aloud by screen readers."""
        return self._copy(description=text)

    def size(self, width: int | str | None = None, height: int | None = None) -> Chart:
        """Pixels, or ``width="container"`` to fill the space it is shown in."""
        updates: dict[str, Any] = {}
        if width is not None:
            updates["width"] = width
        if height is not None:
            updates["height"] = height
        return self._copy(**updates)

    def max_rows(self, limit: int) -> Chart:
        """How many rows to carry before sampling (at most the profile's
        ceiling). A chart that aggregates is never sampled: past this limit
        it is refused instead, since a statistic over a sample is a different
        statistic."""
        if not 1 <= limit <= _profile.MAX_INLINE_ROWS:
            raise ChartError(f"max_rows must be between 1 and {_profile.MAX_INLINE_ROWS}")
        twin = self._copy()
        twin._max_rows = limit
        return twin

    # -- transforms ---------------------------------------------------------

    def transform(self, *transforms: Mapping[str, Any]) -> Chart:
        """Append raw Vega-Lite transforms (validated with the chart)."""
        return self._copy(
            transform=[*self._spec.get("transform", []), *(dict(t) for t in transforms)]
        )

    def filter(self, field: str, **predicate: Any) -> Chart:
        """Keep rows where ``field`` matches: ``equal``, ``lt``, ``lte``,
        ``gt``, ``gte``, ``range=[lo, hi]``, ``one_of=[...]`` or
        ``valid=True``. Several keywords combine with AND."""
        terms = [
            {"field": _profile.escape_field(field), _snake_to_camel(op): operand}
            for op, operand in predicate.items()
        ]
        if not terms:
            raise ChartError('filter needs a condition, e.g. filter("units", gt=10)')
        return self.transform({"filter": terms[0] if len(terms) == 1 else {"and": terms}})

    def regression(self, y: str, on: str, *, method: str = "linear") -> Chart:
        """Replace the rows with a least-squares fit of ``y`` on ``on``."""
        return self.transform(
            {
                "regression": _profile.escape_field(y),
                "on": _profile.escape_field(on),
                "method": method,
            }
        )

    def loess(self, y: str, on: str, *, bandwidth: float = 0.3) -> Chart:
        return self.transform(
            {
                "loess": _profile.escape_field(y),
                "on": _profile.escape_field(on),
                "bandwidth": bandwidth,
            }
        )

    def density(
        self, field: str, *, groupby: Sequence[str] = (), as_: Sequence[str] = ("value", "density")
    ) -> Chart:
        spec: dict[str, Any] = {"density": _profile.escape_field(field), "as": list(as_)}
        if groupby:
            spec["groupby"] = [_profile.escape_field(g) for g in groupby]
        return self.transform(spec)

    def fold(self, fields: Sequence[str], *, as_: Sequence[str] = ("key", "value")) -> Chart:
        return self.transform({"fold": [_profile.escape_field(f) for f in fields], "as": list(as_)})

    # -- interaction ----------------------------------------------------------

    def tooltip(self, *fields: Any) -> Chart:
        """Show values on hover: every encoded field, or the ones named."""
        if fields:
            return self.encode(tooltip=list(fields))
        mark = self._spec.get("mark")
        if mark is None:
            raise ChartError("tooltip() follows a mark, e.g. chart(df).line(...).tooltip()")
        mark = {"type": mark} if isinstance(mark, str) else dict(mark)
        mark["tooltip"] = True
        return self._copy(mark=mark)

    def _param(self, param: dict[str, Any]) -> Chart:
        return self._copy(params=[*self._spec.get("params", []), param])

    def zoom(self, axes: str = "xy") -> Chart:
        """Wheel to zoom, drag to pan, double-click to reset."""
        return self._param(
            {
                "name": "zoom",
                "select": {"type": "interval", "encodings": _axes(axes)},
                "bind": "scales",
            }
        )

    def brush(self, axes: str = "x", *, dim: str = "#9e9b94") -> Chart:
        """Drag to select a range. Marks outside it fade: drawn in ``dim``
        when color names a column, at a quarter opacity otherwise."""
        chart = self._param(
            {"name": "brush", "select": {"type": "interval", "encodings": _axes(axes)}}
        )
        encoding = dict(chart._spec.get("encoding", {}))
        color = encoding.get("color")
        if isinstance(color, Mapping) and "field" in color:
            encoding["color"] = {"condition": {"param": "brush", **color}, "value": dim}
        else:
            encoding["opacity"] = {"condition": {"param": "brush", "value": 1}, "value": 0.25}
        return chart._copy(encoding=encoding)

    def filter_selection(self, name: str = "brush") -> Chart:
        """Keep only the rows inside a selection made in another view of the
        same chart, e.g. ``overview.brush() & detail.filter_selection()``."""
        return self.transform({"filter": {"param": name}})

    def highlight_legend(self) -> Chart:
        """Click a legend entry to bring its series forward."""
        color = self._spec.get("encoding", {}).get("color")
        if not isinstance(color, Mapping) or "field" not in color:
            raise ChartError("highlight_legend() needs a color channel bound to a column")
        encoding = dict(self._spec["encoding"])
        encoding["opacity"] = {"condition": {"param": "series", "value": 1}, "value": 0.2}
        chart = self._copy(encoding=encoding)
        return chart._param(
            {
                "name": "series",
                "select": {"type": "point", "fields": [color["field"]]},
                "bind": "legend",
            }
        )

    # -- composition ----------------------------------------------------------

    def _compose(self, kind: str, other: object) -> Chart:
        if not isinstance(other, Chart):
            raise TypeError("charts compose only with other charts")
        out = Chart()
        out._spec = {kind: None}
        flat: list[Chart] = []
        for part in (self, other):
            if list(part._spec) == [kind] and part._children:
                flat.extend(part._children)
            else:
                flat.append(part)
        out._children = flat
        out._max_rows = max(self._max_rows, other._max_rows)
        return out

    def __add__(self, other: Chart) -> Chart:
        return self._compose("layer", other)

    def __or__(self, other: Chart) -> Chart:
        return self._compose("hconcat", other)

    def __and__(self, other: Chart) -> Chart:
        return self._compose("vconcat", other)

    def facet(self, row: Any = None, column: Any = None, *, columns: int | None = None) -> Chart:
        """Small multiples: one panel per value of ``row`` and/or ``column``."""
        facet = {}
        if row is not None:
            facet["row"] = self._channel("row", row)
        if column is not None:
            facet["column"] = self._channel("column", column)
        if not facet:
            raise ChartError("facet needs row= or column=")
        out = Chart(_table=self._table)
        out._max_rows = self._max_rows
        out._spec = {"facet": facet, "spec": copy.deepcopy(self._spec)}
        if columns is not None:
            out._spec["columns"] = columns
        return out

    # -- output ---------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """The Vega-Lite spec, admitted by the Alkera chart profile."""
        datasets: dict[str, list[dict[str, Any]]] = {}
        sampled: list[dict[str, Any]] = []
        spec = self._assemble(datasets, sampled, top=True)
        if datasets:
            spec["datasets"] = datasets
        if sampled:
            spec["usermeta"] = {
                "alkera": {
                    "sampled": {
                        "rows": sum(s["rows"] for s in sampled),
                        "of": sum(s["of"] for s in sampled),
                        "method": "systematic",
                    }
                }
            }
        spec = {"$schema": _profile.VEGA_LITE_V6_SCHEMA, **spec}
        try:
            return _profile.validate(spec, policy=_profile.INLINE).persisted()
        except _profile.ChartSpecError as exc:
            raise self._explain(exc, spec) from None

    def _assemble(
        self, datasets: dict[str, list[dict[str, Any]]], sampled: list[dict[str, Any]], *, top: bool
    ) -> dict[str, Any]:
        spec = copy.deepcopy(self._spec)
        kinds = [k for k in ("layer", "hconcat", "vconcat") if k in spec]
        if kinds and self._children:
            spec[kinds[0]] = [
                child._assemble(datasets, sampled, top=False) for child in self._children
            ]
        if self._table is not None:
            rows = self._rows_for(spec, sampled)
            key = _rows_key(rows)
            datasets[key] = rows
            spec["data"] = {"name": key}
        return spec

    def _rows_for(
        self, spec: Mapping[str, Any], sampled: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        assert self._table is not None
        rows = self._table.rows
        if len(rows) <= self._max_rows:
            return rows
        if _summarizes(spec):
            raise ChartError(
                f"this chart summarizes {len(rows):,} rows, more than the {self._max_rows:,} it "
                "carries, and a statistic over a sample is a different statistic. Aggregate "
                "first (e.g. df.groupby(...).agg(...)), or raise the limit with "
                f".max_rows(n) up to {_profile.MAX_INLINE_ROWS:,}"
            )
        kept = systematic_sample(rows, self._max_rows)
        sampled.append({"rows": len(kept), "of": len(rows), "method": "systematic"})
        return kept

    def _explain(self, exc: _profile.ChartSpecError, spec: Mapping[str, Any]) -> ChartError:
        """The profile's refusal, plus what only the author's own process
        may say: the field it named and the columns it could have meant."""
        message = str(exc)
        columns = self._all_columns()
        if "does not have" in message and columns:
            name = _lookup(spec, exc.path)
            if isinstance(name, str):
                root = _profile.field_root(name)
                close = difflib.get_close_matches(root, columns, n=3)
                hint = f"; did you mean {', '.join(repr(c) for c in close)}?" if close else ""
                message = (
                    f"{exc.path}: {root!r} is not a column of this chart's data "
                    f"(columns: {', '.join(columns)}){hint}"
                )
        return ChartError(message, exc.path)

    def _all_columns(self) -> list[str]:
        seen: list[str] = list(self.columns)
        for child in self._children:
            seen.extend(c for c in child._all_columns() if c not in seen)
        return seen

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def _repr_mimebundle_(self, include: Any = None, exclude: Any = None) -> dict[str, Any]:
        spec = self.to_dict()
        return {
            _profile.CHART_MIME: spec,
            "application/vnd.vegalite.v6+json": spec,
            "text/plain": repr(self),
        }

    def _mime_(self) -> tuple[str, str]:
        """marimo's display hook."""
        return _profile.CHART_MIME, json.dumps(self.to_dict())

    def __repr__(self) -> str:
        kind = next((k for k in ("layer", "hconcat", "vconcat", "facet") if k in self._spec), None)
        if kind:
            return f"<alkera chart: {kind}>"
        mark = self._spec.get("mark")
        name = mark.get("type") if isinstance(mark, Mapping) else mark
        rows = f", {len(self._table)} rows" if self._table is not None else ""
        return f"<alkera chart: {name or 'no mark yet'}{rows}>"


def _xy(x: Any, y: Any, color: Any, channels: Mapping[str, Any]) -> dict[str, Any]:
    return _drop_none({"x": x, "y": y, "color": color, **channels})


def _drop_none(values: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v is not None}


def _axes(axes: str) -> list[str]:
    picked = [a for a in axes if a in "xy"]
    if not picked or len(picked) != len(axes):
        raise ChartError('axes is "x", "y" or "xy"')
    return picked


def _summarizes(spec: Mapping[str, Any]) -> bool:
    """Whether a view computes a statistic over its rows."""
    mark = spec.get("mark")
    name = mark.get("type") if isinstance(mark, Mapping) else mark
    if name in _COMPOSITE_MARKS:
        return True
    for transform in spec.get("transform", []) or []:
        if isinstance(transform, Mapping) and any(
            key in transform for key in _STATISTIC_TRANSFORMS
        ):
            return True
    encoding = spec.get("encoding", {}) or {}
    for channel in encoding.values():
        for definition in channel if isinstance(channel, list) else [channel]:
            if isinstance(definition, Mapping) and any(definition.get(k) for k in _ROW_STATISTICS):
                return True
    child = spec.get("spec")
    return isinstance(child, Mapping) and _summarizes(child)


def _lookup(spec: Mapping[str, Any], path: str) -> object:
    node: Any = spec
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        if part.startswith("["):
            index = int(part[1:-1])
            if not isinstance(node, list) or index >= len(node):
                return None
            node = node[index]
        else:
            if not isinstance(node, Mapping) or part not in node:
                return None
            node = node[part]
    return node
