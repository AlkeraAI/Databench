from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.admin_user_update import AdminUserUpdate
from ...models.error_envelope import ErrorEnvelope
from ...models.user_read import UserRead
from ...types import Response


def _get_kwargs(
    user_id: UUID,
    *,
    body: AdminUserUpdate,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "patch",
        "url": "/admin/v1/users/{user_id}".format(
            user_id=quote(str(user_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | UserRead | None:
    if response.status_code == 200:
        response_200 = UserRead.from_dict(response.json())

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
) -> Response[ErrorEnvelope | UserRead]:
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
    body: AdminUserUpdate,
) -> Response[ErrorEnvelope | UserRead]:
    """Update User

    Args:
        user_id (UUID):
        body (AdminUserUpdate): Fields a Support-tier admin may edit cross-tenant.

            Deliberately *cosmetic only*. Admin/staff can NEVER reset a user's password
            or change their email — both are account-takeover vectors (a password reset
            is a direct hijack; an email change re-routes the self-serve reset link).
            Password changes are the user's own action via the self-serve reset flow.
            Platform role is its own ADMIN-gated endpoint.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | UserRead]
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
    body: AdminUserUpdate,
) -> ErrorEnvelope | UserRead | None:
    """Update User

    Args:
        user_id (UUID):
        body (AdminUserUpdate): Fields a Support-tier admin may edit cross-tenant.

            Deliberately *cosmetic only*. Admin/staff can NEVER reset a user's password
            or change their email — both are account-takeover vectors (a password reset
            is a direct hijack; an email change re-routes the self-serve reset link).
            Password changes are the user's own action via the self-serve reset flow.
            Platform role is its own ADMIN-gated endpoint.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | UserRead
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
    body: AdminUserUpdate,
) -> Response[ErrorEnvelope | UserRead]:
    """Update User

    Args:
        user_id (UUID):
        body (AdminUserUpdate): Fields a Support-tier admin may edit cross-tenant.

            Deliberately *cosmetic only*. Admin/staff can NEVER reset a user's password
            or change their email — both are account-takeover vectors (a password reset
            is a direct hijack; an email change re-routes the self-serve reset link).
            Password changes are the user's own action via the self-serve reset flow.
            Platform role is its own ADMIN-gated endpoint.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | UserRead]
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
    body: AdminUserUpdate,
) -> ErrorEnvelope | UserRead | None:
    """Update User

    Args:
        user_id (UUID):
        body (AdminUserUpdate): Fields a Support-tier admin may edit cross-tenant.

            Deliberately *cosmetic only*. Admin/staff can NEVER reset a user's password
            or change their email — both are account-takeover vectors (a password reset
            is a direct hijack; an email change re-routes the self-serve reset link).
            Password changes are the user's own action via the self-serve reset flow.
            Platform role is its own ADMIN-gated endpoint.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | UserRead
    """

    return (
        await asyncio_detailed(
            user_id=user_id,
            client=client,
            body=body,
        )
    ).parsed
