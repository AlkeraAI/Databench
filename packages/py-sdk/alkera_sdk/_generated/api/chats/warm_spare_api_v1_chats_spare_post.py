from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.chat_spare_state import ChatSpareState
from ...types import Response


def _get_kwargs() -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/chats/spare",
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ChatSpareState | None:
    if response.status_code == 200:
        response_200 = ChatSpareState.from_dict(response.json())

        return response_200

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ChatSpareState]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[ChatSpareState]:
    """Warm Spare

     The chat page's heartbeat: keep one chat warmed for the caller.

    Called when the page opens and every minute while it is visible. Idempotent
    and never an error the page acts on: ``warm`` when a spare stands (its
    session opened on the box, or opening), ``none`` when nothing could be
    warmed — no live machine, the drive refusing a create, no model to pin —
    in which case the first message takes the ordinary create path.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatSpareState]
    """

    kwargs = _get_kwargs()

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
) -> ChatSpareState | None:
    """Warm Spare

     The chat page's heartbeat: keep one chat warmed for the caller.

    Called when the page opens and every minute while it is visible. Idempotent
    and never an error the page acts on: ``warm`` when a spare stands (its
    session opened on the box, or opening), ``none`` when nothing could be
    warmed — no live machine, the drive refusing a create, no model to pin —
    in which case the first message takes the ordinary create path.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatSpareState
    """

    return sync_detailed(
        client=client,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[ChatSpareState]:
    """Warm Spare

     The chat page's heartbeat: keep one chat warmed for the caller.

    Called when the page opens and every minute while it is visible. Idempotent
    and never an error the page acts on: ``warm`` when a spare stands (its
    session opened on the box, or opening), ``none`` when nothing could be
    warmed — no live machine, the drive refusing a create, no model to pin —
    in which case the first message takes the ordinary create path.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatSpareState]
    """

    kwargs = _get_kwargs()

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
) -> ChatSpareState | None:
    """Warm Spare

     The chat page's heartbeat: keep one chat warmed for the caller.

    Called when the page opens and every minute while it is visible. Idempotent
    and never an error the page acts on: ``warm`` when a spare stands (its
    session opened on the box, or opening), ``none`` when nothing could be
    warmed — no live machine, the drive refusing a create, no model to pin —
    in which case the first message takes the ordinary create path.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatSpareState
    """

    return (
        await asyncio_detailed(
            client=client,
        )
    ).parsed
