from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.item import Item
from ...types import Response


def _get_kwargs(
    drive_id: str,
    item_path: str,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/files/drives/{drive_id}/root:/{item_path}".format(
            drive_id=quote(str(drive_id), safe=""),
            item_path=quote(str(item_path), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | Item | None:
    if response.status_code == 200:
        response_200 = Item.from_dict(response.json())

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
) -> Response[ErrorEnvelope | Item]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    drive_id: str,
    item_path: str,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | Item]:
    """Get Item By Path

     The path form. The id form is canonical; this one exists so a client
    that knows only where a thing is can start.

    The resolution is a plain lookup and its answer is then authorized exactly
    as the id form's is — an unreadable node on the way down leaves the same
    404 as a path that does not exist, and the path never becomes a download
    filename because this route returns an item, never bytes.

    Args:
        drive_id (str):
        item_path (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | Item]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_path=item_path,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    drive_id: str,
    item_path: str,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | Item | None:
    """Get Item By Path

     The path form. The id form is canonical; this one exists so a client
    that knows only where a thing is can start.

    The resolution is a plain lookup and its answer is then authorized exactly
    as the id form's is — an unreadable node on the way down leaves the same
    404 as a path that does not exist, and the path never becomes a download
    filename because this route returns an item, never bytes.

    Args:
        drive_id (str):
        item_path (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | Item
    """

    return sync_detailed(
        drive_id=drive_id,
        item_path=item_path,
        client=client,
    ).parsed


async def asyncio_detailed(
    drive_id: str,
    item_path: str,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | Item]:
    """Get Item By Path

     The path form. The id form is canonical; this one exists so a client
    that knows only where a thing is can start.

    The resolution is a plain lookup and its answer is then authorized exactly
    as the id form's is — an unreadable node on the way down leaves the same
    404 as a path that does not exist, and the path never becomes a download
    filename because this route returns an item, never bytes.

    Args:
        drive_id (str):
        item_path (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | Item]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_path=item_path,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: str,
    item_path: str,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | Item | None:
    """Get Item By Path

     The path form. The id form is canonical; this one exists so a client
    that knows only where a thing is can start.

    The resolution is a plain lookup and its answer is then authorized exactly
    as the id form's is — an unreadable node on the way down leaves the same
    404 as a path that does not exist, and the path never becomes a download
    filename because this route returns an item, never bytes.

    Args:
        drive_id (str):
        item_path (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | Item
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_path=item_path,
            client=client,
        )
    ).parsed
