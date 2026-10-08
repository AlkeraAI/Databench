from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.chat_gateway_token_read import ChatGatewayTokenRead
from ...models.error_envelope import ErrorEnvelope
from ...types import Response


def _get_kwargs(
    chat_id: UUID,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/chats/{chat_id}/gateway-token".format(
            chat_id=quote(str(chat_id), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ChatGatewayTokenRead | ErrorEnvelope | None:
    if response.status_code == 200:
        response_200 = ChatGatewayTokenRead.from_dict(response.json())

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
) -> Response[ChatGatewayTokenRead | ErrorEnvelope]:
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
) -> Response[ChatGatewayTokenRead | ErrorEnvelope]:
    """Mint Chat Gateway Token

     The chat's gateway credential, for the machine that publishes it.

    The agent that runs a chat must hold a credential the gateway accepts and
    nothing else accepts: the box's own bearer would let a prompt-injected
    agent act as the box against this API. So the box asks here, per chat,
    and hands the agent what comes back — a token scoped to the gateway and
    bound to this chat. Only the publisher may ask: the same decision as its
    state report (``WRITE`` on ``chat.access``).

    Who pays follows the parent. A box on a person's session mints the token
    UNDER that session, billed to that person and revoked with the session and
    their membership; the caller must be a session with a ``jti``. A box on
    its own machine credential mints under the credential, billed to the
    chat's payer while the credential and the payer's membership stand. A
    payer who can no longer read the chat is refused (``payer_lost_access``).

    Args:
        chat_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatGatewayTokenRead | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        chat_id=chat_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    chat_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> ChatGatewayTokenRead | ErrorEnvelope | None:
    """Mint Chat Gateway Token

     The chat's gateway credential, for the machine that publishes it.

    The agent that runs a chat must hold a credential the gateway accepts and
    nothing else accepts: the box's own bearer would let a prompt-injected
    agent act as the box against this API. So the box asks here, per chat,
    and hands the agent what comes back — a token scoped to the gateway and
    bound to this chat. Only the publisher may ask: the same decision as its
    state report (``WRITE`` on ``chat.access``).

    Who pays follows the parent. A box on a person's session mints the token
    UNDER that session, billed to that person and revoked with the session and
    their membership; the caller must be a session with a ``jti``. A box on
    its own machine credential mints under the credential, billed to the
    chat's payer while the credential and the payer's membership stand. A
    payer who can no longer read the chat is refused (``payer_lost_access``).

    Args:
        chat_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatGatewayTokenRead | ErrorEnvelope
    """

    return sync_detailed(
        chat_id=chat_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    chat_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ChatGatewayTokenRead | ErrorEnvelope]:
    """Mint Chat Gateway Token

     The chat's gateway credential, for the machine that publishes it.

    The agent that runs a chat must hold a credential the gateway accepts and
    nothing else accepts: the box's own bearer would let a prompt-injected
    agent act as the box against this API. So the box asks here, per chat,
    and hands the agent what comes back — a token scoped to the gateway and
    bound to this chat. Only the publisher may ask: the same decision as its
    state report (``WRITE`` on ``chat.access``).

    Who pays follows the parent. A box on a person's session mints the token
    UNDER that session, billed to that person and revoked with the session and
    their membership; the caller must be a session with a ``jti``. A box on
    its own machine credential mints under the credential, billed to the
    chat's payer while the credential and the payer's membership stand. A
    payer who can no longer read the chat is refused (``payer_lost_access``).

    Args:
        chat_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatGatewayTokenRead | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        chat_id=chat_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    chat_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> ChatGatewayTokenRead | ErrorEnvelope | None:
    """Mint Chat Gateway Token

     The chat's gateway credential, for the machine that publishes it.

    The agent that runs a chat must hold a credential the gateway accepts and
    nothing else accepts: the box's own bearer would let a prompt-injected
    agent act as the box against this API. So the box asks here, per chat,
    and hands the agent what comes back — a token scoped to the gateway and
    bound to this chat. Only the publisher may ask: the same decision as its
    state report (``WRITE`` on ``chat.access``).

    Who pays follows the parent. A box on a person's session mints the token
    UNDER that session, billed to that person and revoked with the session and
    their membership; the caller must be a session with a ``jti``. A box on
    its own machine credential mints under the credential, billed to the
    chat's payer while the credential and the payer's membership stand. A
    payer who can no longer read the chat is refused (``payer_lost_access``).

    Args:
        chat_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatGatewayTokenRead | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            chat_id=chat_id,
            client=client,
        )
    ).parsed
