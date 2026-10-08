from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.workspace_machine_move_read import WorkspaceMachineMoveRead
from ...types import Response


def _get_kwargs(
    workspace_id: UUID,
    move_id: UUID,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/workspaces/{workspace_id}/machine/moves/{move_id}/cancel".format(
            workspace_id=quote(str(workspace_id), safe=""),
            move_id=quote(str(move_id), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | WorkspaceMachineMoveRead | None:
    if response.status_code == 202:
        response_202 = WorkspaceMachineMoveRead.from_dict(response.json())

        return response_202

    if response.status_code == 422:
        response_422 = ErrorEnvelope.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ErrorEnvelope | WorkspaceMachineMoveRead]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    workspace_id: UUID,
    move_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | WorkspaceMachineMoveRead]:
    """Cancel Workspace Move

     Cancel a move before its chats move; the pin goes back to where it was.

    Args:
        workspace_id (UUID):
        move_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | WorkspaceMachineMoveRead]
    """

    kwargs = _get_kwargs(
        workspace_id=workspace_id,
        move_id=move_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    workspace_id: UUID,
    move_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | WorkspaceMachineMoveRead | None:
    """Cancel Workspace Move

     Cancel a move before its chats move; the pin goes back to where it was.

    Args:
        workspace_id (UUID):
        move_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | WorkspaceMachineMoveRead
    """

    return sync_detailed(
        workspace_id=workspace_id,
        move_id=move_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    workspace_id: UUID,
    move_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | WorkspaceMachineMoveRead]:
    """Cancel Workspace Move

     Cancel a move before its chats move; the pin goes back to where it was.

    Args:
        workspace_id (UUID):
        move_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | WorkspaceMachineMoveRead]
    """

    kwargs = _get_kwargs(
        workspace_id=workspace_id,
        move_id=move_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    workspace_id: UUID,
    move_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | WorkspaceMachineMoveRead | None:
    """Cancel Workspace Move

     Cancel a move before its chats move; the pin goes back to where it was.

    Args:
        workspace_id (UUID):
        move_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | WorkspaceMachineMoveRead
    """

    return (
        await asyncio_detailed(
            workspace_id=workspace_id,
            move_id=move_id,
            client=client,
        )
    ).parsed
