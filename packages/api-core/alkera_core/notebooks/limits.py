"""How large a notebook's events may be at each hop, in one place.

A kernel's events go from the box (one ``POST .../events`` per batch) into
one outbox row per batch on the backend, and from there to each reader as
one realtime frame. Each hop has a ceiling, and every one is derived here
from the outbox's own payload cap (``alkera_core.events.outbox``), so no
hop can take what the next one refuses:

* :data:`EVENT_MAX_BYTES`: the most one event may weigh as JSON. Below the
  outbox cap by :data:`BATCH_ENVELOPE_BYTES`, so an event that fits always
  fits a row on its own.
* :data:`POST_MAX_BYTES`: the most one box post carries past its first
  event (a post always carries at least one event, and that one is at most
  :data:`EVENT_MAX_BYTES`). The edge's body exemption for ``/events`` is
  sized from :data:`POST_BODY_MAX_BYTES`.
* :data:`INLINE_VALUE_MAX_BYTES`: one output value larger than this does not
  travel inline in an event at all.

An event that would weigh more than :data:`EVENT_MAX_BYTES` is fitted by
:func:`fit_event`: each output value too large to carry is replaced by a
visible "output too large" marker (:data:`TOO_LARGE_MIME`, with a plain-text
line saying so). Nothing on the way is ever refused for size.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Final

from alkera_core.events.outbox import MAX_PAYLOAD_BYTES

#: Room a batch's own envelope takes in its outbox row (the kernel id, the
#: state, the list around the events).
BATCH_ENVELOPE_BYTES: Final = 64 * 1024
#: The most one event may weigh as JSON.
EVENT_MAX_BYTES: Final = MAX_PAYLOAD_BYTES - BATCH_ENVELOPE_BYTES
#: The most a batch's events may weigh together, for one outbox row.
BATCH_MAX_BYTES: Final = EVENT_MAX_BYTES
#: The most one box post carries past its first event.
POST_MAX_BYTES: Final = 512 * 1024
#: The most output one cell's run may hold in the kernel (its rich bundles,
#: as the kernel and the engine bound them). The one event a box running a
#: build from before events were fitted can send at its largest.
KERNEL_OUTPUT_MAX_BYTES: Final = 8 * 1024 * 1024
#: The largest body ``POST .../events`` may be: one event as large as a
#: kernel's output (a box that does not fit its events yet sends it whole,
#: and the backend fits it), the rest of a post, and the request's envelope.
POST_BODY_MAX_BYTES: Final = KERNEL_OUTPUT_MAX_BYTES + POST_MAX_BYTES + BATCH_ENVELOPE_BYTES
#: One output value larger than this is not carried inline.
INLINE_VALUE_MAX_BYTES: Final = 256 * 1024
#: The largest batch of document operations (``POST .../ops``) and the
#: largest widget message (``POST .../comm``, its buffers base64): each is
#: bounded here, and the route never sees more.
OPS_BODY_MAX_BYTES: Final = 8 * 1024 * 1024
COMM_BODY_MAX_BYTES: Final = 8 * 1024 * 1024
#: Every other notebook request that can pass the edge's 8 KB body rule (a
#: run naming many cells, a frame naming many models, an install naming many
#: packages) is a description of work, never content.
REQUEST_BODY_MAX_BYTES: Final = 256 * 1024

#: The MIME type an output too large to carry is replaced by.
TOO_LARGE_MIME: Final = "application/vnd.alkera.too-large+json"


def json_bytes(value: Any) -> int:
    """What ``value`` weighs as compact JSON."""
    return len(json.dumps(value, separators=(",", ":"), default=str).encode("utf-8"))


def plain_json(value: Any) -> Any:
    """``value`` as strict JSON can carry it (``allow_nan=False``, the way
    the box's HTTP client and every reader encode it): a date or time as its
    ISO text, a number JSON has no spelling for (``NaN``, an infinity) and
    any other value (a ``Decimal``, a ``UUID``, bytes) as its text, as a
    table output's first page shows them. Plain JSON comes back unchanged."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Mapping):
        return {str(k): plain_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain_json(v) for v in value]
    if isinstance(value, (_dt.date, _dt.time)):
        return value.isoformat()
    return str(value)


def _plain_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """``event`` as strict JSON: checked whole first, walked only when a value
    in it is not JSON (an answer carrying a table page's dates)."""
    whole = dict(event)
    try:
        json.dumps(whole, allow_nan=False)
    except (TypeError, ValueError):
        plain: dict[str, Any] = plain_json(whole)
        return plain
    return whole


def too_large(mime: str, size: int) -> dict[str, Any]:
    """The marker an output value of ``size`` bytes is replaced by."""
    return {"mime": mime, "bytes": size}


def _megabytes(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


def fit_bundle(bundle: Mapping[str, Any], *, limit: int = INLINE_VALUE_MAX_BYTES) -> dict[str, Any]:
    """``bundle`` with every value over ``limit`` replaced by the marker, and
    a plain-text line that says what was left out."""
    kept: dict[str, Any] = {}
    dropped: list[tuple[str, int]] = []
    for mime, value in bundle.items():
        size = json_bytes(value)
        if mime != "text/plain" and size > limit:
            dropped.append((mime, size))
            continue
        kept[mime] = value
    if not dropped:
        return dict(bundle)
    largest = max(size for _, size in dropped)
    kept[TOO_LARGE_MIME] = {"outputs": [too_large(m, s) for m, s in dropped]}
    plain = kept.get("text/plain")
    if not isinstance(plain, str) or json_bytes(plain) > limit:
        kept["text/plain"] = f"Output too large to show here ({_megabytes(largest)})."
    return kept


def map_bundles(value: Any, change: Callable[[Mapping[str, Any]], dict[str, Any]]) -> Any:
    """``value`` with ``change`` applied to every MIME bundle in it, wherever
    an event carries one: its ``output``, each of its ``outputs``, and the
    same inside a snapshot's view. The one place that knows where outputs sit
    in an event."""
    if isinstance(value, Mapping):
        if _is_bundle(value):
            return change(value)
        return {k: map_bundles(v, change) for k, v in value.items()}
    if isinstance(value, list):
        return [map_bundles(v, change) for v in value]
    return value


def _fit_value(value: Any, limit: int) -> Any:
    return map_bundles(value, lambda bundle: fit_bundle(bundle, limit=limit))


def _is_bundle(value: Mapping[str, Any]) -> bool:
    keys = list(value)
    return bool(keys) and all(isinstance(k, str) and "/" in k for k in keys)


def fit_event(event: Mapping[str, Any], *, limit: int = EVENT_MAX_BYTES) -> dict[str, Any]:
    """``event`` as it may travel: strict JSON (:func:`plain_json`), unchanged
    when it fits, else with its output values over the inline limit replaced
    by the marker, and then, if it still does not fit, with every output
    value marked. Its type, kernel and number are always kept. An event that
    cannot be encoded would fail its whole post, and an answer in that post
    would never arrive."""
    whole = _plain_event(event)
    if json_bytes(whole) <= limit:
        return whole
    for inline in (INLINE_VALUE_MAX_BYTES, 0):
        fitted: dict[str, Any] = _fit_value(whole, inline)
        if json_bytes(fitted) <= limit:
            return fitted
    keep = ("type", "kernel_id", "seq", "run_id", "cell_id", "output_id")
    return {
        **{k: whole[k] for k in keep if k in whole},
        "too_large": too_large("application/json", json_bytes(event)),
    }


def batches(
    events: Iterable[Mapping[str, Any]], *, limit: int = BATCH_MAX_BYTES
) -> list[list[dict[str, Any]]]:
    """``events``, each fitted, in order, grouped so no group weighs more
    than ``limit``."""
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for event in events:
        one = fit_event(event)
        weight = json_bytes(one) + 1
        if current and size + weight > limit:
            groups.append(current)
            current, size = [], 0
        current.append(one)
        size += weight
    if current:
        groups.append(current)
    return groups


def fits(events: Sequence[Mapping[str, Any]], *, limit: int = BATCH_MAX_BYTES) -> bool:
    return json_bytes(list(events)) <= limit


__all__ = [
    "BATCH_ENVELOPE_BYTES",
    "BATCH_MAX_BYTES",
    "COMM_BODY_MAX_BYTES",
    "EVENT_MAX_BYTES",
    "INLINE_VALUE_MAX_BYTES",
    "KERNEL_OUTPUT_MAX_BYTES",
    "OPS_BODY_MAX_BYTES",
    "POST_BODY_MAX_BYTES",
    "POST_MAX_BYTES",
    "REQUEST_BODY_MAX_BYTES",
    "TOO_LARGE_MIME",
    "batches",
    "fit_bundle",
    "fit_event",
    "fits",
    "json_bytes",
    "map_bundles",
    "plain_json",
]
