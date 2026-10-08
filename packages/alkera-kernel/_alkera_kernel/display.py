"""Turning values into MIME bundles.

Order for one value: libraries whose own bundle would reach for the network
(plotly and altair embed CDN script tags in their HTML) and marimo UI
elements are formatted by the registry first; then ``_repr_mimebundle_``,
then ``_mime_`` (marimo objects), then the rest of the built-in registry,
then the ``_repr_*_`` methods, then ``repr``. Every bundle carries
``text/plain``.

The registry never imports a library: it recognises a value only when the
library is already in ``sys.modules`` (the value could not exist otherwise).
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import reprlib
import sys
from collections.abc import Callable
from typing import Any

from . import tables

Bundle = dict[str, Any]

TABLE_MIME = "application/vnd.alkera.table+json"
#: What the handle of a shown frame no global names starts with (a table's
#: ``source.name``). Not an identifier's first character, so a handle is
#: never mistaken for a global.
SHOWN_PREFIX = "@"
PLACEHOLDER_MIME = "application/vnd.alkera.placeholder+json"
ERROR_MIME = "application/vnd.alkera.error+json"
WIDGET_MIME = "application/vnd.jupyter.widget-view+json"
PLOTLY_MIME = "application/vnd.plotly.v1+json"
TABLE_ROWS = 50
TEXT_PLAIN_LIMIT = 100_000

_REPR_METHODS = (
    ("_repr_html_", "text/html"),
    ("_repr_markdown_", "text/markdown"),
    ("_repr_svg_", "image/svg+xml"),
    ("_repr_png_", "image/png"),
    ("_repr_jpeg_", "image/jpeg"),
    ("_repr_json_", "application/json"),
    ("_repr_latex_", "text/latex"),
)
_BINARY = {"image/png", "image/jpeg", "image/gif", "image/webp"}


class BoundedRepr(reprlib.Repr):
    """``reprlib`` that never builds the full repr of a large ``bytes``,
    ``bytearray`` or ``memoryview`` (plain ``reprlib`` falls back to the
    builtin repr for them, materializing four times the object's size)."""

    def repr_bytes(self, x: bytes, level: int) -> str:
        return _sliced(x, self.maxstring)

    def repr_bytearray(self, x: bytearray, level: int) -> str:
        return _sliced(x, self.maxstring)

    def repr_memoryview(self, x: memoryview, level: int) -> str:
        return f"<memoryview of {x.nbytes:,} bytes>"


def _sliced(x: bytes | bytearray | str, limit: int) -> str:
    if len(x) <= limit:
        return repr(x)
    unit = "characters" if isinstance(x, str) else "bytes"
    return f"{x[:limit]!r}… ({len(x):,} {unit})"


def cheap_repr(obj: object, limit: int) -> str:
    """``repr(obj)``, except that a large string or bytes-like value is
    represented from a slice instead of in full."""
    if isinstance(obj, (str, bytes, bytearray)) and len(obj) > limit:
        return _sliced(obj, limit)
    if isinstance(obj, memoryview) and obj.nbytes > limit:
        return f"<memoryview of {obj.nbytes:,} bytes>"
    return repr(obj)


def plain(obj: object) -> str:
    try:
        text = cheap_repr(obj, TEXT_PLAIN_LIMIT - 200)
    except Exception as exc:  # a broken __repr__ must not break display
        text = f"<{type(obj).__name__} object; repr failed: {type(exc).__name__}>"
    if len(text) > TEXT_PLAIN_LIMIT:
        text = text[:TEXT_PLAIN_LIMIT] + "…"
    return text


def _module_class(module: str, name: str) -> type | None:
    mod = sys.modules.get(module)
    cls = getattr(mod, name, None) if mod is not None else None
    return cls if isinstance(cls, type) else None


def _is_instance(obj: object, module: str, name: str) -> bool:
    cls = _module_class(module, name)
    return cls is not None and isinstance(obj, cls)


def _encode_binary(mime: str, data: Any) -> Any:
    if mime in _BINARY and isinstance(data, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(data)).decode("ascii")
    return data


def _normalize(bundle: Any) -> Bundle | None:
    if isinstance(bundle, tuple) and len(bundle) == 2:
        bundle = bundle[0]
    if not isinstance(bundle, dict) or not bundle:
        return None
    return {str(k): _encode_binary(str(k), v) for k, v in bundle.items() if v is not None}


# --------------------------------------------------------------------------- registry


def _marimo_ui(obj: object) -> Bundle | None:
    if "marimo" not in sys.modules:
        return None
    for cls in type(obj).__mro__:
        if cls.__name__ == "UIElement" and cls.__module__.startswith("marimo"):
            kind = type(obj).__name__
            message = f"mo.ui.{kind} is not available in Alkera; use alkera.ui.{kind}"
            return {
                ERROR_MIME: {"ename": "UnsupportedWidget", "evalue": message},
                "text/plain": message,
            }
    return None


def _plotly(obj: object) -> Bundle | None:
    if not _is_instance(obj, "plotly.basedatatypes", "BaseFigure"):
        return None
    data = obj.to_plotly_json()  # type: ignore[attr-defined]
    return {PLOTLY_MIME: json.loads(json.dumps(data, default=_json_default))}


def _altair(obj: object) -> Bundle | None:
    alt = sys.modules.get("altair")
    if alt is None:
        return None
    top = getattr(alt, "TopLevelMixin", None)
    if not isinstance(top, type) or not isinstance(obj, top):
        return None
    spec = obj.to_dict()  # type: ignore[attr-defined]
    schema = str(spec.get("$schema", ""))
    major = "6" if "/v6" in schema else "5"
    return {f"application/vnd.vegalite.v{major}+json": spec}


def _figure_of(obj: object) -> Any:
    if _is_instance(obj, "matplotlib.figure", "Figure"):
        return obj
    if _is_instance(obj, "matplotlib.axes", "Axes"):
        return obj.figure  # type: ignore[attr-defined]
    return None


def figure_bundle(fig: Any, *, svg: bool = False, close: bool = True) -> Bundle:
    """A matplotlib figure as PNG at twice its DPI (or SVG), then closed."""
    buf = io.BytesIO()
    width, height = fig.get_size_inches() * fig.dpi
    try:
        if svg:
            fig.savefig(buf, format="svg", bbox_inches="tight")
            bundle: Bundle = {"image/svg+xml": buf.getvalue().decode("utf-8")}
        else:
            fig.savefig(buf, format="png", dpi=fig.dpi * 2, bbox_inches="tight")
            bundle = {"image/png": base64.b64encode(buf.getvalue()).decode("ascii")}
    finally:
        if close:
            plt = sys.modules.get("matplotlib.pyplot")
            if plt is not None:
                plt.close(fig)
    bundle["text/plain"] = f"<Figure size {int(width)}x{int(height)}>"
    return bundle


def _matplotlib(obj: object) -> Bundle | None:
    fig = _figure_of(obj)
    return figure_bundle(fig) if fig is not None else None


def _json_default(value: Any) -> Any:
    for attr in ("isoformat", "tolist", "item"):
        fn = getattr(value, attr, None)
        if callable(fn):
            with contextlib.suppress(Exception):
                return fn()
    return str(value)


def _pandas(obj: object) -> Bundle | None:
    pd = sys.modules.get("pandas")
    if pd is None or not isinstance(obj, (pd.DataFrame, pd.Series)):
        return None
    page = tables.frame_page(obj, 0, TABLE_ROWS)
    if page is None:
        return None
    frame = obj.to_frame() if isinstance(obj, pd.Series) else obj
    return {
        TABLE_MIME: page,
        "text/html": frame.head(TABLE_ROWS).to_html(max_rows=TABLE_ROWS),
    }


def _polars(obj: object) -> Bundle | None:
    pl = sys.modules.get("polars")
    if pl is None or not isinstance(obj, (pl.DataFrame, pl.Series)):
        return None
    page = tables.frame_page(obj, 0, TABLE_ROWS)
    if page is None:
        return None
    frame = obj.to_frame() if isinstance(obj, pl.Series) else obj
    return {TABLE_MIME: page, "text/html": frame.head(TABLE_ROWS)._repr_html_()}


def _pil(obj: object) -> Bundle | None:
    if not _is_instance(obj, "PIL.Image", "Image"):
        return None
    buf = io.BytesIO()
    obj.save(buf, format="PNG")  # type: ignore[attr-defined]
    return {"image/png": base64.b64encode(buf.getvalue()).decode("ascii")}


Formatter = Callable[[object], "Bundle | None"]
#: Run before the value's own protocol methods.
FIRST: list[Formatter] = [_marimo_ui, _plotly, _altair]
#: Run after ``_repr_mimebundle_`` and ``_mime_``, before ``_repr_*_``.
REGISTRY: list[Formatter] = [_matplotlib, _pandas, _polars, _pil]


def register(formatter: Formatter, *, first: bool = False) -> None:
    (FIRST if first else REGISTRY).append(formatter)


def _from_mime(obj: object) -> Bundle | None:
    mime = getattr(obj, "_mime_", None)
    if not callable(mime):
        return None
    result = mime()
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], str):
        kind, data = result
        if kind == "text/markdown" and type(obj).__module__.startswith("marimo"):
            # marimo's Html objects answer text/markdown with rendered HTML.
            kind = "text/html"
        return {kind: _encode_binary(kind, data)}
    return None


def _from_repr_methods(obj: object) -> Bundle | None:
    bundle: Bundle = {}
    for method, mime in _REPR_METHODS:
        fn = getattr(obj, method, None)
        if not callable(fn) or isinstance(obj, type):
            continue
        try:
            value = fn()
        except NotImplementedError:
            continue
        if value is None:
            continue
        if isinstance(value, tuple) and len(value) == 2:
            value = value[0]
        bundle[mime] = _encode_binary(mime, value)
    return bundle or None


def pageable(obj: object) -> bool:
    """A frame ``inspect.frame`` can page by name (series are shown as a
    table but are not registered as one)."""
    return _is_instance(obj, "pandas", "DataFrame") or _is_instance(obj, "polars", "DataFrame")


def format_value(obj: object, *, name: str | None = None) -> Bundle:
    """The MIME bundle for ``obj`` (always with ``text/plain``). ``name`` is
    the notebook global ``obj`` is bound to, when the caller knows one: a
    table output then carries ``source.name`` so the service can page the
    whole frame through ``inspect.frame``."""
    bundle: Bundle | None = None
    for fmt in FIRST:
        bundle = fmt(obj)
        if bundle:
            break
    if not bundle and not isinstance(obj, type):
        method = getattr(obj, "_repr_mimebundle_", None)
        if callable(method):
            bundle = _normalize(method(include=None, exclude=None))
    if not bundle and not isinstance(obj, type):
        bundle = _from_mime(obj)
    if not bundle:
        for fmt in REGISTRY:
            bundle = fmt(obj)
            if bundle:
                break
    if not bundle:
        bundle = _from_repr_methods(obj)
    bundle = dict(bundle or {})
    table = bundle.get(TABLE_MIME)
    if name is not None and isinstance(table, dict) and pageable(obj):
        bundle[TABLE_MIME] = {**table, "source": {"name": name}}
    if "text/plain" not in bundle:
        bundle["text/plain"] = plain(obj)
    return bundle


# --------------------------------------------------------------------------- caps


def bundle_size(bundle: Bundle) -> int:
    return sum(
        len(v) if isinstance(v, str) else len(json.dumps(v, default=str)) for v in bundle.values()
    )


def placeholder(bundle: Bundle, limit: int) -> Bundle:
    """What replaces a bundle over the cell's cap: never a truncated
    structure, only a note of what was there."""
    mimes = sorted(k for k in bundle if k not in ("text/plain", "metadata"))
    size = bundle_size(bundle)
    return {
        PLACEHOLDER_MIME: {
            "reason": "too_large",
            "mimetypes": mimes,
            "bytes": size,
            "limit": limit,
        },
        "text/plain": f"[output of {size:,} bytes is over this cell's limit of {limit:,} bytes]",
    }
