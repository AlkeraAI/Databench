from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.trash_empty_result import TrashEmptyResult
from ...types import Response


def _get_kwargs(
    drive_id: UUID,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/files/drives/{drive_id}/trash/empty".format(
            drive_id=quote(str(drive_id), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | TrashEmptyResult | None:
    if response.status_code == 200:
        response_200 = TrashEmptyResult.from_dict(response.json())

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
) -> Response[ErrorEnvelope | TrashEmptyResult]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    drive_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | TrashEmptyResult]:
    """Empty Trash

     Purge the trashed roots this caller may delete. A held subtree refuses.

    The decision is per deletion rather than per drive: emptying is not a
    drive-level verb, it is "delete each of these", and a caller who may not
    delete one of them must not have it purged by asking for all of them. What
    that leaves behind is counted and reported, because the browser's notice is
    written from this answer and "removed 50" alone reads as "the trash is now
    empty".

    The whole trash is swept, not one listing page: the listing's page size is a
    rendering budget and has nothing to do with how much a person asked to
    delete. Paging is by the listing's own marker, so a refused root advances
    the cursor past itself instead of being met again forever, and a root seen
    once is never counted twice.

    The precondition names the drive root, the one node the whole sweep is
    addressed at; it is not compared against a single trashed node because the
    sweep addresses many, and one of their etags would say nothing about the
    rest. The header is required all the same, since every mutation carries
    one, and the per-entry decision above is what actually bounds the sweep.

    Args:
        drive_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | TrashEmptyResult]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    drive_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | TrashEmptyResult | None:
    """Empty Trash

     Purge the trashed roots this caller may delete. A held subtree refuses.

    The decision is per deletion rather than per drive: emptying is not a
    drive-level verb, it is "delete each of these", and a caller who may not
    delete one of them must not have it purged by asking for all of them. What
    that leaves behind is counted and reported, because the browser's notice is
    written from this answer and "removed 50" alone reads as "the trash is now
    empty".

    The whole trash is swept, not one listing page: the listing's page size is a
    rendering budget and has nothing to do with how much a person asked to
    delete. Paging is by the listing's own marker, so a refused root advances
    the cursor past itself instead of being met again forever, and a root seen
    once is never counted twice.

    The precondition names the drive root, the one node the whole sweep is
    addressed at; it is not compared against a single trashed node because the
    sweep addresses many, and one of their etags would say nothing about the
    rest. The header is required all the same, since every mutation carries
    one, and the per-entry decision above is what actually bounds the sweep.

    Args:
        drive_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | TrashEmptyResult
    """

    return sync_detailed(
        drive_id=drive_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    drive_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | TrashEmptyResult]:
    """Empty Trash

     Purge the trashed roots this caller may delete. A held subtree refuses.

    The decision is per deletion rather than per drive: emptying is not a
    drive-level verb, it is "delete each of these", and a caller who may not
    delete one of them must not have it purged by asking for all of them. What
    that leaves behind is counted and reported, because the browser's notice is
    written from this answer and "removed 50" alone reads as "the trash is now
    empty".

    The whole trash is swept, not one listing page: the listing's page size is a
    rendering budget and has nothing to do with how much a person asked to
    delete. Paging is by the listing's own marker, so a refused root advances
    the cursor past itself instead of being met again forever, and a root seen
    once is never counted twice.

    The precondition names the drive root, the one node the whole sweep is
    addressed at; it is not compared against a single trashed node because the
    sweep addresses many, and one of their etags would say nothing about the
    rest. The header is required all the same, since every mutation carries
    one, and the per-entry decision above is what actually bounds the sweep.

    Args:
        drive_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | TrashEmptyResult]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | TrashEmptyResult | None:
    """Empty Trash

     Purge the trashed roots this caller may delete. A held subtree refuses.

    The decision is per deletion rather than per drive: emptying is not a
    drive-level verb, it is "delete each of these", and a caller who may not
    delete one of them must not have it purged by asking for all of them. What
    that leaves behind is counted and reported, because the browser's notice is
    written from this answer and "removed 50" alone reads as "the trash is now
    empty".

    The whole trash is swept, not one listing page: the listing's page size is a
    rendering budget and has nothing to do with how much a person asked to
    delete. Paging is by the listing's own marker, so a refused root advances
    the cursor past itself instead of being met again forever, and a root seen
    once is never counted twice.

    The precondition names the drive root, the one node the whole sweep is
    addressed at; it is not compared against a single trashed node because the
    sweep addresses many, and one of their etags would say nothing about the
    rest. The header is required all the same, since every mutation carries
    one, and the per-entry decision above is what actually bounds the sweep.

    Args:
        drive_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | TrashEmptyResult
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            client=client,
        )
    ).parsed
