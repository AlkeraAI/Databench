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
    operation_id: UUID,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/files/drives/{drive_id}/operations/{operation_id}/undo".format(
            drive_id=quote(str(drive_id), safe=""),
            operation_id=quote(str(operation_id), safe=""),
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
    operation_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | OperationWire]:
    """Undo Operation

     Apply an operation's inverse as a NEW operation.

    The answer is the new operation, not the old one: undo is forward work with
    its own id, its own progress and its own inverse, which is what makes undo
    of an undo a redo without a second code path.

    An inverse that could not run inline — an oversized restore whose re-parent
    is a queued move — is answered with THAT operation instead, through the same
    202-with-``Location`` shape the restore route uses, and its runner is
    started here. Answering with the undo row would report work as done that
    nothing had yet started.

    Args:
        drive_id (UUID):
        operation_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OperationWire]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        operation_id=operation_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    drive_id: UUID,
    operation_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | OperationWire | None:
    """Undo Operation

     Apply an operation's inverse as a NEW operation.

    The answer is the new operation, not the old one: undo is forward work with
    its own id, its own progress and its own inverse, which is what makes undo
    of an undo a redo without a second code path.

    An inverse that could not run inline — an oversized restore whose re-parent
    is a queued move — is answered with THAT operation instead, through the same
    202-with-``Location`` shape the restore route uses, and its runner is
    started here. Answering with the undo row would report work as done that
    nothing had yet started.

    Args:
        drive_id (UUID):
        operation_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OperationWire
    """

    return sync_detailed(
        drive_id=drive_id,
        operation_id=operation_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    drive_id: UUID,
    operation_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | OperationWire]:
    """Undo Operation

     Apply an operation's inverse as a NEW operation.

    The answer is the new operation, not the old one: undo is forward work with
    its own id, its own progress and its own inverse, which is what makes undo
    of an undo a redo without a second code path.

    An inverse that could not run inline — an oversized restore whose re-parent
    is a queued move — is answered with THAT operation instead, through the same
    202-with-``Location`` shape the restore route uses, and its runner is
    started here. Answering with the undo row would report work as done that
    nothing had yet started.

    Args:
        drive_id (UUID):
        operation_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OperationWire]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        operation_id=operation_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: UUID,
    operation_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | OperationWire | None:
    """Undo Operation

     Apply an operation's inverse as a NEW operation.

    The answer is the new operation, not the old one: undo is forward work with
    its own id, its own progress and its own inverse, which is what makes undo
    of an undo a redo without a second code path.

    An inverse that could not run inline — an oversized restore whose re-parent
    is a queued move — is answered with THAT operation instead, through the same
    202-with-``Location`` shape the restore route uses, and its runner is
    started here. Answering with the undo row would report work as done that
    nothing had yet started.

    Args:
        drive_id (UUID):
        operation_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OperationWire
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            operation_id=operation_id,
            client=client,
        )
    ).parsed
