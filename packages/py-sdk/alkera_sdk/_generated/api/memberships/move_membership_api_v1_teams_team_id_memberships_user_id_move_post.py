from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.membership_move import MembershipMove
from ...models.team_membership_read import TeamMembershipRead
from ...types import Response


def _get_kwargs(
    team_id: UUID,
    user_id: UUID,
    *,
    body: MembershipMove,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/teams/{team_id}/memberships/{user_id}/move".format(
            team_id=quote(str(team_id), safe=""),
            user_id=quote(str(user_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | TeamMembershipRead | None:
    if response.status_code == 200:
        response_200 = TeamMembershipRead.from_dict(response.json())

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
) -> Response[ErrorEnvelope | TeamMembershipRead]:
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
    body: MembershipMove,
) -> Response[ErrorEnvelope | TeamMembershipRead]:
    """Move Membership

     Move a user from this team to another team in the same org. Requires
    team-admin on BOTH the source (route dep) and the target (checked here);
    org-admin satisfies both via permission descent.

    Args:
        team_id (UUID):
        user_id (UUID):
        body (MembershipMove):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | TeamMembershipRead]
    """

    kwargs = _get_kwargs(
        team_id=team_id,
        user_id=user_id,
        body=body,
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
    body: MembershipMove,
) -> ErrorEnvelope | TeamMembershipRead | None:
    """Move Membership

     Move a user from this team to another team in the same org. Requires
    team-admin on BOTH the source (route dep) and the target (checked here);
    org-admin satisfies both via permission descent.

    Args:
        team_id (UUID):
        user_id (UUID):
        body (MembershipMove):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | TeamMembershipRead
    """

    return sync_detailed(
        team_id=team_id,
        user_id=user_id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    team_id: UUID,
    user_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: MembershipMove,
) -> Response[ErrorEnvelope | TeamMembershipRead]:
    """Move Membership

     Move a user from this team to another team in the same org. Requires
    team-admin on BOTH the source (route dep) and the target (checked here);
    org-admin satisfies both via permission descent.

    Args:
        team_id (UUID):
        user_id (UUID):
        body (MembershipMove):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | TeamMembershipRead]
    """

    kwargs = _get_kwargs(
        team_id=team_id,
        user_id=user_id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    team_id: UUID,
    user_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: MembershipMove,
) -> ErrorEnvelope | TeamMembershipRead | None:
    """Move Membership

     Move a user from this team to another team in the same org. Requires
    team-admin on BOTH the source (route dep) and the target (checked here);
    org-admin satisfies both via permission descent.

    Args:
        team_id (UUID):
        user_id (UUID):
        body (MembershipMove):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | TeamMembershipRead
    """

    return (
        await asyncio_detailed(
            team_id=team_id,
            user_id=user_id,
            client=client,
            body=body,
        )
    ).parsed
