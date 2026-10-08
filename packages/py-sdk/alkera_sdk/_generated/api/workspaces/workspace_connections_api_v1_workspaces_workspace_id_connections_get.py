from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.member_team_connections_response import MemberTeamConnectionsResponse
from ...types import Response


def _get_kwargs(
    workspace_id: UUID,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/workspaces/{workspace_id}/connections".format(
            workspace_id=quote(str(workspace_id), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | MemberTeamConnectionsResponse | None:
    if response.status_code == 200:
        response_200 = MemberTeamConnectionsResponse.from_dict(response.json())

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
) -> Response[ErrorEnvelope | MemberTeamConnectionsResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    workspace_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | MemberTeamConnectionsResponse]:
    """Workspace Connections

     The connections attached to a workspace, for the box that holds it: the
    names its notebook kernels and its chats may use.

    Sharing a workspace shares every connection its owner may use, so this is
    the owner's whole set: team connections, the owner's personal ones and
    per-user ones (used with the owner's own credential). The answer is the
    one the owner's own daemon would materialize. Decided on the workspace:
    the box that reported holding it is admitted on its own machine
    credential; every other machine, and every person, is told the workspace
    does not exist (a person reads their connections on their own door). No
    secret rides this answer; a lease is a separate door.

    Args:
        workspace_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | MemberTeamConnectionsResponse]
    """

    kwargs = _get_kwargs(
        workspace_id=workspace_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    workspace_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | MemberTeamConnectionsResponse | None:
    """Workspace Connections

     The connections attached to a workspace, for the box that holds it: the
    names its notebook kernels and its chats may use.

    Sharing a workspace shares every connection its owner may use, so this is
    the owner's whole set: team connections, the owner's personal ones and
    per-user ones (used with the owner's own credential). The answer is the
    one the owner's own daemon would materialize. Decided on the workspace:
    the box that reported holding it is admitted on its own machine
    credential; every other machine, and every person, is told the workspace
    does not exist (a person reads their connections on their own door). No
    secret rides this answer; a lease is a separate door.

    Args:
        workspace_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | MemberTeamConnectionsResponse
    """

    return sync_detailed(
        workspace_id=workspace_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    workspace_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | MemberTeamConnectionsResponse]:
    """Workspace Connections

     The connections attached to a workspace, for the box that holds it: the
    names its notebook kernels and its chats may use.

    Sharing a workspace shares every connection its owner may use, so this is
    the owner's whole set: team connections, the owner's personal ones and
    per-user ones (used with the owner's own credential). The answer is the
    one the owner's own daemon would materialize. Decided on the workspace:
    the box that reported holding it is admitted on its own machine
    credential; every other machine, and every person, is told the workspace
    does not exist (a person reads their connections on their own door). No
    secret rides this answer; a lease is a separate door.

    Args:
        workspace_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | MemberTeamConnectionsResponse]
    """

    kwargs = _get_kwargs(
        workspace_id=workspace_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    workspace_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | MemberTeamConnectionsResponse | None:
    """Workspace Connections

     The connections attached to a workspace, for the box that holds it: the
    names its notebook kernels and its chats may use.

    Sharing a workspace shares every connection its owner may use, so this is
    the owner's whole set: team connections, the owner's personal ones and
    per-user ones (used with the owner's own credential). The answer is the
    one the owner's own daemon would materialize. Decided on the workspace:
    the box that reported holding it is admitted on its own machine
    credential; every other machine, and every person, is told the workspace
    does not exist (a person reads their connections on their own door). No
    secret rides this answer; a lease is a separate door.

    Args:
        workspace_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | MemberTeamConnectionsResponse
    """

    return (
        await asyncio_detailed(
            workspace_id=workspace_id,
            client=client,
        )
    ).parsed
