from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...types import Response


def _get_kwargs(
    team_id: UUID,
    user_id: UUID,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "delete",
        "url": "/api/v1/teams/{team_id}/memberships/{user_id}".format(
            team_id=quote(str(team_id), safe=""),
            user_id=quote(str(user_id), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Any | ErrorEnvelope | None:
    if response.status_code == 204:
        response_204 = cast(Any, None)
        return response_204

    if response.status_code == 422:
        response_422 = ErrorEnvelope.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[Any | ErrorEnvelope]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    team_id: UUID,
    user_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Response[Any | ErrorEnvelope]:
    """Remove Membership

     Remove a user from a team.

    On the ORG ROOT this is removal from the org, not a team edit: the person's
    membership in the org goes (and with it every team seat they hold here),
    every credential they hold in the org is ended, and any invitation they
    still have outstanding into the org is cancelled. Dropping only the team
    rows would leave them a member, with continued read access to everything
    scoped to the org and continued spend against its pool. Their identity and
    every other org they belong to are untouched.

    Args:
        team_id (UUID):
        user_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        team_id=team_id,
        user_id=user_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    team_id: UUID,
    user_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Any | ErrorEnvelope | None:
    """Remove Membership

     Remove a user from a team.

    On the ORG ROOT this is removal from the org, not a team edit: the person's
    membership in the org goes (and with it every team seat they hold here),
    every credential they hold in the org is ended, and any invitation they
    still have outstanding into the org is cancelled. Dropping only the team
    rows would leave them a member, with continued read access to everything
    scoped to the org and continued spend against its pool. Their identity and
    every other org they belong to are untouched.

    Args:
        team_id (UUID):
        user_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ErrorEnvelope
    """

    return sync_detailed(
        team_id=team_id,
        user_id=user_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    team_id: UUID,
    user_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Response[Any | ErrorEnvelope]:
    """Remove Membership

     Remove a user from a team.

    On the ORG ROOT this is removal from the org, not a team edit: the person's
    membership in the org goes (and with it every team seat they hold here),
    every credential they hold in the org is ended, and any invitation they
    still have outstanding into the org is cancelled. Dropping only the team
    rows would leave them a member, with continued read access to everything
    scoped to the org and continued spend against its pool. Their identity and
    every other org they belong to are untouched.

    Args:
        team_id (UUID):
        user_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        team_id=team_id,
        user_id=user_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    team_id: UUID,
    user_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Any | ErrorEnvelope | None:
    """Remove Membership

     Remove a user from a team.

    On the ORG ROOT this is removal from the org, not a team edit: the person's
    membership in the org goes (and with it every team seat they hold here),
    every credential they hold in the org is ended, and any invitation they
    still have outstanding into the org is cancelled. Dropping only the team
    rows would leave them a member, with continued read access to everything
    scoped to the org and continued spend against its pool. Their identity and
    every other org they belong to are untouched.

    Args:
        team_id (UUID):
        user_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            team_id=team_id,
            user_id=user_id,
            client=client,
        )
    ).parsed
