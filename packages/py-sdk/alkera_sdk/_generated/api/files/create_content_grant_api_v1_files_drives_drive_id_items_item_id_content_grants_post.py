from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.content_grant_request import ContentGrantRequest
from ...models.content_grant_response import ContentGrantResponse
from ...models.error_envelope import ErrorEnvelope
from ...types import Response


def _get_kwargs(
    drive_id: str,
    item_id: str,
    *,
    body: ContentGrantRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/files/drives/{drive_id}/items/{item_id}/content-grants".format(
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
) -> ContentGrantResponse | ErrorEnvelope | None:
    if response.status_code == 201:
        response_201 = ContentGrantResponse.from_dict(response.json())

        return response_201

    if response.status_code == 409:
        response_409 = ErrorEnvelope.from_dict(response.json())

        return response_409

    if response.status_code == 422:
        response_422 = ErrorEnvelope.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ContentGrantResponse | ErrorEnvelope]:
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
    body: ContentGrantRequest,
) -> Response[ContentGrantResponse | ErrorEnvelope]:
    """Create Content Grant

     Mint a read URL for this node without redirecting to it.

    The ``302`` mint is the right shape for a download — the browser follows it
    and saves a file — and the wrong shape for a preview: a ``fetch`` that
    follows a cross-origin redirect sends ``Origin: null`` and fails CORS, and
    an ``<img src>`` of one is refused by the resource policy. A surface that
    wants the bytes *in* the page therefore asks for the URL as data and fetches
    the content origin itself, which is what this route answers.

    Not idempotent, deliberately: a mint is not a retry of the previous mint. A
    grant is a credential with its own deadline and its own counter, so replaying
    a key would hand back a URL closer to death than the caller thinks.

    Args:
        drive_id (str):
        item_id (str):
        body (ContentGrantRequest): What kind of read the caller is asking to be granted.

            ``file`` is the single-use download URL every client already mints through
            the ``302``; ``page`` is the multi-use grant over the entry's own folder
            that lets a rendered document fetch the images beside it. They are one
            route because they are one decision — ``EXPORT`` on the node the caller
            named — and a client that asks for the wrong one for a node's type is told
            so rather than quietly handed the other.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ContentGrantResponse | ErrorEnvelope]
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
    body: ContentGrantRequest,
) -> ContentGrantResponse | ErrorEnvelope | None:
    """Create Content Grant

     Mint a read URL for this node without redirecting to it.

    The ``302`` mint is the right shape for a download — the browser follows it
    and saves a file — and the wrong shape for a preview: a ``fetch`` that
    follows a cross-origin redirect sends ``Origin: null`` and fails CORS, and
    an ``<img src>`` of one is refused by the resource policy. A surface that
    wants the bytes *in* the page therefore asks for the URL as data and fetches
    the content origin itself, which is what this route answers.

    Not idempotent, deliberately: a mint is not a retry of the previous mint. A
    grant is a credential with its own deadline and its own counter, so replaying
    a key would hand back a URL closer to death than the caller thinks.

    Args:
        drive_id (str):
        item_id (str):
        body (ContentGrantRequest): What kind of read the caller is asking to be granted.

            ``file`` is the single-use download URL every client already mints through
            the ``302``; ``page`` is the multi-use grant over the entry's own folder
            that lets a rendered document fetch the images beside it. They are one
            route because they are one decision — ``EXPORT`` on the node the caller
            named — and a client that asks for the wrong one for a node's type is told
            so rather than quietly handed the other.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ContentGrantResponse | ErrorEnvelope
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
    body: ContentGrantRequest,
) -> Response[ContentGrantResponse | ErrorEnvelope]:
    """Create Content Grant

     Mint a read URL for this node without redirecting to it.

    The ``302`` mint is the right shape for a download — the browser follows it
    and saves a file — and the wrong shape for a preview: a ``fetch`` that
    follows a cross-origin redirect sends ``Origin: null`` and fails CORS, and
    an ``<img src>`` of one is refused by the resource policy. A surface that
    wants the bytes *in* the page therefore asks for the URL as data and fetches
    the content origin itself, which is what this route answers.

    Not idempotent, deliberately: a mint is not a retry of the previous mint. A
    grant is a credential with its own deadline and its own counter, so replaying
    a key would hand back a URL closer to death than the caller thinks.

    Args:
        drive_id (str):
        item_id (str):
        body (ContentGrantRequest): What kind of read the caller is asking to be granted.

            ``file`` is the single-use download URL every client already mints through
            the ``302``; ``page`` is the multi-use grant over the entry's own folder
            that lets a rendered document fetch the images beside it. They are one
            route because they are one decision — ``EXPORT`` on the node the caller
            named — and a client that asks for the wrong one for a node's type is told
            so rather than quietly handed the other.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ContentGrantResponse | ErrorEnvelope]
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
    body: ContentGrantRequest,
) -> ContentGrantResponse | ErrorEnvelope | None:
    """Create Content Grant

     Mint a read URL for this node without redirecting to it.

    The ``302`` mint is the right shape for a download — the browser follows it
    and saves a file — and the wrong shape for a preview: a ``fetch`` that
    follows a cross-origin redirect sends ``Origin: null`` and fails CORS, and
    an ``<img src>`` of one is refused by the resource policy. A surface that
    wants the bytes *in* the page therefore asks for the URL as data and fetches
    the content origin itself, which is what this route answers.

    Not idempotent, deliberately: a mint is not a retry of the previous mint. A
    grant is a credential with its own deadline and its own counter, so replaying
    a key would hand back a URL closer to death than the caller thinks.

    Args:
        drive_id (str):
        item_id (str):
        body (ContentGrantRequest): What kind of read the caller is asking to be granted.

            ``file`` is the single-use download URL every client already mints through
            the ``302``; ``page`` is the multi-use grant over the entry's own folder
            that lets a rendered document fetch the images beside it. They are one
            route because they are one decision — ``EXPORT`` on the node the caller
            named — and a client that asks for the wrong one for a node's type is told
            so rather than quietly handed the other.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ContentGrantResponse | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            client=client,
            body=body,
        )
    ).parsed
