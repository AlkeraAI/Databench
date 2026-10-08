"""The OpenAI Responses usage parser splits cache WRITES out of plain input.

GPT-6 and GPT-5.6 report `input_tokens_details.cache_write_tokens` — the prefix
written to OpenAI's prompt cache, billed at 1.25x input. Input, cached input and
cache writes are disjoint parts of `input_tokens`, so each token lands in exactly
one bucket: never billed twice, never dropped.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from alkera_core.model_catalog import Usage
from model_gateway.adapters import OpenAIResponsesUsageParser


def _completed(usage: dict[str, Any]) -> bytes:
    event = {"type": "response.completed", "response": {"usage": usage}}
    return f"event: response.completed\ndata: {json.dumps(event)}\n\n".encode()


def _usage(details: dict[str, Any] | None) -> dict[str, Any]:
    body: dict[str, Any] = {"input_tokens": 5000, "output_tokens": 50}
    if details is not None:
        body["input_tokens_details"] = details
    return body


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        pytest.param(
            {"cached_tokens": 0, "cache_write_tokens": 4800},
            Usage(input=200, output=50, cache_write_5m=4800),
            id="a cold turn that writes its prefix",
        ),
        pytest.param(
            {"cached_tokens": 4800, "cache_write_tokens": 0},
            Usage(input=200, output=50, cache_read=4800),
            id="a warm turn that reads it",
        ),
        pytest.param(
            {"cached_tokens": 3000, "cache_write_tokens": 1500},
            Usage(input=500, output=50, cache_read=3000, cache_write_5m=1500),
            id="a read plus an extension written",
        ),
        pytest.param(
            {"cached_tokens": 1000},
            Usage(input=4000, output=50, cache_read=1000),
            id="an older model that reports no writes",
        ),
        pytest.param(None, Usage(input=5000, output=50), id="no details at all"),
        pytest.param(
            {"cached_tokens": 4000, "cache_write_tokens": 9000},
            Usage(input=0, output=50, cache_read=4000, cache_write_5m=1000),
            id="a write larger than the uncached input is clamped to it",
        ),
        pytest.param(
            {"cached_tokens": 0, "cache_write_tokens": -5},
            Usage(input=5000, output=50),
            id="a negative write is ignored",
        ),
        pytest.param(
            {"cached_tokens": 0, "cache_write_tokens": None},
            Usage(input=5000, output=50),
            id="a null write is none",
        ),
    ],
)
def test_the_parser_puts_every_input_token_in_exactly_one_bucket(
    details: dict[str, Any] | None, expected: Usage
) -> None:
    parser = OpenAIResponsesUsageParser()
    parser.feed(_completed(_usage(details)))

    got = parser.usage()

    assert got == expected
    assert got.input + got.cache_read + got.cache_write_5m == 5000
