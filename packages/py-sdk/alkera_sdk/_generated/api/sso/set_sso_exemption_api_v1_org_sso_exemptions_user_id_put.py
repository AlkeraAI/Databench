from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.sso_exempt_member import SsoExemptMember
from ...models.sso_exempt_update_request import SsoExemptUpdateRequest
from ...types import Response


def _get_kwargs(
    user_id: UUID,
    *,
    body: SsoExemptUpdateRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "put",
        "url": "/api/v1/org/sso/exemptions/{user_id}".format(
            user_id=quote(str(user_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | SsoExemptMember | None:
    if response.status_code == 200:
        response_200 = SsoExemptMember.from_dict(response.json())

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
) -> Response[ErrorEnvelope | SsoExemptMember]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    user_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: SsoExemptUpdateRequest,
) -> Response[ErrorEnvelope | SsoExemptMember]:
    """Set Sso Exemption

     Grant or revoke a member's break-glass SSO exemption, on their
    membership in this org: it has no effect in any other org.

    Args:
        user_id (UUID):
        body (SsoExemptUpdateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | SsoExemptMember]
    """

    kwargs = _get_kwargs(
        user_id=user_id,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    user_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: SsoExemptUpdateRequest,
) -> ErrorEnvelope | SsoExemptMember | None:
    """Set Sso Exemption

     Grant or revoke a member's break-glass SSO exemption, on their
    membership in this org: it has no effect in any other org.

    Args:
        user_id (UUID):
        body (SsoExemptUpdateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | SsoExemptMember
    """

    return sync_detailed(
        user_id=user_id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    user_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: SsoExemptUpdateRequest,
) -> Response[ErrorEnvelope | SsoExemptMember]:
    """Set Sso Exemption

     Grant or revoke a member's break-glass SSO exemption, on their
    membership in this org: it has no effect in any other org.

    Args:
        user_id (UUID):
        body (SsoExemptUpdateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | SsoExemptMember]
    """

    kwargs = _get_kwargs(
        user_id=user_id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    user_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: SsoExemptUpdateRequest,
) -> ErrorEnvelope | SsoExemptMember | None:
    """Set Sso Exemption

     Grant or revoke a member's break-glass SSO exemption, on their
    membership in this org: it has no effect in any other org.

    Args:
        user_id (UUID):
        body (SsoExemptUpdateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | SsoExemptMember
    """

    return (
        await asyncio_detailed(
            user_id=user_id,
            client=client,
            body=body,
        )
    ).parsed
