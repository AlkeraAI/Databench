from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.chat_message_list import ChatMessageList
from ...models.error_envelope import ErrorEnvelope
from ...types import UNSET, Response, Unset


def _get_kwargs(
    chat_id: UUID,
    *,
    after_seq: int | Unset = 0,
    limit: int | Unset = 200,
    before: int | None | Unset = UNSET,
    tail: bool | Unset = False,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["after_seq"] = after_seq

    params["limit"] = limit

    json_before: int | None | Unset
    if isinstance(before, Unset):
        json_before = UNSET
    else:
        json_before = before
    params["before"] = json_before

    params["tail"] = tail

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/chats/{chat_id}/messages".format(
            chat_id=quote(str(chat_id), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ChatMessageList | ErrorEnvelope | None:
    if response.status_code == 200:
        response_200 = ChatMessageList.from_dict(response.json())

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
) -> Response[ChatMessageList | ErrorEnvelope]:
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
    after_seq: int | Unset = 0,
    limit: int | Unset = 200,
    before: int | None | Unset = UNSET,
    tail: bool | Unset = False,
) -> Response[ChatMessageList | ErrorEnvelope]:
    """List Chat Messages

     A page of transcript: after ``after_seq`` (the live tail's forward read),
    or — with ``tail`` / ``before`` — the newest page and the pages below it,
    which is how a reader opens a long chat on its last turn and scrolls up.

    A backward page comes back ascending, sized ``limit`` and then lowered
    until its first row BOTH opens the turn it belongs to and opens every
    message the page carries. Two reaches do that, run alternately because
    neither settles it alone: a prompt the reader sent while the machine was
    still writing sits INSIDE that answer's rows, so anchoring on it shows the
    answer's end; and the answer's own first row sits one row under the prompt
    that turn began at, so stopping there opens on the box's echo of the
    person's message instead of on the message itself. The descent stops
    ``limit * (chat_page_turn_reach + chat_page_message_reach)`` rows under the
    page, so one page is at most ``limit`` plus that — 1400 rows at the default;
    past that — or for a message whose rows are further apart than the read
    goes — the page says ``cut`` and the page below carries the rest.
    ``prev_before`` is the next ``before`` and ``has_older`` says whether one
    exists. When the
    forward read asks for messages older than the oldest one still held, the
    page carries ``resync_from`` — an in-band "start again from here" rather
    than an error the client needs a branch for.

    Args:
        chat_id (UUID):
        after_seq (int | Unset):  Default: 0.
        limit (int | Unset):  Default: 200.
        before (int | None | Unset):
        tail (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatMessageList | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        chat_id=chat_id,
        after_seq=after_seq,
        limit=limit,
        before=before,
        tail=tail,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    chat_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    after_seq: int | Unset = 0,
    limit: int | Unset = 200,
    before: int | None | Unset = UNSET,
    tail: bool | Unset = False,
) -> ChatMessageList | ErrorEnvelope | None:
    """List Chat Messages

     A page of transcript: after ``after_seq`` (the live tail's forward read),
    or — with ``tail`` / ``before`` — the newest page and the pages below it,
    which is how a reader opens a long chat on its last turn and scrolls up.

    A backward page comes back ascending, sized ``limit`` and then lowered
    until its first row BOTH opens the turn it belongs to and opens every
    message the page carries. Two reaches do that, run alternately because
    neither settles it alone: a prompt the reader sent while the machine was
    still writing sits INSIDE that answer's rows, so anchoring on it shows the
    answer's end; and the answer's own first row sits one row under the prompt
    that turn began at, so stopping there opens on the box's echo of the
    person's message instead of on the message itself. The descent stops
    ``limit * (chat_page_turn_reach + chat_page_message_reach)`` rows under the
    page, so one page is at most ``limit`` plus that — 1400 rows at the default;
    past that — or for a message whose rows are further apart than the read
    goes — the page says ``cut`` and the page below carries the rest.
    ``prev_before`` is the next ``before`` and ``has_older`` says whether one
    exists. When the
    forward read asks for messages older than the oldest one still held, the
    page carries ``resync_from`` — an in-band "start again from here" rather
    than an error the client needs a branch for.

    Args:
        chat_id (UUID):
        after_seq (int | Unset):  Default: 0.
        limit (int | Unset):  Default: 200.
        before (int | None | Unset):
        tail (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatMessageList | ErrorEnvelope
    """

    return sync_detailed(
        chat_id=chat_id,
        client=client,
        after_seq=after_seq,
        limit=limit,
        before=before,
        tail=tail,
    ).parsed


async def asyncio_detailed(
    chat_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    after_seq: int | Unset = 0,
    limit: int | Unset = 200,
    before: int | None | Unset = UNSET,
    tail: bool | Unset = False,
) -> Response[ChatMessageList | ErrorEnvelope]:
    """List Chat Messages

     A page of transcript: after ``after_seq`` (the live tail's forward read),
    or — with ``tail`` / ``before`` — the newest page and the pages below it,
    which is how a reader opens a long chat on its last turn and scrolls up.

    A backward page comes back ascending, sized ``limit`` and then lowered
    until its first row BOTH opens the turn it belongs to and opens every
    message the page carries. Two reaches do that, run alternately because
    neither settles it alone: a prompt the reader sent while the machine was
    still writing sits INSIDE that answer's rows, so anchoring on it shows the
    answer's end; and the answer's own first row sits one row under the prompt
    that turn began at, so stopping there opens on the box's echo of the
    person's message instead of on the message itself. The descent stops
    ``limit * (chat_page_turn_reach + chat_page_message_reach)`` rows under the
    page, so one page is at most ``limit`` plus that — 1400 rows at the default;
    past that — or for a message whose rows are further apart than the read
    goes — the page says ``cut`` and the page below carries the rest.
    ``prev_before`` is the next ``before`` and ``has_older`` says whether one
    exists. When the
    forward read asks for messages older than the oldest one still held, the
    page carries ``resync_from`` — an in-band "start again from here" rather
    than an error the client needs a branch for.

    Args:
        chat_id (UUID):
        after_seq (int | Unset):  Default: 0.
        limit (int | Unset):  Default: 200.
        before (int | None | Unset):
        tail (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatMessageList | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        chat_id=chat_id,
        after_seq=after_seq,
        limit=limit,
        before=before,
        tail=tail,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    chat_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    after_seq: int | Unset = 0,
    limit: int | Unset = 200,
    before: int | None | Unset = UNSET,
    tail: bool | Unset = False,
) -> ChatMessageList | ErrorEnvelope | None:
    """List Chat Messages

     A page of transcript: after ``after_seq`` (the live tail's forward read),
    or — with ``tail`` / ``before`` — the newest page and the pages below it,
    which is how a reader opens a long chat on its last turn and scrolls up.

    A backward page comes back ascending, sized ``limit`` and then lowered
    until its first row BOTH opens the turn it belongs to and opens every
    message the page carries. Two reaches do that, run alternately because
    neither settles it alone: a prompt the reader sent while the machine was
    still writing sits INSIDE that answer's rows, so anchoring on it shows the
    answer's end; and the answer's own first row sits one row under the prompt
    that turn began at, so stopping there opens on the box's echo of the
    person's message instead of on the message itself. The descent stops
    ``limit * (chat_page_turn_reach + chat_page_message_reach)`` rows under the
    page, so one page is at most ``limit`` plus that — 1400 rows at the default;
    past that — or for a message whose rows are further apart than the read
    goes — the page says ``cut`` and the page below carries the rest.
    ``prev_before`` is the next ``before`` and ``has_older`` says whether one
    exists. When the
    forward read asks for messages older than the oldest one still held, the
    page carries ``resync_from`` — an in-band "start again from here" rather
    than an error the client needs a branch for.

    Args:
        chat_id (UUID):
        after_seq (int | Unset):  Default: 0.
        limit (int | Unset):  Default: 200.
        before (int | None | Unset):
        tail (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatMessageList | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            chat_id=chat_id,
            client=client,
            after_seq=after_seq,
            limit=limit,
            before=before,
            tail=tail,
        )
    ).parsed
