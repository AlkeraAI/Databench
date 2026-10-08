"""Context-window-overflow classification — the single source of truth.

Shared by BOTH the model gateway (`apps/model-gateway`, which classifies an upstream
provider error ONCE and stamps a structured `error.code` on the in-band SSE error) and
the CLI harness translator (`apps/cli`, which keys off that code to drive auto-compaction
+ retry). Keeping it here means the patterns + the canonical code live in exactly one
place instead of drifting across two apps.

The detection is deliberately CONSERVATIVE and asymmetric: a real context-window
overflow classifies; a generic 400 / 429 / 5xx with an unrelated message does NOT (a
false positive would trigger a needless compaction + retry).
"""

from __future__ import annotations

import json
import re
from typing import Any

#: The canonical, provider-agnostic error code stamped on a context-overflow SSE error.
#: Matches the value opencode's `parseStreamError` switches on
#: (`vendor/opencode/.../provider/error.ts`) → `context_overflow` → native compaction;
#: the Alkera harness translator keys off the same code.
CONTEXT_LENGTH_EXCEEDED_CODE = "context_length_exceeded"

# Mirror of opencode's OVERFLOW_PATTERNS (`vendor/opencode/.../provider/error.ts`) — the
# substrings each major provider uses for a context-window-exceeded rejection. Kept in
# lockstep with that list (whenever live-provider wording drifts, update both); matched
# case-insensitively. Each is specific enough that a generic error never matches — e.g.
# "connection took too long" does NOT match "prompt is too long".
_OVERFLOW_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"prompt is too long",  # Anthropic
        r"input is too long for requested model",  # Amazon Bedrock
        r"exceeds the context window",  # OpenAI (Completions + Responses)
        r"input token count.*exceeds the maximum",  # Google (Gemini)
        r"maximum prompt length is \d+",  # xAI (Grok)
        r"reduce the length of the messages",  # Groq / OpenAI
        r"maximum context length is \d+ tokens",  # OpenAI / OpenRouter / DeepSeek / vLLM
        r"exceeds the limit of \d+",  # GitHub Copilot
        r"exceeds the available context size",  # llama.cpp
        r"greater than the context length",  # LM Studio
        r"context window exceeds limit",  # MiniMax
        r"exceeded model token limit",  # Kimi / Moonshot
        r"context[_ ]length[_ ]exceeded",  # Generic fallback (also catches the stamped code)
        r"request entity too large",  # HTTP 413
        r"context length is only \d+ tokens",  # vLLM
        r"input length.*exceeds.*context length",  # vLLM
        r"prompt too long; exceeded (?:max )?context length",  # Ollama
        r"too large for model with \d+ maximum context length",  # Mistral
        r"model_context_window_exceeded",  # z.ai
    )
)


def matches_overflow_text(text: str) -> bool:
    """True iff ``text`` contains a known context-overflow message pattern."""
    return any(p.search(text) for p in _OVERFLOW_PATTERNS)


def is_context_overflow(status_code: int, detail: str, *, code: str | None = None) -> bool:
    """Gateway-side: classify an upstream error from ``(status_code, detail[, code])``.

    Overflow iff a structured ``context_length_exceeded`` code (OpenAI), HTTP 413
    (Request Entity Too Large), or one of the cross-provider message patterns.
    """
    if code == CONTEXT_LENGTH_EXCEEDED_CODE:
        return True
    if status_code == 413:
        return True
    return matches_overflow_text(detail or "")


def error_payload_is_overflow(error_field: Any) -> bool:
    """Translator-side: classify an opencode ``session.error`` payload.

    Keys off the structured ``context_length_exceeded`` code the gateway stamps —
    found directly, nested under ``error.code``, or inside a stringified-JSON
    ``message`` / ``data.message`` (the shape opencode surfaces an in-band SSE error
    as, per the gateway codecs) — with a message-pattern fallback. Serializing the
    whole payload and scanning it catches the code wherever it landed without guessing
    the exact nesting; the patterns are specific enough that a generic error is never
    a false positive.
    """
    if error_field is None:
        return False
    if isinstance(error_field, str):
        blob = error_field
    else:
        try:
            blob = json.dumps(error_field, default=str)
        except (TypeError, ValueError):
            blob = str(error_field)
    if CONTEXT_LENGTH_EXCEEDED_CODE in blob:
        return True
    return matches_overflow_text(blob)
