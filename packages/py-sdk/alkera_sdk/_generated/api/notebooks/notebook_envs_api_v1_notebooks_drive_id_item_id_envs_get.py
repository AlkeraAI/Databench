from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.env_listing import EnvListing
from ...models.error_envelope import ErrorEnvelope
from ...types import Response


def _get_kwargs(
    drive_id: str,
    item_id: str,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/notebooks/{drive_id}/{item_id}/envs".format(
            drive_id=quote(str(drive_id), safe=""),
            item_id=quote(str(item_id), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> EnvListing | ErrorEnvelope | None:
    if response.status_code == 200:
        response_200 = EnvListing.from_dict(response.json())

        return response_200

    if response.status_code == 404:
        response_404 = ErrorEnvelope.from_dict(response.json())

        return response_404

    if response.status_code == 409:
        response_409 = ErrorEnvelope.from_dict(response.json())

        return response_409

    if response.status_code == 422:
        response_422 = ErrorEnvelope.from_dict(response.json())

        return response_422

    if response.status_code == 503:
        response_503 = ErrorEnvelope.from_dict(response.json())

        return response_503

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[EnvListing | ErrorEnvelope]:
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
) -> Response[EnvListing | ErrorEnvelope]:
    """Notebook Envs

     The notebook's environment and the others it may use, asked of the box
    holding it. A reader who may run the notebook wakes its sleeping chat
    first and is answered ``notebook.waking``.

    Args:
        drive_id (str):
        item_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[EnvListing | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
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
) -> EnvListing | ErrorEnvelope | None:
    """Notebook Envs

     The notebook's environment and the others it may use, asked of the box
    holding it. A reader who may run the notebook wakes its sleeping chat
    first and is answered ``notebook.waking``.

    Args:
        drive_id (str):
        item_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        EnvListing | ErrorEnvelope
    """

    return sync_detailed(
        drive_id=drive_id,
        item_id=item_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
) -> Response[EnvListing | ErrorEnvelope]:
    """Notebook Envs

     The notebook's environment and the others it may use, asked of the box
    holding it. A reader who may run the notebook wakes its sleeping chat
    first and is answered ``notebook.waking``.

    Args:
        drive_id (str):
        item_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[EnvListing | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
) -> EnvListing | ErrorEnvelope | None:
    """Notebook Envs

     The notebook's environment and the others it may use, asked of the box
    holding it. A reader who may run the notebook wakes its sleeping chat
    first and is answered ``notebook.waking``.

    Args:
        drive_id (str):
        item_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        EnvListing | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            client=client,
        )
    ).parsed
