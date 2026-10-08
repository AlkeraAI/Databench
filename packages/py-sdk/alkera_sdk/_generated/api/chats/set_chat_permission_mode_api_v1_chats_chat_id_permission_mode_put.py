from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.chat_permission_mode_update import ChatPermissionModeUpdate
from ...models.chat_session_read import ChatSessionRead
from ...models.error_envelope import ErrorEnvelope
from ...types import Response


def _get_kwargs(
    chat_id: UUID,
    *,
    body: ChatPermissionModeUpdate,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "put",
        "url": "/api/v1/chats/{chat_id}/permission-mode".format(
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
    body: ChatPermissionModeUpdate,
) -> Response[ChatSessionRead | ErrorEnvelope]:
    """Set Chat Permission Mode

     Put this chat's session into a permission stance.

    Choosing the stance is speaking in the chat — it changes what the agent will
    do on the next turn — so the gate is ``SEND``, the same one a message and an
    answer pass. A reader who may not speak here may not decide how the agent
    behaves either.

    The row is the durable answer: the box reads it when it opens the session,
    so a chat resumed on a fresh machine comes back in the stance the reader
    left it in. The relay is how a box that is running the chat RIGHT NOW hears
    about it without waiting for a resume; a box that is not running it never
    sees the relay and loses nothing.

    Only a stance a harness runs in is spellable (``ChatPermissionModeUpdate``)
    — a body naming anything else is a 422. The set is the editor's own five,
    because a stance only ever decides WHO IS ASKED: the machine keeps its path
    fence whatever mode a chat is in, and the connected data is read-only in
    every one of them.

    Args:
        chat_id (UUID):
        body (ChatPermissionModeUpdate): The stance a reader puts a chat's session in.

            Only a stance a cloud chat may run in is spellable (``CloudPermissionMode``).
            A body naming anything else — a word a newer client invented, a retired one,
            a different casing — is a 422, not a silent downgrade to something the
            reader did not ask for.

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
    body: ChatPermissionModeUpdate,
) -> ChatSessionRead | ErrorEnvelope | None:
    """Set Chat Permission Mode

     Put this chat's session into a permission stance.

    Choosing the stance is speaking in the chat — it changes what the agent will
    do on the next turn — so the gate is ``SEND``, the same one a message and an
    answer pass. A reader who may not speak here may not decide how the agent
    behaves either.

    The row is the durable answer: the box reads it when it opens the session,
    so a chat resumed on a fresh machine comes back in the stance the reader
    left it in. The relay is how a box that is running the chat RIGHT NOW hears
    about it without waiting for a resume; a box that is not running it never
    sees the relay and loses nothing.

    Only a stance a harness runs in is spellable (``ChatPermissionModeUpdate``)
    — a body naming anything else is a 422. The set is the editor's own five,
    because a stance only ever decides WHO IS ASKED: the machine keeps its path
    fence whatever mode a chat is in, and the connected data is read-only in
    every one of them.

    Args:
        chat_id (UUID):
        body (ChatPermissionModeUpdate): The stance a reader puts a chat's session in.

            Only a stance a cloud chat may run in is spellable (``CloudPermissionMode``).
            A body naming anything else — a word a newer client invented, a retired one,
            a different casing — is a 422, not a silent downgrade to something the
            reader did not ask for.

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
    body: ChatPermissionModeUpdate,
) -> Response[ChatSessionRead | ErrorEnvelope]:
    """Set Chat Permission Mode

     Put this chat's session into a permission stance.

    Choosing the stance is speaking in the chat — it changes what the agent will
    do on the next turn — so the gate is ``SEND``, the same one a message and an
    answer pass. A reader who may not speak here may not decide how the agent
    behaves either.

    The row is the durable answer: the box reads it when it opens the session,
    so a chat resumed on a fresh machine comes back in the stance the reader
    left it in. The relay is how a box that is running the chat RIGHT NOW hears
    about it without waiting for a resume; a box that is not running it never
    sees the relay and loses nothing.

    Only a stance a harness runs in is spellable (``ChatPermissionModeUpdate``)
    — a body naming anything else is a 422. The set is the editor's own five,
    because a stance only ever decides WHO IS ASKED: the machine keeps its path
    fence whatever mode a chat is in, and the connected data is read-only in
    every one of them.

    Args:
        chat_id (UUID):
        body (ChatPermissionModeUpdate): The stance a reader puts a chat's session in.

            Only a stance a cloud chat may run in is spellable (``CloudPermissionMode``).
            A body naming anything else — a word a newer client invented, a retired one,
            a different casing — is a 422, not a silent downgrade to something the
            reader did not ask for.

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
    body: ChatPermissionModeUpdate,
) -> ChatSessionRead | ErrorEnvelope | None:
    """Set Chat Permission Mode

     Put this chat's session into a permission stance.

    Choosing the stance is speaking in the chat — it changes what the agent will
    do on the next turn — so the gate is ``SEND``, the same one a message and an
    answer pass. A reader who may not speak here may not decide how the agent
    behaves either.

    The row is the durable answer: the box reads it when it opens the session,
    so a chat resumed on a fresh machine comes back in the stance the reader
    left it in. The relay is how a box that is running the chat RIGHT NOW hears
    about it without waiting for a resume; a box that is not running it never
    sees the relay and loses nothing.

    Only a stance a harness runs in is spellable (``ChatPermissionModeUpdate``)
    — a body naming anything else is a 422. The set is the editor's own five,
    because a stance only ever decides WHO IS ASKED: the machine keeps its path
    fence whatever mode a chat is in, and the connected data is read-only in
    every one of them.

    Args:
        chat_id (UUID):
        body (ChatPermissionModeUpdate): The stance a reader puts a chat's session in.

            Only a stance a cloud chat may run in is spellable (``CloudPermissionMode``).
            A body naming anything else — a word a newer client invented, a retired one,
            a different casing — is a 422, not a silent downgrade to something the
            reader did not ask for.

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
