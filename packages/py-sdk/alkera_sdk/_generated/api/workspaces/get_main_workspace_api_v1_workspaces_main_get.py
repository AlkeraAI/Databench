from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.workspace_read import WorkspaceRead
from ...types import Response


def _get_kwargs() -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/workspaces/main",
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> WorkspaceRead | None:
    if response.status_code == 200:
        response_200 = WorkspaceRead.from_dict(response.json())

        return response_200

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[WorkspaceRead]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[WorkspaceRead]:
    """Get Main Workspace

     The caller's main workspace, made now if they have none: where a chat
    with no place named lands once a workspace may hold several chats.
    Making it is decided as a ``CREATE``; reading one that exists as a
    ``READ``.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[WorkspaceRead]
    """

    kwargs = _get_kwargs()

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
) -> WorkspaceRead | None:
    """Get Main Workspace

     The caller's main workspace, made now if they have none: where a chat
    with no place named lands once a workspace may hold several chats.
    Making it is decided as a ``CREATE``; reading one that exists as a
    ``READ``.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        WorkspaceRead
    """

    return sync_detailed(
        client=client,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[WorkspaceRead]:
    """Get Main Workspace

     The caller's main workspace, made now if they have none: where a chat
    with no place named lands once a workspace may hold several chats.
    Making it is decided as a ``CREATE``; reading one that exists as a
    ``READ``.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[WorkspaceRead]
    """

    kwargs = _get_kwargs()

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
) -> WorkspaceRead | None:
    """Get Main Workspace

     The caller's main workspace, made now if they have none: where a chat
    with no place named lands once a workspace may hold several chats.
    Making it is decided as a ``CREATE``; reading one that exists as a
    ``READ``.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        WorkspaceRead
    """

    return (
        await asyncio_detailed(
            client=client,
        )
    ).parsed
