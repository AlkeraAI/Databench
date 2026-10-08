"""A Bedrock refusal must not hand the caller our AWS identity.

botocore renders the calling principal's full ARN into the message of an
authorization failure ("User: arn:aws:sts::<account>:assumed-role/<role>/<id> is
not authorized to perform: bedrock:..."). That message used to become the upstream
error body verbatim, and the pipeline relays an upstream error body to the client
in-band on the SSE stream — so any authenticated caller who selected a
Bedrock-routed model could read back the account number and the exact IAM role to
target.

The wording still has to survive redaction: Bedrock signals an over-long prompt
only in prose, and the context-overflow classifier reads that prose to tell the
client to compact.
"""

from __future__ import annotations

import json

import pytest
from alkera_core.overflow import CONTEXT_LENGTH_EXCEEDED_CODE, is_context_overflow
from botocore.exceptions import ClientError
from httpx import AsyncClient
from model_gateway.adapters import BedrockInvokeTransport, UpstreamError

_ACCOUNT = "123456789012"
_ROLE_ARN = f"arn:aws:sts::{_ACCOUNT}:assumed-role/example-gateway-task-role/9f3c"
_DENIED = (
    "An error occurred (AccessDeniedException) when calling the "
    f"InvokeModelWithResponseStream operation: User: {_ROLE_ARN} is not authorized "
    "to perform: bedrock:InvokeModelWithResponseStream on resource: "
    f"arn:aws:bedrock:us-east-1:{_ACCOUNT}:inference-profile/us.anthropic.claude"
)


def _client_error(code: str, message: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message}}, "InvokeModelWithResponseStream"
    )


async def _raise_from(exc: Exception, client_cls) -> UpstreamError:
    transport = BedrockInvokeTransport(client_factory=lambda: client_cls([], error=exc))
    with pytest.raises(UpstreamError) as info:
        async with transport.stream(
            upstream_model_id="m", region=None, body={"messages": []}
        ) as chunks:
            async for _ in chunks:  # pragma: no cover - the open itself raises
                pass
    return info.value


@pytest.mark.asyncio
async def test_an_authorization_failure_does_not_carry_our_arn(bedrock_client_cls) -> None:
    err = await _raise_from(_client_error("AccessDeniedException", _DENIED), bedrock_client_cls)

    body = err.body.decode()
    assert "arn:aws" not in body
    assert _ACCOUNT not in body
    assert "example-gateway-task-role" not in body
    # The same holds for what actually reaches the client, which is `detail`.
    assert "arn:aws" not in err.detail
    assert _ACCOUNT not in err.detail


@pytest.mark.asyncio
async def test_the_caller_still_learns_which_bedrock_error_it_was(bedrock_client_cls) -> None:
    """Redaction must leave something actionable — an opaque body turns every
    Bedrock misconfiguration into an unattributable failure for the operator."""
    err = await _raise_from(_client_error("AccessDeniedException", _DENIED), bedrock_client_cls)

    assert err.status_code == 400
    assert json.loads(err.body)["error"]["type"] == "AccessDeniedException"
    assert "AccessDeniedException" in err.detail
    assert "not authorized" in err.detail


@pytest.mark.asyncio
async def test_an_over_long_prompt_still_classifies_as_a_context_overflow(
    bedrock_client_cls,
) -> None:
    """The classifier keys off Bedrock's own wording, so scrubbing must not take
    the wording with it — otherwise the client loses its auto-compaction signal."""
    message = (
        "An error occurred (ValidationException) when calling the "
        "InvokeModelWithResponseStream operation: input is too long for requested model"
    )
    err = await _raise_from(_client_error("ValidationException", message), bedrock_client_cls)

    assert is_context_overflow(err.status_code, err.detail, code=err.error_code)


@pytest.mark.asyncio
async def test_a_retryable_bedrock_fault_keeps_its_status_and_hint(bedrock_client_cls) -> None:
    """Scrubbing the body must not disturb the retry/failover decision, which is
    driven by the status and the Retry-After hint."""
    exc = ClientError(
        {
            "Error": {"Code": "ThrottlingException", "Message": "slow down"},
            "ResponseMetadata": {"HTTPHeaders": {"retry-after": "7"}},
        },
        "InvokeModelWithResponseStream",
    )
    err = await _raise_from(exc, bedrock_client_cls)

    assert err.status_code == 429
    assert err.retryable is True
    assert err.retry_after == 7.0


@pytest.mark.asyncio
async def test_the_error_the_client_reads_off_the_stream_is_scrubbed(
    gateway_client: AsyncClient, seed, install_bedrock, bedrock_client_cls
) -> None:
    """End to end: the whole relayed SSE body, as a caller actually sees it."""
    s = await seed(granted_nanos=10**12)
    install_bedrock(bedrock_client_cls([], error=_client_error("AccessDeniedException", _DENIED)))

    resp = await gateway_client.post(
        "/anthropic/v1/messages",
        headers={"Authorization": f"Bearer {s.token}"},
        json={
            "model": s.model_id,
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "x"}],
        },
    )

    assert resp.status_code == 200  # the error rides in-band on the SSE stream
    text = resp.text
    assert "arn:aws" not in text
    assert _ACCOUNT not in text
    assert "example-gateway-task-role" not in text
    assert "AccessDeniedException" in text  # still says what went wrong
    assert CONTEXT_LENGTH_EXCEEDED_CODE not in text  # and is not misclassified
