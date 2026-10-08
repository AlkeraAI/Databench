from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.chat_publisher_state_update import ChatPublisherStateUpdate
from ...models.chat_session_read import ChatSessionRead
from ...models.error_envelope import ErrorEnvelope
from ...types import Response


def _get_kwargs(
    chat_id: UUID,
    *,
    body: ChatPublisherStateUpdate,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "put",
        "url": "/api/v1/chats/{chat_id}/publisher-state".format(
            chat_id=quote(str(chat_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ChatSessionRead | ErrorEnvelope | None:
    if response.status_code == 200:
        response_200 = ChatSessionRead.from_dict(response.json())

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
) -> Response[ChatSessionRead | ErrorEnvelope]:
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
    body: ChatPublisherStateUpdate,
) -> Response[ChatSessionRead | ErrorEnvelope]:
    """Set Publisher State

     The machine says whether it can publish this chat.

    A box that the gateway will not let write a chat's transcript must not
    leave the reader with a working composer and no answer: it reports the
    refusal here, the chat reads as ``refused`` with the reason, and the banner
    says so. When it publishes again it reports ``publishing`` and the flag
    clears. Only a machine may say it — an agent whose asserted id is the
    chat's bound machine or the org's current workspace machine (``WRITE`` on
    ``chat.access``), or a platform box on its own credential for a chat bound
    to it; a person, or any other agent, is refused.

    Args:
        chat_id (UUID):
        body (ChatPublisherStateUpdate): What the machine bound to a chat reports about it:
            ``refused`` (with
            the gateway's reason) when it cannot publish, ``publishing`` when it holds
            the chat's session (again), ``asleep`` when it closed that session — the
            folder pushed and released, the chat resumable by any box. Only a machine
            may say it — the route decides who. ``waiting``: it has the chat's
            message but no free slot to open it in yet.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatSessionRead | ErrorEnvelope]
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
    body: ChatPublisherStateUpdate,
) -> ChatSessionRead | ErrorEnvelope | None:
    """Set Publisher State

     The machine says whether it can publish this chat.

    A box that the gateway will not let write a chat's transcript must not
    leave the reader with a working composer and no answer: it reports the
    refusal here, the chat reads as ``refused`` with the reason, and the banner
    says so. When it publishes again it reports ``publishing`` and the flag
    clears. Only a machine may say it — an agent whose asserted id is the
    chat's bound machine or the org's current workspace machine (``WRITE`` on
    ``chat.access``), or a platform box on its own credential for a chat bound
    to it; a person, or any other agent, is refused.

    Args:
        chat_id (UUID):
        body (ChatPublisherStateUpdate): What the machine bound to a chat reports about it:
            ``refused`` (with
            the gateway's reason) when it cannot publish, ``publishing`` when it holds
            the chat's session (again), ``asleep`` when it closed that session — the
            folder pushed and released, the chat resumable by any box. Only a machine
            may say it — the route decides who. ``waiting``: it has the chat's
            message but no free slot to open it in yet.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatSessionRead | ErrorEnvelope
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
    body: ChatPublisherStateUpdate,
) -> Response[ChatSessionRead | ErrorEnvelope]:
    """Set Publisher State

     The machine says whether it can publish this chat.

    A box that the gateway will not let write a chat's transcript must not
    leave the reader with a working composer and no answer: it reports the
    refusal here, the chat reads as ``refused`` with the reason, and the banner
    says so. When it publishes again it reports ``publishing`` and the flag
    clears. Only a machine may say it — an agent whose asserted id is the
    chat's bound machine or the org's current workspace machine (``WRITE`` on
    ``chat.access``), or a platform box on its own credential for a chat bound
    to it; a person, or any other agent, is refused.

    Args:
        chat_id (UUID):
        body (ChatPublisherStateUpdate): What the machine bound to a chat reports about it:
            ``refused`` (with
            the gateway's reason) when it cannot publish, ``publishing`` when it holds
            the chat's session (again), ``asleep`` when it closed that session — the
            folder pushed and released, the chat resumable by any box. Only a machine
            may say it — the route decides who. ``waiting``: it has the chat's
            message but no free slot to open it in yet.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatSessionRead | ErrorEnvelope]
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
    body: ChatPublisherStateUpdate,
) -> ChatSessionRead | ErrorEnvelope | None:
    """Set Publisher State

     The machine says whether it can publish this chat.

    A box that the gateway will not let write a chat's transcript must not
    leave the reader with a working composer and no answer: it reports the
    refusal here, the chat reads as ``refused`` with the reason, and the banner
    says so. When it publishes again it reports ``publishing`` and the flag
    clears. Only a machine may say it — an agent whose asserted id is the
    chat's bound machine or the org's current workspace machine (``WRITE`` on
    ``chat.access``), or a platform box on its own credential for a chat bound
    to it; a person, or any other agent, is refused.

    Args:
        chat_id (UUID):
        body (ChatPublisherStateUpdate): What the machine bound to a chat reports about it:
            ``refused`` (with
            the gateway's reason) when it cannot publish, ``publishing`` when it holds
            the chat's session (again), ``asleep`` when it closed that session — the
            folder pushed and released, the chat resumable by any box. Only a machine
            may say it — the route decides who. ``waiting``: it has the chat's
            message but no free slot to open it in yet.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatSessionRead | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            chat_id=chat_id,
            client=client,
            body=body,
        )
    ).parsed
