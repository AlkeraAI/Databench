"""Every Bedrock client signs with its own credentials, never a neighbour's.

botocore prefers a bearer token found in ``AWS_BEARER_TOKEN_BEDROCK`` over SigV4
unless the client pins its signature version in code, and explicit access keys do
not count as pinning. So a platform bearer that reaches the process environment
(exported by the gateway, or injected by the deployment) would sign an org's BYOK
request. These tests build real aioboto3 clients with the production factory,
capture the signed request at ``before-send`` and abort it there, so nothing
leaves the process.

All credential values below are fakes.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from model_gateway.adapters import aioboto3_bedrock_client_factory

PLATFORM_BEARER = "fake-platform-bearer-0000"
OTHER_BEARER = "fake-second-bearer-1111"
ORG_KEY_ID = "AKIAFAKEORGKEY000001"
ORG_SECRET = "fake-org-secret"
ENV_KEY_ID = "AKIAFAKEENVKEY000002"


class _Captured(Exception):  # noqa: N818 - control flow, not an error
    def __init__(self, authorization: str) -> None:
        super().__init__("request captured before send")
        self.authorization = authorization


def _abort_with_auth(request: Any, **_kwargs: Any) -> None:
    raw = request.headers.get("Authorization", b"")
    raise _Captured(raw.decode() if isinstance(raw, bytes) else str(raw))


async def _signed_authorization(factory: Any) -> str:
    async with factory() as client:
        client.meta.events.register("before-send", _abort_with_auth)
        with pytest.raises(_Captured) as captured:
            await client.invoke_model_with_response_stream(
                modelId="anthropic.claude-fake", body=b"{}"
            )
    return captured.value.authorization


@pytest.fixture(autouse=True)
def _isolated_aws_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """No ambient profile, key pair or bearer from the developer's machine; the
    default chain finds only the stand-in key pair below."""
    for name in list(os.environ):
        if name.startswith("AWS_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", ENV_KEY_ID)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake-env-secret")


def _platform_factory() -> Any:
    return aioboto3_bedrock_client_factory(region="us-east-1", bearer_token=PLATFORM_BEARER)


def _org_factory() -> Any:
    return aioboto3_bedrock_client_factory(
        region="eu-west-1", aws_access_key_id=ORG_KEY_ID, aws_secret_access_key=ORG_SECRET
    )


@pytest.mark.asyncio
async def test_platform_client_signs_with_its_bearer() -> None:
    assert await _signed_authorization(_platform_factory()) == f"Bearer {PLATFORM_BEARER}"


@pytest.mark.asyncio
async def test_org_keys_sign_sigv4_after_a_platform_bearer_client_ran() -> None:
    """The cross-tenant case: platform and BYOK clients in one process."""
    await _signed_authorization(_platform_factory())
    auth = await _signed_authorization(_org_factory())
    assert auth.startswith(f"AWS4-HMAC-SHA256 Credential={ORG_KEY_ID}/")
    assert PLATFORM_BEARER not in auth


@pytest.mark.parametrize(
    ("factory_kwargs", "expected_key_id"),
    [
        pytest.param(
            {"aws_access_key_id": ORG_KEY_ID, "aws_secret_access_key": ORG_SECRET},
            ORG_KEY_ID,
            id="org-static-keys",
        ),
        pytest.param({}, ENV_KEY_ID, id="credential-chain"),
    ],
)
@pytest.mark.asyncio
async def test_a_bearer_in_the_environment_never_outranks_sigv4(
    monkeypatch: pytest.MonkeyPatch, factory_kwargs: dict[str, str], expected_key_id: str
) -> None:
    """A deployment can inject the bearer straight into the environment; a client
    built without a bearer must still sign with its own SigV4 credentials."""
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", PLATFORM_BEARER)
    factory = aioboto3_bedrock_client_factory(region="eu-west-1", **factory_kwargs)
    auth = await _signed_authorization(factory)
    assert auth.startswith(f"AWS4-HMAC-SHA256 Credential={expected_key_id}/")


@pytest.mark.asyncio
async def test_two_bearer_clients_each_carry_their_own_token() -> None:
    assert await _signed_authorization(_platform_factory()) == f"Bearer {PLATFORM_BEARER}"
    other = aioboto3_bedrock_client_factory(region="us-east-1", bearer_token=OTHER_BEARER)
    assert await _signed_authorization(other) == f"Bearer {OTHER_BEARER}"


@pytest.mark.asyncio
async def test_building_a_bearer_client_leaves_the_environment_untouched() -> None:
    before = dict(os.environ)
    await _signed_authorization(_platform_factory())
    assert dict(os.environ) == before
