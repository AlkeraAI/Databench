from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.deletion_request_body import DeletionRequestBody
from ...models.deletion_status_read import DeletionStatusRead
from ...models.error_envelope import ErrorEnvelope
from ...types import Response


def _get_kwargs(
    *,
    body: DeletionRequestBody,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/me/account/deletion",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> DeletionStatusRead | ErrorEnvelope | None:
    if response.status_code == 202:
        response_202 = DeletionStatusRead.from_dict(response.json())

        return response_202

    if response.status_code == 422:
        response_422 = ErrorEnvelope.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[DeletionStatusRead | ErrorEnvelope]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: DeletionRequestBody,
) -> Response[DeletionStatusRead | ErrorEnvelope]:
    """Request Deletion

     Schedule the account's deletion after the grace window.

    The person types their email and proves a current factor. Every other
    credential they hold ends at once (sessions elsewhere, CLI tokens, access
    tokens); this browser stays signed in so they can cancel. 409 with the
    blockers when something must be settled first.

    Args:
        body (DeletionRequestBody):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DeletionStatusRead | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
    body: DeletionRequestBody,
) -> DeletionStatusRead | ErrorEnvelope | None:
    """Request Deletion

     Schedule the account's deletion after the grace window.

    The person types their email and proves a current factor. Every other
    credential they hold ends at once (sessions elsewhere, CLI tokens, access
    tokens); this browser stays signed in so they can cancel. 409 with the
    blockers when something must be settled first.

    Args:
        body (DeletionRequestBody):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DeletionStatusRead | ErrorEnvelope
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: DeletionRequestBody,
) -> Response[DeletionStatusRead | ErrorEnvelope]:
    """Request Deletion

     Schedule the account's deletion after the grace window.

    The person types their email and proves a current factor. Every other
    credential they hold ends at once (sessions elsewhere, CLI tokens, access
    tokens); this browser stays signed in so they can cancel. 409 with the
    blockers when something must be settled first.

    Args:
        body (DeletionRequestBody):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DeletionStatusRead | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: DeletionRequestBody,
) -> DeletionStatusRead | ErrorEnvelope | None:
    """Request Deletion

     Schedule the account's deletion after the grace window.

    The person types their email and proves a current factor. Every other
    credential they hold ends at once (sessions elsewhere, CLI tokens, access
    tokens); this browser stays signed in so they can cancel. 409 with the
    blockers when something must be settled first.

    Args:
        body (DeletionRequestBody):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DeletionStatusRead | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
