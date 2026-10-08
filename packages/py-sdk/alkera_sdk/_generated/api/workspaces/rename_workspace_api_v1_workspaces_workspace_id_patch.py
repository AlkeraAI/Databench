from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.workspace_read import WorkspaceRead
from ...models.workspace_update import WorkspaceUpdate
from ...types import Response


def _get_kwargs(
    workspace_id: UUID,
    *,
    body: WorkspaceUpdate,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "patch",
        "url": "/api/v1/workspaces/{workspace_id}".format(
            workspace_id=quote(str(workspace_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | WorkspaceRead | None:
    if response.status_code == 200:
        response_200 = WorkspaceRead.from_dict(response.json())

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
) -> Response[ErrorEnvelope | WorkspaceRead]:
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
    body: WorkspaceUpdate,
) -> Response[ErrorEnvelope | WorkspaceRead]:
    """Rename Workspace

     Rename, naming the version read. A workspace of one is renamed with its
    chat: to the person looking at it they are one thing.

    Args:
        workspace_id (UUID):
        body (WorkspaceUpdate): A rename. ``expected_version`` is required: a write that does not
            say
            which row it read is a write that did not read one.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | WorkspaceRead]
    """

    kwargs = _get_kwargs(
        workspace_id=workspace_id,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    workspace_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: WorkspaceUpdate,
) -> ErrorEnvelope | WorkspaceRead | None:
    """Rename Workspace

     Rename, naming the version read. A workspace of one is renamed with its
    chat: to the person looking at it they are one thing.

    Args:
        workspace_id (UUID):
        body (WorkspaceUpdate): A rename. ``expected_version`` is required: a write that does not
            say
            which row it read is a write that did not read one.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | WorkspaceRead
    """

    return sync_detailed(
        workspace_id=workspace_id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    workspace_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: WorkspaceUpdate,
) -> Response[ErrorEnvelope | WorkspaceRead]:
    """Rename Workspace

     Rename, naming the version read. A workspace of one is renamed with its
    chat: to the person looking at it they are one thing.

    Args:
        workspace_id (UUID):
        body (WorkspaceUpdate): A rename. ``expected_version`` is required: a write that does not
            say
            which row it read is a write that did not read one.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | WorkspaceRead]
    """

    kwargs = _get_kwargs(
        workspace_id=workspace_id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    workspace_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: WorkspaceUpdate,
) -> ErrorEnvelope | WorkspaceRead | None:
    """Rename Workspace

     Rename, naming the version read. A workspace of one is renamed with its
    chat: to the person looking at it they are one thing.

    Args:
        workspace_id (UUID):
        body (WorkspaceUpdate): A rename. ``expected_version`` is required: a write that does not
            say
            which row it read is a write that did not read one.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | WorkspaceRead
    """

    return (
        await asyncio_detailed(
            workspace_id=workspace_id,
            client=client,
            body=body,
        )
    ).parsed
