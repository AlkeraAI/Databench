"""BYOK credential plumbing: org DTOs override env keys per provider (wholesale),
Bedrock org credentials never carry the instance bearer token, the
OpenAI org header reaches the wire, and non-BYOK behavior stays byte-identical."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import model_gateway.api as gw_api
import pytest
from alkera_core.config import settings
from alkera_core.llm_provider import Provider
from alkera_core.model_providers import (
    AnthropicOrgCredentials,
    BedrockOrgCredentials,
)
from alkera_core.models.model_catalog import ModelRoute
from model_gateway.adapters import AnthropicDirectTransport, OpenAIResponsesTransport
from model_gateway.credentials import env_configured_providers, usable_providers


def _request(override: Any = None) -> Any:
    def _ok(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    state = SimpleNamespace(
        transport_factory_override=override,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_ok)),
        byok_http_client=httpx.AsyncClient(transport=httpx.MockTransport(_ok)),
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _route(provider: Provider, *, region: str | None = None) -> ModelRoute:
    return ModelRoute(
        model_id="m-x", provider=provider, upstream_model_id="up-x", enabled=True, region=region
    )


@pytest.fixture
def _direct(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "gateway_upstream", "direct")


def test_env_parity_without_org_creds(_direct: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """No org DTOs -> transports are built from settings exactly as before."""
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-env-key")
    build = gw_api._transport_factory(_request(), {})
    transport = build(_route(Provider.ANTHROPIC))
    assert isinstance(transport, AnthropicDirectTransport)
    assert transport._api_key == "sk-ant-env-key"
    assert transport._base_url == settings.anthropic_base_url.rstrip("/")


def test_org_anthropic_credentials_win_wholesale(
    _direct: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-env-key")
    creds = {
        Provider.ANTHROPIC: AnthropicOrgCredentials(
            api_key="sk-ant-org-key", base_url="https://gw.acme.internal"
        )
    }
    build = gw_api._transport_factory(_request(), creds)
    transport = build(_route(Provider.ANTHROPIC))
    assert isinstance(transport, AnthropicDirectTransport)
    assert transport._api_key == "sk-ant-org-key"  # org wins over env
    assert transport._base_url == "https://gw.acme.internal"  # per-org endpoint honored


def test_bedrock_org_static_keys_never_carry_a_bearer(
    _direct: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An org's static pair must construct the client factory with
    bearer_token=None even when the instance has a bearer configured; the wire
    proof that it then signs SigV4 is in test_bedrock_auth_scope.py."""
    monkeypatch.setattr(settings, "aws_bearer_token_bedrock", "ABSK-instance-bearer")
    captured: dict[str, Any] = {}

    def recording_factory(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return lambda: None

    monkeypatch.setattr(gw_api, "aioboto3_bedrock_client_factory", recording_factory)
    creds = {
        Provider.BEDROCK: BedrockOrgCredentials(
            region="eu-west-1",
            auth_mode="access_key",
            aws_access_key_id="AKIAORG12345",
            aws_secret_access_key="org-secret",
        )
    }
    build = gw_api._transport_factory(_request(), creds)
    build(_route(Provider.BEDROCK))
    assert captured["bearer_token"] is None
    assert captured["aws_access_key_id"] == "AKIAORG12345"
    assert captured["aws_secret_access_key"] == "org-secret"
    assert captured["aws_session_token"] is None
    assert captured["region"] == "eu-west-1"


def test_bedrock_region_precedence_route_over_org(
    _direct: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def recording_factory(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return lambda: None

    monkeypatch.setattr(gw_api, "aioboto3_bedrock_client_factory", recording_factory)
    creds = {Provider.BEDROCK: BedrockOrgCredentials(region="eu-west-1", auth_mode="iam")}
    build = gw_api._transport_factory(_request(), creds)
    build(_route(Provider.BEDROCK, region="us-west-2"))
    assert captured["region"] == "us-west-2"  # an explicit route region wins
    build(_route(Provider.BEDROCK, region=None))
    assert captured["region"] == "eu-west-1"  # else the org's default region


@pytest.mark.asyncio
async def test_openai_org_header_reaches_the_wire() -> None:
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["req"] = request
        return httpx.Response(
            200, content=b"data: {}\n\n", headers={"content-type": "text/event-stream"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = OpenAIResponsesTransport(
            base_url="https://api.openai.com/v1",
            api_key="sk-proj-org",
            organization_id="org-acme",
            client=client,
        )
        async with transport.stream(
            upstream_model_id="gpt-x", region=None, body={"input": "hi"}
        ) as chunks:
            async for _ in chunks:
                break
    assert seen["req"].headers["OpenAI-Organization"] == "org-acme"
    assert seen["req"].headers["authorization"] == "Bearer sk-proj-org"


def test_env_configured_providers_reflects_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    monkeypatch.setattr(settings, "openai_api_key", "sk-proj-env")
    monkeypatch.setattr(settings, "aws_bearer_token_bedrock", None)
    monkeypatch.setattr(settings, "aws_access_key_id", None)
    monkeypatch.setattr(settings, "gateway_assume_bedrock_iam", True)
    assert env_configured_providers() == {Provider.OPENAI, Provider.BEDROCK}
    creds = {Provider.ANTHROPIC: AnthropicOrgCredentials(api_key="sk-ant-org")}
    assert usable_providers(creds) == {Provider.ANTHROPIC, Provider.OPENAI, Provider.BEDROCK}


def test_override_seam_bypasses_credential_plumbing(_direct: None) -> None:
    sentinel = object()
    build = gw_api._transport_factory(_request(override=lambda route: sentinel), {})
    assert build(_route(Provider.ANTHROPIC)) is sentinel
