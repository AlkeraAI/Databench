from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.team_connection_read import TeamConnectionRead
from ...types import Response


def _get_kwargs() -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/me/connections",
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> list[TeamConnectionRead] | None:
    if response.status_code == 200:
        response_200 = []
        _response_200 = response.json()
        for response_200_item_data in _response_200:
            response_200_item = TeamConnectionRead.from_dict(response_200_item_data)

            response_200.append(response_200_item)

        return response_200

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[list[TeamConnectionRead]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[list[TeamConnectionRead]]:
    """My Connections

     Every connection this person can use, in one list.

    Their own personal rows, the rows configured by every team they belong to,
    AND the rows of every team they administer — an admin who adds a sub-team's
    connection from this page has to find it on this page afterwards, and the
    sub-team is one they usually have no membership row on. Each row is stamped
    with whether THEY may change it — the owner of a personal row, an admin of
    the owning team or of any team above it. A member who can only use a row
    still gets the whole definition, because that is what the row's dialog reads
    to explain what it connects to.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[list[TeamConnectionRead]]
    """

    kwargs = _get_kwargs()

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
) -> list[TeamConnectionRead] | None:
    """My Connections

     Every connection this person can use, in one list.

    Their own personal rows, the rows configured by every team they belong to,
    AND the rows of every team they administer — an admin who adds a sub-team's
    connection from this page has to find it on this page afterwards, and the
    sub-team is one they usually have no membership row on. Each row is stamped
    with whether THEY may change it — the owner of a personal row, an admin of
    the owning team or of any team above it. A member who can only use a row
    still gets the whole definition, because that is what the row's dialog reads
    to explain what it connects to.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        list[TeamConnectionRead]
    """

    return sync_detailed(
        client=client,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[list[TeamConnectionRead]]:
    """My Connections

     Every connection this person can use, in one list.

    Their own personal rows, the rows configured by every team they belong to,
    AND the rows of every team they administer — an admin who adds a sub-team's
    connection from this page has to find it on this page afterwards, and the
    sub-team is one they usually have no membership row on. Each row is stamped
    with whether THEY may change it — the owner of a personal row, an admin of
    the owning team or of any team above it. A member who can only use a row
    still gets the whole definition, because that is what the row's dialog reads
    to explain what it connects to.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[list[TeamConnectionRead]]
    """

    kwargs = _get_kwargs()

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
) -> list[TeamConnectionRead] | None:
    """My Connections

     Every connection this person can use, in one list.

    Their own personal rows, the rows configured by every team they belong to,
    AND the rows of every team they administer — an admin who adds a sub-team's
    connection from this page has to find it on this page afterwards, and the
    sub-team is one they usually have no membership row on. Each row is stamped
    with whether THEY may change it — the owner of a personal row, an admin of
    the owning team or of any team above it. A member who can only use a row
    still gets the whole definition, because that is what the row's dialog reads
    to explain what it connects to.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        list[TeamConnectionRead]
    """

    return (
        await asyncio_detailed(
            client=client,
        )
    ).parsed
