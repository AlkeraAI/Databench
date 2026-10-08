from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.live_batch_answer import LiveBatchAnswer
from ...models.live_batch_body import LiveBatchBody
from ...types import Response


def _get_kwargs(
    drive_id: UUID,
    item_id: UUID,
    *,
    body: LiveBatchBody,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/files/drives/{drive_id}/items/{item_id}/lease/live".format(
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
) -> ErrorEnvelope | LiveBatchAnswer | None:
    if response.status_code == 200:
        response_200 = LiveBatchAnswer.from_dict(response.json())

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
) -> Response[ErrorEnvelope | LiveBatchAnswer]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    drive_id: UUID,
    item_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: LiveBatchBody,
) -> Response[ErrorEnvelope | LiveBatchAnswer]:
    """Live Batch

     The holder's word on what it is doing to each node — and, for what the
    drive took on its behalf, that it has applied it.

    ``applied`` and ``superseded`` clear the node's row, so the next drain does
    not offer it again: a re-offered drop would be downloaded a second time
    onto a file the agent may since have edited. Fenced like every write, and
    idempotent by construction — a batch the holder resends clears nothing
    twice. Not under an idempotency key because a heartbeat-cadence report
    that is refused for a missing key is worse than one applied twice.

    Args:
        drive_id (UUID):
        item_id (UUID):
        body (LiveBatchBody): One live batch, at most ``files_live_max_batch_entries`` entries
            long.

            The same number the grant hands the holder as ``maxBatchEntries``, read
            here rather than compiled in so a deployment that lowers it lowers what it
            will accept in the same breath. A batch is one statement per entry inside
            one transaction holding the lease row, so an unbounded list is an unbounded
            transaction — and the holder is told it sent too many rather than having
            them applied.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | LiveBatchAnswer]
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
    drive_id: UUID,
    item_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: LiveBatchBody,
) -> ErrorEnvelope | LiveBatchAnswer | None:
    """Live Batch

     The holder's word on what it is doing to each node — and, for what the
    drive took on its behalf, that it has applied it.

    ``applied`` and ``superseded`` clear the node's row, so the next drain does
    not offer it again: a re-offered drop would be downloaded a second time
    onto a file the agent may since have edited. Fenced like every write, and
    idempotent by construction — a batch the holder resends clears nothing
    twice. Not under an idempotency key because a heartbeat-cadence report
    that is refused for a missing key is worse than one applied twice.

    Args:
        drive_id (UUID):
        item_id (UUID):
        body (LiveBatchBody): One live batch, at most ``files_live_max_batch_entries`` entries
            long.

            The same number the grant hands the holder as ``maxBatchEntries``, read
            here rather than compiled in so a deployment that lowers it lowers what it
            will accept in the same breath. A batch is one statement per entry inside
            one transaction holding the lease row, so an unbounded list is an unbounded
            transaction — and the holder is told it sent too many rather than having
            them applied.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | LiveBatchAnswer
    """

    return sync_detailed(
        drive_id=drive_id,
        item_id=item_id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    drive_id: UUID,
    item_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: LiveBatchBody,
) -> Response[ErrorEnvelope | LiveBatchAnswer]:
    """Live Batch

     The holder's word on what it is doing to each node — and, for what the
    drive took on its behalf, that it has applied it.

    ``applied`` and ``superseded`` clear the node's row, so the next drain does
    not offer it again: a re-offered drop would be downloaded a second time
    onto a file the agent may since have edited. Fenced like every write, and
    idempotent by construction — a batch the holder resends clears nothing
    twice. Not under an idempotency key because a heartbeat-cadence report
    that is refused for a missing key is worse than one applied twice.

    Args:
        drive_id (UUID):
        item_id (UUID):
        body (LiveBatchBody): One live batch, at most ``files_live_max_batch_entries`` entries
            long.

            The same number the grant hands the holder as ``maxBatchEntries``, read
            here rather than compiled in so a deployment that lowers it lowers what it
            will accept in the same breath. A batch is one statement per entry inside
            one transaction holding the lease row, so an unbounded list is an unbounded
            transaction — and the holder is told it sent too many rather than having
            them applied.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | LiveBatchAnswer]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: UUID,
    item_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: LiveBatchBody,
) -> ErrorEnvelope | LiveBatchAnswer | None:
    """Live Batch

     The holder's word on what it is doing to each node — and, for what the
    drive took on its behalf, that it has applied it.

    ``applied`` and ``superseded`` clear the node's row, so the next drain does
    not offer it again: a re-offered drop would be downloaded a second time
    onto a file the agent may since have edited. Fenced like every write, and
    idempotent by construction — a batch the holder resends clears nothing
    twice. Not under an idempotency key because a heartbeat-cadence report
    that is refused for a missing key is worse than one applied twice.

    Args:
        drive_id (UUID):
        item_id (UUID):
        body (LiveBatchBody): One live batch, at most ``files_live_max_batch_entries`` entries
            long.

            The same number the grant hands the holder as ``maxBatchEntries``, read
            here rather than compiled in so a deployment that lowers it lowers what it
            will accept in the same breath. A batch is one statement per entry inside
            one transaction holding the lease row, so an unbounded list is an unbounded
            transaction — and the holder is told it sent too many rather than having
            them applied.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | LiveBatchAnswer
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            client=client,
            body=body,
        )
    ).parsed
