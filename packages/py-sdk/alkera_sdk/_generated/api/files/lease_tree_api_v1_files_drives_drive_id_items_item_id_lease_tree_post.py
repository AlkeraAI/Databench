from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.tree_batch import TreeBatch
from ...models.tree_batch_answer import TreeBatchAnswer
from ...types import Response


def _get_kwargs(
    drive_id: str,
    item_id: UUID,
    *,
    body: TreeBatch,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/files/drives/{drive_id}/items/{item_id}/lease/tree".format(
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
) -> ErrorEnvelope | TreeBatchAnswer | None:
    if response.status_code == 200:
        response_200 = TreeBatchAnswer.from_dict(response.json())

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
) -> Response[ErrorEnvelope | TreeBatchAnswer]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    drive_id: str,
    item_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: TreeBatch,
) -> Response[ErrorEnvelope | TreeBatchAnswer]:
    """Lease Tree

     The holder's report of its tree: every name, kind, size and modified
    time under the folder it holds, ahead of the bytes.

    Holder only and fenced like every write the holder makes; one transaction;
    all or nothing. Rows the drive did not have are minted with no bytes and
    carry the report until the bytes land. ``batch_id`` makes a resend free: it
    answers the first answer and changes nothing. The body may arrive gzipped.

    Args:
        drive_id (str):
        item_id (UUID):
        body (TreeBatch): One flush of the holder's metadata queue.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | TreeBatchAnswer]
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
    item_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: TreeBatch,
) -> ErrorEnvelope | TreeBatchAnswer | None:
    """Lease Tree

     The holder's report of its tree: every name, kind, size and modified
    time under the folder it holds, ahead of the bytes.

    Holder only and fenced like every write the holder makes; one transaction;
    all or nothing. Rows the drive did not have are minted with no bytes and
    carry the report until the bytes land. ``batch_id`` makes a resend free: it
    answers the first answer and changes nothing. The body may arrive gzipped.

    Args:
        drive_id (str):
        item_id (UUID):
        body (TreeBatch): One flush of the holder's metadata queue.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | TreeBatchAnswer
    """

    return sync_detailed(
        drive_id=drive_id,
        item_id=item_id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    drive_id: str,
    item_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: TreeBatch,
) -> Response[ErrorEnvelope | TreeBatchAnswer]:
    """Lease Tree

     The holder's report of its tree: every name, kind, size and modified
    time under the folder it holds, ahead of the bytes.

    Holder only and fenced like every write the holder makes; one transaction;
    all or nothing. Rows the drive did not have are minted with no bytes and
    carry the report until the bytes land. ``batch_id`` makes a resend free: it
    answers the first answer and changes nothing. The body may arrive gzipped.

    Args:
        drive_id (str):
        item_id (UUID):
        body (TreeBatch): One flush of the holder's metadata queue.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | TreeBatchAnswer]
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
    item_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: TreeBatch,
) -> ErrorEnvelope | TreeBatchAnswer | None:
    """Lease Tree

     The holder's report of its tree: every name, kind, size and modified
    time under the folder it holds, ahead of the bytes.

    Holder only and fenced like every write the holder makes; one transaction;
    all or nothing. Rows the drive did not have are minted with no bytes and
    carry the report until the bytes land. ``batch_id`` makes a resend free: it
    answers the first answer and changes nothing. The body may arrive gzipped.

    Args:
        drive_id (str):
        item_id (UUID):
        body (TreeBatch): One flush of the holder's metadata queue.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | TreeBatchAnswer
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            client=client,
            body=body,
        )
    ).parsed
