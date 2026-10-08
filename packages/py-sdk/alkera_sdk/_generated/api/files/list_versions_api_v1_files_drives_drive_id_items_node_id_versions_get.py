from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.version_list import VersionList
from ...types import Response


def _get_kwargs(
    drive_id: UUID,
    node_id: UUID,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/files/drives/{drive_id}/items/{node_id}/versions".format(
            drive_id=quote(str(drive_id), safe=""),
            node_id=quote(str(node_id), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | VersionList | None:
    if response.status_code == 200:
        response_200 = VersionList.from_dict(response.json())

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
) -> Response[ErrorEnvelope | VersionList]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    drive_id: UUID,
    node_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | VersionList]:
    """List Versions

     Every version of this file, oldest first.

    Args:
        drive_id (UUID):
        node_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | VersionList]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        node_id=node_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    drive_id: UUID,
    node_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | VersionList | None:
    """List Versions

     Every version of this file, oldest first.

    Args:
        drive_id (UUID):
        node_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | VersionList
    """

    return sync_detailed(
        drive_id=drive_id,
        node_id=node_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    drive_id: UUID,
    node_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | VersionList]:
    """List Versions

     Every version of this file, oldest first.

    Args:
        drive_id (UUID):
        node_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | VersionList]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        node_id=node_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: UUID,
    node_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | VersionList | None:
    """List Versions

     Every version of this file, oldest first.

    Args:
        drive_id (UUID):
        node_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | VersionList
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            node_id=node_id,
            client=client,
        )
    ).parsed
