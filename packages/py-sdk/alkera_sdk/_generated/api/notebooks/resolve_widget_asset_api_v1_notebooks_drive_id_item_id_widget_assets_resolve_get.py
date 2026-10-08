from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.widget_asset_resolved import WidgetAssetResolved
from ...types import UNSET, Response


def _get_kwargs(
    drive_id: str,
    item_id: str,
    *,
    module: str,
    version: str,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["module"] = module

    params["version"] = version

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/notebooks/{drive_id}/{item_id}/widget-assets/resolve".format(
            drive_id=quote(str(drive_id), safe=""),
            item_id=quote(str(item_id), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | WidgetAssetResolved | None:
    if response.status_code == 200:
        response_200 = WidgetAssetResolved.from_dict(response.json())

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
) -> Response[ErrorEnvelope | WidgetAssetResolved]:
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
    module: str,
    version: str,
) -> Response[ErrorEnvelope | WidgetAssetResolved]:
    """Resolve Widget Asset

     The hash of a widget module's code: a platform bundle, or a module
    this notebook's kernel offered; 404 for anything else.

    Args:
        drive_id (str):
        item_id (str):
        module (str):
        version (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | WidgetAssetResolved]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        module=module,
        version=version,
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
    module: str,
    version: str,
) -> ErrorEnvelope | WidgetAssetResolved | None:
    """Resolve Widget Asset

     The hash of a widget module's code: a platform bundle, or a module
    this notebook's kernel offered; 404 for anything else.

    Args:
        drive_id (str):
        item_id (str):
        module (str):
        version (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | WidgetAssetResolved
    """

    return sync_detailed(
        drive_id=drive_id,
        item_id=item_id,
        client=client,
        module=module,
        version=version,
    ).parsed


async def asyncio_detailed(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
    module: str,
    version: str,
) -> Response[ErrorEnvelope | WidgetAssetResolved]:
    """Resolve Widget Asset

     The hash of a widget module's code: a platform bundle, or a module
    this notebook's kernel offered; 404 for anything else.

    Args:
        drive_id (str):
        item_id (str):
        module (str):
        version (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | WidgetAssetResolved]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        module=module,
        version=version,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
    module: str,
    version: str,
) -> ErrorEnvelope | WidgetAssetResolved | None:
    """Resolve Widget Asset

     The hash of a widget module's code: a platform bundle, or a module
    this notebook's kernel offered; 404 for anything else.

    Args:
        drive_id (str):
        item_id (str):
        module (str):
        version (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | WidgetAssetResolved
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            client=client,
            module=module,
            version=version,
        )
    ).parsed
