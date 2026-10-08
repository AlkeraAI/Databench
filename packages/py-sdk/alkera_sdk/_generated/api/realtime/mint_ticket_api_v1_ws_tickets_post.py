from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.ws_ticket_response import WsTicketResponse
from ...types import Response


def _get_kwargs() -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/ws/tickets",
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> WsTicketResponse | None:
    if response.status_code == 200:
        response_200 = WsTicketResponse.from_dict(response.json())

        return response_200

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[WsTicketResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[WsTicketResponse]:
    """Mint a single-use socket ticket

     A ticket for the caller: a person's, bound to the session this request
    authenticated with, or — for a box on its own machine credential — a
    machine ticket bound to that credential. Every other credential is refused
    where ``CurrentUser`` refuses it.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[WsTicketResponse]
    """

    kwargs = _get_kwargs()

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
) -> WsTicketResponse | None:
    """Mint a single-use socket ticket

     A ticket for the caller: a person's, bound to the session this request
    authenticated with, or — for a box on its own machine credential — a
    machine ticket bound to that credential. Every other credential is refused
    where ``CurrentUser`` refuses it.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        WsTicketResponse
    """

    return sync_detailed(
        client=client,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[WsTicketResponse]:
    """Mint a single-use socket ticket

     A ticket for the caller: a person's, bound to the session this request
    authenticated with, or — for a box on its own machine credential — a
    machine ticket bound to that credential. Every other credential is refused
    where ``CurrentUser`` refuses it.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[WsTicketResponse]
    """

    kwargs = _get_kwargs()

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
) -> WsTicketResponse | None:
    """Mint a single-use socket ticket

     A ticket for the caller: a person's, bound to the session this request
    authenticated with, or — for a box on its own machine credential — a
    machine ticket bound to that credential. Every other credential is refused
    where ``CurrentUser`` refuses it.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        WsTicketResponse
    """

    return (
        await asyncio_detailed(
            client=client,
        )
    ).parsed
