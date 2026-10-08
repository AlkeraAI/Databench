"""A failed tool call's error text is its dispatch envelope; a reader gets the message.

The producer is ``tool_error_result``; the reader must accept exactly what it
writes — a JSON object with string ``error``, ``tool`` and ``classification``,
extras allowed — and leave every other text as it came.
"""

from __future__ import annotations

import json

import pytest
from alkera_cli.plugins.plugin_base.wire import (
    as_sentence,
    model_facing_result,
    readable_tool_error,
    tool_error_result,
)

MESSAGE = "permission denied: sharing this knowledge item was not approved"
#: The same reason as a reader sees it: the first letter raised, nothing else.
MESSAGE_AS_SENTENCE = MESSAGE[0].upper() + MESSAGE[1:]


def test_the_reader_takes_the_message_off_what_the_producer_writes() -> None:
    envelope = model_facing_result(
        tool_error_result(MESSAGE, tool="context_note", classification="error", preview="x")
    )
    assert readable_tool_error(json.dumps(envelope)) == MESSAGE_AS_SENTENCE
    assert readable_tool_error(f"  {json.dumps(envelope)}\n") == MESSAGE_AS_SENTENCE


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(json.dumps({"error": MESSAGE, "tool": "context_note"}), id="missing-key"),
        pytest.param(
            json.dumps({"error": MESSAGE, "tool": 1, "classification": "error"}), id="mistyped-key"
        ),
        pytest.param(
            json.dumps({"error": [MESSAGE], "tool": "t", "classification": "error"}),
            id="message-not-a-string",
        ),
        pytest.param(
            json.dumps([{"error": MESSAGE, "tool": "t", "classification": "error"}]), id="array"
        ),
        pytest.param(
            "Error: " + json.dumps({"error": MESSAGE, "tool": "t", "classification": "error"}),
            id="prefixed",
        ),
        pytest.param(MESSAGE, id="plain-text"),
        pytest.param("Tool execution aborted", id="the-harness-abort-sentence"),
        pytest.param('{"error": ', id="broken-json"),
        pytest.param("", id="empty"),
    ],
)
def test_anything_that_is_not_the_envelope_is_left_as_it_came(text: str) -> None:
    """Not parsed as an envelope: the text comes back whole, read as a sentence."""
    assert readable_tool_error(text) == (MESSAGE_AS_SENTENCE if text == MESSAGE else text)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        pytest.param(
            'query failed: relation "t" does not exist',
            'Query failed: relation "t" does not exist',
            id="lowercase-prefix",
        ),
        pytest.param("permission denied: bash", "Permission denied: bash", id="permission"),
        pytest.param(
            "  invalid blob handle: x", "  Invalid blob handle: x", id="leading-space-kept"
        ),
    ],
)
def test_a_failed_calls_line_reads_as_a_sentence(given: str, expected: str) -> None:
    assert as_sentence(given) == expected
    assert readable_tool_error(given) == expected
    envelope = json.dumps({"error": given, "tool": "sql.query", "classification": "provider"})
    assert readable_tool_error(envelope) == expected


@pytest.mark.parametrize(
    "given",
    [
        pytest.param("web.fetch failed for https://example.com", id="tool-name"),
        pytest.param("files.lease_mismatch: the folder moved", id="error-code"),
        pytest.param("/etc/shadow is not readable", id="path"),
        pytest.param("https://example.com answered 500", id="url"),
        pytest.param("Already a sentence.", id="already"),
        pytest.param("42 rows is too many", id="digit"),
        pytest.param("", id="empty"),
    ],
)
def test_an_identifier_or_a_sentence_is_left_as_it_came(given: str) -> None:
    assert as_sentence(given) == given
