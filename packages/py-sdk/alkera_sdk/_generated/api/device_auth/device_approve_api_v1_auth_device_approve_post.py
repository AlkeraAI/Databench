from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.device_approve_request import DeviceApproveRequest
from ...models.error_envelope import ErrorEnvelope
from ...models.message_response import MessageResponse
from ...types import Response


def _get_kwargs(
    *,
    body: DeviceApproveRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/auth/device/approve",
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
    body: DeviceApproveRequest,
) -> Response[ErrorEnvelope | MessageResponse]:
    """Device Approve

     Approve a pending authorization, binding it to the authenticated user and
    to one of their orgs: the one their browser session is in, or the one the
    body names.

    A named org must be one of the approver's active memberships; anything else
    is the same 404, so the route never says whether an org exists. The org's
    sign-in policy is then asked about the approving session: a CLI token for
    an org that requires SSO is only ever born under a fresh sign-in through
    that org's IdP.

    Args:
        body (DeviceApproveRequest): SPA approve body: the user code, and optionally which of the
            approver's
            orgs the device acts in. Omitted, it is the org the approving browser
            session is in. Checked against the approver's own active memberships; it
            never grants an org a membership does not.

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
    body: DeviceApproveRequest,
) -> ErrorEnvelope | MessageResponse | None:
    """Device Approve

     Approve a pending authorization, binding it to the authenticated user and
    to one of their orgs: the one their browser session is in, or the one the
    body names.

    A named org must be one of the approver's active memberships; anything else
    is the same 404, so the route never says whether an org exists. The org's
    sign-in policy is then asked about the approving session: a CLI token for
    an org that requires SSO is only ever born under a fresh sign-in through
    that org's IdP.

    Args:
        body (DeviceApproveRequest): SPA approve body: the user code, and optionally which of the
            approver's
            orgs the device acts in. Omitted, it is the org the approving browser
            session is in. Checked against the approver's own active memberships; it
            never grants an org a membership does not.

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
    body: DeviceApproveRequest,
) -> Response[ErrorEnvelope | MessageResponse]:
    """Device Approve

     Approve a pending authorization, binding it to the authenticated user and
    to one of their orgs: the one their browser session is in, or the one the
    body names.

    A named org must be one of the approver's active memberships; anything else
    is the same 404, so the route never says whether an org exists. The org's
    sign-in policy is then asked about the approving session: a CLI token for
    an org that requires SSO is only ever born under a fresh sign-in through
    that org's IdP.

    Args:
        body (DeviceApproveRequest): SPA approve body: the user code, and optionally which of the
            approver's
            orgs the device acts in. Omitted, it is the org the approving browser
            session is in. Checked against the approver's own active memberships; it
            never grants an org a membership does not.

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
    body: DeviceApproveRequest,
) -> ErrorEnvelope | MessageResponse | None:
    """Device Approve

     Approve a pending authorization, binding it to the authenticated user and
    to one of their orgs: the one their browser session is in, or the one the
    body names.

    A named org must be one of the approver's active memberships; anything else
    is the same 404, so the route never says whether an org exists. The org's
    sign-in policy is then asked about the approving session: a CLI token for
    an org that requires SSO is only ever born under a fresh sign-in through
    that org's IdP.

    Args:
        body (DeviceApproveRequest): SPA approve body: the user code, and optionally which of the
            approver's
            orgs the device acts in. Omitted, it is the org the approving browser
            session is in. Checked against the approver's own active memberships; it
            never grants an org a membership does not.

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
