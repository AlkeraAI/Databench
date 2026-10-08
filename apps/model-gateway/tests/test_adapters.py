"""AnthropicUsageParser + codec estimation (no DB, no network)."""

from __future__ import annotations

import json

import pytest
from model_gateway.adapters import (
    AnthropicMessagesCodec,
    AnthropicUsageParser,
    OpenAIResponsesCodec,
    UpstreamError,
)


def _sse(*events: dict) -> bytes:
    out = b""
    for ev in events:
        out += f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n".encode()
    return out


def test_usage_parser_extracts_input_output_and_cache(make_sse) -> None:
    parser = AnthropicUsageParser()
    parser.feed(make_sse(input_tokens=100, output_tokens=42, cache_read=8000, cache_write_5m=2000))
    usage = parser.usage()
    assert usage.input == 100
    assert usage.output == 42  # from message_delta, not the message_start "1"
    assert usage.cache_read == 8000
    assert usage.cache_write_5m == 2000


def test_usage_parser_is_robust_to_split_chunks(make_sse) -> None:
    sse = make_sse(input_tokens=50, output_tokens=10)
    parser = AnthropicUsageParser()
    for i in range(0, len(sse), 7):  # feed in 7-byte slices to stress the buffer
        parser.feed(sse[i : i + 7])
    usage = parser.usage()
    assert usage.input == 50
    assert usage.output == 10


def test_usage_parser_ignores_unparseable_lines() -> None:
    parser = AnthropicUsageParser()
    parser.feed(b"event: ping\ndata: not-json\n\n")
    parser.feed(b": a comment\n\n")
    assert parser.usage().output == 0


def test_usage_parser_flags_in_band_error_as_stream_failure() -> None:
    """Anthropic streams a terminal `error` event (overloaded_error, api_error, …)
    in-band on a 200 stream. The parser must flag it so settle records FAILED rather
    than a 0-usage success — the Anthropic mirror of OpenAI's `response.failed`."""
    parser = AnthropicUsageParser()
    parser.feed(_sse({"type": "message_start", "message": {"usage": {"input_tokens": 5}}}))
    assert parser.stream_failed() is False
    parser.feed(
        _sse({"type": "error", "error": {"type": "overloaded_error", "message": "overloaded"}})
    )
    assert parser.stream_failed() is True


def test_usage_parser_normal_stream_is_not_a_failure(make_sse) -> None:
    parser = AnthropicUsageParser()
    parser.feed(make_sse(input_tokens=10, output_tokens=3))
    assert parser.stream_failed() is False


def test_codec_estimation() -> None:
    codec = AnthropicMessagesCodec()
    body = {
        "model": "m",
        "max_tokens": 1234,
        "system": "you are helpful",
        "messages": [{"role": "user", "content": "hello world"}],
    }
    assert codec.max_output_tokens(body) == 1234
    assert codec.estimate_input_tokens(body) >= 1
    # falls back to the default when max_tokens is absent
    assert codec.max_output_tokens({"model": "m"}) == AnthropicMessagesCodec.DEFAULT_MAX_OUTPUT


def test_parser_cache_creation_breakdown_by_ttl() -> None:
    parser = AnthropicUsageParser()
    parser.feed(
        _sse(
            {
                "type": "message_start",
                "message": {
                    "usage": {
                        "input_tokens": 10,
                        "cache_creation_input_tokens": 3000,
                        "cache_creation": {
                            "ephemeral_5m_input_tokens": 1000,
                            "ephemeral_1h_input_tokens": 2000,
                        },
                        "cache_read_input_tokens": 500,
                        "output_tokens": 1,
                    }
                },
            },
            {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 20}},
        )
    )
    usage = parser.usage()
    assert usage.input == 10
    assert usage.cache_read == 500
    assert usage.cache_write_5m == 1000
    assert usage.cache_write_1h == 2000
    assert usage.output == 20


def test_parser_truncated_stream_keeps_what_it_got() -> None:
    # Connection/provider dropped after message_start, before any message_delta.
    parser = AnthropicUsageParser()
    parser.feed(
        _sse(
            {
                "type": "message_start",
                "message": {"usage": {"input_tokens": 777, "output_tokens": 1}},
            }
        )
    )
    usage = parser.usage()
    assert usage.input == 777
    assert usage.output == 1  # billed for what was actually produced


def test_parser_delta_without_start() -> None:
    parser = AnthropicUsageParser()
    parser.feed(_sse({"type": "message_delta", "delta": {}, "usage": {"output_tokens": 33}}))
    assert parser.usage().output == 33
    assert parser.usage().input == 0


def test_parser_last_delta_wins() -> None:
    parser = AnthropicUsageParser()
    parser.feed(
        _sse(
            {
                "type": "message_start",
                "message": {"usage": {"input_tokens": 5, "output_tokens": 1}},
            },
            {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 10}},
            {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 25}},
        )
    )
    assert parser.usage().output == 25


def test_parser_empty_stream_is_zero() -> None:
    assert AnthropicUsageParser().usage().output == 0


def test_codec_estimate_handles_list_content(estimator_path) -> None:
    codec = AnthropicMessagesCodec()
    text = "a" * 300
    body = {
        "model": "m",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": text}, {"type": "image"}]}
        ],
    }
    # The text block's own worth on the path under test, and never zero.
    assert codec.estimate_input_tokens(body) >= estimator_path(len(text), "anthropic")


def test_codec_error_sse_is_anthropic_shaped() -> None:
    out = AnthropicMessagesCodec().error_sse("boom").decode()
    # A `message_start` prefix initializes the downstream stream parser so the
    # `error` event terminates the turn instead of hanging it.
    assert out.startswith("event: message_start\n")
    assert "event: error\n" in out
    assert '"type": "error"' in out
    assert "boom" in out


def test_anthropic_error_sse_without_code_is_byte_identical_legacy() -> None:
    # Adding the `code` param must NOT change the bytes for an unclassified error —
    # the keyword default reproduces exactly today's shape (a silent wire change
    # could break every downstream consumer).
    explicit_none = AnthropicMessagesCodec().error_sse("boom", code=None)
    positional = AnthropicMessagesCodec().error_sse("boom")
    assert explicit_none == positional
    assert b'"code"' not in explicit_none


def test_anthropic_error_sse_stamps_context_overflow_code() -> None:
    out = AnthropicMessagesCodec().error_sse("prompt too long", code="context_length_exceeded")
    # The error event is opencode's `parseStreamError` shape: error.code drives its
    # native compaction. Parse the error frame and assert the structured code lands.
    error_block = out.decode().split("event: error\ndata: ", 1)[1]
    err = json.loads(error_block.strip())
    assert err["type"] == "error"
    assert err["error"]["code"] == "context_length_exceeded"


def test_openai_error_sse_without_code_is_byte_identical_legacy() -> None:
    explicit_none = OpenAIResponsesCodec().error_sse("boom", code=None)
    positional = OpenAIResponsesCodec().error_sse("boom")
    assert explicit_none == positional
    # Legacy OpenAI shape keeps its default code.
    assert b'"gateway_error"' in explicit_none


def test_openai_error_sse_stamps_context_overflow_code() -> None:
    out = OpenAIResponsesCodec().error_sse("prompt too long", code="context_length_exceeded")
    error_block = out.decode().split("event: error\ndata: ", 1)[1]
    err = json.loads(error_block.strip())
    assert err["error"]["code"] == "context_length_exceeded"


@pytest.mark.parametrize(
    ("body", "expected_code"),
    [
        pytest.param(
            b'{"error": {"code": "context_length_exceeded"}}',
            "context_length_exceeded",
            id="openai-code",
        ),
        pytest.param(b'{"error": {"type": "invalid_request_error"}}', None, id="no-code"),
        pytest.param(b'{"message": "top level"}', None, id="no-nested-error"),
        pytest.param(b"not json", None, id="unparseable"),
    ],
)
def test_upstream_error_code_extraction(body: bytes, expected_code: str | None) -> None:
    assert UpstreamError(400, body).error_code == expected_code


# --------------------------------------------------------------------------- #
# Admission estimate — it must be an UPPER BOUND over the whole request
# --------------------------------------------------------------------------- #
#
# `estimate_hold` is the only thing keeping a request's settled cost near its
# reservation, and it is fed `codec.estimate_input_tokens(body)`. A text-only walk
# of `messages[].content` scored a body carrying an 800 KB tool schema (~200k real
# input tokens) at ONE token — a ~28,000x under-hold, admitted against a
# fractions-of-a-cent reservation and written off at settle by the overdraft clamp.
# Every field the provider bills as input must move the estimate.

_BIG = "z" * 300_000  # ~75k-100k real input tokens whichever tokenizer you use


@pytest.mark.parametrize(
    ("codec", "body"),
    [
        pytest.param(
            AnthropicMessagesCodec(),
            {"model": "m", "messages": [], "tools": [{"name": "t", "description": _BIG}]},
            id="anthropic-tools",
        ),
        pytest.param(
            AnthropicMessagesCodec(),
            {
                "model": "m",
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "x", "content": _BIG}],
                    }
                ],
            },
            id="anthropic-tool-result",
        ),
        pytest.param(
            AnthropicMessagesCodec(),
            {
                "model": "m",
                "messages": [
                    {
                        "role": "assistant",
                        "content": [{"type": "tool_use", "name": "t", "input": {"q": _BIG}}],
                    }
                ],
            },
            id="anthropic-tool-use",
        ),
        pytest.param(
            OpenAIResponsesCodec(),
            {"model": "m", "input": [], "tools": [{"name": "t", "description": _BIG}]},
            id="openai-tools",
        ),
        pytest.param(
            OpenAIResponsesCodec(),
            {
                "model": "m",
                "input": [{"type": "function_call_output", "call_id": "c", "output": _BIG}],
            },
            id="openai-function-call-output",
        ),
    ],
)
def test_estimate_counts_every_billed_field(codec, body: dict, estimator_path) -> None:
    """The provider bills this payload as input tokens, so the hold must reflect
    it wherever the client put it — on either path the estimate can take.

    The floor is exactly what the path under test says that many characters are
    worth, so a walk that skipped the field comes back near zero against it. It
    is derived rather than written down because `_BIG` is a synthetic run of one
    character, and how many tokens that is depends entirely on which tokenizer
    is loaded."""
    assert codec.estimate_input_tokens(body) >= estimator_path(len(_BIG), codec.FAMILY)


@pytest.mark.parametrize(
    "codec", [AnthropicMessagesCodec(), OpenAIResponsesCodec()], ids=["anthropic", "openai"]
)
def test_estimate_is_monotone_in_the_body(codec) -> None:
    """Adding payload can never LOWER the estimate — the property that makes it a
    usable admission bound however a client shapes the request."""
    base = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    grown = {**base, "tools": [{"name": "t", "description": "d" * 9_000}]}
    assert codec.estimate_input_tokens(grown) > codec.estimate_input_tokens(base)


def _plain(text: str) -> dict:
    return {"model": "m", "messages": [{"role": "user", "content": text}]}


@pytest.mark.parametrize(
    "codec", [AnthropicMessagesCodec(), OpenAIResponsesCodec()], ids=["anthropic", "openai"]
)
@pytest.mark.parametrize(
    ("label", "shape"),
    [
        pytest.param("bare-string", lambda t: _plain(t), id="bare-string"),
        pytest.param(
            "data-url-prefixed",
            lambda t: _plain("data:x;base64," + t),
            id="data-url-prefixed-string",
        ),
        pytest.param(
            "text-block",
            lambda t: {
                "model": "m",
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "data:x;base64," + t}]}
                ],
            },
            id="data-url-prefixed-text-block",
        ),
        pytest.param(
            "base64-shaped-dict",
            lambda t: {"model": "m", "tools": [{"name": "t", "type": "base64", "data": t}]},
            id="base64-shaped-dict",
        ),
        pytest.param(
            "openai-input-text",
            lambda t: {
                "model": "m",
                "input": [{"type": "input_text", "text": "data:x;base64," + t}],
            },
            id="openai-input-text",
        ),
        pytest.param(
            "text-document-source",
            lambda t: {
                "model": "m",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "document",
                                "source": {"type": "text", "media_type": "text/plain", "data": t},
                            }
                        ],
                    }
                ],
            },
            id="plain-text-document-source",
        ),
    ],
)
def test_text_can_never_be_relocated_behind_the_attachment_charge(codec, label, shape) -> None:
    """The monotonicity property that matters: a flat attachment charge keyed off
    anything an attacker controls is a bypass, not a bound.

    Every shape here carries the SAME billable text the provider tokenizes character
    by character — as a bare string, dressed up as a data URL, wrapped in a
    base64-shaped dict, or sitting in Anthropic's text-bearing ``document`` source
    (whose payload really is raw text). None of them may estimate below the plain
    string, or the request is admitted against a hold orders of magnitude under what
    it settles for."""
    text = "z" * 400_000
    baseline = codec.estimate_input_tokens(_plain(text))
    assert codec.estimate_input_tokens(shape(text)) >= baseline


@pytest.mark.parametrize(
    ("codec", "body"),
    [
        pytest.param(
            AnthropicMessagesCodec(),
            {
                "model": "m",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": "A" * 400_000,
                                },
                            }
                        ],
                    }
                ],
            },
            id="anthropic-base64-image",
        ),
        pytest.param(
            OpenAIResponsesCodec(),
            {
                "model": "m",
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_image",
                                "image_url": "data:image/png;base64," + "A" * 400_000,
                            }
                        ],
                    }
                ],
            },
            id="openai-data-url-image",
        ),
    ],
)
def test_binary_attachments_are_charged_flat_not_by_base64_length(codec, body: dict) -> None:
    """The asymmetric case that keeps the bound usable: a provider prices an image
    by its CONTENT (~1.6k tokens), not by how many base64 chars carried it. Charging
    the encoding would hold ~130k tokens for one screenshot and 402 a funded user —
    so an attachment is charged flat, while still never counting as zero."""
    estimate = codec.estimate_input_tokens(body)
    assert estimate > 1_000  # not free, unlike the pre-fix text-only walk
    assert estimate < 400_000 // 3  # and nowhere near its base64 length


@pytest.mark.parametrize(
    "codec", [AnthropicMessagesCodec(), OpenAIResponsesCodec()], ids=["anthropic", "openai"]
)
def test_a_document_holds_far_more_than_an_image(codec) -> None:
    """One flat charge for every attachment kind under-holds documents badly: an
    image is ~1.6k tokens whatever it depicts, while a PDF is priced per PAGE and a
    long one bills tens of thousands. The charge is per block type."""
    payload = "A" * 400_000

    def _block(kind: str, media: str) -> dict:
        return {
            "model": "m",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": kind,
                            "source": {"type": "base64", "media_type": media, "data": payload},
                        }
                    ],
                }
            ],
        }

    image = codec.estimate_input_tokens(_block("image", "image/png"))
    document = codec.estimate_input_tokens(_block("document", "application/pdf"))
    assert document > image * 5


@pytest.mark.parametrize(
    "codec", [AnthropicMessagesCodec(), OpenAIResponsesCodec()], ids=["anthropic", "openai"]
)
@pytest.mark.parametrize("kind", ["image", "document"], ids=["image", "document"])
def test_a_bigger_attachment_never_holds_less(codec, kind: str) -> None:
    """A pure floor is not a bound: a max-size PDF bills tens of thousands of tokens
    while a one-page PDF bills a couple, so charging both the same lets the expensive
    one be admitted at the cheap one's price. Above the floor the charge tracks the
    payload, which is what makes the estimate monotone in every byte of the body."""

    def _at(size: int) -> int:
        return codec.estimate_input_tokens(
            {
                "model": "m",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": kind, "source": {"type": "base64", "data": "A" * size}}
                        ],
                    }
                ],
            }
        )

    sizes = [1_000, 100_000, 1_000_000, 5_000_000]
    estimates = [_at(size) for size in sizes]
    assert estimates == sorted(estimates)
    assert estimates[-1] > estimates[0]  # a max-size attachment is NOT held at the floor


@pytest.mark.parametrize(
    "codec", [AnthropicMessagesCodec(), OpenAIResponsesCodec()], ids=["anthropic", "openai"]
)
def test_estimate_survives_adversarial_bodies(codec) -> None:
    """Deep nesting must not blow the stack, and a degenerate body still estimates
    at least one token (the hold is never zero)."""
    deep: dict = {"model": "m"}
    node = deep
    for _ in range(5_000):
        node["messages"] = [{"content": {}}]
        node = node["messages"][0]["content"]
    assert codec.estimate_input_tokens(deep) >= 1
    assert codec.estimate_input_tokens({}) == 1


# The truncated-stream fallback only runs when a Responses stream died before the
# terminal event carried authoritative usage — so its split between fresh and
# cached input IS the number the request settles on. Pricing fresh input at the
# cache-read rate there is free inference, which is what these pin. Asserted
# through `usage()`, the value the pipeline bills, not the internal estimates.


def _turn(text: str) -> dict:
    return {"role": "user", "content": [{"type": "input_text", "text": text}]}


def _truncated_usage(body: dict):
    """Settled usage for a stream that delivered tokens and then died before the
    terminal `response.completed` — i.e. the estimate path."""
    parser = OpenAIResponsesCodec().usage_parser(body)
    parser.feed(b'data: {"type":"response.output_text.delta","delta":"hello there"}\n\n')
    assert parser.usage_is_estimated()
    return parser.usage()


def test_new_tools_and_instructions_are_fresh_input_not_a_cached_prefix() -> None:
    """A caller may ship a new tool schema or new instructions on any turn. Those
    are fresh input the provider has never seen, so they must not fall into the
    cached bucket — measuring the prefix from the whole body instead of from the
    prior turns prices them at the cache-read rate and settles the request under
    its real cost."""
    conversation = [_turn("a" * 4_000), _turn("b" * 4_000), _turn("c" * 4_000)]
    lean = _truncated_usage({"model": "m", "input": conversation})
    fat = _truncated_usage(
        {
            "model": "m",
            "input": conversation,
            "instructions": "i" * 40_000,
            "tools": [{"name": "t", "parameters": {"blob": "p" * 40_000}}],
        }
    )

    # The new tools/instructions are real input, so they raise what we bill...
    assert fat.input + fat.cache_read > lean.input + lean.cache_read
    # ...entirely as FRESH input: the cached bucket is unchanged by them.
    assert fat.cache_read == lean.cache_read
    assert fat.input > lean.input


def test_the_cached_bucket_is_covered_by_the_turns_that_precede_the_newest() -> None:
    """Upper bound: whatever bills at the cache-read rate has to be explainable by
    the earlier turns alone, even when a large tool block dwarfs them."""
    earlier = "a" * 8_000, "b" * 8_000
    turns = [_turn(earlier[0]), _turn(earlier[1]), _turn("c" * 200)]
    usage = _truncated_usage({"model": "m", "input": turns, "tools": [{"blob": "z" * 50_000}]})
    # chars/token >= 1, so the prior turns' character count bounds their tokens.
    assert 0 < usage.cache_read <= sum(len(t) for t in earlier)


def test_a_single_turn_request_has_no_cached_prefix() -> None:
    """The asymmetric case: one giant one-shot prompt is entirely fresh however
    large — otherwise the cheapest way to send a huge prompt is to send it once."""
    usage = _truncated_usage({"model": "m", "input": [_turn("a" * 200_000)]})
    assert usage.cache_read == 0
    assert usage.input > 0
