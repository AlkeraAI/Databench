"""Transport unit tests — Anthropic-direct (httpx MockTransport) + Bedrock
(fake aioboto3 client). No DB, no real network/AWS."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager

import httpx
import pytest
from botocore.exceptions import ClientError
from model_gateway.adapters import (
    AnthropicDirectTransport,
    AnthropicUsageParser,
    BedrockInvokeTransport,
    OpenAIResponsesTransport,
    UpstreamError,
)


async def _drain(cm: AbstractAsyncContextManager[AsyncIterator[bytes]]) -> bytes:
    out = b""
    async with cm as stream:
        async for chunk in stream:
            out += chunk
    return out


# --------------------------------------------------------------------------- #
# Anthropic-direct
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_anthropic_transport_relays_200_bytes_unchanged() -> None:
    sse = b"event: message_start\ndata: {}\n\nevent: message_stop\ndata: {}\n\n"
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=sse))
    )
    transport = AnthropicDirectTransport(
        base_url="https://api.anthropic.com",
        api_key="k",
        anthropic_version="2023-06-01",
        client=client,
    )
    out = await _drain(
        transport.stream(upstream_model_id="m", region=None, body={"model": "s", "messages": []})
    )
    assert out == sse
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "retryable"),
    [
        (400, False),
        (401, False),
        (404, False),
        (408, True),
        (429, True),
        (500, True),
        (503, True),
        (529, True),
    ],
)
async def test_anthropic_transport_maps_error_status(status: int, retryable: bool) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(status, content=b'{"error":"x"}'))
    )
    transport = AnthropicDirectTransport(
        base_url="https://api.anthropic.com", api_key="k", anthropic_version="v", client=client
    )
    with pytest.raises(UpstreamError) as excinfo:
        await _drain(transport.stream(upstream_model_id="m", region=None, body={"messages": []}))
    assert excinfo.value.status_code == status
    assert excinfo.value.retryable is retryable
    await client.aclose()


@pytest.mark.asyncio
async def test_anthropic_transport_shapes_request() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = AnthropicDirectTransport(
        base_url="https://api.anthropic.com",
        api_key="secret",
        anthropic_version="2023-06-01",
        client=client,
    )
    await _drain(
        transport.stream(
            upstream_model_id="claude-real",
            region=None,
            body={"model": "our-slug", "messages": [], "max_tokens": 5},
        )
    )
    assert captured["url"].endswith("/v1/messages")
    assert captured["body"]["model"] == "claude-real"  # our upstream id replaces the slug
    assert captured["body"]["stream"] is True
    assert captured["headers"]["x-api-key"] == "secret"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    await client.aclose()


@pytest.mark.asyncio
async def test_anthropic_transport_omits_empty_api_key_header() -> None:
    # An unconfigured key must NOT send "x-api-key: " (malformed) — omit it.
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, content=b"")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = AnthropicDirectTransport(
        base_url="https://api.anthropic.com", api_key="", anthropic_version="v", client=client
    )
    await _drain(transport.stream(upstream_model_id="m", region=None, body={"messages": []}))
    assert "x-api-key" not in captured["headers"]
    await client.aclose()


@pytest.mark.asyncio
async def test_anthropic_transport_connection_error_propagates() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = AnthropicDirectTransport(
        base_url="https://api.anthropic.com", api_key="k", anthropic_version="v", client=client
    )
    with pytest.raises(httpx.HTTPError):
        await _drain(transport.stream(upstream_model_id="m", region=None, body={"messages": []}))
    await client.aclose()


# --------------------------------------------------------------------------- #
# Bedrock
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_bedrock_transport_reframes_and_shapes(
    bedrock_client_cls, make_bedrock_events
) -> None:
    client = bedrock_client_cls(make_bedrock_events(input_tokens=100, output_tokens=50))
    transport = BedrockInvokeTransport(client_factory=lambda: client)

    out = await _drain(
        transport.stream(
            upstream_model_id="anthropic.claude-x",
            region="us-east-1",
            body={"model": "slug", "stream": True, "messages": [], "max_tokens": 10},
        )
    )
    # The re-framed SSE parses with the same Anthropic parser.
    parser = AnthropicUsageParser()
    parser.feed(out)
    usage = parser.usage()
    assert usage.input == 100
    assert usage.output == 50
    assert b"event: message_start" in out

    sent = client.calls[0]
    assert sent["modelId"] == "anthropic.claude-x"
    body = json.loads(sent["body"])
    assert body["anthropic_version"] == "bedrock-2023-05-31"
    assert "model" not in body  # model goes in modelId, not the body
    assert "stream" not in body  # InvokeModelWithResponseStream is inherently streaming


@pytest.mark.asyncio
async def test_bedrock_transport_throttling_is_retryable(bedrock_client_cls) -> None:
    err = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "InvokeModelWithResponseStream",
    )
    client = bedrock_client_cls([], error=err)
    transport = BedrockInvokeTransport(client_factory=lambda: client)
    with pytest.raises(UpstreamError) as excinfo:
        await _drain(
            transport.stream(upstream_model_id="m", region="us-east-1", body={"messages": []})
        )
    assert excinfo.value.status_code == 429
    assert excinfo.value.retryable is True
    assert excinfo.value.retry_after is None  # no headers on this error → no hint


@pytest.mark.asyncio
async def test_bedrock_transport_carries_the_retry_after_hint(bedrock_client_cls) -> None:
    """Bedrock is the one non-httpx transport — its Retry-After rides in the
    botocore response metadata (lowercased header keys), and dropping it here
    would leave Bedrock throttles on blind backoff while every other provider's
    hint is honored."""
    err = ClientError(
        {
            "Error": {"Code": "ThrottlingException", "Message": "slow down"},
            "ResponseMetadata": {"HTTPHeaders": {"retry-after": "7"}},
        },
        "InvokeModelWithResponseStream",
    )
    client = bedrock_client_cls([], error=err)
    transport = BedrockInvokeTransport(client_factory=lambda: client)
    with pytest.raises(UpstreamError) as excinfo:
        await _drain(
            transport.stream(upstream_model_id="m", region="us-east-1", body={"messages": []})
        )
    assert excinfo.value.status_code == 429
    assert excinfo.value.retry_after == 7.0


@pytest.mark.asyncio
async def test_bedrock_transport_validation_is_not_retryable(bedrock_client_cls) -> None:
    err = ClientError(
        {"Error": {"Code": "ValidationException", "Message": "bad"}},
        "InvokeModelWithResponseStream",
    )
    client = bedrock_client_cls([], error=err)
    transport = BedrockInvokeTransport(client_factory=lambda: client)
    with pytest.raises(UpstreamError) as excinfo:
        await _drain(transport.stream(upstream_model_id="m", region=None, body={"messages": []}))
    assert excinfo.value.status_code == 400  # client error → terminal
    assert excinfo.value.retryable is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("ThrottlingException", 429),
        ("ModelNotReadyException", 503),
        ("ServiceUnavailableException", 503),
        ("InternalServerException", 500),  # server fault → 5xx (not 400), still retryable
        ("ModelTimeoutException", 504),
        ("AccessDeniedException", 400),  # genuine client error → terminal
        ("ResourceNotFoundException", 400),
    ],
)
async def test_bedrock_transport_status_mapping(bedrock_client_cls, code: str, status: int) -> None:
    err = ClientError({"Error": {"Code": code, "Message": "x"}}, "InvokeModelWithResponseStream")
    client = bedrock_client_cls([], error=err)
    transport = BedrockInvokeTransport(client_factory=lambda: client)
    with pytest.raises(UpstreamError) as excinfo:
        await _drain(transport.stream(upstream_model_id="m", region=None, body={"messages": []}))
    assert excinfo.value.status_code == status
    # Server-class faults (5xx) + throttling are retryable; client errors aren't.
    assert excinfo.value.retryable is (status == 429 or 500 <= status < 600)


@pytest.mark.asyncio
async def test_bedrock_transport_error_detail_surfaces_message(bedrock_client_cls) -> None:
    """A Bedrock ClientError must carry its human-readable message through
    UpstreamError.detail (botocore's error string isn't JSON, so detail falls back
    to the trimmed raw text — which still contains the provider message)."""
    err = ClientError(
        {"Error": {"Code": "ValidationException", "Message": "output_config.effort not permitted"}},
        "InvokeModelWithResponseStream",
    )
    client = bedrock_client_cls([], error=err)
    transport = BedrockInvokeTransport(client_factory=lambda: client)
    with pytest.raises(UpstreamError) as excinfo:
        await _drain(transport.stream(upstream_model_id="m", region=None, body={"messages": []}))
    assert "output_config.effort not permitted" in excinfo.value.detail


@pytest.mark.asyncio
async def test_bedrock_transport_ignores_non_chunk_events(
    bedrock_client_cls, make_bedrock_events
) -> None:
    client = bedrock_client_cls(make_bedrock_events(input_tokens=7, output_tokens=3))
    transport = BedrockInvokeTransport(client_factory=lambda: client)
    out = await _drain(transport.stream(upstream_model_id="m", region=None, body={"messages": []}))
    parser = AnthropicUsageParser()
    parser.feed(out)
    assert parser.usage().input == 7
    assert parser.usage().output == 3


# --------------------------------------------------------------------------- #
# OpenAI Responses API
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_openai_responses_transport_relays_200_bytes_unchanged() -> None:
    sse = b'event: response.created\ndata: {"type":"response.created"}\n\n'
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=sse))
    )
    transport = OpenAIResponsesTransport(
        base_url="https://api.openai.com/v1", api_key="k", client=client
    )
    out = await _drain(
        transport.stream(upstream_model_id="m", region=None, body={"model": "s", "input": []})
    )
    assert out == sse
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "retryable"),
    [(400, False), (401, False), (404, False), (408, True), (429, True), (500, True), (503, True)],
)
async def test_openai_responses_transport_maps_error_status(status: int, retryable: bool) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(status, content=b'{"error":{}}'))
    )
    transport = OpenAIResponsesTransport(
        base_url="https://api.openai.com/v1", api_key="k", client=client
    )
    with pytest.raises(UpstreamError) as excinfo:
        await _drain(transport.stream(upstream_model_id="m", region=None, body={"input": []}))
    assert excinfo.value.status_code == status
    assert excinfo.value.retryable is retryable
    await client.aclose()


@pytest.mark.asyncio
async def test_openai_responses_transport_forces_zdr_and_continuity() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = OpenAIResponsesTransport(
        base_url="https://api.openai.com/v1", api_key="secret", client=client
    )
    await _drain(
        transport.stream(
            upstream_model_id="gpt-real",
            region=None,
            # Caller tried to set store=True — the transport must override it for ZDR
            # and ensure the encrypted-reasoning include for continuity.
            body={"model": "our-slug", "input": [], "store": True},
        )
    )
    assert captured["url"].endswith("/responses")
    body = captured["body"]
    assert body["model"] == "gpt-real"  # our upstream id replaces the slug
    assert body["stream"] is True
    assert body["store"] is False  # ZDR: never retained server-side
    # The encrypted reasoning item is requested back → stateless continuity.
    assert "reasoning.encrypted_content" in body["include"]
    assert captured["headers"]["authorization"] == "Bearer secret"
    await client.aclose()


@pytest.mark.asyncio
async def test_openai_responses_transport_injects_effort() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = OpenAIResponsesTransport(
        base_url="https://api.openai.com/v1", api_key="k", client=client
    )
    await _drain(
        transport.stream(
            upstream_model_id="m",
            region=None,
            # Caller already set reasoning.summary — effort is merged in, summary kept.
            body={"input": [], "reasoning": {"summary": "auto"}},
            effort="high",
        )
    )
    assert captured["body"]["reasoning"] == {"summary": "auto", "effort": "high"}
    await client.aclose()


@pytest.mark.asyncio
async def test_openai_responses_transport_preserves_caller_include() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = OpenAIResponsesTransport(
        base_url="https://api.openai.com/v1", api_key="k", client=client
    )
    await _drain(
        transport.stream(
            upstream_model_id="m",
            region=None,
            body={"input": [], "include": ["message.output_text.logprobs"]},
        )
    )
    # Caller's include survives; encrypted_content is appended (not deduped away),
    # and never duplicated.
    include = captured["body"]["include"]
    assert "message.output_text.logprobs" in include
    assert include.count("reasoning.encrypted_content") == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_openai_responses_transport_omits_empty_api_key_header() -> None:
    # Regression: an empty key produced "Authorization: Bearer " (trailing
    # space) which httpcore rejects outright — breaking the upstream call. Omit.
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, content=b"")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = OpenAIResponsesTransport(
        base_url="https://api.openai.com/v1", api_key="", client=client
    )
    await _drain(transport.stream(upstream_model_id="m", region=None, body={"input": []}))
    assert "authorization" not in captured["headers"]
    await client.aclose()


@pytest.mark.asyncio
async def test_openai_responses_transport_tolerates_non_list_include() -> None:
    # A malformed caller include must not crash the transport — it's replaced, and
    # the encrypted-reasoning include is still forced.
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = OpenAIResponsesTransport(
        base_url="https://api.openai.com/v1", api_key="k", client=client
    )
    await _drain(
        transport.stream(upstream_model_id="m", region=None, body={"input": [], "include": "oops"})
    )
    assert captured["body"]["include"] == ["reasoning.encrypted_content"]
    await client.aclose()


@pytest.mark.asyncio
async def test_openai_responses_transport_connection_error_propagates() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = OpenAIResponsesTransport(
        base_url="https://api.openai.com/v1", api_key="k", client=client
    )
    with pytest.raises(httpx.HTTPError):
        await _drain(transport.stream(upstream_model_id="m", region=None, body={"input": []}))
    await client.aclose()
