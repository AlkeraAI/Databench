from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.team_connection_credential_lease import TeamConnectionCredentialLease
from ...types import Response


def _get_kwargs(
    chat_id: UUID,
    connection_id: UUID,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/chats/{chat_id}/connections/{connection_id}/credential-lease".format(
            chat_id=quote(str(chat_id), safe=""),
            connection_id=quote(str(connection_id), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | TeamConnectionCredentialLease | None:
    if response.status_code == 200:
        response_200 = TeamConnectionCredentialLease.from_dict(response.json())

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
) -> Response[ErrorEnvelope | TeamConnectionCredentialLease]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    chat_id: UUID,
    connection_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | TeamConnectionCredentialLease]:
    """Lease Chat Connection Credential

     One connection's shared credential bundle, leased to the box that runs
    this chat, for the chat's owner.

    The listing above hands the box the names; this hands it a secret, so it is
    decided twice and on record twice. The chat policy admits the box the chat
    is bound to and nobody else — every other machine and every person, the
    owner included, is told the chat does not exist. The connector policy then
    decides the connection on the OWNER's entitlement, exactly as the owner's
    own daemon would be decided on ``/me/team-connections/{id}/credential-lease``,
    with the chat's binding compared against the box a second time. The bundle
    is time-bounded like the person's lease and the box keeps it in memory
    only; the org's audit chain records the lease in the owner's name with the
    machine's chain and the chat it was for.

    Leased for the workspace owner, by the same rule as the listing above.

    Args:
        chat_id (UUID):
        connection_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | TeamConnectionCredentialLease]
    """

    kwargs = _get_kwargs(
        chat_id=chat_id,
        connection_id=connection_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    chat_id: UUID,
    connection_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | TeamConnectionCredentialLease | None:
    """Lease Chat Connection Credential

     One connection's shared credential bundle, leased to the box that runs
    this chat, for the chat's owner.

    The listing above hands the box the names; this hands it a secret, so it is
    decided twice and on record twice. The chat policy admits the box the chat
    is bound to and nobody else — every other machine and every person, the
    owner included, is told the chat does not exist. The connector policy then
    decides the connection on the OWNER's entitlement, exactly as the owner's
    own daemon would be decided on ``/me/team-connections/{id}/credential-lease``,
    with the chat's binding compared against the box a second time. The bundle
    is time-bounded like the person's lease and the box keeps it in memory
    only; the org's audit chain records the lease in the owner's name with the
    machine's chain and the chat it was for.

    Leased for the workspace owner, by the same rule as the listing above.

    Args:
        chat_id (UUID):
        connection_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | TeamConnectionCredentialLease
    """

    return sync_detailed(
        chat_id=chat_id,
        connection_id=connection_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    chat_id: UUID,
    connection_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | TeamConnectionCredentialLease]:
    """Lease Chat Connection Credential

     One connection's shared credential bundle, leased to the box that runs
    this chat, for the chat's owner.

    The listing above hands the box the names; this hands it a secret, so it is
    decided twice and on record twice. The chat policy admits the box the chat
    is bound to and nobody else — every other machine and every person, the
    owner included, is told the chat does not exist. The connector policy then
    decides the connection on the OWNER's entitlement, exactly as the owner's
    own daemon would be decided on ``/me/team-connections/{id}/credential-lease``,
    with the chat's binding compared against the box a second time. The bundle
    is time-bounded like the person's lease and the box keeps it in memory
    only; the org's audit chain records the lease in the owner's name with the
    machine's chain and the chat it was for.

    Leased for the workspace owner, by the same rule as the listing above.

    Args:
        chat_id (UUID):
        connection_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | TeamConnectionCredentialLease]
    """

    kwargs = _get_kwargs(
        chat_id=chat_id,
        connection_id=connection_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    chat_id: UUID,
    connection_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | TeamConnectionCredentialLease | None:
    """Lease Chat Connection Credential

     One connection's shared credential bundle, leased to the box that runs
    this chat, for the chat's owner.

    The listing above hands the box the names; this hands it a secret, so it is
    decided twice and on record twice. The chat policy admits the box the chat
    is bound to and nobody else — every other machine and every person, the
    owner included, is told the chat does not exist. The connector policy then
    decides the connection on the OWNER's entitlement, exactly as the owner's
    own daemon would be decided on ``/me/team-connections/{id}/credential-lease``,
    with the chat's binding compared against the box a second time. The bundle
    is time-bounded like the person's lease and the box keeps it in memory
    only; the org's audit chain records the lease in the owner's name with the
    machine's chain and the chat it was for.

    Leased for the workspace owner, by the same rule as the listing above.

    Args:
        chat_id (UUID):
        connection_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | TeamConnectionCredentialLease
    """

    return (
        await asyncio_detailed(
            chat_id=chat_id,
            connection_id=connection_id,
            client=client,
        )
    ).parsed
