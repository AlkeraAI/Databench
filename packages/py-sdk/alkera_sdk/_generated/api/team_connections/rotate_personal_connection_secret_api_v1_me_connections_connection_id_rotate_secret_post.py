from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.team_connection_read import TeamConnectionRead
from ...models.team_connection_rotate_secret_request import TeamConnectionRotateSecretRequest
from ...types import Response


def _get_kwargs(
    connection_id: UUID,
    *,
    body: TeamConnectionRotateSecretRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/me/connections/{connection_id}/rotate-secret".format(
            connection_id=quote(str(connection_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | TeamConnectionRead | None:
    if response.status_code == 200:
        response_200 = TeamConnectionRead.from_dict(response.json())

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
) -> Response[ErrorEnvelope | TeamConnectionRead]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    connection_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: TeamConnectionRotateSecretRequest,
) -> Response[ErrorEnvelope | TeamConnectionRead]:
    """Rotate Personal Connection Secret

     Replace the stored credential on one of this person's own connections.

    Args:
        connection_id (UUID):
        body (TeamConnectionRotateSecretRequest): Rotate only the primary shared credential.

            Named roles change through the connection edit that owns their endpoint.
            This distinct verb records primary rotations and bumps the bundle version.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | TeamConnectionRead]
    """

    kwargs = _get_kwargs(
        connection_id=connection_id,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    connection_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: TeamConnectionRotateSecretRequest,
) -> ErrorEnvelope | TeamConnectionRead | None:
    """Rotate Personal Connection Secret

     Replace the stored credential on one of this person's own connections.

    Args:
        connection_id (UUID):
        body (TeamConnectionRotateSecretRequest): Rotate only the primary shared credential.

            Named roles change through the connection edit that owns their endpoint.
            This distinct verb records primary rotations and bumps the bundle version.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | TeamConnectionRead
    """

    return sync_detailed(
        connection_id=connection_id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    connection_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: TeamConnectionRotateSecretRequest,
) -> Response[ErrorEnvelope | TeamConnectionRead]:
    """Rotate Personal Connection Secret

     Replace the stored credential on one of this person's own connections.

    Args:
        connection_id (UUID):
        body (TeamConnectionRotateSecretRequest): Rotate only the primary shared credential.

            Named roles change through the connection edit that owns their endpoint.
            This distinct verb records primary rotations and bumps the bundle version.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | TeamConnectionRead]
    """

    kwargs = _get_kwargs(
        connection_id=connection_id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    connection_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: TeamConnectionRotateSecretRequest,
) -> ErrorEnvelope | TeamConnectionRead | None:
    """Rotate Personal Connection Secret

     Replace the stored credential on one of this person's own connections.

    Args:
        connection_id (UUID):
        body (TeamConnectionRotateSecretRequest): Rotate only the primary shared credential.

            Named roles change through the connection edit that owns their endpoint.
            This distinct verb records primary rotations and bumps the bundle version.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | TeamConnectionRead
    """

    return (
        await asyncio_detailed(
            connection_id=connection_id,
            client=client,
            body=body,
        )
    ).parsed
