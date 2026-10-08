from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.chat_message_create import ChatMessageCreate
from ...models.chat_message_read import ChatMessageRead
from ...models.error_envelope import ErrorEnvelope
from ...types import Response


def _get_kwargs(
    chat_id: UUID,
    *,
    body: ChatMessageCreate,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/chats/{chat_id}/messages".format(
            chat_id=quote(str(chat_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ChatMessageRead | ErrorEnvelope | None:
    if response.status_code == 201:
        response_201 = ChatMessageRead.from_dict(response.json())

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
) -> Response[ChatMessageRead | ErrorEnvelope]:
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
    body: ChatMessageCreate,
) -> Response[ChatMessageRead | ErrorEnvelope]:
    """Post Chat Message

     Say something in a chat: recorded, then relayed to the machine.

    Any reader may send — a message is a relay to the publisher, not a write to
    the transcript by the reader — so ``SEND`` is the audience gate plus a
    verified email. The recording and the relay are the same function the
    socket path calls, so a message typed into an open socket and one posted
    here become the same kind of transcript entry.

    Args:
        chat_id (UUID):
        body (ChatMessageCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatMessageRead | ErrorEnvelope]
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
    body: ChatMessageCreate,
) -> ChatMessageRead | ErrorEnvelope | None:
    """Post Chat Message

     Say something in a chat: recorded, then relayed to the machine.

    Any reader may send — a message is a relay to the publisher, not a write to
    the transcript by the reader — so ``SEND`` is the audience gate plus a
    verified email. The recording and the relay are the same function the
    socket path calls, so a message typed into an open socket and one posted
    here become the same kind of transcript entry.

    Args:
        chat_id (UUID):
        body (ChatMessageCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatMessageRead | ErrorEnvelope
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
    body: ChatMessageCreate,
) -> Response[ChatMessageRead | ErrorEnvelope]:
    """Post Chat Message

     Say something in a chat: recorded, then relayed to the machine.

    Any reader may send — a message is a relay to the publisher, not a write to
    the transcript by the reader — so ``SEND`` is the audience gate plus a
    verified email. The recording and the relay are the same function the
    socket path calls, so a message typed into an open socket and one posted
    here become the same kind of transcript entry.

    Args:
        chat_id (UUID):
        body (ChatMessageCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatMessageRead | ErrorEnvelope]
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
    body: ChatMessageCreate,
) -> ChatMessageRead | ErrorEnvelope | None:
    """Post Chat Message

     Say something in a chat: recorded, then relayed to the machine.

    Any reader may send — a message is a relay to the publisher, not a write to
    the transcript by the reader — so ``SEND`` is the audience gate plus a
    verified email. The recording and the relay are the same function the
    socket path calls, so a message typed into an open socket and one posted
    here become the same kind of transcript entry.

    Args:
        chat_id (UUID):
        body (ChatMessageCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatMessageRead | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            chat_id=chat_id,
            client=client,
            body=body,
        )
    ).parsed
