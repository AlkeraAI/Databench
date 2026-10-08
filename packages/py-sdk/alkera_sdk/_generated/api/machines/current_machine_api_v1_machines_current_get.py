from http import HTTPStatus
from typing import Any
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.machine_state_read import MachineStateRead
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    workspace_id: None | Unset | UUID = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    json_workspace_id: None | str | Unset
    if isinstance(workspace_id, Unset):
        json_workspace_id = UNSET
    elif isinstance(workspace_id, UUID):
        json_workspace_id = str(workspace_id)
    else:
        json_workspace_id = workspace_id
    params["workspace_id"] = json_workspace_id

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/machines/current",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | MachineStateRead | None:
    if response.status_code == 200:
        response_200 = MachineStateRead.from_dict(response.json())

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
) -> Response[ErrorEnvelope | MachineStateRead]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    workspace_id: None | Unset | UUID = UNSET,
) -> Response[ErrorEnvelope | MachineStateRead]:
    """Current Machine

     The machine the caller's next chat would run on. With ``workspace_id``,
    a chat of that workspace: a workspace pinned to an org machine places its
    chats there, not where the org's other chats go. A workspace the caller
    may not read is the same not-found as a missing one.

    Args:
        workspace_id (None | Unset | UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | MachineStateRead]
    """

    kwargs = _get_kwargs(
        workspace_id=workspace_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
    workspace_id: None | Unset | UUID = UNSET,
) -> ErrorEnvelope | MachineStateRead | None:
    """Current Machine

     The machine the caller's next chat would run on. With ``workspace_id``,
    a chat of that workspace: a workspace pinned to an org machine places its
    chats there, not where the org's other chats go. A workspace the caller
    may not read is the same not-found as a missing one.

    Args:
        workspace_id (None | Unset | UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | MachineStateRead
    """

    return sync_detailed(
        client=client,
        workspace_id=workspace_id,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    workspace_id: None | Unset | UUID = UNSET,
) -> Response[ErrorEnvelope | MachineStateRead]:
    """Current Machine

     The machine the caller's next chat would run on. With ``workspace_id``,
    a chat of that workspace: a workspace pinned to an org machine places its
    chats there, not where the org's other chats go. A workspace the caller
    may not read is the same not-found as a missing one.

    Args:
        workspace_id (None | Unset | UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | MachineStateRead]
    """

    kwargs = _get_kwargs(
        workspace_id=workspace_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    workspace_id: None | Unset | UUID = UNSET,
) -> ErrorEnvelope | MachineStateRead | None:
    """Current Machine

     The machine the caller's next chat would run on. With ``workspace_id``,
    a chat of that workspace: a workspace pinned to an org machine places its
    chats there, not where the org's other chats go. A workspace the caller
    may not read is the same not-found as a missing one.

    Args:
        workspace_id (None | Unset | UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | MachineStateRead
    """

    return (
        await asyncio_detailed(
            client=client,
            workspace_id=workspace_id,
        )
    ).parsed
