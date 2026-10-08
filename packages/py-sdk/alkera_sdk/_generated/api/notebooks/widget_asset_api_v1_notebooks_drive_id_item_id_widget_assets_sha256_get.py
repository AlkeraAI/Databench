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
        "url": "/api/v1/notebooks/{drive_id}/{item_id}/widget-assets/{sha256}".format(
            drive_id=quote(str(drive_id), safe=""),
            item_id=quote(str(item_id), safe=""),
            sha256=quote(str(sha256), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Any | ErrorEnvelope | None:
    if response.status_code == 200:
        response_200 = response.json()
        return response_200

    if response.status_code == 302:
        response_302 = cast(Any, None)
        return response_302

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
    """Widget Asset

     Widget JavaScript by hash: a platform bundle, or an environment asset
    this notebook's kernel offered (never any other hash).

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
    """Widget Asset

     Widget JavaScript by hash: a platform bundle, or an environment asset
    this notebook's kernel offered (never any other hash).

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
    """Widget Asset

     Widget JavaScript by hash: a platform bundle, or an environment asset
    this notebook's kernel offered (never any other hash).

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
    """Widget Asset

     Widget JavaScript by hash: a platform bundle, or an environment asset
    this notebook's kernel offered (never any other hash).

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
