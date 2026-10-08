from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...types import Response


def _get_kwargs(
    chat_id: UUID,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/chats/{chat_id}/stop".format(
            chat_id=quote(str(chat_id), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Any | ErrorEnvelope | None:
    if response.status_code == 202:
        response_202 = response.json()
        return response_202

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
    *,
    client: AuthenticatedClient | Client,
) -> Response[Any | ErrorEnvelope]:
    """Stop Chat Turn

     Stop the turn this chat's session is running.

    Stopping is speaking in the chat — it decides what the agent does next —
    so the gate is ``SEND``, the same one a message, an answer and a mode
    switch pass. A reader who may say something here may end what they started.

    The relay is how a box running the chat right now cancels its turn, and a
    box that is not running it ignores the relay exactly as it ignores a mode
    switch for a chat it does not hold — which is why the 202 says the stop was
    relayed and never that a turn was caught.

    What is durable is what the transcript keeps, and it is recorded BEFORE the
    relay so nothing depends on a box being there to hear it. Two facts: the
    line naming who ended the turn, because only the server knows which member
    pressed Stop; and, for every message no machine has begun answering, that
    it was never run. That second one is what makes a Stop pressed in the
    window between sending a message and the box starting it mean anything at
    all — the message is a row, and a row a reader wrote and nothing answered
    is what the next box to open the chat would otherwise pick up and run.

    Args:
        chat_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ErrorEnvelope]
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
) -> Any | ErrorEnvelope | None:
    """Stop Chat Turn

     Stop the turn this chat's session is running.

    Stopping is speaking in the chat — it decides what the agent does next —
    so the gate is ``SEND``, the same one a message, an answer and a mode
    switch pass. A reader who may say something here may end what they started.

    The relay is how a box running the chat right now cancels its turn, and a
    box that is not running it ignores the relay exactly as it ignores a mode
    switch for a chat it does not hold — which is why the 202 says the stop was
    relayed and never that a turn was caught.

    What is durable is what the transcript keeps, and it is recorded BEFORE the
    relay so nothing depends on a box being there to hear it. Two facts: the
    line naming who ended the turn, because only the server knows which member
    pressed Stop; and, for every message no machine has begun answering, that
    it was never run. That second one is what makes a Stop pressed in the
    window between sending a message and the box starting it mean anything at
    all — the message is a row, and a row a reader wrote and nothing answered
    is what the next box to open the chat would otherwise pick up and run.

    Args:
        chat_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ErrorEnvelope
    """

    return sync_detailed(
        chat_id=chat_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    chat_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Response[Any | ErrorEnvelope]:
    """Stop Chat Turn

     Stop the turn this chat's session is running.

    Stopping is speaking in the chat — it decides what the agent does next —
    so the gate is ``SEND``, the same one a message, an answer and a mode
    switch pass. A reader who may say something here may end what they started.

    The relay is how a box running the chat right now cancels its turn, and a
    box that is not running it ignores the relay exactly as it ignores a mode
    switch for a chat it does not hold — which is why the 202 says the stop was
    relayed and never that a turn was caught.

    What is durable is what the transcript keeps, and it is recorded BEFORE the
    relay so nothing depends on a box being there to hear it. Two facts: the
    line naming who ended the turn, because only the server knows which member
    pressed Stop; and, for every message no machine has begun answering, that
    it was never run. That second one is what makes a Stop pressed in the
    window between sending a message and the box starting it mean anything at
    all — the message is a row, and a row a reader wrote and nothing answered
    is what the next box to open the chat would otherwise pick up and run.

    Args:
        chat_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ErrorEnvelope]
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
) -> Any | ErrorEnvelope | None:
    """Stop Chat Turn

     Stop the turn this chat's session is running.

    Stopping is speaking in the chat — it decides what the agent does next —
    so the gate is ``SEND``, the same one a message, an answer and a mode
    switch pass. A reader who may say something here may end what they started.

    The relay is how a box running the chat right now cancels its turn, and a
    box that is not running it ignores the relay exactly as it ignores a mode
    switch for a chat it does not hold — which is why the 202 says the stop was
    relayed and never that a turn was caught.

    What is durable is what the transcript keeps, and it is recorded BEFORE the
    relay so nothing depends on a box being there to hear it. Two facts: the
    line naming who ended the turn, because only the server knows which member
    pressed Stop; and, for every message no machine has begun answering, that
    it was never run. That second one is what makes a Stop pressed in the
    window between sending a message and the box starting it mean anything at
    all — the message is a row, and a row a reader wrote and nothing answered
    is what the next box to open the chat would otherwise pick up and run.

    Args:
        chat_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            chat_id=chat_id,
            client=client,
        )
    ).parsed
