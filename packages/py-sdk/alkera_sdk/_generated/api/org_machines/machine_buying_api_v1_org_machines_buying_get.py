from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.machine_buying_read import MachineBuyingRead
from ...types import Response


def _get_kwargs() -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/org/machines/buying",
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> MachineBuyingRead | None:
    if response.status_code == 200:
        response_200 = MachineBuyingRead.from_dict(response.json())

        return response_200

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[MachineBuyingRead]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[MachineBuyingRead]:
    """Machine Buying

     Whether the org may buy another machine now, and if not, why: its plan
    buys none, or it holds as many as it may. Any member may ask, so a team
    admin learns it before trying; the plan itself is not named.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[MachineBuyingRead]
    """

    kwargs = _get_kwargs()

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
) -> MachineBuyingRead | None:
    """Machine Buying

     Whether the org may buy another machine now, and if not, why: its plan
    buys none, or it holds as many as it may. Any member may ask, so a team
    admin learns it before trying; the plan itself is not named.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        MachineBuyingRead
    """

    return sync_detailed(
        client=client,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[MachineBuyingRead]:
    """Machine Buying

     Whether the org may buy another machine now, and if not, why: its plan
    buys none, or it holds as many as it may. Any member may ask, so a team
    admin learns it before trying; the plan itself is not named.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[MachineBuyingRead]
    """

    kwargs = _get_kwargs()

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
) -> MachineBuyingRead | None:
    """Machine Buying

     Whether the org may buy another machine now, and if not, why: its plan
    buys none, or it holds as many as it may. Any member may ask, so a team
    admin learns it before trying; the plan itself is not named.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        MachineBuyingRead
    """

    return (
        await asyncio_detailed(
            client=client,
        )
    ).parsed
