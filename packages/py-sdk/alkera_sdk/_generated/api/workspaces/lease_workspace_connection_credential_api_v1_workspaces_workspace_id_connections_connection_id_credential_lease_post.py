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
    workspace_id: UUID,
    connection_id: UUID,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/workspaces/{workspace_id}/connections/{connection_id}/credential-lease".format(
            workspace_id=quote(str(workspace_id), safe=""),
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
    workspace_id: UUID,
    connection_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | TeamConnectionCredentialLease]:
    """Lease Workspace Connection Credential

     One of the workspace owner's connections, leased to the box that holds
    the workspace for that workspace alone: a shared row's bundle, or a
    per-user row's OWNER grant. Decided twice and on record twice: the
    workspace policy admits only the holding box, then the connector policy
    decides the connection on the owner's entitlement with the binding
    compared against the box again. Written to the org's audit chain in the
    owner's name with the machine's chain and the workspace it was for.

    Args:
        workspace_id (UUID):
        connection_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | TeamConnectionCredentialLease]
    """

    kwargs = _get_kwargs(
        workspace_id=workspace_id,
        connection_id=connection_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    workspace_id: UUID,
    connection_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | TeamConnectionCredentialLease | None:
    """Lease Workspace Connection Credential

     One of the workspace owner's connections, leased to the box that holds
    the workspace for that workspace alone: a shared row's bundle, or a
    per-user row's OWNER grant. Decided twice and on record twice: the
    workspace policy admits only the holding box, then the connector policy
    decides the connection on the owner's entitlement with the binding
    compared against the box again. Written to the org's audit chain in the
    owner's name with the machine's chain and the workspace it was for.

    Args:
        workspace_id (UUID):
        connection_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | TeamConnectionCredentialLease
    """

    return sync_detailed(
        workspace_id=workspace_id,
        connection_id=connection_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    workspace_id: UUID,
    connection_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | TeamConnectionCredentialLease]:
    """Lease Workspace Connection Credential

     One of the workspace owner's connections, leased to the box that holds
    the workspace for that workspace alone: a shared row's bundle, or a
    per-user row's OWNER grant. Decided twice and on record twice: the
    workspace policy admits only the holding box, then the connector policy
    decides the connection on the owner's entitlement with the binding
    compared against the box again. Written to the org's audit chain in the
    owner's name with the machine's chain and the workspace it was for.

    Args:
        workspace_id (UUID):
        connection_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | TeamConnectionCredentialLease]
    """

    kwargs = _get_kwargs(
        workspace_id=workspace_id,
        connection_id=connection_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    workspace_id: UUID,
    connection_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | TeamConnectionCredentialLease | None:
    """Lease Workspace Connection Credential

     One of the workspace owner's connections, leased to the box that holds
    the workspace for that workspace alone: a shared row's bundle, or a
    per-user row's OWNER grant. Decided twice and on record twice: the
    workspace policy admits only the holding box, then the connector policy
    decides the connection on the owner's entitlement with the binding
    compared against the box again. Written to the org's audit chain in the
    owner's name with the machine's chain and the workspace it was for.

    Args:
        workspace_id (UUID):
        connection_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | TeamConnectionCredentialLease
    """

    return (
        await asyncio_detailed(
            workspace_id=workspace_id,
            connection_id=connection_id,
            client=client,
        )
    ).parsed
