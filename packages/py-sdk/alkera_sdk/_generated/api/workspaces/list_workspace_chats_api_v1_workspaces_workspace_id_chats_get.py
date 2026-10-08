from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.chat_session_list import ChatSessionList
from ...models.error_envelope import ErrorEnvelope
from ...types import UNSET, Response, Unset


def _get_kwargs(
    workspace_id: UUID,
    *,
    limit: int | Unset = 1000,
    cursor: None | str | Unset = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["limit"] = limit

    json_cursor: None | str | Unset
    if isinstance(cursor, Unset):
        json_cursor = UNSET
    else:
        json_cursor = cursor
    params["cursor"] = json_cursor

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/workspaces/{workspace_id}/chats".format(
            workspace_id=quote(str(workspace_id), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ChatSessionList | ErrorEnvelope | None:
    if response.status_code == 200:
        response_200 = ChatSessionList.from_dict(response.json())

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
) -> Response[ChatSessionList | ErrorEnvelope]:
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
    limit: int | Unset = 1000,
    cursor: None | str | Unset = UNSET,
) -> Response[ChatSessionList | ErrorEnvelope]:
    """List Workspace Chats

     The chats in a workspace the caller may read, oldest first, a page at a
    time. Each is decided by the chat's own read, as on the chat rail: reading
    a workspace shows the chats it holds that its sharing reaches. A page is
    cut by that read after it is taken, so it may hold fewer than ``limit``;
    ``next_cursor`` says whether more follow.

    Args:
        workspace_id (UUID):
        limit (int | Unset):  Default: 1000.
        cursor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatSessionList | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        workspace_id=workspace_id,
        limit=limit,
        cursor=cursor,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    workspace_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    limit: int | Unset = 1000,
    cursor: None | str | Unset = UNSET,
) -> ChatSessionList | ErrorEnvelope | None:
    """List Workspace Chats

     The chats in a workspace the caller may read, oldest first, a page at a
    time. Each is decided by the chat's own read, as on the chat rail: reading
    a workspace shows the chats it holds that its sharing reaches. A page is
    cut by that read after it is taken, so it may hold fewer than ``limit``;
    ``next_cursor`` says whether more follow.

    Args:
        workspace_id (UUID):
        limit (int | Unset):  Default: 1000.
        cursor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatSessionList | ErrorEnvelope
    """

    return sync_detailed(
        workspace_id=workspace_id,
        client=client,
        limit=limit,
        cursor=cursor,
    ).parsed


async def asyncio_detailed(
    workspace_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    limit: int | Unset = 1000,
    cursor: None | str | Unset = UNSET,
) -> Response[ChatSessionList | ErrorEnvelope]:
    """List Workspace Chats

     The chats in a workspace the caller may read, oldest first, a page at a
    time. Each is decided by the chat's own read, as on the chat rail: reading
    a workspace shows the chats it holds that its sharing reaches. A page is
    cut by that read after it is taken, so it may hold fewer than ``limit``;
    ``next_cursor`` says whether more follow.

    Args:
        workspace_id (UUID):
        limit (int | Unset):  Default: 1000.
        cursor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatSessionList | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        workspace_id=workspace_id,
        limit=limit,
        cursor=cursor,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    workspace_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    limit: int | Unset = 1000,
    cursor: None | str | Unset = UNSET,
) -> ChatSessionList | ErrorEnvelope | None:
    """List Workspace Chats

     The chats in a workspace the caller may read, oldest first, a page at a
    time. Each is decided by the chat's own read, as on the chat rail: reading
    a workspace shows the chats it holds that its sharing reaches. A page is
    cut by that read after it is taken, so it may hold fewer than ``limit``;
    ``next_cursor`` says whether more follow.

    Args:
        workspace_id (UUID):
        limit (int | Unset):  Default: 1000.
        cursor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatSessionList | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            workspace_id=workspace_id,
            client=client,
            limit=limit,
            cursor=cursor,
        )
    ).parsed
