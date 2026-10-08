from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.digest_answer import DigestAnswer
from ...models.digest_request import DigestRequest
from ...models.error_envelope import ErrorEnvelope
from ...types import Response


def _get_kwargs(
    drive_id: str,
    item_id: UUID,
    *,
    body: DigestRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/files/drives/{drive_id}/items/{item_id}/lease/tree/digests".format(
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
) -> DigestAnswer | ErrorEnvelope | None:
    if response.status_code == 200:
        response_200 = DigestAnswer.from_dict(response.json())

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
) -> Response[DigestAnswer | ErrorEnvelope]:
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
    body: DigestRequest,
) -> Response[DigestAnswer | ErrorEnvelope]:
    """Lease Tree Digests

     The drive's digest of each folder the holder's walk names, so the walk
    sends only the folders that differ, and the names of the children of the
    folders it asks to list, so the walk learns what its disk no longer has.

    Holder only and fenced like the tree report; a read, in one statement
    whatever the number of folders, and one more whatever the number listed.
    The body may arrive gzipped. The answer is rendered with every non-ASCII
    character escaped: a name that is not UTF-8 travels as its surrogate
    escapes, which no UTF-8 encoder would write.

    Args:
        drive_id (str):
        item_id (UUID):
        body (DigestRequest): The folders a holder's walk wants the drive's digests of.

            Relative to the leased folder, ``/``-separated, ``""`` for the folder
            itself; the same byte rules as a tree report's paths.

            ``names`` asks, for some of those folders, the names of their children as
            the drive files them: what a walk that found a folder differing needs to
            learn which of the drive's rows its disk no longer has. Each must also be
            one of ``paths``.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DigestAnswer | ErrorEnvelope]
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
    body: DigestRequest,
) -> DigestAnswer | ErrorEnvelope | None:
    """Lease Tree Digests

     The drive's digest of each folder the holder's walk names, so the walk
    sends only the folders that differ, and the names of the children of the
    folders it asks to list, so the walk learns what its disk no longer has.

    Holder only and fenced like the tree report; a read, in one statement
    whatever the number of folders, and one more whatever the number listed.
    The body may arrive gzipped. The answer is rendered with every non-ASCII
    character escaped: a name that is not UTF-8 travels as its surrogate
    escapes, which no UTF-8 encoder would write.

    Args:
        drive_id (str):
        item_id (UUID):
        body (DigestRequest): The folders a holder's walk wants the drive's digests of.

            Relative to the leased folder, ``/``-separated, ``""`` for the folder
            itself; the same byte rules as a tree report's paths.

            ``names`` asks, for some of those folders, the names of their children as
            the drive files them: what a walk that found a folder differing needs to
            learn which of the drive's rows its disk no longer has. Each must also be
            one of ``paths``.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DigestAnswer | ErrorEnvelope
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
    body: DigestRequest,
) -> Response[DigestAnswer | ErrorEnvelope]:
    """Lease Tree Digests

     The drive's digest of each folder the holder's walk names, so the walk
    sends only the folders that differ, and the names of the children of the
    folders it asks to list, so the walk learns what its disk no longer has.

    Holder only and fenced like the tree report; a read, in one statement
    whatever the number of folders, and one more whatever the number listed.
    The body may arrive gzipped. The answer is rendered with every non-ASCII
    character escaped: a name that is not UTF-8 travels as its surrogate
    escapes, which no UTF-8 encoder would write.

    Args:
        drive_id (str):
        item_id (UUID):
        body (DigestRequest): The folders a holder's walk wants the drive's digests of.

            Relative to the leased folder, ``/``-separated, ``""`` for the folder
            itself; the same byte rules as a tree report's paths.

            ``names`` asks, for some of those folders, the names of their children as
            the drive files them: what a walk that found a folder differing needs to
            learn which of the drive's rows its disk no longer has. Each must also be
            one of ``paths``.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DigestAnswer | ErrorEnvelope]
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
    body: DigestRequest,
) -> DigestAnswer | ErrorEnvelope | None:
    """Lease Tree Digests

     The drive's digest of each folder the holder's walk names, so the walk
    sends only the folders that differ, and the names of the children of the
    folders it asks to list, so the walk learns what its disk no longer has.

    Holder only and fenced like the tree report; a read, in one statement
    whatever the number of folders, and one more whatever the number listed.
    The body may arrive gzipped. The answer is rendered with every non-ASCII
    character escaped: a name that is not UTF-8 travels as its surrogate
    escapes, which no UTF-8 encoder would write.

    Args:
        drive_id (str):
        item_id (UUID):
        body (DigestRequest): The folders a holder's walk wants the drive's digests of.

            Relative to the leased folder, ``/``-separated, ``""`` for the folder
            itself; the same byte rules as a tree report's paths.

            ``names`` asks, for some of those folders, the names of their children as
            the drive files them: what a walk that found a folder differing needs to
            learn which of the drive's rows its disk no longer has. Each must also be
            one of ``paths``.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DigestAnswer | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            client=client,
            body=body,
        )
    ).parsed
