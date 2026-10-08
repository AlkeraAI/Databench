from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.live_inbound_page import LiveInboundPage
from ...types import UNSET, Response, Unset


def _get_kwargs(
    drive_id: UUID,
    item_id: UUID,
    *,
    inbound: bool | Unset = True,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["inbound"] = inbound

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/files/drives/{drive_id}/items/{item_id}/lease/live".format(
            drive_id=quote(str(drive_id), safe=""),
            item_id=quote(str(item_id), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | LiveInboundPage | None:
    if response.status_code == 200:
        response_200 = LiveInboundPage.from_dict(response.json())

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
) -> Response[ErrorEnvelope | LiveInboundPage]:
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
    inbound: bool | Unset = True,
) -> Response[ErrorEnvelope | LiveInboundPage]:
    """Live Inbound

     What the drive took into the holder's folder on its behalf — the files
    a person dropped into the chat while the box ran — oldest first, for the
    box to apply on its next drain.

    Read under the holder's fence the way a write is: a caller with no epoch,
    a superseded epoch or another instance is a stranger to the plane and is
    told so, never shown what is waiting. The inbound page is the only page of
    the plane this route serves; ``inbound`` is the flag the holder spells.

    Args:
        drive_id (UUID):
        item_id (UUID):
        inbound (bool | Unset):  Default: True.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | LiveInboundPage]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        inbound=inbound,
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
    inbound: bool | Unset = True,
) -> ErrorEnvelope | LiveInboundPage | None:
    """Live Inbound

     What the drive took into the holder's folder on its behalf — the files
    a person dropped into the chat while the box ran — oldest first, for the
    box to apply on its next drain.

    Read under the holder's fence the way a write is: a caller with no epoch,
    a superseded epoch or another instance is a stranger to the plane and is
    told so, never shown what is waiting. The inbound page is the only page of
    the plane this route serves; ``inbound`` is the flag the holder spells.

    Args:
        drive_id (UUID):
        item_id (UUID):
        inbound (bool | Unset):  Default: True.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | LiveInboundPage
    """

    return sync_detailed(
        drive_id=drive_id,
        item_id=item_id,
        client=client,
        inbound=inbound,
    ).parsed


async def asyncio_detailed(
    drive_id: UUID,
    item_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    inbound: bool | Unset = True,
) -> Response[ErrorEnvelope | LiveInboundPage]:
    """Live Inbound

     What the drive took into the holder's folder on its behalf — the files
    a person dropped into the chat while the box ran — oldest first, for the
    box to apply on its next drain.

    Read under the holder's fence the way a write is: a caller with no epoch,
    a superseded epoch or another instance is a stranger to the plane and is
    told so, never shown what is waiting. The inbound page is the only page of
    the plane this route serves; ``inbound`` is the flag the holder spells.

    Args:
        drive_id (UUID):
        item_id (UUID):
        inbound (bool | Unset):  Default: True.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | LiveInboundPage]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        inbound=inbound,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: UUID,
    item_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    inbound: bool | Unset = True,
) -> ErrorEnvelope | LiveInboundPage | None:
    """Live Inbound

     What the drive took into the holder's folder on its behalf — the files
    a person dropped into the chat while the box ran — oldest first, for the
    box to apply on its next drain.

    Read under the holder's fence the way a write is: a caller with no epoch,
    a superseded epoch or another instance is a stranger to the plane and is
    told so, never shown what is waiting. The inbound page is the only page of
    the plane this route serves; ``inbound`` is the flag the holder spells.

    Args:
        drive_id (UUID):
        item_id (UUID):
        inbound (bool | Unset):  Default: True.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | LiveInboundPage
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            client=client,
            inbound=inbound,
        )
    ).parsed
