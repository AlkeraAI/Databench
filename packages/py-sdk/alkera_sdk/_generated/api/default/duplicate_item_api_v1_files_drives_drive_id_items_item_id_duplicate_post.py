from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.duplicate_item import DuplicateItem
from ...models.duplicate_result import DuplicateResult
from ...models.error_envelope import ErrorEnvelope
from ...types import Response


def _get_kwargs(
    drive_id: str,
    item_id: str,
    *,
    body: DuplicateItem,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/files/drives/{drive_id}/items/{item_id}/duplicate".format(
            drive_id=quote(str(drive_id), safe=""),
            item_id=quote(str(item_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> DuplicateResult | ErrorEnvelope | None:
    if response.status_code == 201:
        response_201 = DuplicateResult.from_dict(response.json())

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
) -> Response[DuplicateResult | ErrorEnvelope]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
    body: DuplicateItem,
) -> Response[DuplicateResult | ErrorEnvelope]:
    """Duplicate Item

     Copy a node into the caller's own drive — or a folder they name — and
    answer the copy.

    One operation for everything the tree holds: a file, a folder with all
    beneath it, a chat, a chat template, a saved query, a report. The source is
    decided as ``COPY`` (reading it is all a copy takes; the policy refuses a
    trashed or a sealed source), the destination as ``WRITE``. With no
    destination a folder object lands in the place its own kind names inside
    the caller's home — a chat in ``Chats``, a template in ``Chat Templates``,
    created if missing — and anything else in their home.

    The copy is NEW: new ids, the copier as owner, the destination's sharing
    and nothing of the source's, stamps set now. A chat's copy is a new chat
    object holding the source's transcript and no machine; it opens and binds
    like a fresh chat. A template's copy is a new template holding the same
    starting files, and unsealed, because a template is material a member is
    meant to take away. A query's or a report's copy is a new object with its
    own payload, projected into the tree like a new one. A plain folder that
    HOLDS chats or templates copies each of them as one of its own too, so a
    copied conversation is never a second door onto the source's rows.

    Synchronous, in batches that each commit: a large folder's copy is the
    same resumable operation the tracked ``/copy`` route runs, driven here to
    its end so the answer can name the node. Should the request die mid-way,
    what was committed stays as a whole tree and the operation row is the
    worker's to resume — the chat object is only made once the tree is whole,
    so a partial copy never leaves a chat with half a folder.

    Args:
        drive_id (str):
        item_id (str):
        body (DuplicateItem): The `POST …/duplicate` body.

            Both fields are optional: with no destination the copy lands in the
            caller's own drive — their home, or the place the source's own kind names
            inside it — and with no name it keeps the source's (a chat's copy is named
            for its new title).

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DuplicateResult | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
    body: DuplicateItem,
) -> DuplicateResult | ErrorEnvelope | None:
    """Duplicate Item

     Copy a node into the caller's own drive — or a folder they name — and
    answer the copy.

    One operation for everything the tree holds: a file, a folder with all
    beneath it, a chat, a chat template, a saved query, a report. The source is
    decided as ``COPY`` (reading it is all a copy takes; the policy refuses a
    trashed or a sealed source), the destination as ``WRITE``. With no
    destination a folder object lands in the place its own kind names inside
    the caller's home — a chat in ``Chats``, a template in ``Chat Templates``,
    created if missing — and anything else in their home.

    The copy is NEW: new ids, the copier as owner, the destination's sharing
    and nothing of the source's, stamps set now. A chat's copy is a new chat
    object holding the source's transcript and no machine; it opens and binds
    like a fresh chat. A template's copy is a new template holding the same
    starting files, and unsealed, because a template is material a member is
    meant to take away. A query's or a report's copy is a new object with its
    own payload, projected into the tree like a new one. A plain folder that
    HOLDS chats or templates copies each of them as one of its own too, so a
    copied conversation is never a second door onto the source's rows.

    Synchronous, in batches that each commit: a large folder's copy is the
    same resumable operation the tracked ``/copy`` route runs, driven here to
    its end so the answer can name the node. Should the request die mid-way,
    what was committed stays as a whole tree and the operation row is the
    worker's to resume — the chat object is only made once the tree is whole,
    so a partial copy never leaves a chat with half a folder.

    Args:
        drive_id (str):
        item_id (str):
        body (DuplicateItem): The `POST …/duplicate` body.

            Both fields are optional: with no destination the copy lands in the
            caller's own drive — their home, or the place the source's own kind names
            inside it — and with no name it keeps the source's (a chat's copy is named
            for its new title).

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DuplicateResult | ErrorEnvelope
    """

    return sync_detailed(
        drive_id=drive_id,
        item_id=item_id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
    body: DuplicateItem,
) -> Response[DuplicateResult | ErrorEnvelope]:
    """Duplicate Item

     Copy a node into the caller's own drive — or a folder they name — and
    answer the copy.

    One operation for everything the tree holds: a file, a folder with all
    beneath it, a chat, a chat template, a saved query, a report. The source is
    decided as ``COPY`` (reading it is all a copy takes; the policy refuses a
    trashed or a sealed source), the destination as ``WRITE``. With no
    destination a folder object lands in the place its own kind names inside
    the caller's home — a chat in ``Chats``, a template in ``Chat Templates``,
    created if missing — and anything else in their home.

    The copy is NEW: new ids, the copier as owner, the destination's sharing
    and nothing of the source's, stamps set now. A chat's copy is a new chat
    object holding the source's transcript and no machine; it opens and binds
    like a fresh chat. A template's copy is a new template holding the same
    starting files, and unsealed, because a template is material a member is
    meant to take away. A query's or a report's copy is a new object with its
    own payload, projected into the tree like a new one. A plain folder that
    HOLDS chats or templates copies each of them as one of its own too, so a
    copied conversation is never a second door onto the source's rows.

    Synchronous, in batches that each commit: a large folder's copy is the
    same resumable operation the tracked ``/copy`` route runs, driven here to
    its end so the answer can name the node. Should the request die mid-way,
    what was committed stays as a whole tree and the operation row is the
    worker's to resume — the chat object is only made once the tree is whole,
    so a partial copy never leaves a chat with half a folder.

    Args:
        drive_id (str):
        item_id (str):
        body (DuplicateItem): The `POST …/duplicate` body.

            Both fields are optional: with no destination the copy lands in the
            caller's own drive — their home, or the place the source's own kind names
            inside it — and with no name it keeps the source's (a chat's copy is named
            for its new title).

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DuplicateResult | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
    body: DuplicateItem,
) -> DuplicateResult | ErrorEnvelope | None:
    """Duplicate Item

     Copy a node into the caller's own drive — or a folder they name — and
    answer the copy.

    One operation for everything the tree holds: a file, a folder with all
    beneath it, a chat, a chat template, a saved query, a report. The source is
    decided as ``COPY`` (reading it is all a copy takes; the policy refuses a
    trashed or a sealed source), the destination as ``WRITE``. With no
    destination a folder object lands in the place its own kind names inside
    the caller's home — a chat in ``Chats``, a template in ``Chat Templates``,
    created if missing — and anything else in their home.

    The copy is NEW: new ids, the copier as owner, the destination's sharing
    and nothing of the source's, stamps set now. A chat's copy is a new chat
    object holding the source's transcript and no machine; it opens and binds
    like a fresh chat. A template's copy is a new template holding the same
    starting files, and unsealed, because a template is material a member is
    meant to take away. A query's or a report's copy is a new object with its
    own payload, projected into the tree like a new one. A plain folder that
    HOLDS chats or templates copies each of them as one of its own too, so a
    copied conversation is never a second door onto the source's rows.

    Synchronous, in batches that each commit: a large folder's copy is the
    same resumable operation the tracked ``/copy`` route runs, driven here to
    its end so the answer can name the node. Should the request die mid-way,
    what was committed stays as a whole tree and the operation row is the
    worker's to resume — the chat object is only made once the tree is whole,
    so a partial copy never leaves a chat with half a folder.

    Args:
        drive_id (str):
        item_id (str):
        body (DuplicateItem): The `POST …/duplicate` body.

            Both fields are optional: with no destination the copy lands in the
            caller's own drive — their home, or the place the source's own kind names
            inside it — and with no name it keeps the source's (a chat's copy is named
            for its new title).

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DuplicateResult | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            client=client,
            body=body,
        )
    ).parsed
