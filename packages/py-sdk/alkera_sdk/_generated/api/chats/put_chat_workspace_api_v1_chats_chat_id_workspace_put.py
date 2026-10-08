from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.chat_workspace_read import ChatWorkspaceRead
from ...models.chat_workspace_write import ChatWorkspaceWrite
from ...models.error_envelope import ErrorEnvelope
from ...types import Response


def _get_kwargs(
    chat_id: UUID,
    *,
    body: ChatWorkspaceWrite,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "put",
        "url": "/api/v1/chats/{chat_id}/workspace".format(
            chat_id=quote(str(chat_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ChatWorkspaceRead | ErrorEnvelope | None:
    if response.status_code == 200:
        response_200 = ChatWorkspaceRead.from_dict(response.json())

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
) -> Response[ChatWorkspaceRead | ErrorEnvelope]:
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
    body: ChatWorkspaceWrite,
) -> Response[ChatWorkspaceRead | ErrorEnvelope]:
    """Put Chat Workspace

     Replace this reader's tabs beside this chat.

    The decision comes before the parse so a caller who may not read the chat
    learns nothing about what a valid document looks like.

    Args:
        chat_id (UUID):
        body (ChatWorkspaceWrite): A full replacement. The document is taken raw and parsed by the
            route so
            a refusal can name what was wrong with it rather than leaking the shape of
            the model through a framework error.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatWorkspaceRead | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        chat_id=chat_id,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    chat_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: ChatWorkspaceWrite,
) -> ChatWorkspaceRead | ErrorEnvelope | None:
    """Put Chat Workspace

     Replace this reader's tabs beside this chat.

    The decision comes before the parse so a caller who may not read the chat
    learns nothing about what a valid document looks like.

    Args:
        chat_id (UUID):
        body (ChatWorkspaceWrite): A full replacement. The document is taken raw and parsed by the
            route so
            a refusal can name what was wrong with it rather than leaking the shape of
            the model through a framework error.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatWorkspaceRead | ErrorEnvelope
    """

    return sync_detailed(
        chat_id=chat_id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    chat_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: ChatWorkspaceWrite,
) -> Response[ChatWorkspaceRead | ErrorEnvelope]:
    """Put Chat Workspace

     Replace this reader's tabs beside this chat.

    The decision comes before the parse so a caller who may not read the chat
    learns nothing about what a valid document looks like.

    Args:
        chat_id (UUID):
        body (ChatWorkspaceWrite): A full replacement. The document is taken raw and parsed by the
            route so
            a refusal can name what was wrong with it rather than leaking the shape of
            the model through a framework error.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatWorkspaceRead | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        chat_id=chat_id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    chat_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: ChatWorkspaceWrite,
) -> ChatWorkspaceRead | ErrorEnvelope | None:
    """Put Chat Workspace

     Replace this reader's tabs beside this chat.

    The decision comes before the parse so a caller who may not read the chat
    learns nothing about what a valid document looks like.

    Args:
        chat_id (UUID):
        body (ChatWorkspaceWrite): A full replacement. The document is taken raw and parsed by the
            route so
            a refusal can name what was wrong with it rather than leaking the shape of
            the model through a framework error.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatWorkspaceRead | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            chat_id=chat_id,
            client=client,
            body=body,
        )
    ).parsed
