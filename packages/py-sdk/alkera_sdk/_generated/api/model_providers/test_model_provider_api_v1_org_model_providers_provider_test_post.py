from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.model_provider_test_result import ModelProviderTestResult
from ...models.provider import Provider
from ...types import Response


def _get_kwargs(
    provider: Provider,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/org/model-providers/{provider}/test".format(
            provider=quote(str(provider), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | ModelProviderTestResult | None:
    if response.status_code == 200:
        response_200 = ModelProviderTestResult.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ErrorEnvelope.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ErrorEnvelope | ModelProviderTestResult]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    provider: Provider,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | ModelProviderTestResult]:
    """Test Model Provider

     A cheap REAL provider call with the STORED credentials (save first, then
    test). 200 even on a failed test — the failure IS the result.

    Args:
        provider (Provider): Upstream LLM provider a request is fulfilled by (route + cost
            keying).

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | ModelProviderTestResult]
    """

    kwargs = _get_kwargs(
        provider=provider,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    provider: Provider,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | ModelProviderTestResult | None:
    """Test Model Provider

     A cheap REAL provider call with the STORED credentials (save first, then
    test). 200 even on a failed test — the failure IS the result.

    Args:
        provider (Provider): Upstream LLM provider a request is fulfilled by (route + cost
            keying).

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | ModelProviderTestResult
    """

    return sync_detailed(
        provider=provider,
        client=client,
    ).parsed


async def asyncio_detailed(
    provider: Provider,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | ModelProviderTestResult]:
    """Test Model Provider

     A cheap REAL provider call with the STORED credentials (save first, then
    test). 200 even on a failed test — the failure IS the result.

    Args:
        provider (Provider): Upstream LLM provider a request is fulfilled by (route + cost
            keying).

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | ModelProviderTestResult]
    """

    kwargs = _get_kwargs(
        provider=provider,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    provider: Provider,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | ModelProviderTestResult | None:
    """Test Model Provider

     A cheap REAL provider call with the STORED credentials (save first, then
    test). 200 even on a failed test — the failure IS the result.

    Args:
        provider (Provider): Upstream LLM provider a request is fulfilled by (route + cost
            keying).

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | ModelProviderTestResult
    """

    return (
        await asyncio_detailed(
            provider=provider,
            client=client,
        )
    ).parsed
