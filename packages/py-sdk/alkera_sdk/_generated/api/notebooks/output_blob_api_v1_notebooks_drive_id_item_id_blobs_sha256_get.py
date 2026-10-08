from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...types import Response


def _get_kwargs(
    drive_id: str,
    item_id: str,
    sha256: str,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/notebooks/{drive_id}/{item_id}/blobs/{sha256}".format(
            drive_id=quote(str(drive_id), safe=""),
            item_id=quote(str(item_id), safe=""),
            sha256=quote(str(sha256), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Any | ErrorEnvelope | None:
    if response.status_code == 302:
        response_302 = cast(Any, None)
        return response_302

    if response.status_code == 404:
        response_404 = ErrorEnvelope.from_dict(response.json())

        return response_404

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
    drive_id: str,
    item_id: str,
    sha256: str,
    *,
    client: AuthenticatedClient | Client,
) -> Response[Any | ErrorEnvelope]:
    """Output Blob

     An output stored beside the notebook, by its hash: a redirect to its
    bytes, fetched from the machine holding the folder first when the drive
    does not have them yet. A raster image small enough to be carried inline
    has no file of its own; it is answered as its bytes, found among the
    outputs the cells show now (under the same READ as the notebook's view)
    or, failing that, in the saved snapshot (under the same EXPORT as the
    stored notebook). An output no cell holds any more is a 404.

    Args:
        drive_id (str):
        item_id (str):
        sha256 (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        sha256=sha256,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    drive_id: str,
    item_id: str,
    sha256: str,
    *,
    client: AuthenticatedClient | Client,
) -> Any | ErrorEnvelope | None:
    """Output Blob

     An output stored beside the notebook, by its hash: a redirect to its
    bytes, fetched from the machine holding the folder first when the drive
    does not have them yet. A raster image small enough to be carried inline
    has no file of its own; it is answered as its bytes, found among the
    outputs the cells show now (under the same READ as the notebook's view)
    or, failing that, in the saved snapshot (under the same EXPORT as the
    stored notebook). An output no cell holds any more is a 404.

    Args:
        drive_id (str):
        item_id (str):
        sha256 (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ErrorEnvelope
    """

    return sync_detailed(
        drive_id=drive_id,
        item_id=item_id,
        sha256=sha256,
        client=client,
    ).parsed


async def asyncio_detailed(
    drive_id: str,
    item_id: str,
    sha256: str,
    *,
    client: AuthenticatedClient | Client,
) -> Response[Any | ErrorEnvelope]:
    """Output Blob

     An output stored beside the notebook, by its hash: a redirect to its
    bytes, fetched from the machine holding the folder first when the drive
    does not have them yet. A raster image small enough to be carried inline
    has no file of its own; it is answered as its bytes, found among the
    outputs the cells show now (under the same READ as the notebook's view)
    or, failing that, in the saved snapshot (under the same EXPORT as the
    stored notebook). An output no cell holds any more is a 404.

    Args:
        drive_id (str):
        item_id (str):
        sha256 (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        sha256=sha256,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: str,
    item_id: str,
    sha256: str,
    *,
    client: AuthenticatedClient | Client,
) -> Any | ErrorEnvelope | None:
    """Output Blob

     An output stored beside the notebook, by its hash: a redirect to its
    bytes, fetched from the machine holding the folder first when the drive
    does not have them yet. A raster image small enough to be carried inline
    has no file of its own; it is answered as its bytes, found among the
    outputs the cells show now (under the same READ as the notebook's view)
    or, failing that, in the saved snapshot (under the same EXPORT as the
    stored notebook). An output no cell holds any more is a 404.

    Args:
        drive_id (str):
        item_id (str):
        sha256 (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            sha256=sha256,
            client=client,
        )
    ).parsed
