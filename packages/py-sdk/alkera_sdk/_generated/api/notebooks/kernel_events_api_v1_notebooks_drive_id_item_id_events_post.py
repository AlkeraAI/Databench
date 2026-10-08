from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.kernel_events_accepted import KernelEventsAccepted
from ...models.kernel_events_batch import KernelEventsBatch
from ...types import Response


def _get_kwargs(
    drive_id: str,
    item_id: str,
    *,
    body: KernelEventsBatch,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/notebooks/{drive_id}/{item_id}/events".format(
            drive_id=quote(str(drive_id), safe=""),
            item_id=quote(str(item_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | KernelEventsAccepted | None:
    if response.status_code == 200:
        response_200 = KernelEventsAccepted.from_dict(response.json())

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
) -> Response[ErrorEnvelope | KernelEventsAccepted]:
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
    body: KernelEventsBatch,
) -> Response[ErrorEnvelope | KernelEventsAccepted]:
    """Kernel Events

     Box -> backend: the kernel's events. Taken only from the machine the
    kernel is bound to, in the kernel's own org; a kernel is bound on its
    first batch to the machine holding the notebook's folder.

    Args:
        drive_id (str):
        item_id (str):
        body (KernelEventsBatch): Box -> backend: a batch of a kernel's events, in sequence order.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | KernelEventsAccepted]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        body=body,
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
    body: KernelEventsBatch,
) -> ErrorEnvelope | KernelEventsAccepted | None:
    """Kernel Events

     Box -> backend: the kernel's events. Taken only from the machine the
    kernel is bound to, in the kernel's own org; a kernel is bound on its
    first batch to the machine holding the notebook's folder.

    Args:
        drive_id (str):
        item_id (str):
        body (KernelEventsBatch): Box -> backend: a batch of a kernel's events, in sequence order.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | KernelEventsAccepted
    """

    return sync_detailed(
        drive_id=drive_id,
        item_id=item_id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
    body: KernelEventsBatch,
) -> Response[ErrorEnvelope | KernelEventsAccepted]:
    """Kernel Events

     Box -> backend: the kernel's events. Taken only from the machine the
    kernel is bound to, in the kernel's own org; a kernel is bound on its
    first batch to the machine holding the notebook's folder.

    Args:
        drive_id (str):
        item_id (str):
        body (KernelEventsBatch): Box -> backend: a batch of a kernel's events, in sequence order.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | KernelEventsAccepted]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
    body: KernelEventsBatch,
) -> ErrorEnvelope | KernelEventsAccepted | None:
    """Kernel Events

     Box -> backend: the kernel's events. Taken only from the machine the
    kernel is bound to, in the kernel's own org; a kernel is bound on its
    first batch to the machine holding the notebook's folder.

    Args:
        drive_id (str):
        item_id (str):
        body (KernelEventsBatch): Box -> backend: a batch of a kernel's events, in sequence order.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | KernelEventsAccepted
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            client=client,
            body=body,
        )
    ).parsed
