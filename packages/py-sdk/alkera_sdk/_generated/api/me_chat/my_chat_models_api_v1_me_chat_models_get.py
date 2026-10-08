from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.chat_model_list import ChatModelList
from ...types import Response


def _get_kwargs() -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/me/chat-models",
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ChatModelList | None:
    if response.status_code == 200:
        response_200 = ChatModelList.from_dict(response.json())

        return response_200

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ChatModelList]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[ChatModelList]:
    """My Chat Models

     The models this caller may start a chat on.

    The gateway decides it — entitlement, BYOK credentials, routes — and this is
    a proxy, not a second opinion. A gateway that cannot be reached answers an
    EMPTY list rather than a 502: the composer polls while the catalog is empty
    and self-heals, which is what the editor's picker already does, and a failed
    page load would leave the reader with no chat at all.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatModelList]
    """

    kwargs = _get_kwargs()

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
) -> ChatModelList | None:
    """My Chat Models

     The models this caller may start a chat on.

    The gateway decides it — entitlement, BYOK credentials, routes — and this is
    a proxy, not a second opinion. A gateway that cannot be reached answers an
    EMPTY list rather than a 502: the composer polls while the catalog is empty
    and self-heals, which is what the editor's picker already does, and a failed
    page load would leave the reader with no chat at all.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatModelList
    """

    return sync_detailed(
        client=client,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[ChatModelList]:
    """My Chat Models

     The models this caller may start a chat on.

    The gateway decides it — entitlement, BYOK credentials, routes — and this is
    a proxy, not a second opinion. A gateway that cannot be reached answers an
    EMPTY list rather than a 502: the composer polls while the catalog is empty
    and self-heals, which is what the editor's picker already does, and a failed
    page load would leave the reader with no chat at all.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatModelList]
    """

    kwargs = _get_kwargs()

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
) -> ChatModelList | None:
    """My Chat Models

     The models this caller may start a chat on.

    The gateway decides it — entitlement, BYOK credentials, routes — and this is
    a proxy, not a second opinion. A gateway that cannot be reached answers an
    EMPTY list rather than a 502: the composer polls while the catalog is empty
    and self-heals, which is what the editor's picker already does, and a failed
    page load would leave the reader with no chat at all.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatModelList
    """

    return (
        await asyncio_detailed(
            client=client,
        )
    ).parsed
