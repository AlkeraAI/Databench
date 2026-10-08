from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.connection_move_request import ConnectionMoveRequest
from ...models.error_envelope import ErrorEnvelope
from ...models.team_connection_read import TeamConnectionRead
from ...types import Response


def _get_kwargs(
    connection_id: UUID,
    *,
    body: ConnectionMoveRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/connections/{connection_id}/owner".format(
            connection_id=quote(str(connection_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | TeamConnectionRead | None:
    if response.status_code == 200:
        response_200 = TeamConnectionRead.from_dict(response.json())

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
) -> Response[ErrorEnvelope | TeamConnectionRead]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    connection_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: ConnectionMoveRequest,
) -> Response[ErrorEnvelope | TeamConnectionRead]:
    """Move Connection Owner

     Re-address one connection: to a team, or back to the person asking.

    One route for every direction, because the invariants are the same in all of
    them and a per-direction route would have to repeat each one. Both ends of
    the move are the policy's decision, made before anything is written; what
    the move does to the row and to what the old address derived from it is the
    service's, which is where a future mover — a team that merges, a member who
    leaves — will find it already done.

    Args:
        connection_id (UUID):
        body (ConnectionMoveRequest): Re-address one connection to a different owner.

            ``team_id`` names the team it becomes; ``None`` makes it the caller's own.
            Nothing else travels in the request: the configuration, the stored
            credential and the choice of which values members supply are the row's
            already, and a move that re-sent them could quietly change them.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | TeamConnectionRead]
    """

    kwargs = _get_kwargs(
        connection_id=connection_id,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    connection_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: ConnectionMoveRequest,
) -> ErrorEnvelope | TeamConnectionRead | None:
    """Move Connection Owner

     Re-address one connection: to a team, or back to the person asking.

    One route for every direction, because the invariants are the same in all of
    them and a per-direction route would have to repeat each one. Both ends of
    the move are the policy's decision, made before anything is written; what
    the move does to the row and to what the old address derived from it is the
    service's, which is where a future mover — a team that merges, a member who
    leaves — will find it already done.

    Args:
        connection_id (UUID):
        body (ConnectionMoveRequest): Re-address one connection to a different owner.

            ``team_id`` names the team it becomes; ``None`` makes it the caller's own.
            Nothing else travels in the request: the configuration, the stored
            credential and the choice of which values members supply are the row's
            already, and a move that re-sent them could quietly change them.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | TeamConnectionRead
    """

    return sync_detailed(
        connection_id=connection_id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    connection_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: ConnectionMoveRequest,
) -> Response[ErrorEnvelope | TeamConnectionRead]:
    """Move Connection Owner

     Re-address one connection: to a team, or back to the person asking.

    One route for every direction, because the invariants are the same in all of
    them and a per-direction route would have to repeat each one. Both ends of
    the move are the policy's decision, made before anything is written; what
    the move does to the row and to what the old address derived from it is the
    service's, which is where a future mover — a team that merges, a member who
    leaves — will find it already done.

    Args:
        connection_id (UUID):
        body (ConnectionMoveRequest): Re-address one connection to a different owner.

            ``team_id`` names the team it becomes; ``None`` makes it the caller's own.
            Nothing else travels in the request: the configuration, the stored
            credential and the choice of which values members supply are the row's
            already, and a move that re-sent them could quietly change them.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | TeamConnectionRead]
    """

    kwargs = _get_kwargs(
        connection_id=connection_id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    connection_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: ConnectionMoveRequest,
) -> ErrorEnvelope | TeamConnectionRead | None:
    """Move Connection Owner

     Re-address one connection: to a team, or back to the person asking.

    One route for every direction, because the invariants are the same in all of
    them and a per-direction route would have to repeat each one. Both ends of
    the move are the policy's decision, made before anything is written; what
    the move does to the row and to what the old address derived from it is the
    service's, which is where a future mover — a team that merges, a member who
    leaves — will find it already done.

    Args:
        connection_id (UUID):
        body (ConnectionMoveRequest): Re-address one connection to a different owner.

            ``team_id`` names the team it becomes; ``None`` makes it the caller's own.
            Nothing else travels in the request: the configuration, the stored
            credential and the choice of which values members supply are the row's
            already, and a move that re-sent them could quietly change them.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | TeamConnectionRead
    """

    return (
        await asyncio_detailed(
            connection_id=connection_id,
            client=client,
            body=body,
        )
    ).parsed
