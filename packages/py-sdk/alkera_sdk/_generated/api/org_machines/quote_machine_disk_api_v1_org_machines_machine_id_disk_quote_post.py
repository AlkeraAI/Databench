from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.disk_grow_request import DiskGrowRequest
from ...models.error_envelope import ErrorEnvelope
from ...models.machine_quote import MachineQuote
from ...types import Response


def _get_kwargs(
    machine_id: str,
    *,
    body: DiskGrowRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/org/machines/{machine_id}/disk/quote".format(
            machine_id=quote(str(machine_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | MachineQuote | None:
    if response.status_code == 200:
        response_200 = MachineQuote.from_dict(response.json())

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
) -> Response[ErrorEnvelope | MachineQuote]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    machine_id: str,
    *,
    client: AuthenticatedClient | Client,
    body: DiskGrowRequest,
) -> Response[ErrorEnvelope | MachineQuote]:
    """Quote Machine Disk

     What the machine would cost with a bigger disk, and whether the grow
    would be admitted now. For its managers; nothing changes.

    Args:
        machine_id (str):
        body (DiskGrowRequest): The bigger disk asked for, in GB.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | MachineQuote]
    """

    kwargs = _get_kwargs(
        machine_id=machine_id,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    machine_id: str,
    *,
    client: AuthenticatedClient | Client,
    body: DiskGrowRequest,
) -> ErrorEnvelope | MachineQuote | None:
    """Quote Machine Disk

     What the machine would cost with a bigger disk, and whether the grow
    would be admitted now. For its managers; nothing changes.

    Args:
        machine_id (str):
        body (DiskGrowRequest): The bigger disk asked for, in GB.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | MachineQuote
    """

    return sync_detailed(
        machine_id=machine_id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    machine_id: str,
    *,
    client: AuthenticatedClient | Client,
    body: DiskGrowRequest,
) -> Response[ErrorEnvelope | MachineQuote]:
    """Quote Machine Disk

     What the machine would cost with a bigger disk, and whether the grow
    would be admitted now. For its managers; nothing changes.

    Args:
        machine_id (str):
        body (DiskGrowRequest): The bigger disk asked for, in GB.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | MachineQuote]
    """

    kwargs = _get_kwargs(
        machine_id=machine_id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    machine_id: str,
    *,
    client: AuthenticatedClient | Client,
    body: DiskGrowRequest,
) -> ErrorEnvelope | MachineQuote | None:
    """Quote Machine Disk

     What the machine would cost with a bigger disk, and whether the grow
    would be admitted now. For its managers; nothing changes.

    Args:
        machine_id (str):
        body (DiskGrowRequest): The bigger disk asked for, in GB.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | MachineQuote
    """

    return (
        await asyncio_detailed(
            machine_id=machine_id,
            client=client,
            body=body,
        )
    ).parsed
