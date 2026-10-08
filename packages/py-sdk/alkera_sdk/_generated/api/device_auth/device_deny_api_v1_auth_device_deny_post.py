from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.device_approval_request import DeviceApprovalRequest
from ...models.error_envelope import ErrorEnvelope
from ...models.message_response import MessageResponse
from ...types import Response


def _get_kwargs(
    *,
    body: DeviceApprovalRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/auth/device/deny",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | MessageResponse | None:
    if response.status_code == 200:
        response_200 = MessageResponse.from_dict(response.json())

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
) -> Response[ErrorEnvelope | MessageResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: DeviceApprovalRequest,
) -> Response[ErrorEnvelope | MessageResponse]:
    """Device Deny

     Deny a pending authorization.

    Args:
        body (DeviceApprovalRequest): SPA approve/deny body — the short user_code shown on the
            device.

            Reached via the device flow (RFC 8628): the user opens the verification URI
            with this code pre-filled and confirms it matches what their CLI/editor
            printed before approving.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | MessageResponse]
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
    body: DeviceApprovalRequest,
) -> ErrorEnvelope | MessageResponse | None:
    """Device Deny

     Deny a pending authorization.

    Args:
        body (DeviceApprovalRequest): SPA approve/deny body — the short user_code shown on the
            device.

            Reached via the device flow (RFC 8628): the user opens the verification URI
            with this code pre-filled and confirms it matches what their CLI/editor
            printed before approving.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | MessageResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: DeviceApprovalRequest,
) -> Response[ErrorEnvelope | MessageResponse]:
    """Device Deny

     Deny a pending authorization.

    Args:
        body (DeviceApprovalRequest): SPA approve/deny body — the short user_code shown on the
            device.

            Reached via the device flow (RFC 8628): the user opens the verification URI
            with this code pre-filled and confirms it matches what their CLI/editor
            printed before approving.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | MessageResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: DeviceApprovalRequest,
) -> ErrorEnvelope | MessageResponse | None:
    """Device Deny

     Deny a pending authorization.

    Args:
        body (DeviceApprovalRequest): SPA approve/deny body — the short user_code shown on the
            device.

            Reached via the device flow (RFC 8628): the user opens the verification URI
            with this code pre-filled and confirms it matches what their CLI/editor
            printed before approving.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | MessageResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
