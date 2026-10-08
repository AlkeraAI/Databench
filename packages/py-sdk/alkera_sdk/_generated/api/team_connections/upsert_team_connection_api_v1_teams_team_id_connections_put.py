from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.team_connection_read import TeamConnectionRead
from ...models.team_connection_upsert_request import TeamConnectionUpsertRequest
from ...types import Response


def _get_kwargs(
    team_id: UUID,
    *,
    body: TeamConnectionUpsertRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "put",
        "url": "/api/v1/teams/{team_id}/connections".format(
            team_id=quote(str(team_id), safe=""),
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
    team_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: TeamConnectionUpsertRequest,
) -> Response[ErrorEnvelope | TeamConnectionRead]:
    """Upsert Team Connection

    Args:
        team_id (UUID):
        body (TeamConnectionUpsertRequest): Create/update a preconfigured connection (team
            admins).

            ``fields`` is the COMPLETE raw form input — every value, including the ones
            the admin keeps to themselves — because the server can only verify a whole
            connection, and this is the one moment somebody holds one. It builds and
            test-dials that, then stores none of it: what persists is the subset named
            by ``shared_fields``, raw, plus each independently encrypted credential role
            that was shared. A shared secret left blank on edit preserves that same
            primary or named role; a role is retired when the edited build no longer
            declares it, including when an optional feature is disabled.
            ``oauth_client_secret`` is likewise write-only, with None meaning keep.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | TeamConnectionRead]
    """

    kwargs = _get_kwargs(
        team_id=team_id,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    team_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: TeamConnectionUpsertRequest,
) -> ErrorEnvelope | TeamConnectionRead | None:
    """Upsert Team Connection

    Args:
        team_id (UUID):
        body (TeamConnectionUpsertRequest): Create/update a preconfigured connection (team
            admins).

            ``fields`` is the COMPLETE raw form input — every value, including the ones
            the admin keeps to themselves — because the server can only verify a whole
            connection, and this is the one moment somebody holds one. It builds and
            test-dials that, then stores none of it: what persists is the subset named
            by ``shared_fields``, raw, plus each independently encrypted credential role
            that was shared. A shared secret left blank on edit preserves that same
            primary or named role; a role is retired when the edited build no longer
            declares it, including when an optional feature is disabled.
            ``oauth_client_secret`` is likewise write-only, with None meaning keep.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | TeamConnectionRead
    """

    return sync_detailed(
        team_id=team_id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    team_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: TeamConnectionUpsertRequest,
) -> Response[ErrorEnvelope | TeamConnectionRead]:
    """Upsert Team Connection

    Args:
        team_id (UUID):
        body (TeamConnectionUpsertRequest): Create/update a preconfigured connection (team
            admins).

            ``fields`` is the COMPLETE raw form input — every value, including the ones
            the admin keeps to themselves — because the server can only verify a whole
            connection, and this is the one moment somebody holds one. It builds and
            test-dials that, then stores none of it: what persists is the subset named
            by ``shared_fields``, raw, plus each independently encrypted credential role
            that was shared. A shared secret left blank on edit preserves that same
            primary or named role; a role is retired when the edited build no longer
            declares it, including when an optional feature is disabled.
            ``oauth_client_secret`` is likewise write-only, with None meaning keep.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | TeamConnectionRead]
    """

    kwargs = _get_kwargs(
        team_id=team_id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    team_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: TeamConnectionUpsertRequest,
) -> ErrorEnvelope | TeamConnectionRead | None:
    """Upsert Team Connection

    Args:
        team_id (UUID):
        body (TeamConnectionUpsertRequest): Create/update a preconfigured connection (team
            admins).

            ``fields`` is the COMPLETE raw form input — every value, including the ones
            the admin keeps to themselves — because the server can only verify a whole
            connection, and this is the one moment somebody holds one. It builds and
            test-dials that, then stores none of it: what persists is the subset named
            by ``shared_fields``, raw, plus each independently encrypted credential role
            that was shared. A shared secret left blank on edit preserves that same
            primary or named role; a role is retired when the edited build no longer
            declares it, including when an optional feature is disabled.
            ``oauth_client_secret`` is likewise write-only, with None meaning keep.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | TeamConnectionRead
    """

    return (
        await asyncio_detailed(
            team_id=team_id,
            client=client,
            body=body,
        )
    ).parsed
