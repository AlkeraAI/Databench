from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.restore_request import RestoreRequest
from ...types import Response


def _get_kwargs(
    drive_id: UUID,
    op_id: UUID,
    *,
    body: RestoreRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/files/drives/{drive_id}/trash/{op_id}/restore".format(
            drive_id=quote(str(drive_id), safe=""),
            op_id=quote(str(op_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Any | ErrorEnvelope | None:
    if response.status_code == 200:
        response_200 = response.json()
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
) -> Response[Any | ErrorEnvelope]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    drive_id: UUID,
    op_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: RestoreRequest,
) -> Response[Any | ErrorEnvelope]:
    """Restore From Trash

     Bring one deletion back, at its old place or a named one.

    The precondition names the trash op, which is an immutable record of one
    deletion and so carries no etag of its own to compare: the header is
    required because every mutation carries an ``If-Match``, with no exception, and
    the row itself is what makes the request replay-safe — a second restore of
    a settled op finds nothing to bring back rather than moving a live tree.

    Args:
        drive_id (UUID):
        op_id (UUID):
        body (RestoreRequest): Where a restore should land. Omit ``parentId`` for the original
            place.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        op_id=op_id,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    drive_id: UUID,
    op_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: RestoreRequest,
) -> Any | ErrorEnvelope | None:
    """Restore From Trash

     Bring one deletion back, at its old place or a named one.

    The precondition names the trash op, which is an immutable record of one
    deletion and so carries no etag of its own to compare: the header is
    required because every mutation carries an ``If-Match``, with no exception, and
    the row itself is what makes the request replay-safe — a second restore of
    a settled op finds nothing to bring back rather than moving a live tree.

    Args:
        drive_id (UUID):
        op_id (UUID):
        body (RestoreRequest): Where a restore should land. Omit ``parentId`` for the original
            place.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ErrorEnvelope
    """

    return sync_detailed(
        drive_id=drive_id,
        op_id=op_id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    drive_id: UUID,
    op_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: RestoreRequest,
) -> Response[Any | ErrorEnvelope]:
    """Restore From Trash

     Bring one deletion back, at its old place or a named one.

    The precondition names the trash op, which is an immutable record of one
    deletion and so carries no etag of its own to compare: the header is
    required because every mutation carries an ``If-Match``, with no exception, and
    the row itself is what makes the request replay-safe — a second restore of
    a settled op finds nothing to bring back rather than moving a live tree.

    Args:
        drive_id (UUID):
        op_id (UUID):
        body (RestoreRequest): Where a restore should land. Omit ``parentId`` for the original
            place.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        op_id=op_id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: UUID,
    op_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: RestoreRequest,
) -> Any | ErrorEnvelope | None:
    """Restore From Trash

     Bring one deletion back, at its old place or a named one.

    The precondition names the trash op, which is an immutable record of one
    deletion and so carries no etag of its own to compare: the header is
    required because every mutation carries an ``If-Match``, with no exception, and
    the row itself is what makes the request replay-safe — a second restore of
    a settled op finds nothing to bring back rather than moving a live tree.

    Args:
        drive_id (UUID):
        op_id (UUID):
        body (RestoreRequest): Where a restore should land. Omit ``parentId`` for the original
            place.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            op_id=op_id,
            client=client,
            body=body,
        )
    ).parsed
