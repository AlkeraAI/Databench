from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.items_page import ItemsPage
from ...types import Response


def _get_kwargs(
    drive_id: str,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/files/drives/{drive_id}/starred".format(
            drive_id=quote(str(drive_id), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | ItemsPage | None:
    if response.status_code == 200:
        response_200 = ItemsPage.from_dict(response.json())

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
) -> Response[ErrorEnvelope | ItemsPage]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    drive_id: str,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | ItemsPage]:
    """Starred

     The caller's starred nodes.

    Args:
        drive_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | ItemsPage]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    drive_id: str,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | ItemsPage | None:
    """Starred

     The caller's starred nodes.

    Args:
        drive_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | ItemsPage
    """

    return sync_detailed(
        drive_id=drive_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    drive_id: str,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | ItemsPage]:
    """Starred

     The caller's starred nodes.

    Args:
        drive_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | ItemsPage]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: str,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | ItemsPage | None:
    """Starred

     The caller's starred nodes.

    Args:
        drive_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | ItemsPage
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            client=client,
        )
    ).parsed
