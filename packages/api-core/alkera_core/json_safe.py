"""Make non-finite floats survive a JSON boundary, recoverably.

JSON has no numeric ``NaN``/``Infinity`` — Python's ``json.dumps`` emits the
non-standard tokens ``NaN``/``Infinity`` (its default ``allow_nan=True``), which
**strict parsers reject** (JS ``JSON.parse`` on the OpenCode backend, the
vscode-jsonrpc TS client). So a single ``1.0/0.0`` in a result would corrupt the
whole payload on the wire.

We DON'T drop these to ``null`` (lossy) or to a bare string (a float silently
becoming text). Instead each non-finite float is wrapped in a **typed,
round-trippable** sentinel so a consumer that knows the convention recovers the
exact float:

    NaN        -> {"$nonfinite": "nan"}
    +Infinity  -> {"$nonfinite": "inf"}
    -Infinity  -> {"$nonfinite": "-inf"}

``json_safe`` applies the wrapper (call it where data is serialized to the wire);
``json_restore`` inverts it on the wrapper (call it when reading our own serialized
data back for programmatic use). The non-finite values round-trip exactly
(``json_restore(json_safe(x)) == x`` for dict/list/scalar data); a ``tuple`` comes
back as a ``list``, since JSON has no tuple type and ``json_safe`` serializes it as
an array — the same normalization ``json.dumps`` already performs.

The LLM is also a consumer — it sees ``{"$nonfinite": "nan"}`` in a result cell —
so tools that emit numeric data document the convention in their description.
"""

from __future__ import annotations

import math
from typing import Any, Final

#: The type-tag key. ``$``-prefixed by JSON-type-tag convention; collision with a
#: real data key (a dict that is EXACTLY this one key mapping to a known token) is
#: not realistic for tool output.
NONFINITE_KEY: Final = "$nonfinite"

#: token -> the float it denotes. ``float("nan"/"inf"/"-inf")`` all parse.
_TOKEN_TO_FLOAT: Final[dict[str, float]] = {
    "nan": float("nan"),
    "inf": float("inf"),
    "-inf": float("-inf"),
}


def _wrap(value: float) -> dict[str, str]:
    if math.isnan(value):
        tag = "nan"
    else:
        tag = "inf" if value > 0 else "-inf"
    return {NONFINITE_KEY: tag}


def json_safe(obj: Any) -> Any:
    """Return ``obj`` with every non-finite ``float`` replaced by its ``$nonfinite``
    wrapper, so the result serializes to valid, strict-parseable JSON without
    losing the value. Recurses dicts, lists, and tuples (a tuple a caller embeds in
    event data serializes to a JSON array regardless, so its non-finite floats must
    be wrapped too); leaves everything else (finite floats, ints, ``bool``, ``str``,
    ``None``) untouched. Idempotent."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else _wrap(obj)
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    return obj


def _is_wrapper(obj: dict[Any, Any]) -> bool:
    return len(obj) == 1 and obj.get(NONFINITE_KEY) in _TOKEN_TO_FLOAT


def json_restore(obj: Any) -> Any:
    """The exact inverse of :func:`json_safe`: rehydrate every ``$nonfinite``
    wrapper back to its float. A dict that is EXACTLY ``{NONFINITE_KEY: <known
    token>}`` becomes the float; everything else recurses unchanged. Call this
    when reading our own serialized data back for programmatic use so non-finite
    values arrive as floats, not wrapper dicts."""
    if isinstance(obj, dict):
        if _is_wrapper(obj):
            return _TOKEN_TO_FLOAT[obj[NONFINITE_KEY]]
        return {k: json_restore(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [json_restore(v) for v in obj]
    return obj


__all__ = ["NONFINITE_KEY", "json_restore", "json_safe"]
