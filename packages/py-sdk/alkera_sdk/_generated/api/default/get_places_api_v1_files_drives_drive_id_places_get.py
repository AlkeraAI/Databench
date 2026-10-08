from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.places_read import PlacesRead
from ...types import UNSET, Response, Unset


def _get_kwargs(
    drive_id: str,
    *,
    ensure: None | str | Unset = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    json_ensure: None | str | Unset
    if isinstance(ensure, Unset):
        json_ensure = UNSET
    else:
        json_ensure = ensure
    params["ensure"] = json_ensure

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/files/drives/{drive_id}/places".format(
            drive_id=quote(str(drive_id), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | PlacesRead | None:
    if response.status_code == 200:
        response_200 = PlacesRead.from_dict(response.json())

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
) -> Response[ErrorEnvelope | PlacesRead]:
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
    ensure: None | str | Unset = UNSET,
) -> Response[ErrorEnvelope | PlacesRead]:
    """Get Places

     The caller's places, optionally made on the way.

    ``ensure`` is a comma-separated list of the names in :data:`PLACE_TYPES`;
    a name that is not one of them is refused rather than ignored, so a client
    that misspells a place learns it instead of quietly getting a ``null``
    forever.

    The drive is checked before anything is read or made, and by raising the
    same absence a missing home does: naming another org's drive must not be a
    way to learn that the drive is real.

    One decision covers the whole answer, on the caller's own home, because
    that is the node every place is a child of — ``read`` for the bare read,
    ``write`` for an ``ensure``, which is what putting a folder in somebody's
    home is. A principal with no home (a CI or a proxy token) has nowhere for a
    place to be, so it is the same absence rather than an empty document that
    would read as "you have none yet".

    Args:
        drive_id (str):
        ensure (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | PlacesRead]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        ensure=ensure,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    drive_id: str,
    *,
    client: AuthenticatedClient | Client,
    ensure: None | str | Unset = UNSET,
) -> ErrorEnvelope | PlacesRead | None:
    """Get Places

     The caller's places, optionally made on the way.

    ``ensure`` is a comma-separated list of the names in :data:`PLACE_TYPES`;
    a name that is not one of them is refused rather than ignored, so a client
    that misspells a place learns it instead of quietly getting a ``null``
    forever.

    The drive is checked before anything is read or made, and by raising the
    same absence a missing home does: naming another org's drive must not be a
    way to learn that the drive is real.

    One decision covers the whole answer, on the caller's own home, because
    that is the node every place is a child of — ``read`` for the bare read,
    ``write`` for an ``ensure``, which is what putting a folder in somebody's
    home is. A principal with no home (a CI or a proxy token) has nowhere for a
    place to be, so it is the same absence rather than an empty document that
    would read as "you have none yet".

    Args:
        drive_id (str):
        ensure (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | PlacesRead
    """

    return sync_detailed(
        drive_id=drive_id,
        client=client,
        ensure=ensure,
    ).parsed


async def asyncio_detailed(
    drive_id: str,
    *,
    client: AuthenticatedClient | Client,
    ensure: None | str | Unset = UNSET,
) -> Response[ErrorEnvelope | PlacesRead]:
    """Get Places

     The caller's places, optionally made on the way.

    ``ensure`` is a comma-separated list of the names in :data:`PLACE_TYPES`;
    a name that is not one of them is refused rather than ignored, so a client
    that misspells a place learns it instead of quietly getting a ``null``
    forever.

    The drive is checked before anything is read or made, and by raising the
    same absence a missing home does: naming another org's drive must not be a
    way to learn that the drive is real.

    One decision covers the whole answer, on the caller's own home, because
    that is the node every place is a child of — ``read`` for the bare read,
    ``write`` for an ``ensure``, which is what putting a folder in somebody's
    home is. A principal with no home (a CI or a proxy token) has nowhere for a
    place to be, so it is the same absence rather than an empty document that
    would read as "you have none yet".

    Args:
        drive_id (str):
        ensure (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | PlacesRead]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        ensure=ensure,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: str,
    *,
    client: AuthenticatedClient | Client,
    ensure: None | str | Unset = UNSET,
) -> ErrorEnvelope | PlacesRead | None:
    """Get Places

     The caller's places, optionally made on the way.

    ``ensure`` is a comma-separated list of the names in :data:`PLACE_TYPES`;
    a name that is not one of them is refused rather than ignored, so a client
    that misspells a place learns it instead of quietly getting a ``null``
    forever.

    The drive is checked before anything is read or made, and by raising the
    same absence a missing home does: naming another org's drive must not be a
    way to learn that the drive is real.

    One decision covers the whole answer, on the caller's own home, because
    that is the node every place is a child of — ``read`` for the bare read,
    ``write`` for an ``ensure``, which is what putting a folder in somebody's
    home is. A principal with no home (a CI or a proxy token) has nowhere for a
    place to be, so it is the same absence rather than an empty document that
    would read as "you have none yet".

    Args:
        drive_id (str):
        ensure (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | PlacesRead
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            client=client,
            ensure=ensure,
        )
    ).parsed
