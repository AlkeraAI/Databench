from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.operation_wire import OperationWire
from ...types import Response


def _get_kwargs(
    drive_id: UUID,
    item_id: UUID,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/files/drives/{drive_id}/items/{item_id}/download".format(
            drive_id=quote(str(drive_id), safe=""),
            item_id=quote(str(item_id), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | OperationWire | None:
    if response.status_code == 202:
        response_202 = OperationWire.from_dict(response.json())

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
) -> Response[ErrorEnvelope | OperationWire]:
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
) -> Response[ErrorEnvelope | OperationWire]:
    """Download Subtree

     Start a download of a subtree as one ZIP64 archive.

    How much the request itself does depends on how big the subtree is, and the
    size is asked with a count rather than by reading the rows. Under
    ``FILES_DOWNLOAD_INLINE_MAX_NODES`` the walk runs here, a page at a time,
    so the operation comes back with its ``errors[]`` already listing by id
    everything the archive will not contain. Above it the request answers with
    the subtree's size alone: enumerating a hundred thousand omissions into one
    column is not a better answer than the archive's own ``skipped.txt``, which
    is decided fresh at redemption anyway.

    Either way the bytes happen only when the signed ``resultUrl`` is redeemed,
    streamed, and never buffered.

    Args:
        drive_id (UUID):
        item_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OperationWire]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
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
) -> ErrorEnvelope | OperationWire | None:
    """Download Subtree

     Start a download of a subtree as one ZIP64 archive.

    How much the request itself does depends on how big the subtree is, and the
    size is asked with a count rather than by reading the rows. Under
    ``FILES_DOWNLOAD_INLINE_MAX_NODES`` the walk runs here, a page at a time,
    so the operation comes back with its ``errors[]`` already listing by id
    everything the archive will not contain. Above it the request answers with
    the subtree's size alone: enumerating a hundred thousand omissions into one
    column is not a better answer than the archive's own ``skipped.txt``, which
    is decided fresh at redemption anyway.

    Either way the bytes happen only when the signed ``resultUrl`` is redeemed,
    streamed, and never buffered.

    Args:
        drive_id (UUID):
        item_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OperationWire
    """

    return sync_detailed(
        drive_id=drive_id,
        item_id=item_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    drive_id: UUID,
    item_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | OperationWire]:
    """Download Subtree

     Start a download of a subtree as one ZIP64 archive.

    How much the request itself does depends on how big the subtree is, and the
    size is asked with a count rather than by reading the rows. Under
    ``FILES_DOWNLOAD_INLINE_MAX_NODES`` the walk runs here, a page at a time,
    so the operation comes back with its ``errors[]`` already listing by id
    everything the archive will not contain. Above it the request answers with
    the subtree's size alone: enumerating a hundred thousand omissions into one
    column is not a better answer than the archive's own ``skipped.txt``, which
    is decided fresh at redemption anyway.

    Either way the bytes happen only when the signed ``resultUrl`` is redeemed,
    streamed, and never buffered.

    Args:
        drive_id (UUID):
        item_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OperationWire]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: UUID,
    item_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | OperationWire | None:
    """Download Subtree

     Start a download of a subtree as one ZIP64 archive.

    How much the request itself does depends on how big the subtree is, and the
    size is asked with a count rather than by reading the rows. Under
    ``FILES_DOWNLOAD_INLINE_MAX_NODES`` the walk runs here, a page at a time,
    so the operation comes back with its ``errors[]`` already listing by id
    everything the archive will not contain. Above it the request answers with
    the subtree's size alone: enumerating a hundred thousand omissions into one
    column is not a better answer than the archive's own ``skipped.txt``, which
    is decided fresh at redemption anyway.

    Either way the bytes happen only when the signed ``resultUrl`` is redeemed,
    streamed, and never buffered.

    Args:
        drive_id (UUID):
        item_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OperationWire
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            client=client,
        )
    ).parsed
