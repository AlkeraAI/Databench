from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.lease_row import LeaseRow
from ...types import UNSET, Response, Unset


def _get_kwargs(
    drive_id: UUID,
    *,
    mine: bool | Unset = False,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["mine"] = mine

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/files/drives/{drive_id}/leases".format(
            drive_id=quote(str(drive_id), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | list[LeaseRow] | None:
    if response.status_code == 200:
        response_200 = []
        _response_200 = response.json()
        for response_200_item_data in _response_200:
            response_200_item = LeaseRow.from_dict(response_200_item_data)

            response_200.append(response_200_item)

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
) -> Response[ErrorEnvelope | list[LeaseRow]]:
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
    mine: bool | Unset = False,
) -> Response[ErrorEnvelope | list[LeaseRow]]:
    """My Leases

     My mounts across the drive. ``mine=true`` is required on day one: a
    drive-wide listing has to be filtered by readability per row, and answering
    the unfiltered question badly would list folders the caller cannot see.

    The drive is resolved first, before the flag and before the caller's own id
    is looked at, so a stranger's drive answers the opaque 404 rather than
    telling them which argument they got wrong.

    Args:
        drive_id (UUID):
        mine (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | list[LeaseRow]]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        mine=mine,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    drive_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    mine: bool | Unset = False,
) -> ErrorEnvelope | list[LeaseRow] | None:
    """My Leases

     My mounts across the drive. ``mine=true`` is required on day one: a
    drive-wide listing has to be filtered by readability per row, and answering
    the unfiltered question badly would list folders the caller cannot see.

    The drive is resolved first, before the flag and before the caller's own id
    is looked at, so a stranger's drive answers the opaque 404 rather than
    telling them which argument they got wrong.

    Args:
        drive_id (UUID):
        mine (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | list[LeaseRow]
    """

    return sync_detailed(
        drive_id=drive_id,
        client=client,
        mine=mine,
    ).parsed


async def asyncio_detailed(
    drive_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    mine: bool | Unset = False,
) -> Response[ErrorEnvelope | list[LeaseRow]]:
    """My Leases

     My mounts across the drive. ``mine=true`` is required on day one: a
    drive-wide listing has to be filtered by readability per row, and answering
    the unfiltered question badly would list folders the caller cannot see.

    The drive is resolved first, before the flag and before the caller's own id
    is looked at, so a stranger's drive answers the opaque 404 rather than
    telling them which argument they got wrong.

    Args:
        drive_id (UUID):
        mine (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | list[LeaseRow]]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        mine=mine,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    mine: bool | Unset = False,
) -> ErrorEnvelope | list[LeaseRow] | None:
    """My Leases

     My mounts across the drive. ``mine=true`` is required on day one: a
    drive-wide listing has to be filtered by readability per row, and answering
    the unfiltered question badly would list folders the caller cannot see.

    The drive is resolved first, before the flag and before the caller's own id
    is looked at, so a stranger's drive answers the opaque 404 rather than
    telling them which argument they got wrong.

    Args:
        drive_id (UUID):
        mine (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | list[LeaseRow]
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            client=client,
            mine=mine,
        )
    ).parsed
