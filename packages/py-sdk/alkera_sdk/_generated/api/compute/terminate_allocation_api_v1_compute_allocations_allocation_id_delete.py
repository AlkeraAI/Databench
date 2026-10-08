from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.compute_allocation_info import ComputeAllocationInfo
from ...models.error_envelope import ErrorEnvelope
from ...types import Response


def _get_kwargs(
    allocation_id: str,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "delete",
        "url": "/api/v1/compute/allocations/{allocation_id}".format(
            allocation_id=quote(str(allocation_id), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ComputeAllocationInfo | ErrorEnvelope | None:
    if response.status_code == 200:
        response_200 = ComputeAllocationInfo.from_dict(response.json())

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
) -> Response[ComputeAllocationInfo | ErrorEnvelope]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    allocation_id: str,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ComputeAllocationInfo | ErrorEnvelope]:
    """Terminate Allocation

    Args:
        allocation_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ComputeAllocationInfo | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        allocation_id=allocation_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    allocation_id: str,
    *,
    client: AuthenticatedClient | Client,
) -> ComputeAllocationInfo | ErrorEnvelope | None:
    """Terminate Allocation

    Args:
        allocation_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ComputeAllocationInfo | ErrorEnvelope
    """

    return sync_detailed(
        allocation_id=allocation_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    allocation_id: str,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ComputeAllocationInfo | ErrorEnvelope]:
    """Terminate Allocation

    Args:
        allocation_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ComputeAllocationInfo | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        allocation_id=allocation_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    allocation_id: str,
    *,
    client: AuthenticatedClient | Client,
) -> ComputeAllocationInfo | ErrorEnvelope | None:
    """Terminate Allocation

    Args:
        allocation_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ComputeAllocationInfo | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            allocation_id=allocation_id,
            client=client,
        )
    ).parsed
