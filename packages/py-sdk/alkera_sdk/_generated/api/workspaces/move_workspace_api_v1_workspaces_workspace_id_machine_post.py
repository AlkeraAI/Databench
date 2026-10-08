from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.workspace_machine_move_read import WorkspaceMachineMoveRead
from ...models.workspace_machine_move_request import WorkspaceMachineMoveRequest
from ...types import UNSET, Response, Unset


def _get_kwargs(
    workspace_id: UUID,
    *,
    body: WorkspaceMachineMoveRequest,
    if_match: None | str | Unset = UNSET,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    if not isinstance(if_match, Unset):
        headers["If-Match"] = if_match

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/workspaces/{workspace_id}/machine".format(
            workspace_id=quote(str(workspace_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
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
    *,
    client: AuthenticatedClient | Client,
    body: WorkspaceMachineMoveRequest,
    if_match: None | str | Unset = UNSET,
) -> Response[ErrorEnvelope | WorkspaceMachineMoveRead]:
    """Move Workspace

     Move the workspace to another machine, or to the org's default
    placement (``to_org_machine_id`` null).

    Args:
        workspace_id (UUID):
        if_match (None | str | Unset): The slot's version as the caller last read it. A write
            whose version no longer matches is refused with a 409; omit it, or send *, to write
            unconditionally.
        body (WorkspaceMachineMoveRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | WorkspaceMachineMoveRead]
    """

    kwargs = _get_kwargs(
        workspace_id=workspace_id,
        body=body,
        if_match=if_match,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    workspace_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: WorkspaceMachineMoveRequest,
    if_match: None | str | Unset = UNSET,
) -> ErrorEnvelope | WorkspaceMachineMoveRead | None:
    """Move Workspace

     Move the workspace to another machine, or to the org's default
    placement (``to_org_machine_id`` null).

    Args:
        workspace_id (UUID):
        if_match (None | str | Unset): The slot's version as the caller last read it. A write
            whose version no longer matches is refused with a 409; omit it, or send *, to write
            unconditionally.
        body (WorkspaceMachineMoveRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | WorkspaceMachineMoveRead
    """

    return sync_detailed(
        workspace_id=workspace_id,
        client=client,
        body=body,
        if_match=if_match,
    ).parsed


async def asyncio_detailed(
    workspace_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: WorkspaceMachineMoveRequest,
    if_match: None | str | Unset = UNSET,
) -> Response[ErrorEnvelope | WorkspaceMachineMoveRead]:
    """Move Workspace

     Move the workspace to another machine, or to the org's default
    placement (``to_org_machine_id`` null).

    Args:
        workspace_id (UUID):
        if_match (None | str | Unset): The slot's version as the caller last read it. A write
            whose version no longer matches is refused with a 409; omit it, or send *, to write
            unconditionally.
        body (WorkspaceMachineMoveRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | WorkspaceMachineMoveRead]
    """

    kwargs = _get_kwargs(
        workspace_id=workspace_id,
        body=body,
        if_match=if_match,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    workspace_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: WorkspaceMachineMoveRequest,
    if_match: None | str | Unset = UNSET,
) -> ErrorEnvelope | WorkspaceMachineMoveRead | None:
    """Move Workspace

     Move the workspace to another machine, or to the org's default
    placement (``to_org_machine_id`` null).

    Args:
        workspace_id (UUID):
        if_match (None | str | Unset): The slot's version as the caller last read it. A write
            whose version no longer matches is refused with a 409; omit it, or send *, to write
            unconditionally.
        body (WorkspaceMachineMoveRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | WorkspaceMachineMoveRead
    """

    return (
        await asyncio_detailed(
            workspace_id=workspace_id,
            client=client,
            body=body,
            if_match=if_match,
        )
    ).parsed
