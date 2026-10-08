from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.lease_row import LeaseRow
from ...types import Response


def _get_kwargs(
    drive_id: UUID,
    item_id: UUID,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/files/drives/{drive_id}/items/{item_id}/lease/request-release".format(
            drive_id=quote(str(drive_id), safe=""),
            item_id=quote(str(item_id), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | LeaseRow | None:
    if response.status_code == 200:
        response_200 = LeaseRow.from_dict(response.json())

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
) -> Response[ErrorEnvelope | LeaseRow]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    drive_id: UUID,
    item_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | LeaseRow]:
    """Request Release

     Ask the holder for the folder back. A reader may do this, and the answer
    names the holder — a reader who may be told "Ana has it" is a reader who may
    already read the folder.

    Args:
        drive_id (UUID):
        item_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | LeaseRow]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    drive_id: UUID,
    item_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | LeaseRow | None:
    """Request Release

     Ask the holder for the folder back. A reader may do this, and the answer
    names the holder — a reader who may be told "Ana has it" is a reader who may
    already read the folder.

    Args:
        drive_id (UUID):
        item_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | LeaseRow
    """

    return sync_detailed(
        drive_id=drive_id,
        item_id=item_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    drive_id: UUID,
    item_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | LeaseRow]:
    """Request Release

     Ask the holder for the folder back. A reader may do this, and the answer
    names the holder — a reader who may be told "Ana has it" is a reader who may
    already read the folder.

    Args:
        drive_id (UUID):
        item_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | LeaseRow]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: UUID,
    item_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | LeaseRow | None:
    """Request Release

     Ask the holder for the folder back. A reader may do this, and the answer
    names the holder — a reader who may be told "Ana has it" is a reader who may
    already read the folder.

    Args:
        drive_id (UUID):
        item_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | LeaseRow
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            client=client,
        )
    ).parsed
