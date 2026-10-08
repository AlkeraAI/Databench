from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.kernel_request import KernelRequest
from ...models.kernel_result import KernelResult
from ...types import Response


def _get_kwargs(
    drive_id: str,
    item_id: str,
    *,
    body: KernelRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/notebooks/{drive_id}/{item_id}/kernel".format(
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
) -> ErrorEnvelope | KernelResult | None:
    if response.status_code == 200:
        response_200 = KernelResult.from_dict(response.json())

        return response_200

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
) -> Response[ErrorEnvelope | KernelResult]:
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
    body: KernelRequest,
) -> Response[ErrorEnvelope | KernelResult]:
    """Kernel Action

     The kernel's state (``status``, Files READ), or an interrupt, restart
    or shutdown carried to it (``notebook.run``). A restart, which starts a
    kernel, wakes a sleeping chat first; stopping a kernel never does (a
    sleeping chat runs none).

    Args:
        drive_id (str):
        item_id (str):
        body (KernelRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | KernelResult]
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
    body: KernelRequest,
) -> ErrorEnvelope | KernelResult | None:
    """Kernel Action

     The kernel's state (``status``, Files READ), or an interrupt, restart
    or shutdown carried to it (``notebook.run``). A restart, which starts a
    kernel, wakes a sleeping chat first; stopping a kernel never does (a
    sleeping chat runs none).

    Args:
        drive_id (str):
        item_id (str):
        body (KernelRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | KernelResult
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
    body: KernelRequest,
) -> Response[ErrorEnvelope | KernelResult]:
    """Kernel Action

     The kernel's state (``status``, Files READ), or an interrupt, restart
    or shutdown carried to it (``notebook.run``). A restart, which starts a
    kernel, wakes a sleeping chat first; stopping a kernel never does (a
    sleeping chat runs none).

    Args:
        drive_id (str):
        item_id (str):
        body (KernelRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | KernelResult]
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
    body: KernelRequest,
) -> ErrorEnvelope | KernelResult | None:
    """Kernel Action

     The kernel's state (``status``, Files READ), or an interrupt, restart
    or shutdown carried to it (``notebook.run``). A restart, which starts a
    kernel, wakes a sleeping chat first; stopping a kernel never does (a
    sleeping chat runs none).

    Args:
        drive_id (str):
        item_id (str):
        body (KernelRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | KernelResult
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            client=client,
            body=body,
        )
    ).parsed
