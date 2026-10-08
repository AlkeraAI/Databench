"""Context-overflow classification — the single source of truth shared by the
gateway (stamp a structured code) and the CLI harness translator (react to it).

The whole value of this module is its ASYMMETRY: a real context-window overflow
classifies, a generic 400/429/5xx does NOT (a false positive would trigger a
needless compaction + retry). So the negatives matter as much as the positives.
"""

from __future__ import annotations

import json

import pytest
from alkera_core.overflow import (
    CONTEXT_LENGTH_EXCEEDED_CODE,
    error_payload_is_overflow,
    is_context_overflow,
)

# --------------------------------------------------------------------------- #
# is_context_overflow — gateway side (status_code, detail[, code])
# --------------------------------------------------------------------------- #

_OVERFLOW_DETAILS = [
    pytest.param(
        "prompt is too long: 1063592 tokens > 1000000 maximum", id="anthropic-prompt-too-long"
    ),
    pytest.param("input is too long for requested model", id="bedrock"),
    pytest.param(
        "This model's maximum context length is 128000 tokens. However, your messages "
        "resulted in 130000 tokens. Please reduce the length of the messages.",
        id="openai-max-context-length",
    ),
    pytest.param(
        "Input token count 1100000 exceeds the maximum number of tokens allowed", id="gemini"
    ),
    pytest.param(
        "the input exceeds the context window of this model", id="openai-exceeds-context-window"
    ),
    pytest.param("context_length_exceeded", id="bare-code-in-text"),
    pytest.param("Request Entity Too Large", id="413-text"),
    pytest.param("too large for model with 200000 maximum context length", id="mistral"),
]


@pytest.mark.parametrize("detail", _OVERFLOW_DETAILS)
def test_is_context_overflow_matches_provider_messages(detail: str) -> None:
    # A 400 carrying any provider's overflow wording classifies.
    assert is_context_overflow(400, detail) is True


def test_is_context_overflow_http_413_regardless_of_text() -> None:
    # 413 Request Entity Too Large is overflow even with an empty body.
    assert is_context_overflow(413, "") is True
    assert is_context_overflow(413, "anything at all") is True


def test_is_context_overflow_structured_code_wins() -> None:
    # The structured code classifies even when the message is unhelpful.
    assert is_context_overflow(400, "Bad Request", code=CONTEXT_LENGTH_EXCEEDED_CODE) is True


# ASYMMETRIC NEGATIVES — these MUST NOT classify (would cause a spurious compaction).
_NON_OVERFLOW = [
    pytest.param(400, "invalid_request_error: 'messages' must be an array", id="generic-400"),
    pytest.param(400, "model `gpt-9` does not exist", id="model-not-found"),
    pytest.param(429, "rate limit exceeded, please slow down", id="429-rate-limit"),
    pytest.param(529, "Overloaded", id="529-overloaded"),
    pytest.param(500, "internal server error", id="500"),
    pytest.param(401, "invalid api key", id="auth"),
    pytest.param(400, "connection took too long and timed out", id="too-long-but-not-context"),
    pytest.param(400, "", id="empty-detail"),
    pytest.param(400, "your request was too short", id="too-short"),
]


@pytest.mark.parametrize(("status", "detail"), _NON_OVERFLOW)
def test_is_context_overflow_rejects_non_overflow(status: int, detail: str) -> None:
    assert is_context_overflow(status, detail) is False


def test_is_context_overflow_ignores_unrelated_code() -> None:
    assert is_context_overflow(400, "bad request", code="invalid_request_error") is False
    assert is_context_overflow(400, "bad request", code="gateway_error") is False


# --------------------------------------------------------------------------- #
# error_payload_is_overflow — translator side (opencode session.error payload)
# --------------------------------------------------------------------------- #


def test_payload_none_and_empty_are_not_overflow() -> None:
    assert error_payload_is_overflow(None) is False
    assert error_payload_is_overflow({}) is False
    assert error_payload_is_overflow("") is False


def test_payload_structured_code_direct() -> None:
    err = {"type": "error", "error": {"type": "api_error", "code": CONTEXT_LENGTH_EXCEEDED_CODE}}
    assert error_payload_is_overflow(err) is True


def test_payload_stringified_json_in_message() -> None:
    # The shape opencode surfaces an in-band SSE error as: the whole gateway error
    # JSON (carrying the stamped code) lands as a STRING inside `data.message`.
    inner = json.dumps(
        {
            "type": "error",
            "error": {
                "type": "api_error",
                "message": "upstream error 400: prompt is too long: 1063592 > 1000000",
                "code": CONTEXT_LENGTH_EXCEEDED_CODE,
            },
        }
    )
    payload = {"name": "ProviderModelError", "data": {"message": inner}}
    assert error_payload_is_overflow(payload) is True


def test_payload_free_text_fallback_without_code() -> None:
    # Even with no structured code (e.g. before the gateway deploy), the message
    # pattern still classifies.
    payload = {"type": "error", "error": {"message": "prompt is too long: 9 > 8"}}
    assert error_payload_is_overflow(payload) is True


def test_payload_bare_string_message() -> None:
    assert error_payload_is_overflow("prompt is too long: 9 > 8") is True


_NON_OVERFLOW_PAYLOADS = [
    pytest.param({"type": "error", "error": {"message": "rate limit exceeded"}}, id="rate-limit"),
    pytest.param({"data": {"message": "connection took too long"}}, id="too-long-not-context"),
    pytest.param({"name": "AbortError", "data": {"message": "Aborted"}}, id="aborted"),
    pytest.param("the model is overloaded, please retry", id="overloaded-string"),
    pytest.param(
        {"error": {"code": "insufficient_quota", "message": "quota exceeded"}}, id="quota"
    ),
]


@pytest.mark.parametrize("payload", _NON_OVERFLOW_PAYLOADS)
def test_payload_rejects_non_overflow(payload: object) -> None:
    assert error_payload_is_overflow(payload) is False
