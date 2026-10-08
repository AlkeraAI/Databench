"""ProxyUpstreamTransport — a self-hosted gateway in proxy mode forwards to
Alkera's hosted gateway with the org's proxy token. Unit tests (httpx
MockTransport): the right ingress + token, the client's ORIGINAL model string +
body forwarded untouched (a thin pass-through — Alkera re-splits/routes), and
upstream errors mapped through. No DB, no real network."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager

import httpx
import pytest
from model_gateway.adapters import ProxyUpstreamTransport, UpstreamError


async def _drain(cm: AbstractAsyncContextManager[AsyncIterator[bytes]]) -> bytes:
    out = b""
    async with cm as stream:
        async for chunk in stream:
            out += chunk
    return out


def _capture() -> tuple[list[httpx.Request], httpx.AsyncClient]:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, content=b"event: message_stop\ndata: {}\n\n")

    return seen, httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_forwards_to_anthropic_ingress_with_token() -> None:
    seen, client = _capture()
    transport = ProxyUpstreamTransport(
        base_url="https://gateway.example.com",
        token="alk_proxy_secret",
        wire="anthropic",
        client=client,
    )
    out = await _drain(
        transport.stream(
            upstream_model_id="big-pickle", region="us-east-1", body={"model": "x", "messages": []}
        )
    )
    assert b"message_stop" in out
    req = seen[0]
    assert str(req.url) == "https://gateway.example.com/anthropic/v1/messages"
    assert req.headers["authorization"] == "Bearer alk_proxy_secret"
    body = json.loads(req.content)
    assert body["model"] == "big-pickle"  # region dropped; model passed through
    assert body["stream"] is True
    await client.aclose()


@pytest.mark.asyncio
async def test_openai_wire_uses_responses_ingress() -> None:
    seen, client = _capture()
    transport = ProxyUpstreamTransport(
        base_url="https://gateway.example.com/", token="alk_proxy_x", wire="openai", client=client
    )
    await _drain(transport.stream(upstream_model_id="m", region=None, body={"model": "x"}))
    assert str(seen[0].url) == "https://gateway.example.com/openai/v1/responses"
    await client.aclose()


@pytest.mark.asyncio
async def test_forwards_original_model_string_untouched() -> None:
    # Thin: the caller passes the client's ORIGINAL model string (which already
    # carries the effort/display variant); the transport forwards it verbatim and
    # does NOT re-encode. The `effort` arg is accepted but ignored (it's already in
    # the string) — so it can never be double-appended.
    seen, client = _capture()
    transport = ProxyUpstreamTransport(
        base_url="https://gw", token="alk_proxy_x", wire="anthropic", client=client
    )
    await _drain(
        transport.stream(
            upstream_model_id="big-pickle::high::summarized",
            region=None,
            body={"model": "x"},
            effort="high",
        )
    )
    assert json.loads(seen[0].content)["model"] == "big-pickle::high::summarized"
    await client.aclose()


@pytest.mark.asyncio
async def test_forwards_client_body_verbatim_except_model_and_stream() -> None:
    # Every client body field rides through untouched; only `model` (set to the
    # forwarded string) and `stream` (forced true) are gateway-controlled.
    seen, client = _capture()
    transport = ProxyUpstreamTransport(
        base_url="https://gw", token="alk_proxy_x", wire="anthropic", client=client
    )
    await _drain(
        transport.stream(
            upstream_model_id="big-pickle",
            region=None,
            body={
                "model": "ignored-original",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1024,
                "temperature": 0.7,
                "stream": False,
            },
        )
    )
    body = json.loads(seen[0].content)
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["max_tokens"] == 1024
    assert body["temperature"] == 0.7
    assert body["model"] == "big-pickle"  # set to the forwarded string
    assert body["stream"] is True  # forced on, overriding the client's False
    await client.aclose()


@pytest.mark.asyncio
async def test_meter_rides_the_usage_meter_header() -> None:
    # The self-hosted billing meter crosses the trust boundary as a header so Alkera
    # persists the right bucket; absent the arg, no header is sent.
    from alkera_core.gateway import USAGE_METER_HEADER

    seen, client = _capture()
    transport = ProxyUpstreamTransport(
        base_url="https://gw", token="alk_proxy_x", wire="anthropic", client=client
    )
    await _drain(
        transport.stream(
            upstream_model_id="m", region=None, body={"model": "x"}, meter="additional"
        )
    )
    assert seen[0].headers[USAGE_METER_HEADER] == "additional"
    await _drain(transport.stream(upstream_model_id="m", region=None, body={"model": "x"}))
    assert USAGE_METER_HEADER not in seen[1].headers
    await client.aclose()


@pytest.mark.asyncio
async def test_display_rides_the_thinking_header() -> None:
    from alkera_core.gateway import THINKING_DISPLAY_HEADER

    seen, client = _capture()
    transport = ProxyUpstreamTransport(
        base_url="https://gw", token="alk_proxy_x", wire="anthropic", client=client
    )
    await _drain(
        transport.stream(
            upstream_model_id="m", region=None, body={"model": "x"}, display="summarized"
        )
    )
    assert seen[0].headers[THINKING_DISPLAY_HEADER] == "summarized"
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("gateway_upstream", "expected_model"),
    [("proxy", "claude-haiku-4.5::high"), ("direct", "claude-haiku-4-5")],
)
async def test_open_route_forwards_client_string_in_proxy_mode_else_provider_id(
    monkeypatch: pytest.MonkeyPatch, gateway_upstream: str, expected_model: str
) -> None:
    """In proxy mode the upstream is Alkera's gateway — a THIN pass-through forwards the
    client's ORIGINAL model string (``model_field``, slug + effort) untouched, so Alkera
    re-splits + does its own slug->provider routing exactly as for a normal client.
    Direct mode must still forward the provider's own id (route.upstream_model_id). The
    slug, the client string, and the provider id all differ on purpose here."""
    from contextlib import asynccontextmanager

    from alkera_core.llm_provider import Provider
    from alkera_core.models.model_catalog import ModelRoute
    from model_gateway import pipeline

    monkeypatch.setattr(pipeline.settings, "gateway_upstream", gateway_upstream)
    captured: dict[str, str] = {}

    class _FakeTransport:
        def stream(
            self,
            *,
            upstream_model_id: str,
            region: str | None,
            body: dict[str, object],
            **_: object,
        ) -> AbstractAsyncContextManager[AsyncIterator[bytes]]:
            captured["model"] = upstream_model_id

            @asynccontextmanager
            async def _cm() -> AsyncIterator[AsyncIterator[bytes]]:
                async def _gen() -> AsyncIterator[bytes]:
                    if False:  # an empty stream
                        yield b""

                yield _gen()

            return _cm()

    route = ModelRoute(
        model_id="claude-haiku-4.5",
        upstream_model_id="claude-haiku-4-5",
        provider=Provider.ANTHROPIC,
        region=None,
    )
    outcome, cm, _chunks, _err = await pipeline._open_route(
        transport=_FakeTransport(),  # type: ignore[arg-type]
        route=route,
        body={"model": "claude-haiku-4.5::high"},
        codec=object(),  # type: ignore[arg-type]  # untouched on the happy path
        model_field="claude-haiku-4.5::high",
    )
    assert outcome == "opened"
    assert captured["model"] == expected_model
    await cm.__aexit__(None, None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
async def test_upstream_error_status_propagates(status: int) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(status, content=b'{"error":"x"}'))
    )
    transport = ProxyUpstreamTransport(
        base_url="https://gw", token="alk_proxy_x", wire="anthropic", client=client
    )
    with pytest.raises(UpstreamError) as excinfo:
        await _drain(transport.stream(upstream_model_id="m", region=None, body={"model": "x"}))
    assert excinfo.value.status_code == status
    await client.aclose()


@pytest.mark.asyncio
async def test_a_hop_sends_one_key_per_request_derived_with_the_deployments_token() -> None:
    """A retried hop must reach the upstream gateway as the same request, so every
    attempt sends the same key; two deployments' keys for one request id differ, so
    they never meet in the upstream's request table; a hop with no id sends none."""
    from alkera_core.gateway import IDEMPOTENCY_KEY_HEADER

    seen, client = _capture()
    ours = ProxyUpstreamTransport(
        base_url="https://gw", token="alk_proxy_ours", wire="anthropic", client=client
    )
    theirs = ProxyUpstreamTransport(
        base_url="https://gw", token="alk_proxy_theirs", wire="anthropic", client=client
    )
    for transport, request_id in ((ours, "req-1"), (ours, "req-1"), (ours, "req-2")):
        await _drain(
            transport.stream(
                upstream_model_id="m", region=None, body={"model": "x"}, request_id=request_id
            )
        )
    await _drain(
        theirs.stream(upstream_model_id="m", region=None, body={"model": "x"}, request_id="req-1")
    )
    await _drain(ours.stream(upstream_model_id="m", region=None, body={"model": "x"}))

    keys = [request.headers.get(IDEMPOTENCY_KEY_HEADER) for request in seen]
    assert keys[0] == keys[1]
    assert len({keys[0], keys[2], keys[3]}) == 3
    assert keys[4] is None
    assert "req-1" not in keys[0]
    await client.aclose()


@pytest.mark.asyncio
async def test_every_attempt_of_one_request_carries_its_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route's retries resend the same request id, so a hop the upstream
    admitted before the connection broke is refused on the retry, not admitted
    again."""
    from contextlib import asynccontextmanager

    from alkera_core.llm_provider import Provider
    from alkera_core.models.model_catalog import ModelRoute
    from model_gateway import pipeline

    monkeypatch.setattr(pipeline.settings, "gateway_upstream_max_attempts", 3)
    sent: list[object] = []

    class _FlakyHop:
        def stream(self, **kwargs: object) -> AbstractAsyncContextManager[AsyncIterator[bytes]]:
            sent.append(kwargs.get("request_id"))
            fail = len(sent) < 3

            @asynccontextmanager
            async def _cm() -> AsyncIterator[AsyncIterator[bytes]]:
                if fail:
                    raise UpstreamError(502, b"bad gateway")

                async def _gen() -> AsyncIterator[bytes]:
                    if False:  # an empty stream
                        yield b""

                yield _gen()

            return _cm()

    route = ModelRoute(
        model_id="m", upstream_model_id="m", provider=Provider.ANTHROPIC, region=None
    )
    outcome, cm, _chunks, _err = await pipeline._open_route(
        transport=_FlakyHop(),
        route=route,
        body={"model": "m"},
        codec=object(),  # untouched on these paths
        model_field="m",
        request_id="req-7",
    )

    assert outcome == "opened"
    assert sent == ["req-7", "req-7", "req-7"]
    await cm.__aexit__(None, None, None)
