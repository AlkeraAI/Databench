from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.chat_attachment_list import ChatAttachmentList
from ...models.error_envelope import ErrorEnvelope
from ...types import UNSET, Response, Unset


def _get_kwargs(
    chat_id: UUID,
    *,
    limit: int | Unset = 50,
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
        "url": "/api/v1/chats/{chat_id}/attachments".format(
            chat_id=quote(str(chat_id), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ChatAttachmentList | ErrorEnvelope | None:
    if response.status_code == 200:
        response_200 = ChatAttachmentList.from_dict(response.json())

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
) -> Response[ChatAttachmentList | ErrorEnvelope]:
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
    limit: int | Unset = 50,
    cursor: None | str | Unset = UNSET,
) -> Response[ChatAttachmentList | ErrorEnvelope]:
    """List Chat Attachments

     A page of the chat's attachments, as THIS caller may read them.

    PAGED, because the links are unbounded over a chat's life: a tab that read
    every one of them decided every one of them too, and a conversation that
    accumulated files for a year answered slower every week. ``next_cursor``
    names the place AND the node the page ended on — both, because two links can
    hold one place — and a read that carries none is the last page.

    The stored list is a set of references, so it is never served verbatim:
    every id is decided again here. A node this caller may not read comes back
    as ``state="unavailable"`` with no name, and a node that is not in this org
    at all — the opaque not-yours answer the repo gives — is absent entirely,
    because the fact that such a node exists is not this chat's to disclose.

    The decision is BATCHED. Deciding one node at a time through ``enforce()``
    would write a committed ``authz.decision`` row per unreadable node, on a
    GET a portal tab re-issues on every invalidation — an ordinary read that
    any member could turn into unbounded audit growth, and rows that look like
    genuine access attempts while being a rendering pass. ``readable_ids``
    answers the same question the per-node decider answers, over rows already
    in memory, and records nothing: the one decision this request makes is the
    READ on the chat above.

    Args:
        chat_id (UUID):
        limit (int | Unset):  Default: 50.
        cursor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatAttachmentList | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        chat_id=chat_id,
        limit=limit,
        cursor=cursor,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    chat_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    limit: int | Unset = 50,
    cursor: None | str | Unset = UNSET,
) -> ChatAttachmentList | ErrorEnvelope | None:
    """List Chat Attachments

     A page of the chat's attachments, as THIS caller may read them.

    PAGED, because the links are unbounded over a chat's life: a tab that read
    every one of them decided every one of them too, and a conversation that
    accumulated files for a year answered slower every week. ``next_cursor``
    names the place AND the node the page ended on — both, because two links can
    hold one place — and a read that carries none is the last page.

    The stored list is a set of references, so it is never served verbatim:
    every id is decided again here. A node this caller may not read comes back
    as ``state="unavailable"`` with no name, and a node that is not in this org
    at all — the opaque not-yours answer the repo gives — is absent entirely,
    because the fact that such a node exists is not this chat's to disclose.

    The decision is BATCHED. Deciding one node at a time through ``enforce()``
    would write a committed ``authz.decision`` row per unreadable node, on a
    GET a portal tab re-issues on every invalidation — an ordinary read that
    any member could turn into unbounded audit growth, and rows that look like
    genuine access attempts while being a rendering pass. ``readable_ids``
    answers the same question the per-node decider answers, over rows already
    in memory, and records nothing: the one decision this request makes is the
    READ on the chat above.

    Args:
        chat_id (UUID):
        limit (int | Unset):  Default: 50.
        cursor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatAttachmentList | ErrorEnvelope
    """

    return sync_detailed(
        chat_id=chat_id,
        client=client,
        limit=limit,
        cursor=cursor,
    ).parsed


async def asyncio_detailed(
    chat_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    limit: int | Unset = 50,
    cursor: None | str | Unset = UNSET,
) -> Response[ChatAttachmentList | ErrorEnvelope]:
    """List Chat Attachments

     A page of the chat's attachments, as THIS caller may read them.

    PAGED, because the links are unbounded over a chat's life: a tab that read
    every one of them decided every one of them too, and a conversation that
    accumulated files for a year answered slower every week. ``next_cursor``
    names the place AND the node the page ended on — both, because two links can
    hold one place — and a read that carries none is the last page.

    The stored list is a set of references, so it is never served verbatim:
    every id is decided again here. A node this caller may not read comes back
    as ``state="unavailable"`` with no name, and a node that is not in this org
    at all — the opaque not-yours answer the repo gives — is absent entirely,
    because the fact that such a node exists is not this chat's to disclose.

    The decision is BATCHED. Deciding one node at a time through ``enforce()``
    would write a committed ``authz.decision`` row per unreadable node, on a
    GET a portal tab re-issues on every invalidation — an ordinary read that
    any member could turn into unbounded audit growth, and rows that look like
    genuine access attempts while being a rendering pass. ``readable_ids``
    answers the same question the per-node decider answers, over rows already
    in memory, and records nothing: the one decision this request makes is the
    READ on the chat above.

    Args:
        chat_id (UUID):
        limit (int | Unset):  Default: 50.
        cursor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatAttachmentList | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        chat_id=chat_id,
        limit=limit,
        cursor=cursor,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    chat_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    limit: int | Unset = 50,
    cursor: None | str | Unset = UNSET,
) -> ChatAttachmentList | ErrorEnvelope | None:
    """List Chat Attachments

     A page of the chat's attachments, as THIS caller may read them.

    PAGED, because the links are unbounded over a chat's life: a tab that read
    every one of them decided every one of them too, and a conversation that
    accumulated files for a year answered slower every week. ``next_cursor``
    names the place AND the node the page ended on — both, because two links can
    hold one place — and a read that carries none is the last page.

    The stored list is a set of references, so it is never served verbatim:
    every id is decided again here. A node this caller may not read comes back
    as ``state="unavailable"`` with no name, and a node that is not in this org
    at all — the opaque not-yours answer the repo gives — is absent entirely,
    because the fact that such a node exists is not this chat's to disclose.

    The decision is BATCHED. Deciding one node at a time through ``enforce()``
    would write a committed ``authz.decision`` row per unreadable node, on a
    GET a portal tab re-issues on every invalidation — an ordinary read that
    any member could turn into unbounded audit growth, and rows that look like
    genuine access attempts while being a rendering pass. ``readable_ids``
    answers the same question the per-node decider answers, over rows already
    in memory, and records nothing: the one decision this request makes is the
    READ on the chat above.

    Args:
        chat_id (UUID):
        limit (int | Unset):  Default: 50.
        cursor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatAttachmentList | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            chat_id=chat_id,
            client=client,
            limit=limit,
            cursor=cursor,
        )
    ).parsed
