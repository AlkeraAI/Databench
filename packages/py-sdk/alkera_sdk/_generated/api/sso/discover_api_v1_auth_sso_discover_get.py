from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.sso_discover_response import SsoDiscoverResponse
from ...types import UNSET, Response


def _get_kwargs(
    *,
    email: str,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["email"] = email

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/auth/sso/discover",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | SsoDiscoverResponse | None:
    if response.status_code == 200:
        response_200 = SsoDiscoverResponse.from_dict(response.json())

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
) -> Response[ErrorEnvelope | SsoDiscoverResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    email: str,
) -> Response[ErrorEnvelope | SsoDiscoverResponse]:
    """Discover

     Whether the email's domain is assigned to an org with an enabled SSO
    connection, so the login screen can route the user to that org's IdP.
    Reveals only SSO availability for a domain, never whether an account
    exists.

    Args:
        email (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | SsoDiscoverResponse]
    """

    kwargs = _get_kwargs(
        email=email,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
    email: str,
) -> ErrorEnvelope | SsoDiscoverResponse | None:
    """Discover

     Whether the email's domain is assigned to an org with an enabled SSO
    connection, so the login screen can route the user to that org's IdP.
    Reveals only SSO availability for a domain, never whether an account
    exists.

    Args:
        email (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | SsoDiscoverResponse
    """

    return sync_detailed(
        client=client,
        email=email,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    email: str,
) -> Response[ErrorEnvelope | SsoDiscoverResponse]:
    """Discover

     Whether the email's domain is assigned to an org with an enabled SSO
    connection, so the login screen can route the user to that org's IdP.
    Reveals only SSO availability for a domain, never whether an account
    exists.

    Args:
        email (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | SsoDiscoverResponse]
    """

    kwargs = _get_kwargs(
        email=email,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    email: str,
) -> ErrorEnvelope | SsoDiscoverResponse | None:
    """Discover

     Whether the email's domain is assigned to an org with an enabled SSO
    connection, so the login screen can route the user to that org's IdP.
    Reveals only SSO availability for a domain, never whether an account
    exists.

    Args:
        email (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | SsoDiscoverResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            email=email,
        )
    ).parsed
