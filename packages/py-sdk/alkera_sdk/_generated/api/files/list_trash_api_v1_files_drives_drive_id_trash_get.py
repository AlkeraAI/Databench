from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.trash_page import TrashPage
from ...types import UNSET, Response, Unset


def _get_kwargs(
    drive_id: UUID,
    *,
    marker: None | str | Unset = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    json_marker: None | str | Unset
    if isinstance(marker, Unset):
        json_marker = UNSET
    else:
        json_marker = marker
    params["marker"] = json_marker

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/files/drives/{drive_id}/trash".format(
            drive_id=quote(str(drive_id), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | TrashPage | None:
    if response.status_code == 200:
        response_200 = TrashPage.from_dict(response.json())

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
) -> Response[ErrorEnvelope | TrashPage]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    drive_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    marker: None | str | Unset = UNSET,
) -> Response[ErrorEnvelope | TrashPage]:
    """List Trash

     One page of this drive's trashed roots, filtered to what the caller sees.

    Args:
        drive_id (UUID):
        marker (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | TrashPage]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        marker=marker,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    drive_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    marker: None | str | Unset = UNSET,
) -> ErrorEnvelope | TrashPage | None:
    """List Trash

     One page of this drive's trashed roots, filtered to what the caller sees.

    Args:
        drive_id (UUID):
        marker (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | TrashPage
    """

    return sync_detailed(
        drive_id=drive_id,
        client=client,
        marker=marker,
    ).parsed


async def asyncio_detailed(
    drive_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    marker: None | str | Unset = UNSET,
) -> Response[ErrorEnvelope | TrashPage]:
    """List Trash

     One page of this drive's trashed roots, filtered to what the caller sees.

    Args:
        drive_id (UUID):
        marker (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | TrashPage]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        marker=marker,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    marker: None | str | Unset = UNSET,
) -> ErrorEnvelope | TrashPage | None:
    """List Trash

     One page of this drive's trashed roots, filtered to what the caller sees.

    Args:
        drive_id (UUID):
        marker (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | TrashPage
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            client=client,
            marker=marker,
        )
    ).parsed
