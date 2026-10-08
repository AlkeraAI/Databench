from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.disk_grow_request import DiskGrowRequest
from ...models.error_envelope import ErrorEnvelope
from ...models.org_machine_read import OrgMachineRead
from ...types import UNSET, Response, Unset


def _get_kwargs(
    machine_id: str,
    *,
    body: DiskGrowRequest,
    if_match: None | str | Unset = UNSET,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    if not isinstance(if_match, Unset):
        headers["If-Match"] = if_match

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/org/machines/{machine_id}/disk".format(
            machine_id=quote(str(machine_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | OrgMachineRead | None:
    if response.status_code == 202:
        response_202 = OrgMachineRead.from_dict(response.json())

        return response_202

    if response.status_code == 422:
        response_422 = ErrorEnvelope.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ErrorEnvelope | OrgMachineRead]:
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
    if_match: None | str | Unset = UNSET,
) -> Response[ErrorEnvelope | OrgMachineRead]:
    """Grow Machine Disk

     Grow its disk: only larger (422), only where the provider can (409),
    402 when the credit cannot carry the bigger disk. A running machine
    restarts to grow it, its running chats finishing first.

    Args:
        machine_id (str):
        if_match (None | str | Unset): The slot's version as the caller last read it. A write
            whose version no longer matches is refused with a 409; omit it, or send *, to write
            unconditionally.
        body (DiskGrowRequest): The bigger disk asked for, in GB.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OrgMachineRead]
    """

    kwargs = _get_kwargs(
        machine_id=machine_id,
        body=body,
        if_match=if_match,
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
    if_match: None | str | Unset = UNSET,
) -> ErrorEnvelope | OrgMachineRead | None:
    """Grow Machine Disk

     Grow its disk: only larger (422), only where the provider can (409),
    402 when the credit cannot carry the bigger disk. A running machine
    restarts to grow it, its running chats finishing first.

    Args:
        machine_id (str):
        if_match (None | str | Unset): The slot's version as the caller last read it. A write
            whose version no longer matches is refused with a 409; omit it, or send *, to write
            unconditionally.
        body (DiskGrowRequest): The bigger disk asked for, in GB.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OrgMachineRead
    """

    return sync_detailed(
        machine_id=machine_id,
        client=client,
        body=body,
        if_match=if_match,
    ).parsed


async def asyncio_detailed(
    machine_id: str,
    *,
    client: AuthenticatedClient | Client,
    body: DiskGrowRequest,
    if_match: None | str | Unset = UNSET,
) -> Response[ErrorEnvelope | OrgMachineRead]:
    """Grow Machine Disk

     Grow its disk: only larger (422), only where the provider can (409),
    402 when the credit cannot carry the bigger disk. A running machine
    restarts to grow it, its running chats finishing first.

    Args:
        machine_id (str):
        if_match (None | str | Unset): The slot's version as the caller last read it. A write
            whose version no longer matches is refused with a 409; omit it, or send *, to write
            unconditionally.
        body (DiskGrowRequest): The bigger disk asked for, in GB.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OrgMachineRead]
    """

    kwargs = _get_kwargs(
        machine_id=machine_id,
        body=body,
        if_match=if_match,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    machine_id: str,
    *,
    client: AuthenticatedClient | Client,
    body: DiskGrowRequest,
    if_match: None | str | Unset = UNSET,
) -> ErrorEnvelope | OrgMachineRead | None:
    """Grow Machine Disk

     Grow its disk: only larger (422), only where the provider can (409),
    402 when the credit cannot carry the bigger disk. A running machine
    restarts to grow it, its running chats finishing first.

    Args:
        machine_id (str):
        if_match (None | str | Unset): The slot's version as the caller last read it. A write
            whose version no longer matches is refused with a 409; omit it, or send *, to write
            unconditionally.
        body (DiskGrowRequest): The bigger disk asked for, in GB.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OrgMachineRead
    """

    return (
        await asyncio_detailed(
            machine_id=machine_id,
            client=client,
            body=body,
            if_match=if_match,
        )
    ).parsed
