"""The shared BYOK provider-credentials contract: row→DTO resolution (fail-closed
per credential), the masked hint, and the probe's failure classification —
everything the backend test button, the gateway, and the health runner share."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from alkera_core.auth.secret_box import encrypt_secret
from alkera_core.model_providers import (
    AnthropicOrgCredentials,
    BedrockOrgCredentials,
    OpenAIOrgCredentials,
    credentials_from_row,
    probe_provider_credentials,
    secret_hint,
)
from alkera_core.models import ModelProviderConfig


def _row(**overrides: Any) -> ModelProviderConfig:
    base: dict[str, Any] = {
        "org_team_id": uuid.uuid4(),
        "provider": "anthropic",
        "enabled": True,
        "api_key_encrypted": encrypt_secret("sk-ant-test-1234abcd"),
    }
    base.update(overrides)
    return ModelProviderConfig(**base)


# --- secret_hint -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("plaintext", "expected"),
    [
        pytest.param("sk-ant-test-1234abcd", "…abcd", id="normal-key"),
        pytest.param("AKIAIOSFODNN7EXAMPLE", "…MPLE", id="aws-key-id"),
        pytest.param("short", "", id="too-short-no-hint"),
        pytest.param("12345678", "…5678", id="exactly-8"),
    ],
)
def test_secret_hint(plaintext: str, expected: str) -> None:
    assert secret_hint(plaintext) == expected


# --- row -> DTO resolution ---------------------------------------------------------


def test_anthropic_row_decrypts_with_default_base_url() -> None:
    creds = credentials_from_row(_row())
    assert isinstance(creds, AnthropicOrgCredentials)
    assert creds.api_key == "sk-ant-test-1234abcd"
    assert creds.base_url == "https://api.anthropic.com"


def test_openai_row_carries_org_id_and_custom_base_url() -> None:
    creds = credentials_from_row(
        _row(
            provider="openai",
            api_key_encrypted=encrypt_secret("sk-proj-xyz-9999"),
            base_url="https://llm.acme.internal/v1",
            openai_organization_id="org-acme",
        )
    )
    assert isinstance(creds, OpenAIOrgCredentials)
    assert creds.api_key == "sk-proj-xyz-9999"
    assert creds.base_url == "https://llm.acme.internal/v1"
    assert creds.organization_id == "org-acme"


def test_bedrock_iam_row_is_credential_less() -> None:
    creds = credentials_from_row(
        _row(
            provider="bedrock",
            api_key_encrypted=None,
            bedrock_region="us-east-1",
            bedrock_auth_mode="iam",
        )
    )
    assert isinstance(creds, BedrockOrgCredentials)
    assert creds.auth_mode == "iam"
    assert creds.aws_access_key_id is None
    assert creds.aws_secret_access_key is None


def test_bedrock_access_key_row_decrypts_both_halves() -> None:
    creds = credentials_from_row(
        _row(
            provider="bedrock",
            api_key_encrypted=None,
            bedrock_region="eu-west-1",
            bedrock_auth_mode="access_key",
            aws_access_key_id_encrypted=encrypt_secret("AKIAEXAMPLE12345"),
            aws_secret_access_key_encrypted=encrypt_secret("wJalrXUtnFEMI/K7MDENG"),
        )
    )
    assert isinstance(creds, BedrockOrgCredentials)
    assert creds.aws_access_key_id == "AKIAEXAMPLE12345"
    assert creds.aws_secret_access_key == "wJalrXUtnFEMI/K7MDENG"


@pytest.mark.parametrize(
    "row",
    [
        pytest.param(_row(enabled=False), id="disabled"),
        pytest.param(_row(api_key_encrypted=None), id="anthropic-no-key"),
        pytest.param(
            _row(provider="bedrock", api_key_encrypted=None, bedrock_region=None),
            id="bedrock-no-region",
        ),
        pytest.param(
            _row(
                provider="bedrock",
                api_key_encrypted=None,
                bedrock_region="us-east-1",
                bedrock_auth_mode="access_key",
                aws_access_key_id_encrypted=encrypt_secret("AKIAEXAMPLE12345"),
                aws_secret_access_key_encrypted=None,
            ),
            id="bedrock-half-a-key-pair",
        ),
        pytest.param(
            _row(api_key_encrypted="not-real-fernet-ciphertext"),
            id="corrupt-ciphertext-fails-closed",
        ),
        pytest.param(_row(provider="futureprovider"), id="unknown-provider"),
    ],
)
def test_incomplete_or_corrupt_rows_resolve_to_none(row: ModelProviderConfig) -> None:
    assert credentials_from_row(row) is None


# --- the probe: classification per failure mode ------------------------------------


def _mock_client(status_code: int) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"data": []})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        pytest.param(200, "ok", id="200-ok"),
        pytest.param(401, "invalid_key", id="401-invalid-key"),
        pytest.param(403, "permission", id="403-permission"),
        pytest.param(429, "ok", id="429-rate-limited-means-authenticated"),
        pytest.param(500, "network", id="500-provider-error"),
    ],
)
async def test_http_probe_classification(status_code: int, expected: str) -> None:
    creds = AnthropicOrgCredentials(api_key="sk-ant-x-12345678")
    async with _mock_client(status_code) as client:
        result = await probe_provider_credentials(creds, http_client=client)
    assert result.classification == expected
    assert "sk-ant" not in result.detail  # never any key material


@pytest.mark.asyncio
async def test_probe_sends_the_right_auth_headers() -> None:
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["req"] = request
        return httpx.Response(200, json={"data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await probe_provider_credentials(
            AnthropicOrgCredentials(api_key="sk-ant-x-12345678"), http_client=client
        )
        anthropic_req = seen["req"]
        await probe_provider_credentials(
            OpenAIOrgCredentials(api_key="sk-proj-99999999", organization_id="org-acme"),
            http_client=client,
        )
        openai_req = seen["req"]

    assert anthropic_req.headers["x-api-key"] == "sk-ant-x-12345678"
    assert anthropic_req.url.path.endswith("/v1/models")
    assert openai_req.headers["Authorization"] == "Bearer sk-proj-99999999"
    assert openai_req.headers["OpenAI-Organization"] == "org-acme"
    assert openai_req.url.path.endswith("/models")


@pytest.mark.asyncio
async def test_probe_network_error_classifies_without_raising() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await probe_provider_credentials(
            OpenAIOrgCredentials(api_key="sk-proj-99999999"), http_client=client
        )
    assert result.classification == "network"


class _FakeAwsError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class NoCredentialsError(Exception):  # name-matched by the classifier
    pass


class EndpointConnectionError(Exception):  # name-matched by the classifier
    pass


def _fake_bedrock_factory(exc: Exception | None) -> Any:
    @asynccontextmanager
    async def _cm(creds: Any) -> Any:
        class _Client:
            async def list_foundation_models(self, **kwargs: Any) -> dict[str, Any]:
                if exc is not None:
                    raise exc
                return {"modelSummaries": []}

        yield _Client()

    return _cm


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        pytest.param(None, "ok", id="success"),
        pytest.param(_FakeAwsError("UnrecognizedClientException"), "invalid_key", id="bad-keys"),
        pytest.param(_FakeAwsError("AccessDeniedException"), "permission", id="denied"),
        pytest.param(NoCredentialsError(), "invalid_key", id="no-ambient-creds"),
        pytest.param(EndpointConnectionError(), "network", id="unreachable"),
        pytest.param(RuntimeError("boom"), "network", id="unknown-error"),
    ],
)
async def test_bedrock_probe_classification(exc: Exception | None, expected: str) -> None:
    creds = BedrockOrgCredentials(region="us-east-1", auth_mode="iam")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200))
    ) as client:
        result = await probe_provider_credentials(
            creds, http_client=client, bedrock_client_factory=_fake_bedrock_factory(exc)
        )
    assert result.classification == expected


@pytest.mark.asyncio
async def test_bedrock_iam_no_creds_detail_names_the_fix() -> None:
    creds = BedrockOrgCredentials(region="us-east-1", auth_mode="iam")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200))
    ) as client:
        result = await probe_provider_credentials(
            creds,
            http_client=client,
            bedrock_client_factory=_fake_bedrock_factory(NoCredentialsError()),
        )
    assert "IAM role" in result.detail
