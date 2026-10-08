from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.chat_attachment_create import ChatAttachmentCreate
from ...models.chat_attachment_read import ChatAttachmentRead
from ...models.error_envelope import ErrorEnvelope
from ...types import Response


def _get_kwargs(
    chat_id: UUID,
    *,
    body: ChatAttachmentCreate,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/chats/{chat_id}/attachments".format(
            chat_id=quote(str(chat_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ChatAttachmentRead | ErrorEnvelope | None:
    if response.status_code == 201:
        response_201 = ChatAttachmentRead.from_dict(response.json())

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
) -> Response[ChatAttachmentRead | ErrorEnvelope]:
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
    body: ChatAttachmentCreate,
) -> Response[ChatAttachmentRead | ErrorEnvelope]:
    """Attach File

     Link a Files node to a chat.

    TWO decisions, both on record: ``SEND`` on the chat (attaching a file is
    speaking in the conversation) and ``READ`` on the node through the Files
    decider (you cannot hand somebody a file you cannot read yourself). The
    link itself grants nothing — every later read of the node is decided again.

    Args:
        chat_id (UUID):
        body (ChatAttachmentCreate): The body that links one Files node to a chat.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatAttachmentRead | ErrorEnvelope]
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
    body: ChatAttachmentCreate,
) -> ChatAttachmentRead | ErrorEnvelope | None:
    """Attach File

     Link a Files node to a chat.

    TWO decisions, both on record: ``SEND`` on the chat (attaching a file is
    speaking in the conversation) and ``READ`` on the node through the Files
    decider (you cannot hand somebody a file you cannot read yourself). The
    link itself grants nothing — every later read of the node is decided again.

    Args:
        chat_id (UUID):
        body (ChatAttachmentCreate): The body that links one Files node to a chat.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatAttachmentRead | ErrorEnvelope
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
    body: ChatAttachmentCreate,
) -> Response[ChatAttachmentRead | ErrorEnvelope]:
    """Attach File

     Link a Files node to a chat.

    TWO decisions, both on record: ``SEND`` on the chat (attaching a file is
    speaking in the conversation) and ``READ`` on the node through the Files
    decider (you cannot hand somebody a file you cannot read yourself). The
    link itself grants nothing — every later read of the node is decided again.

    Args:
        chat_id (UUID):
        body (ChatAttachmentCreate): The body that links one Files node to a chat.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatAttachmentRead | ErrorEnvelope]
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
    body: ChatAttachmentCreate,
) -> ChatAttachmentRead | ErrorEnvelope | None:
    """Attach File

     Link a Files node to a chat.

    TWO decisions, both on record: ``SEND`` on the chat (attaching a file is
    speaking in the conversation) and ``READ`` on the node through the Files
    decider (you cannot hand somebody a file you cannot read yourself). The
    link itself grants nothing — every later read of the node is decided again.

    Args:
        chat_id (UUID):
        body (ChatAttachmentCreate): The body that links one Files node to a chat.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatAttachmentRead | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            chat_id=chat_id,
            client=client,
            body=body,
        )
    ).parsed
