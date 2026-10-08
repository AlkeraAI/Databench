from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.chat_model_update import ChatModelUpdate
from ...models.chat_session_read import ChatSessionRead
from ...models.error_envelope import ErrorEnvelope
from ...types import Response


def _get_kwargs(
    chat_id: UUID,
    *,
    body: ChatModelUpdate,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "put",
        "url": "/api/v1/chats/{chat_id}/model".format(
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
    body: ChatModelUpdate,
) -> Response[ChatSessionRead | ErrorEnvelope]:
    """Set Chat Model

     Move this chat onto a model, or change its effort. Gated on ``SEND``
    (whoever may send may switch). Refusals write nothing: 422 for a pick the
    payer's catalog cannot confirm, 409 for one the reasoning history rules out
    or when ``expected_model_id`` names a model the chat has since left.

    Args:
        chat_id (UUID):
        body (ChatModelUpdate): The model a reader moves an open chat onto.

            The same two fields a create takes, and resolved against the same catalog by
            the same rule: an id the workspace cannot run a chat on is a 422, never a
            quiet no-op. Naming no model is not spellable here — a create may decline to
            pick one (the box falls back to its own default), but a SWITCH that pins
            nothing would leave the chat on the model it was already on while telling
            the reader it had moved.

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
    body: ChatModelUpdate,
) -> ChatSessionRead | ErrorEnvelope | None:
    """Set Chat Model

     Move this chat onto a model, or change its effort. Gated on ``SEND``
    (whoever may send may switch). Refusals write nothing: 422 for a pick the
    payer's catalog cannot confirm, 409 for one the reasoning history rules out
    or when ``expected_model_id`` names a model the chat has since left.

    Args:
        chat_id (UUID):
        body (ChatModelUpdate): The model a reader moves an open chat onto.

            The same two fields a create takes, and resolved against the same catalog by
            the same rule: an id the workspace cannot run a chat on is a 422, never a
            quiet no-op. Naming no model is not spellable here — a create may decline to
            pick one (the box falls back to its own default), but a SWITCH that pins
            nothing would leave the chat on the model it was already on while telling
            the reader it had moved.

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
    body: ChatModelUpdate,
) -> Response[ChatSessionRead | ErrorEnvelope]:
    """Set Chat Model

     Move this chat onto a model, or change its effort. Gated on ``SEND``
    (whoever may send may switch). Refusals write nothing: 422 for a pick the
    payer's catalog cannot confirm, 409 for one the reasoning history rules out
    or when ``expected_model_id`` names a model the chat has since left.

    Args:
        chat_id (UUID):
        body (ChatModelUpdate): The model a reader moves an open chat onto.

            The same two fields a create takes, and resolved against the same catalog by
            the same rule: an id the workspace cannot run a chat on is a 422, never a
            quiet no-op. Naming no model is not spellable here — a create may decline to
            pick one (the box falls back to its own default), but a SWITCH that pins
            nothing would leave the chat on the model it was already on while telling
            the reader it had moved.

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
    body: ChatModelUpdate,
) -> ChatSessionRead | ErrorEnvelope | None:
    """Set Chat Model

     Move this chat onto a model, or change its effort. Gated on ``SEND``
    (whoever may send may switch). Refusals write nothing: 422 for a pick the
    payer's catalog cannot confirm, 409 for one the reasoning history rules out
    or when ``expected_model_id`` names a model the chat has since left.

    Args:
        chat_id (UUID):
        body (ChatModelUpdate): The model a reader moves an open chat onto.

            The same two fields a create takes, and resolved against the same catalog by
            the same rule: an id the workspace cannot run a chat on is a 422, never a
            quiet no-op. Naming no model is not spellable here — a create may decline to
            pick one (the box falls back to its own default), but a SWITCH that pins
            nothing would leave the chat on the model it was already on while telling
            the reader it had moved.

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
