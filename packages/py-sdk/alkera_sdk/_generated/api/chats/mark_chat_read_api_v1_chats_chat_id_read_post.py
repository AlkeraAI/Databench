from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.chat_read_mark_update import ChatReadMarkUpdate
from ...models.chat_read_state_read import ChatReadStateRead
from ...models.error_envelope import ErrorEnvelope
from ...types import Response


def _get_kwargs(
    chat_id: UUID,
    *,
    body: ChatReadMarkUpdate,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/chats/{chat_id}/read".format(
            chat_id=quote(str(chat_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ChatReadStateRead | ErrorEnvelope | None:
    if response.status_code == 200:
        response_200 = ChatReadStateRead.from_dict(response.json())

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
) -> Response[ChatReadStateRead | ErrorEnvelope]:
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
    body: ChatReadMarkUpdate,
) -> Response[ChatReadStateRead | ErrorEnvelope]:
    """Mark Chat Read

     Mark the chat read up to ``seq`` for the caller, and clear their "Mark
    as unread". Idempotent; a lower ``seq`` leaves the mark where it is.

    Args:
        chat_id (UUID):
        body (ChatReadMarkUpdate): ``POST /chats/{id}/read``: the highest transcript sequence the
            caller's
            page has shown. The mark only moves forward and never past the chat's
            ``last_seq``.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatReadStateRead | ErrorEnvelope]
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
    body: ChatReadMarkUpdate,
) -> ChatReadStateRead | ErrorEnvelope | None:
    """Mark Chat Read

     Mark the chat read up to ``seq`` for the caller, and clear their "Mark
    as unread". Idempotent; a lower ``seq`` leaves the mark where it is.

    Args:
        chat_id (UUID):
        body (ChatReadMarkUpdate): ``POST /chats/{id}/read``: the highest transcript sequence the
            caller's
            page has shown. The mark only moves forward and never past the chat's
            ``last_seq``.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatReadStateRead | ErrorEnvelope
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
    body: ChatReadMarkUpdate,
) -> Response[ChatReadStateRead | ErrorEnvelope]:
    """Mark Chat Read

     Mark the chat read up to ``seq`` for the caller, and clear their "Mark
    as unread". Idempotent; a lower ``seq`` leaves the mark where it is.

    Args:
        chat_id (UUID):
        body (ChatReadMarkUpdate): ``POST /chats/{id}/read``: the highest transcript sequence the
            caller's
            page has shown. The mark only moves forward and never past the chat's
            ``last_seq``.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatReadStateRead | ErrorEnvelope]
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
    body: ChatReadMarkUpdate,
) -> ChatReadStateRead | ErrorEnvelope | None:
    """Mark Chat Read

     Mark the chat read up to ``seq`` for the caller, and clear their "Mark
    as unread". Idempotent; a lower ``seq`` leaves the mark where it is.

    Args:
        chat_id (UUID):
        body (ChatReadMarkUpdate): ``POST /chats/{id}/read``: the highest transcript sequence the
            caller's
            page has shown. The mark only moves forward and never past the chat's
            ``last_seq``.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatReadStateRead | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            chat_id=chat_id,
            client=client,
            body=body,
        )
    ).parsed
