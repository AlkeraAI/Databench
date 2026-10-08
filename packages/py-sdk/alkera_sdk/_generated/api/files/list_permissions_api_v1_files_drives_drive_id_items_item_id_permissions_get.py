from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.grant_list import GrantList
from ...types import UNSET, Response, Unset


def _get_kwargs(
    drive_id: UUID,
    item_id: UUID,
    *,
    effective: bool | Unset = False,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["effective"] = effective

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/files/drives/{drive_id}/items/{item_id}/permissions".format(
            drive_id=quote(str(drive_id), safe=""),
            item_id=quote(str(item_id), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | GrantList | None:
    if response.status_code == 200:
        response_200 = GrantList.from_dict(response.json())

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
) -> Response[ErrorEnvelope | GrantList]:
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
    effective: bool | Unset = False,
) -> Response[ErrorEnvelope | GrantList]:
    """List Permissions

     The direct grants on this node, or the effective set with its origins.

    Each row says what the caller may do to it and the list says which rungs
    the caller may hand out, so a share dialog renders the server's answer
    rather than re-deciding the ladder.

    Args:
        drive_id (UUID):
        item_id (UUID):
        effective (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | GrantList]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        effective=effective,
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
    effective: bool | Unset = False,
) -> ErrorEnvelope | GrantList | None:
    """List Permissions

     The direct grants on this node, or the effective set with its origins.

    Each row says what the caller may do to it and the list says which rungs
    the caller may hand out, so a share dialog renders the server's answer
    rather than re-deciding the ladder.

    Args:
        drive_id (UUID):
        item_id (UUID):
        effective (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | GrantList
    """

    return sync_detailed(
        drive_id=drive_id,
        item_id=item_id,
        client=client,
        effective=effective,
    ).parsed


async def asyncio_detailed(
    drive_id: UUID,
    item_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    effective: bool | Unset = False,
) -> Response[ErrorEnvelope | GrantList]:
    """List Permissions

     The direct grants on this node, or the effective set with its origins.

    Each row says what the caller may do to it and the list says which rungs
    the caller may hand out, so a share dialog renders the server's answer
    rather than re-deciding the ladder.

    Args:
        drive_id (UUID):
        item_id (UUID):
        effective (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | GrantList]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        effective=effective,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: UUID,
    item_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    effective: bool | Unset = False,
) -> ErrorEnvelope | GrantList | None:
    """List Permissions

     The direct grants on this node, or the effective set with its origins.

    Each row says what the caller may do to it and the list says which rungs
    the caller may hand out, so a share dialog renders the server's answer
    rather than re-deciding the ladder.

    Args:
        drive_id (UUID):
        item_id (UUID):
        effective (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | GrantList
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            client=client,
            effective=effective,
        )
    ).parsed
