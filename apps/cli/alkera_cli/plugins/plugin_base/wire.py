"""The wire leaf — the ONE encoder, measure, serializer, and prefix search.

Every model-facing byte in the system is produced or measured here. The defect
this module exists to end is a second encoder measuring what a first one
sends. Nothing outside this module may call ``json.dumps`` for model-facing
bytes or re-derive a payload's cost.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Final, cast

from alkera_core.json_safe import json_safe
from pydantic import BaseModel
from pydantic_core import to_jsonable_python

#: Reserved top-level key stamped on a dispatch result that represents a FAILED
#: tool call. Every transport reads it to set the provider's error signal (MCP
#: ``isError``, the Claude SDK's ``is_error``). Deliberately NOT the ``"error"``
#: substring, since a successful tool's own Output can legitimately carry an
#: ``error`` field, and the leading underscore can never collide with a Pydantic
#: field name. A transport signal, not content, so ``model_facing_result``
#: strips it before the model sees the dict.
TOOL_ERROR_FLAG: Final = "_alkera_tool_error"

#: The MCP ``_meta`` key a transport stamps on a result whose tool ALREADY
#: shortened itself and wrote the whole output to a file. A backend that bounds
#: tool results reads it and leaves such a result alone: its own spill would hold
#: the tail we handed back, and the pointer it stamps on the tool call is the one
#: the transcript row and the reader are given — so without this the reader is
#: sent to a copy of the preview while the real output sits in a file nothing
#: names. Namespaced, because ``_meta`` is shared with the MCP spec's own keys.
HOST_SPILL_META_KEY: Final = "ai.alkera/host-spill"


def host_spill_pointer(result: Any) -> str | None:
    """The file a dispatch result says it spilled its whole output to.

    ``None`` unless the result both declares itself ``truncated`` and names a
    non-empty ``output_path``: a marker with nothing to point at would ask a
    backend to keep a path that reaches nothing, which is worse than letting it
    shorten the result itself. ``call_tool`` wraps the tool it dispatched, so the
    pointer is read through that one envelope too."""
    if not isinstance(result, dict):
        return None
    if result.get("truncated") is not True:
        inner = result.get("result")
        if not isinstance(inner, dict):
            return None
        result = inner
    if result.get("truncated") is not True:
        return None
    path = result.get("output_path")
    return path if isinstance(path, str) and path else None


def tool_error_result(
    message: str, *, tool: str, classification: str = "error", **extra: Any
) -> dict[str, Any]:
    """Build a dispatch result for a FAILED tool call: the readable ``error``
    message, the ``tool`` name, the ``TOOL_ERROR_FLAG`` transports key off,
    its classification, plus any ``extra`` fields (e.g. a truncated preview)."""
    extra["classification"] = classification
    return {TOOL_ERROR_FLAG: True, "error": message, "tool": tool, **extra}


def is_tool_error_result(result: Any) -> bool:
    """Whether a dispatch result represents a failed tool call. Transports call
    this to set the provider error flag; ``call_tool`` calls it to re-raise an
    inner tool's failure as its own."""
    return isinstance(result, dict) and bool(result.get(TOOL_ERROR_FLAG))


def readable_tool_error(text: str) -> str:
    """The message a reader sees for a failed call's error text: the ``error`` of a
    :func:`tool_error_result` envelope when the text is one — a JSON object carrying
    string ``error``, ``tool`` and ``classification``, nothing looser — else the text
    exactly as it came (the harness's own abort sentence, a provider's message, a
    tool's plain text). The portal reads the envelope the same way
    (``packages/chat-model/src/toolError.ts``)."""
    stripped = text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return as_sentence(text)
    try:
        parsed = json.loads(stripped)
    except ValueError:
        return as_sentence(text)
    if not isinstance(parsed, dict):
        return as_sentence(text)
    error, tool, classification = (
        parsed.get("error"),
        parsed.get("tool"),
        parsed.get("classification"),
    )
    if isinstance(error, str) and isinstance(tool, str) and isinstance(classification, str):
        return as_sentence(error)
    return as_sentence(text)


def as_sentence(text: str) -> str:
    """``text`` with its first letter raised, and only the first: the line a
    reader sees starts a sentence, and tools write their reasons in the
    register they think in (``query failed: …``). A message that opens with an
    identifier — ``web.fetch failed …``, ``files.lease_mismatch: …``, a path or a
    URL — is left alone, since raising it would misspell a name. The portal
    applies the same rule (``packages/chat-model/src/toolError.ts``)."""
    stripped = text.lstrip()
    if not stripped:
        return text
    word = stripped.split(None, 1)[0]
    if any(mark in word for mark in "._/:@"):
        return text
    lead = len(text) - len(stripped)
    first = stripped[0]
    if first == first.upper():
        return text
    return text[:lead] + first.upper() + stripped[1:]


def model_facing_result(result: dict[str, Any]) -> dict[str, Any]:
    """The payload as the model / editor should see it, the internal
    ``TOOL_ERROR_FLAG`` removed. A no-op for success results."""
    if TOOL_ERROR_FLAG in result:
        return {k: v for k, v in result.items() if k != TOOL_ERROR_FLAG}
    return result


def encode_wire(value: Any) -> str:
    """The exact text a model-facing transport sends for ``value``. Non-ASCII is
    written as itself; a lone surrogate (which UTF-8 cannot encode) falls back to
    its escaped form so the text always encodes."""
    text = json.dumps(value, ensure_ascii=False)
    try:
        text.encode()
    except UnicodeEncodeError:
        return text.encode(errors="backslashreplace").decode()
    return text


def model_facing_text(result: dict[str, Any]) -> str:
    """The exact text a transport sends for a dispatch result: the error flag
    stripped, then the one encoder."""
    return encode_wire(model_facing_result(result))


def serialize_tool_result(result: BaseModel) -> dict[str, Any]:
    """Serialize a tool's native result model to the wire dict.

    ``mode="python"`` then ``to_jsonable_python`` then ``json_safe`` (never
    ``model_dump(mode="json")``): pydantic's JSON mode silently turns a
    non-finite float in an ``Any`` field into ``null`` before it can be wrapped,
    while this pipeline preserves it as the recoverable ``$nonfinite`` sentinel.
    Shared by the foreground dispatch AND the background card/wake so the two
    can never drift."""
    coerced = to_jsonable_python(result.model_dump(mode="python"), bytes_mode="base64")
    return cast("dict[str, Any]", json_safe(coerced))


def result_wire_bytes(result: BaseModel) -> int:
    """What a tool's native result model weighs on the wire, measured through the
    exact serialize + encode pair the transports send."""
    return len(model_facing_text(serialize_tool_result(result)).encode())


def result_field_bytes(name: str, value: Any) -> int:
    """What one field of a tool result costs the model, measured rather than
    restated: the value is coerced and encoded exactly as ``model_facing_text``
    will encode it, so a tool that budgets what it emits cannot drift from what the
    transports actually send."""
    coerced = json_safe(to_jsonable_python(value, bytes_mode="base64"))
    return len(model_facing_text({name: coerced}).encode()) - len(model_facing_text({}).encode())


def longest_fitting_prefix(n: int, fits: Callable[[int], bool], *, floor: int = 0) -> int:
    """The largest ``k <= n`` with ``fits(k)``, never below ``floor``. ``fits``
    must be monotone (a shorter prefix never weighs more). Below the floor the
    result is unmeasured: ``fits(floor)`` is never evaluated, so a caller whose
    floor cannot fit ships it anyway and the generic delivery door catches the
    oversize. Every bounded prefix in the system (preview rows, page rows, page
    characters) is found here, so there is one search to trust and one floor
    convention."""
    if n <= floor or fits(n):
        return n
    low, high = floor, n
    while low < high:
        mid = (low + high + 1) // 2
        if fits(mid):
            low = mid
        else:
            high = mid - 1
    return low


__all__ = [
    "HOST_SPILL_META_KEY",
    "TOOL_ERROR_FLAG",
    "encode_wire",
    "host_spill_pointer",
    "is_tool_error_result",
    "longest_fitting_prefix",
    "model_facing_result",
    "model_facing_text",
    "result_field_bytes",
    "result_wire_bytes",
    "serialize_tool_result",
    "tool_error_result",
]
