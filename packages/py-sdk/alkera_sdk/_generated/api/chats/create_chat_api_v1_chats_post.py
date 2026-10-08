from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.chat_create import ChatCreate
from ...models.chat_session_read import ChatSessionRead
from ...models.error_envelope import ErrorEnvelope
from ...types import Response


def _get_kwargs(
    *,
    body: ChatCreate,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/chats",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ChatSessionRead | ErrorEnvelope | None:
    if response.status_code == 201:
        response_201 = ChatSessionRead.from_dict(response.json())

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
) -> Response[ChatSessionRead | ErrorEnvelope]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ChatCreate,
) -> Response[ChatSessionRead | ErrorEnvelope]:
    """Create Chat

     Open a conversation and bind it to a machine.

    No machine is a state, not a failure: the chat is created with
    ``machine_status="none"`` and the UI says so. Creating with the same
    ``client_id`` twice returns the first chat rather than a second one.

    The decision is filed under the id the chat will have — the row a retried
    ``client_id`` already made, or the one minted here — so the audit trail
    for a chat starts at its creation and never at a placeholder.

    The chat is created in a workspace: the one ``workspace_id`` names (see
    :func:`_workspace_taking_a_chat`), else a workspace of its own while a
    workspace holds one chat, else its owner's main workspace.

    Args:
        body (ChatCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatSessionRead | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
    body: ChatCreate,
) -> ChatSessionRead | ErrorEnvelope | None:
    """Create Chat

     Open a conversation and bind it to a machine.

    No machine is a state, not a failure: the chat is created with
    ``machine_status="none"`` and the UI says so. Creating with the same
    ``client_id`` twice returns the first chat rather than a second one.

    The decision is filed under the id the chat will have — the row a retried
    ``client_id`` already made, or the one minted here — so the audit trail
    for a chat starts at its creation and never at a placeholder.

    The chat is created in a workspace: the one ``workspace_id`` names (see
    :func:`_workspace_taking_a_chat`), else a workspace of its own while a
    workspace holds one chat, else its owner's main workspace.

    Args:
        body (ChatCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatSessionRead | ErrorEnvelope
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ChatCreate,
) -> Response[ChatSessionRead | ErrorEnvelope]:
    """Create Chat

     Open a conversation and bind it to a machine.

    No machine is a state, not a failure: the chat is created with
    ``machine_status="none"`` and the UI says so. Creating with the same
    ``client_id`` twice returns the first chat rather than a second one.

    The decision is filed under the id the chat will have — the row a retried
    ``client_id`` already made, or the one minted here — so the audit trail
    for a chat starts at its creation and never at a placeholder.

    The chat is created in a workspace: the one ``workspace_id`` names (see
    :func:`_workspace_taking_a_chat`), else a workspace of its own while a
    workspace holds one chat, else its owner's main workspace.

    Args:
        body (ChatCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatSessionRead | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ChatCreate,
) -> ChatSessionRead | ErrorEnvelope | None:
    """Create Chat

     Open a conversation and bind it to a machine.

    No machine is a state, not a failure: the chat is created with
    ``machine_status="none"`` and the UI says so. Creating with the same
    ``client_id`` twice returns the first chat rather than a second one.

    The decision is filed under the id the chat will have — the row a retried
    ``client_id`` already made, or the one minted here — so the audit trail
    for a chat starts at its creation and never at a placeholder.

    The chat is created in a workspace: the one ``workspace_id`` names (see
    :func:`_workspace_taking_a_chat`), else a workspace of its own while a
    workspace holds one chat, else its owner's main workspace.

    Args:
        body (ChatCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatSessionRead | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
