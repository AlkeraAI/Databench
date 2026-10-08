from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.member_team_connections_response import MemberTeamConnectionsResponse
from ...types import Response


def _get_kwargs() -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/me/team-connections",
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> MemberTeamConnectionsResponse | None:
    if response.status_code == 200:
        response_200 = MemberTeamConnectionsResponse.from_dict(response.json())

        return response_200

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[MemberTeamConnectionsResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[MemberTeamConnectionsResponse]:
    """My Team Connections

     What this member's daemon reconciles into their workspace.

    Their own personal connections ride the same route as the team's: a daemon
    that syncs one syncs the other, and neither needs a second pass nor a second
    shape to know what to materialize.

    Usage follows the same visibility the Connections page has: the rows of
    every team this person belongs to AND of every team they administer by
    descent. The org admin who configures a sub-team's connection usually holds
    no membership row on that team (joining materializes rows upward, never
    down), and a listing that followed membership alone left them unable to use
    — or test — the connection they had just verified and saved.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[MemberTeamConnectionsResponse]
    """

    kwargs = _get_kwargs()

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
) -> MemberTeamConnectionsResponse | None:
    """My Team Connections

     What this member's daemon reconciles into their workspace.

    Their own personal connections ride the same route as the team's: a daemon
    that syncs one syncs the other, and neither needs a second pass nor a second
    shape to know what to materialize.

    Usage follows the same visibility the Connections page has: the rows of
    every team this person belongs to AND of every team they administer by
    descent. The org admin who configures a sub-team's connection usually holds
    no membership row on that team (joining materializes rows upward, never
    down), and a listing that followed membership alone left them unable to use
    — or test — the connection they had just verified and saved.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        MemberTeamConnectionsResponse
    """

    return sync_detailed(
        client=client,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[MemberTeamConnectionsResponse]:
    """My Team Connections

     What this member's daemon reconciles into their workspace.

    Their own personal connections ride the same route as the team's: a daemon
    that syncs one syncs the other, and neither needs a second pass nor a second
    shape to know what to materialize.

    Usage follows the same visibility the Connections page has: the rows of
    every team this person belongs to AND of every team they administer by
    descent. The org admin who configures a sub-team's connection usually holds
    no membership row on that team (joining materializes rows upward, never
    down), and a listing that followed membership alone left them unable to use
    — or test — the connection they had just verified and saved.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[MemberTeamConnectionsResponse]
    """

    kwargs = _get_kwargs()

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
) -> MemberTeamConnectionsResponse | None:
    """My Team Connections

     What this member's daemon reconciles into their workspace.

    Their own personal connections ride the same route as the team's: a daemon
    that syncs one syncs the other, and neither needs a second pass nor a second
    shape to know what to materialize.

    Usage follows the same visibility the Connections page has: the rows of
    every team this person belongs to AND of every team they administer by
    descent. The org admin who configures a sub-team's connection usually holds
    no membership row on that team (joining materializes rows upward, never
    down), and a listing that followed membership alone left them unable to use
    — or test — the connection they had just verified and saved.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        MemberTeamConnectionsResponse
    """

    return (
        await asyncio_detailed(
            client=client,
        )
    ).parsed
