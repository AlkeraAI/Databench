from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.complete_profile_request import CompleteProfileRequest
from ...models.error_envelope import ErrorEnvelope
from ...models.user_read import UserRead
from ...types import Response


def _get_kwargs(
    *,
    body: CompleteProfileRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/auth/complete-profile",
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
    *,
    client: AuthenticatedClient | Client,
    body: CompleteProfileRequest,
) -> Response[ErrorEnvelope | UserRead]:
    """Complete Profile

     Finish a profile provisioned without one (minimal signup, JIT/SSO).

    Sets the caller's name. `org_name`, when present, renames the caller's org —
    but only when they ADMIN it (a new-org signup makes them the org admin); it is
    ignored for an invited member, who doesn't own the org they joined.

    Args:
        body (CompleteProfileRequest): Finish a profile provisioned without one (minimal email +
            password signup,
            or a JIT/SSO account).

            `org_name`, when present, renames the caller's org — but only when they admin
            it (the new-org / minimal-signup case). It is ignored for an invited member,
            who doesn't own the organization they joined.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | UserRead]
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
    body: CompleteProfileRequest,
) -> ErrorEnvelope | UserRead | None:
    """Complete Profile

     Finish a profile provisioned without one (minimal signup, JIT/SSO).

    Sets the caller's name. `org_name`, when present, renames the caller's org —
    but only when they ADMIN it (a new-org signup makes them the org admin); it is
    ignored for an invited member, who doesn't own the org they joined.

    Args:
        body (CompleteProfileRequest): Finish a profile provisioned without one (minimal email +
            password signup,
            or a JIT/SSO account).

            `org_name`, when present, renames the caller's org — but only when they admin
            it (the new-org / minimal-signup case). It is ignored for an invited member,
            who doesn't own the organization they joined.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | UserRead
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CompleteProfileRequest,
) -> Response[ErrorEnvelope | UserRead]:
    """Complete Profile

     Finish a profile provisioned without one (minimal signup, JIT/SSO).

    Sets the caller's name. `org_name`, when present, renames the caller's org —
    but only when they ADMIN it (a new-org signup makes them the org admin); it is
    ignored for an invited member, who doesn't own the org they joined.

    Args:
        body (CompleteProfileRequest): Finish a profile provisioned without one (minimal email +
            password signup,
            or a JIT/SSO account).

            `org_name`, when present, renames the caller's org — but only when they admin
            it (the new-org / minimal-signup case). It is ignored for an invited member,
            who doesn't own the organization they joined.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | UserRead]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: CompleteProfileRequest,
) -> ErrorEnvelope | UserRead | None:
    """Complete Profile

     Finish a profile provisioned without one (minimal signup, JIT/SSO).

    Sets the caller's name. `org_name`, when present, renames the caller's org —
    but only when they ADMIN it (a new-org signup makes them the org admin); it is
    ignored for an invited member, who doesn't own the org they joined.

    Args:
        body (CompleteProfileRequest): Finish a profile provisioned without one (minimal email +
            password signup,
            or a JIT/SSO account).

            `org_name`, when present, renames the caller's org — but only when they admin
            it (the new-org / minimal-signup case). It is ignored for an invited member,
            who doesn't own the organization they joined.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | UserRead
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
