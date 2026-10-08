from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.fleet_machine_list import FleetMachineList
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    include_gone: bool | Unset = False,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["include_gone"] = include_gone

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/admin/v1/machines",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | FleetMachineList | None:
    if response.status_code == 200:
        response_200 = FleetMachineList.from_dict(response.json())

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
) -> Response[ErrorEnvelope | FleetMachineList]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    include_gone: bool | Unset = False,
) -> Response[ErrorEnvelope | FleetMachineList]:
    """List Platform Machines

     Every platform box: one row per credential minted (the dedicated-box
    picker's fields), with the machine behind it — provisioned or registered —
    its lifecycle, cost and load. A machine that is released or failed is
    history, listed only with ``include_gone``; a lost one stays, since an admin
    has to see a box that stopped answering.

    Args:
        include_gone (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | FleetMachineList]
    """

    kwargs = _get_kwargs(
        include_gone=include_gone,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
    include_gone: bool | Unset = False,
) -> ErrorEnvelope | FleetMachineList | None:
    """List Platform Machines

     Every platform box: one row per credential minted (the dedicated-box
    picker's fields), with the machine behind it — provisioned or registered —
    its lifecycle, cost and load. A machine that is released or failed is
    history, listed only with ``include_gone``; a lost one stays, since an admin
    has to see a box that stopped answering.

    Args:
        include_gone (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | FleetMachineList
    """

    return sync_detailed(
        client=client,
        include_gone=include_gone,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    include_gone: bool | Unset = False,
) -> Response[ErrorEnvelope | FleetMachineList]:
    """List Platform Machines

     Every platform box: one row per credential minted (the dedicated-box
    picker's fields), with the machine behind it — provisioned or registered —
    its lifecycle, cost and load. A machine that is released or failed is
    history, listed only with ``include_gone``; a lost one stays, since an admin
    has to see a box that stopped answering.

    Args:
        include_gone (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | FleetMachineList]
    """

    kwargs = _get_kwargs(
        include_gone=include_gone,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    include_gone: bool | Unset = False,
) -> ErrorEnvelope | FleetMachineList | None:
    """List Platform Machines

     Every platform box: one row per credential minted (the dedicated-box
    picker's fields), with the machine behind it — provisioned or registered —
    its lifecycle, cost and load. A machine that is released or failed is
    history, listed only with ``include_gone``; a lost one stays, since an admin
    has to see a box that stopped answering.

    Args:
        include_gone (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | FleetMachineList
    """

    return (
        await asyncio_detailed(
            client=client,
            include_gone=include_gone,
        )
    ).parsed
