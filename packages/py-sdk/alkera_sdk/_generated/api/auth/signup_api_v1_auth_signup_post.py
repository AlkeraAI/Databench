from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.login_response import LoginResponse
from ...models.signup_request import SignupRequest
from ...types import Response


def _get_kwargs(
    *,
    body: SignupRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/auth/signup",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | LoginResponse | None:
    if response.status_code == 201:
        response_201 = LoginResponse.from_dict(response.json())

        return response_201

    if response.status_code == 422:
        response_422 = ErrorEnvelope.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ErrorEnvelope | LoginResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: SignupRequest,
) -> Response[ErrorEnvelope | LoginResponse]:
    """Signup

    Args:
        body (SignupRequest): Minimal signup: only email + password are required. Name and org are
            finished on the complete-profile step.

            Org selection (mutually exclusive — never both):
            - `invite_token` set → join the inviter's org with the invitation's role.
            - `org_name` set → create a new org with that name (the caller becomes Org
              Admin). Optional: omitting it creates a new *unnamed* org the caller names
              on complete-profile.

            Names are optional (the empty-string "profile incomplete" sentinel) and
            collected on complete-profile; a caller that already knows them (apps/web,
            OAuth) may still send them here.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | LoginResponse]
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
    body: SignupRequest,
) -> ErrorEnvelope | LoginResponse | None:
    """Signup

    Args:
        body (SignupRequest): Minimal signup: only email + password are required. Name and org are
            finished on the complete-profile step.

            Org selection (mutually exclusive — never both):
            - `invite_token` set → join the inviter's org with the invitation's role.
            - `org_name` set → create a new org with that name (the caller becomes Org
              Admin). Optional: omitting it creates a new *unnamed* org the caller names
              on complete-profile.

            Names are optional (the empty-string "profile incomplete" sentinel) and
            collected on complete-profile; a caller that already knows them (apps/web,
            OAuth) may still send them here.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | LoginResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: SignupRequest,
) -> Response[ErrorEnvelope | LoginResponse]:
    """Signup

    Args:
        body (SignupRequest): Minimal signup: only email + password are required. Name and org are
            finished on the complete-profile step.

            Org selection (mutually exclusive — never both):
            - `invite_token` set → join the inviter's org with the invitation's role.
            - `org_name` set → create a new org with that name (the caller becomes Org
              Admin). Optional: omitting it creates a new *unnamed* org the caller names
              on complete-profile.

            Names are optional (the empty-string "profile incomplete" sentinel) and
            collected on complete-profile; a caller that already knows them (apps/web,
            OAuth) may still send them here.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | LoginResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: SignupRequest,
) -> ErrorEnvelope | LoginResponse | None:
    """Signup

    Args:
        body (SignupRequest): Minimal signup: only email + password are required. Name and org are
            finished on the complete-profile step.

            Org selection (mutually exclusive — never both):
            - `invite_token` set → join the inviter's org with the invitation's role.
            - `org_name` set → create a new org with that name (the caller becomes Org
              Admin). Optional: omitting it creates a new *unnamed* org the caller names
              on complete-profile.

            Names are optional (the empty-string "profile incomplete" sentinel) and
            collected on complete-profile; a caller that already knows them (apps/web,
            OAuth) may still send them here.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | LoginResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
