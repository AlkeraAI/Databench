from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.workspace_create import WorkspaceCreate
from ...models.workspace_read import WorkspaceRead
from ...types import Response


def _get_kwargs(
    *,
    body: WorkspaceCreate,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/workspaces",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | WorkspaceRead | None:
    if response.status_code == 201:
        response_201 = WorkspaceRead.from_dict(response.json())

        return response_201

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
    *,
    client: AuthenticatedClient | Client,
    body: WorkspaceCreate,
) -> Response[ErrorEnvelope | WorkspaceRead]:
    """Create Workspace

     Start a project workspace with a folder of its own. Creating with the
    same ``client_id`` twice returns the first one, decided as a READ. With no
    ``machine_pin`` sent it runs on the org's default machine for new
    workspaces when the caller may use it; a ``machine_pin`` sent, null
    included, is the caller's choice and wins.

    Args:
        body (WorkspaceCreate): A new project workspace, with a folder of its own.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | WorkspaceRead]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
    body: WorkspaceCreate,
) -> ErrorEnvelope | WorkspaceRead | None:
    """Create Workspace

     Start a project workspace with a folder of its own. Creating with the
    same ``client_id`` twice returns the first one, decided as a READ. With no
    ``machine_pin`` sent it runs on the org's default machine for new
    workspaces when the caller may use it; a ``machine_pin`` sent, null
    included, is the caller's choice and wins.

    Args:
        body (WorkspaceCreate): A new project workspace, with a folder of its own.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | WorkspaceRead
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: WorkspaceCreate,
) -> Response[ErrorEnvelope | WorkspaceRead]:
    """Create Workspace

     Start a project workspace with a folder of its own. Creating with the
    same ``client_id`` twice returns the first one, decided as a READ. With no
    ``machine_pin`` sent it runs on the org's default machine for new
    workspaces when the caller may use it; a ``machine_pin`` sent, null
    included, is the caller's choice and wins.

    Args:
        body (WorkspaceCreate): A new project workspace, with a folder of its own.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | WorkspaceRead]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: WorkspaceCreate,
) -> ErrorEnvelope | WorkspaceRead | None:
    """Create Workspace

     Start a project workspace with a folder of its own. Creating with the
    same ``client_id`` twice returns the first one, decided as a READ. With no
    ``machine_pin`` sent it runs on the org's default machine for new
    workspaces when the caller may use it; a ``machine_pin`` sent, null
    included, is the caller's choice and wins.

    Args:
        body (WorkspaceCreate): A new project workspace, with a folder of its own.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | WorkspaceRead
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
