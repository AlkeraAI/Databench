from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...types import Response


def _get_kwargs(
    chat_id: UUID,
    node_id: UUID,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "delete",
        "url": "/api/v1/chats/{chat_id}/attachments/{node_id}".format(
            chat_id=quote(str(chat_id), safe=""),
            node_id=quote(str(node_id), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Any | ErrorEnvelope | None:
    if response.status_code == 204:
        response_204 = cast(Any, None)
        return response_204

    if response.status_code == 422:
        response_422 = ErrorEnvelope.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[Any | ErrorEnvelope]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    chat_id: UUID,
    node_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Response[Any | ErrorEnvelope]:
    """Detach File

     Take a linked node back off a chat.

    ``SEND`` on the chat, the same decision attaching makes: putting a file
    into the conversation and taking it out again are both speaking in it, and
    neither is decided against the file — a member who has since lost read on
    the node must still be able to withdraw the attachment they made.

    Idempotent, and it never reads the node. The reference outlives what it
    points at, so a node that was trashed or purged still detaches; a second
    DELETE of the same pair is another 204.

    Args:
        chat_id (UUID):
        node_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        chat_id=chat_id,
        node_id=node_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    chat_id: UUID,
    node_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Any | ErrorEnvelope | None:
    """Detach File

     Take a linked node back off a chat.

    ``SEND`` on the chat, the same decision attaching makes: putting a file
    into the conversation and taking it out again are both speaking in it, and
    neither is decided against the file — a member who has since lost read on
    the node must still be able to withdraw the attachment they made.

    Idempotent, and it never reads the node. The reference outlives what it
    points at, so a node that was trashed or purged still detaches; a second
    DELETE of the same pair is another 204.

    Args:
        chat_id (UUID):
        node_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ErrorEnvelope
    """

    return sync_detailed(
        chat_id=chat_id,
        node_id=node_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    chat_id: UUID,
    node_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Response[Any | ErrorEnvelope]:
    """Detach File

     Take a linked node back off a chat.

    ``SEND`` on the chat, the same decision attaching makes: putting a file
    into the conversation and taking it out again are both speaking in it, and
    neither is decided against the file — a member who has since lost read on
    the node must still be able to withdraw the attachment they made.

    Idempotent, and it never reads the node. The reference outlives what it
    points at, so a node that was trashed or purged still detaches; a second
    DELETE of the same pair is another 204.

    Args:
        chat_id (UUID):
        node_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        chat_id=chat_id,
        node_id=node_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    chat_id: UUID,
    node_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Any | ErrorEnvelope | None:
    """Detach File

     Take a linked node back off a chat.

    ``SEND`` on the chat, the same decision attaching makes: putting a file
    into the conversation and taking it out again are both speaking in it, and
    neither is decided against the file — a member who has since lost read on
    the node must still be able to withdraw the attachment they made.

    Idempotent, and it never reads the node. The reference outlives what it
    points at, so a node that was trashed or purged still detaches; a second
    DELETE of the same pair is another 204.

    Args:
        chat_id (UUID):
        node_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            chat_id=chat_id,
            node_id=node_id,
            client=client,
        )
    ).parsed
