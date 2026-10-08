from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.lookup_page import LookupPage
from ...models.lookup_request import LookupRequest
from ...types import Response


def _get_kwargs(
    drive_id: str,
    *,
    body: LookupRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/files/drives/{drive_id}/items/lookup".format(
            drive_id=quote(str(drive_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | LookupPage | None:
    if response.status_code == 200:
        response_200 = LookupPage.from_dict(response.json())

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
) -> Response[ErrorEnvelope | LookupPage]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    drive_id: str,
    *,
    client: AuthenticatedClient | Client,
    body: LookupRequest,
) -> Response[ErrorEnvelope | LookupPage]:
    """Lookup Items

     Many items by id, in the statements one item costs.

    The finish of a folder drop used to read every file it had just committed
    back one GET at a time -- five hundred files, five hundred reads, and the
    per-principal read budget refused the tail of the drop. The client already
    holds the ids (the commit operations name them), so it asks for them here
    in one request, and the page is rendered the way a folder listing renders
    its rows: every row from its OWN access decision, one statement per fact
    for the whole page -- plus one chain walk per DISTINCT parent, which for
    the drop this exists for is one, and for the worst case a caller can write
    is still no more than the reads it replaces.

    A listing, not a door: like the children page, rows are cut by the same
    READ decision the single read makes and no decision row is written per
    id -- an id the caller may not read is simply absent, indistinguishable
    from one that never existed, so the response is not an oracle over ids.

    The drive in the path is refused by :func:`caller_drive` before the body is
    even validated, so a stranger's drive id is the opaque 404 every other
    route in the family answers rather than a 422 about the body.

    Args:
        drive_id (str):
        body (LookupRequest): The ids a client already holds -- the nodes a drop's commits
            reported.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | LookupPage]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    drive_id: str,
    *,
    client: AuthenticatedClient | Client,
    body: LookupRequest,
) -> ErrorEnvelope | LookupPage | None:
    """Lookup Items

     Many items by id, in the statements one item costs.

    The finish of a folder drop used to read every file it had just committed
    back one GET at a time -- five hundred files, five hundred reads, and the
    per-principal read budget refused the tail of the drop. The client already
    holds the ids (the commit operations name them), so it asks for them here
    in one request, and the page is rendered the way a folder listing renders
    its rows: every row from its OWN access decision, one statement per fact
    for the whole page -- plus one chain walk per DISTINCT parent, which for
    the drop this exists for is one, and for the worst case a caller can write
    is still no more than the reads it replaces.

    A listing, not a door: like the children page, rows are cut by the same
    READ decision the single read makes and no decision row is written per
    id -- an id the caller may not read is simply absent, indistinguishable
    from one that never existed, so the response is not an oracle over ids.

    The drive in the path is refused by :func:`caller_drive` before the body is
    even validated, so a stranger's drive id is the opaque 404 every other
    route in the family answers rather than a 422 about the body.

    Args:
        drive_id (str):
        body (LookupRequest): The ids a client already holds -- the nodes a drop's commits
            reported.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | LookupPage
    """

    return sync_detailed(
        drive_id=drive_id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    drive_id: str,
    *,
    client: AuthenticatedClient | Client,
    body: LookupRequest,
) -> Response[ErrorEnvelope | LookupPage]:
    """Lookup Items

     Many items by id, in the statements one item costs.

    The finish of a folder drop used to read every file it had just committed
    back one GET at a time -- five hundred files, five hundred reads, and the
    per-principal read budget refused the tail of the drop. The client already
    holds the ids (the commit operations name them), so it asks for them here
    in one request, and the page is rendered the way a folder listing renders
    its rows: every row from its OWN access decision, one statement per fact
    for the whole page -- plus one chain walk per DISTINCT parent, which for
    the drop this exists for is one, and for the worst case a caller can write
    is still no more than the reads it replaces.

    A listing, not a door: like the children page, rows are cut by the same
    READ decision the single read makes and no decision row is written per
    id -- an id the caller may not read is simply absent, indistinguishable
    from one that never existed, so the response is not an oracle over ids.

    The drive in the path is refused by :func:`caller_drive` before the body is
    even validated, so a stranger's drive id is the opaque 404 every other
    route in the family answers rather than a 422 about the body.

    Args:
        drive_id (str):
        body (LookupRequest): The ids a client already holds -- the nodes a drop's commits
            reported.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | LookupPage]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: str,
    *,
    client: AuthenticatedClient | Client,
    body: LookupRequest,
) -> ErrorEnvelope | LookupPage | None:
    """Lookup Items

     Many items by id, in the statements one item costs.

    The finish of a folder drop used to read every file it had just committed
    back one GET at a time -- five hundred files, five hundred reads, and the
    per-principal read budget refused the tail of the drop. The client already
    holds the ids (the commit operations name them), so it asks for them here
    in one request, and the page is rendered the way a folder listing renders
    its rows: every row from its OWN access decision, one statement per fact
    for the whole page -- plus one chain walk per DISTINCT parent, which for
    the drop this exists for is one, and for the worst case a caller can write
    is still no more than the reads it replaces.

    A listing, not a door: like the children page, rows are cut by the same
    READ decision the single read makes and no decision row is written per
    id -- an id the caller may not read is simply absent, indistinguishable
    from one that never existed, so the response is not an oracle over ids.

    The drive in the path is refused by :func:`caller_drive` before the body is
    even validated, so a stranger's drive id is the opaque 404 every other
    route in the family answers rather than a 422 about the body.

    Args:
        drive_id (str):
        body (LookupRequest): The ids a client already holds -- the nodes a drop's commits
            reported.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | LookupPage
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            client=client,
            body=body,
        )
    ).parsed
