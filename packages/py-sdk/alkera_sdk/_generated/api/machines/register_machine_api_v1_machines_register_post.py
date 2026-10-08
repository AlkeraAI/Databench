from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.machine_read import MachineRead
from ...models.machine_register_request import MachineRegisterRequest
from ...types import Response


def _get_kwargs(
    *,
    body: MachineRegisterRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/machines/register",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | MachineRead | None:
    if response.status_code == 200:
        response_200 = MachineRead.from_dict(response.json())

        return response_200

    if response.status_code == 201:
        response_201 = MachineRead.from_dict(response.json())

        return response_201

    if response.status_code == 402:
        response_402 = ErrorEnvelope.from_dict(response.json())

        return response_402

    if response.status_code == 422:
        response_422 = ErrorEnvelope.from_dict(response.json())

        return response_422

    if response.status_code == 429:
        response_429 = ErrorEnvelope.from_dict(response.json())

        return response_429

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ErrorEnvelope | MachineRead]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: MachineRegisterRequest,
) -> Response[ErrorEnvelope | MachineRead]:
    """Register Machine

    Args:
        body (MachineRegisterRequest): Register a running pod as the org's workspace machine
            (idempotent by
            ``provider_pod_id``: registering the same pod twice returns the same row).

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | MachineRead]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
    body: MachineRegisterRequest,
) -> ErrorEnvelope | MachineRead | None:
    """Register Machine

    Args:
        body (MachineRegisterRequest): Register a running pod as the org's workspace machine
            (idempotent by
            ``provider_pod_id``: registering the same pod twice returns the same row).

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | MachineRead
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: MachineRegisterRequest,
) -> Response[ErrorEnvelope | MachineRead]:
    """Register Machine

    Args:
        body (MachineRegisterRequest): Register a running pod as the org's workspace machine
            (idempotent by
            ``provider_pod_id``: registering the same pod twice returns the same row).

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | MachineRead]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: MachineRegisterRequest,
) -> ErrorEnvelope | MachineRead | None:
    """Register Machine

    Args:
        body (MachineRegisterRequest): Register a running pod as the org's workspace machine
            (idempotent by
            ``provider_pod_id``: registering the same pod twice returns the same row).

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | MachineRead
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
