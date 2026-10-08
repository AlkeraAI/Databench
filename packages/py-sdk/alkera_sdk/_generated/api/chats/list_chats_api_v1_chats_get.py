from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.chat_session_list import ChatSessionList
from ...models.error_envelope import ErrorEnvelope
from ...types import UNSET, Response, Unset


def _get_kwargs(
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
        "url": "/api/v1/chats",
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
    *,
    client: AuthenticatedClient | Client,
    limit: int | Unset = 1000,
    cursor: None | str | Unset = UNSET,
) -> Response[ChatSessionList | ErrorEnvelope]:
    """List Chats

     The org's chats, newest first, cut to the ones the caller may read: their
    own, the ones shared with them, and — for a box — the ones bound to it.

    A box on its own machine credential has no org to list. It lists the chats
    bound to it, in every org it serves: that listing is how it discovers the
    turns it owes, and each row is admitted by the same decision the chat's
    own route makes, the tenancy floor included.

    Args:
        limit (int | Unset):  Default: 1000.
        cursor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatSessionList | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        limit=limit,
        cursor=cursor,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
    limit: int | Unset = 1000,
    cursor: None | str | Unset = UNSET,
) -> ChatSessionList | ErrorEnvelope | None:
    """List Chats

     The org's chats, newest first, cut to the ones the caller may read: their
    own, the ones shared with them, and — for a box — the ones bound to it.

    A box on its own machine credential has no org to list. It lists the chats
    bound to it, in every org it serves: that listing is how it discovers the
    turns it owes, and each row is admitted by the same decision the chat's
    own route makes, the tenancy floor included.

    Args:
        limit (int | Unset):  Default: 1000.
        cursor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatSessionList | ErrorEnvelope
    """

    return sync_detailed(
        client=client,
        limit=limit,
        cursor=cursor,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    limit: int | Unset = 1000,
    cursor: None | str | Unset = UNSET,
) -> Response[ChatSessionList | ErrorEnvelope]:
    """List Chats

     The org's chats, newest first, cut to the ones the caller may read: their
    own, the ones shared with them, and — for a box — the ones bound to it.

    A box on its own machine credential has no org to list. It lists the chats
    bound to it, in every org it serves: that listing is how it discovers the
    turns it owes, and each row is admitted by the same decision the chat's
    own route makes, the tenancy floor included.

    Args:
        limit (int | Unset):  Default: 1000.
        cursor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatSessionList | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        limit=limit,
        cursor=cursor,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    limit: int | Unset = 1000,
    cursor: None | str | Unset = UNSET,
) -> ChatSessionList | ErrorEnvelope | None:
    """List Chats

     The org's chats, newest first, cut to the ones the caller may read: their
    own, the ones shared with them, and — for a box — the ones bound to it.

    A box on its own machine credential has no org to list. It lists the chats
    bound to it, in every org it serves: that listing is how it discovers the
    turns it owes, and each row is admitted by the same decision the chat's
    own route makes, the tenancy floor included.

    Args:
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
            client=client,
            limit=limit,
            cursor=cursor,
        )
    ).parsed
