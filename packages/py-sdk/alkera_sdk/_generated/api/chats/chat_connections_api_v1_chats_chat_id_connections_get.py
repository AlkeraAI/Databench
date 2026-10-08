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
    chat_id: UUID,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/chats/{chat_id}/connections".format(
            chat_id=quote(str(chat_id), safe=""),
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
    chat_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | MemberTeamConnectionsResponse]:
    """Chat Connections

     The connections the agent running this chat may use, for the box that
    runs it.

    A chat on a shared machine acts for its owner without the box ever holding
    the owner's credential, so the box cannot ask ``/me/team-connections`` — a
    person's door, answered about the caller. It asks here instead, per chat,
    on its own machine credential, and is answered the set the OWNER's own
    daemon would materialize (the same builder as ``/me/team-connections``,
    resolved server-side from the chat's owner). Decided on the chat: the box
    the chat is bound to is admitted, every other machine and every person is
    told the chat does not exist. The secrets themselves never ride this
    answer; a lease is a separate door.

    The owner here is the WORKSPACE's: every chat in a workspace uses its
    owner's connections, so a chat a collaborator started in somebody else's
    workspace is answered the workspace owner's set, never the collaborator's.
    For a workspace of one that is the chat's own owner.

    Args:
        chat_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | MemberTeamConnectionsResponse]
    """

    kwargs = _get_kwargs(
        chat_id=chat_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    chat_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | MemberTeamConnectionsResponse | None:
    """Chat Connections

     The connections the agent running this chat may use, for the box that
    runs it.

    A chat on a shared machine acts for its owner without the box ever holding
    the owner's credential, so the box cannot ask ``/me/team-connections`` — a
    person's door, answered about the caller. It asks here instead, per chat,
    on its own machine credential, and is answered the set the OWNER's own
    daemon would materialize (the same builder as ``/me/team-connections``,
    resolved server-side from the chat's owner). Decided on the chat: the box
    the chat is bound to is admitted, every other machine and every person is
    told the chat does not exist. The secrets themselves never ride this
    answer; a lease is a separate door.

    The owner here is the WORKSPACE's: every chat in a workspace uses its
    owner's connections, so a chat a collaborator started in somebody else's
    workspace is answered the workspace owner's set, never the collaborator's.
    For a workspace of one that is the chat's own owner.

    Args:
        chat_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | MemberTeamConnectionsResponse
    """

    return sync_detailed(
        chat_id=chat_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    chat_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | MemberTeamConnectionsResponse]:
    """Chat Connections

     The connections the agent running this chat may use, for the box that
    runs it.

    A chat on a shared machine acts for its owner without the box ever holding
    the owner's credential, so the box cannot ask ``/me/team-connections`` — a
    person's door, answered about the caller. It asks here instead, per chat,
    on its own machine credential, and is answered the set the OWNER's own
    daemon would materialize (the same builder as ``/me/team-connections``,
    resolved server-side from the chat's owner). Decided on the chat: the box
    the chat is bound to is admitted, every other machine and every person is
    told the chat does not exist. The secrets themselves never ride this
    answer; a lease is a separate door.

    The owner here is the WORKSPACE's: every chat in a workspace uses its
    owner's connections, so a chat a collaborator started in somebody else's
    workspace is answered the workspace owner's set, never the collaborator's.
    For a workspace of one that is the chat's own owner.

    Args:
        chat_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | MemberTeamConnectionsResponse]
    """

    kwargs = _get_kwargs(
        chat_id=chat_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    chat_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | MemberTeamConnectionsResponse | None:
    """Chat Connections

     The connections the agent running this chat may use, for the box that
    runs it.

    A chat on a shared machine acts for its owner without the box ever holding
    the owner's credential, so the box cannot ask ``/me/team-connections`` — a
    person's door, answered about the caller. It asks here instead, per chat,
    on its own machine credential, and is answered the set the OWNER's own
    daemon would materialize (the same builder as ``/me/team-connections``,
    resolved server-side from the chat's owner). Decided on the chat: the box
    the chat is bound to is admitted, every other machine and every person is
    told the chat does not exist. The secrets themselves never ride this
    answer; a lease is a separate door.

    The owner here is the WORKSPACE's: every chat in a workspace uses its
    owner's connections, so a chat a collaborator started in somebody else's
    workspace is answered the workspace owner's set, never the collaborator's.
    For a workspace of one that is the chat's own owner.

    Args:
        chat_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | MemberTeamConnectionsResponse
    """

    return (
        await asyncio_detailed(
            chat_id=chat_id,
            client=client,
        )
    ).parsed
