from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.chat_interrupt_answer import ChatInterruptAnswer
from ...models.error_envelope import ErrorEnvelope
from ...types import Response


def _get_kwargs(
    chat_id: UUID,
    *,
    body: ChatInterruptAnswer,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/chats/{chat_id}/answer".format(
            chat_id=quote(str(chat_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
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
    body: ChatInterruptAnswer,
) -> Response[Any | ErrorEnvelope]:
    """Answer Chat Interrupt

     Answer an ask the agent is blocked on: recorded, then relayed.

    A permission or question ask is raised by the harness on the machine and
    waits there — for an hour, overnight, past the box's sleep. Answering it is
    speaking in the chat, so the gate is ``SEND``, the same one a message
    passes. Unlike a message the answer is not a prompt row: it is recorded as
    the ask's resolution (``permission.resolved`` / ``question.answered`` /
    ``question.rejected``, decided by this user), the very event the machine
    publishes when its harness settles an ask, so the transcript is the one
    record of what was asked and what was decided.

    The row is the durable answer. A box holding the ask hears the relay and
    settles it at once; a chat asleep, or whose box is gone, has nobody to
    relay to, and the row is what the next box reads when it opens the chat —
    it acts on the decision instead of asking the person a second time. The
    ask must be one the transcript holds (an id nobody asked is a 404
    ``ask_not_found``), still open (a second answer is a 409
    ``ask_already_answered``), and the answer one the ask can take (an option
    it never offered, a question's answers to a permission: 422
    ``answer_does_not_fit_ask``). An "Always" option becomes the chat owner's
    rule in their other chats, so it is decided again as ``ANSWER_STANDING``.
    Whether an allow may approve a write stays the machine's (the fence's).
    The caller is stamped onto the resolution: the option alone does not say
    whose call it was, and the roster lives here, not on the machine.

    Args:
        chat_id (UUID):
        body (ChatInterruptAnswer): An answer to an ask the agent is blocked on.

            Not a message: an ask is answered, not said, so this makes no transcript
            entry — the machine records the resolution as the harness settles it. The
            shape is the relay the daemon validates: it names the outstanding ask by id
            and carries exactly one answer, which is either a permission option the ask
            itself offered, a set of answers to a question's prompts, or a rejection.

            The server does not judge whether the answer is allowed to have that effect;
            the machine does (it refuses an option the ask never offered, an id that is
            not outstanding, and any approval of a write on a read-only session). What
            the shape enforces is that exactly one answer arrives, so an ambiguous relay
            never reaches the machine to be resolved by field order.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ErrorEnvelope]
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
    body: ChatInterruptAnswer,
) -> Any | ErrorEnvelope | None:
    """Answer Chat Interrupt

     Answer an ask the agent is blocked on: recorded, then relayed.

    A permission or question ask is raised by the harness on the machine and
    waits there — for an hour, overnight, past the box's sleep. Answering it is
    speaking in the chat, so the gate is ``SEND``, the same one a message
    passes. Unlike a message the answer is not a prompt row: it is recorded as
    the ask's resolution (``permission.resolved`` / ``question.answered`` /
    ``question.rejected``, decided by this user), the very event the machine
    publishes when its harness settles an ask, so the transcript is the one
    record of what was asked and what was decided.

    The row is the durable answer. A box holding the ask hears the relay and
    settles it at once; a chat asleep, or whose box is gone, has nobody to
    relay to, and the row is what the next box reads when it opens the chat —
    it acts on the decision instead of asking the person a second time. The
    ask must be one the transcript holds (an id nobody asked is a 404
    ``ask_not_found``), still open (a second answer is a 409
    ``ask_already_answered``), and the answer one the ask can take (an option
    it never offered, a question's answers to a permission: 422
    ``answer_does_not_fit_ask``). An "Always" option becomes the chat owner's
    rule in their other chats, so it is decided again as ``ANSWER_STANDING``.
    Whether an allow may approve a write stays the machine's (the fence's).
    The caller is stamped onto the resolution: the option alone does not say
    whose call it was, and the roster lives here, not on the machine.

    Args:
        chat_id (UUID):
        body (ChatInterruptAnswer): An answer to an ask the agent is blocked on.

            Not a message: an ask is answered, not said, so this makes no transcript
            entry — the machine records the resolution as the harness settles it. The
            shape is the relay the daemon validates: it names the outstanding ask by id
            and carries exactly one answer, which is either a permission option the ask
            itself offered, a set of answers to a question's prompts, or a rejection.

            The server does not judge whether the answer is allowed to have that effect;
            the machine does (it refuses an option the ask never offered, an id that is
            not outstanding, and any approval of a write on a read-only session). What
            the shape enforces is that exactly one answer arrives, so an ambiguous relay
            never reaches the machine to be resolved by field order.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ErrorEnvelope
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
    body: ChatInterruptAnswer,
) -> Response[Any | ErrorEnvelope]:
    """Answer Chat Interrupt

     Answer an ask the agent is blocked on: recorded, then relayed.

    A permission or question ask is raised by the harness on the machine and
    waits there — for an hour, overnight, past the box's sleep. Answering it is
    speaking in the chat, so the gate is ``SEND``, the same one a message
    passes. Unlike a message the answer is not a prompt row: it is recorded as
    the ask's resolution (``permission.resolved`` / ``question.answered`` /
    ``question.rejected``, decided by this user), the very event the machine
    publishes when its harness settles an ask, so the transcript is the one
    record of what was asked and what was decided.

    The row is the durable answer. A box holding the ask hears the relay and
    settles it at once; a chat asleep, or whose box is gone, has nobody to
    relay to, and the row is what the next box reads when it opens the chat —
    it acts on the decision instead of asking the person a second time. The
    ask must be one the transcript holds (an id nobody asked is a 404
    ``ask_not_found``), still open (a second answer is a 409
    ``ask_already_answered``), and the answer one the ask can take (an option
    it never offered, a question's answers to a permission: 422
    ``answer_does_not_fit_ask``). An "Always" option becomes the chat owner's
    rule in their other chats, so it is decided again as ``ANSWER_STANDING``.
    Whether an allow may approve a write stays the machine's (the fence's).
    The caller is stamped onto the resolution: the option alone does not say
    whose call it was, and the roster lives here, not on the machine.

    Args:
        chat_id (UUID):
        body (ChatInterruptAnswer): An answer to an ask the agent is blocked on.

            Not a message: an ask is answered, not said, so this makes no transcript
            entry — the machine records the resolution as the harness settles it. The
            shape is the relay the daemon validates: it names the outstanding ask by id
            and carries exactly one answer, which is either a permission option the ask
            itself offered, a set of answers to a question's prompts, or a rejection.

            The server does not judge whether the answer is allowed to have that effect;
            the machine does (it refuses an option the ask never offered, an id that is
            not outstanding, and any approval of a write on a read-only session). What
            the shape enforces is that exactly one answer arrives, so an ambiguous relay
            never reaches the machine to be resolved by field order.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ErrorEnvelope]
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
    body: ChatInterruptAnswer,
) -> Any | ErrorEnvelope | None:
    """Answer Chat Interrupt

     Answer an ask the agent is blocked on: recorded, then relayed.

    A permission or question ask is raised by the harness on the machine and
    waits there — for an hour, overnight, past the box's sleep. Answering it is
    speaking in the chat, so the gate is ``SEND``, the same one a message
    passes. Unlike a message the answer is not a prompt row: it is recorded as
    the ask's resolution (``permission.resolved`` / ``question.answered`` /
    ``question.rejected``, decided by this user), the very event the machine
    publishes when its harness settles an ask, so the transcript is the one
    record of what was asked and what was decided.

    The row is the durable answer. A box holding the ask hears the relay and
    settles it at once; a chat asleep, or whose box is gone, has nobody to
    relay to, and the row is what the next box reads when it opens the chat —
    it acts on the decision instead of asking the person a second time. The
    ask must be one the transcript holds (an id nobody asked is a 404
    ``ask_not_found``), still open (a second answer is a 409
    ``ask_already_answered``), and the answer one the ask can take (an option
    it never offered, a question's answers to a permission: 422
    ``answer_does_not_fit_ask``). An "Always" option becomes the chat owner's
    rule in their other chats, so it is decided again as ``ANSWER_STANDING``.
    Whether an allow may approve a write stays the machine's (the fence's).
    The caller is stamped onto the resolution: the option alone does not say
    whose call it was, and the roster lives here, not on the machine.

    Args:
        chat_id (UUID):
        body (ChatInterruptAnswer): An answer to an ask the agent is blocked on.

            Not a message: an ask is answered, not said, so this makes no transcript
            entry — the machine records the resolution as the harness settles it. The
            shape is the relay the daemon validates: it names the outstanding ask by id
            and carries exactly one answer, which is either a permission option the ask
            itself offered, a set of answers to a question's prompts, or a rejection.

            The server does not judge whether the answer is allowed to have that effect;
            the machine does (it refuses an option the ask never offered, an id that is
            not outstanding, and any approval of a write on a read-only session). What
            the shape enforces is that exactly one answer arrives, so an ambiguous relay
            never reaches the machine to be resolved by field order.

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
            body=body,
        )
    ).parsed
