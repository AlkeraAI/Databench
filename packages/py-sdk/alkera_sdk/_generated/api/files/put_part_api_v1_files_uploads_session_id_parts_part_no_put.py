from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.part_response import PartResponse
from ...types import Response


def _get_kwargs(
    session_id: UUID,
    part_no: int,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "put",
        "url": "/api/v1/files/uploads/{session_id}/parts/{part_no}".format(
            session_id=quote(str(session_id), safe=""),
            part_no=quote(str(part_no), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | PartResponse | None:
    if response.status_code == 200:
        response_200 = PartResponse.from_dict(response.json())

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
) -> Response[ErrorEnvelope | PartResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    session_id: UUID,
    part_no: int,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | PartResponse]:
    """Put Part

     Stream one proxied part in, verified against ``X-Part-Checksum``.

    The part is *not* wrapped in the idempotency record: a resend is already a
    no-op decided by the stored checksum, and buffering a 32 MiB body to hash it
    for a key would defeat the streaming this route exists for. The key is
    still required, so a part with no key is the same 428 as every other
    non-GET.

    Args:
        session_id (UUID):
        part_no (int):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | PartResponse]
    """

    kwargs = _get_kwargs(
        session_id=session_id,
        part_no=part_no,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    session_id: UUID,
    part_no: int,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | PartResponse | None:
    """Put Part

     Stream one proxied part in, verified against ``X-Part-Checksum``.

    The part is *not* wrapped in the idempotency record: a resend is already a
    no-op decided by the stored checksum, and buffering a 32 MiB body to hash it
    for a key would defeat the streaming this route exists for. The key is
    still required, so a part with no key is the same 428 as every other
    non-GET.

    Args:
        session_id (UUID):
        part_no (int):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | PartResponse
    """

    return sync_detailed(
        session_id=session_id,
        part_no=part_no,
        client=client,
    ).parsed


async def asyncio_detailed(
    session_id: UUID,
    part_no: int,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | PartResponse]:
    """Put Part

     Stream one proxied part in, verified against ``X-Part-Checksum``.

    The part is *not* wrapped in the idempotency record: a resend is already a
    no-op decided by the stored checksum, and buffering a 32 MiB body to hash it
    for a key would defeat the streaming this route exists for. The key is
    still required, so a part with no key is the same 428 as every other
    non-GET.

    Args:
        session_id (UUID):
        part_no (int):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | PartResponse]
    """

    kwargs = _get_kwargs(
        session_id=session_id,
        part_no=part_no,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    session_id: UUID,
    part_no: int,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | PartResponse | None:
    """Put Part

     Stream one proxied part in, verified against ``X-Part-Checksum``.

    The part is *not* wrapped in the idempotency record: a resend is already a
    no-op decided by the stored checksum, and buffering a 32 MiB body to hash it
    for a key would defeat the streaming this route exists for. The key is
    still required, so a part with no key is the same 428 as every other
    non-GET.

    Args:
        session_id (UUID):
        part_no (int):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | PartResponse
    """

    return (
        await asyncio_detailed(
            session_id=session_id,
            part_no=part_no,
            client=client,
        )
    ).parsed
