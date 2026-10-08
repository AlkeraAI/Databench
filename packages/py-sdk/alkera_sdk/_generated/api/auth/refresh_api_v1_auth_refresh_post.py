from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.login_response import LoginResponse
from ...types import Response


def _get_kwargs() -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/auth/refresh",
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> LoginResponse | None:
    if response.status_code == 200:
        response_200 = LoginResponse.from_dict(response.json())

        return response_200

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[LoginResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[LoginResponse]:
    """Refresh

     Exchange the refresh cookie for a fresh access token and a rotated
    refresh token. Reuse of a rotated token ends the whole family; every
    refusal clears both cookies so the browser lands on login instead of
    retrying a dead credential. The refresh cookie's ``Path`` means it
    reaches only this route.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[LoginResponse]
    """

    kwargs = _get_kwargs()

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
) -> LoginResponse | None:
    """Refresh

     Exchange the refresh cookie for a fresh access token and a rotated
    refresh token. Reuse of a rotated token ends the whole family; every
    refusal clears both cookies so the browser lands on login instead of
    retrying a dead credential. The refresh cookie's ``Path`` means it
    reaches only this route.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        LoginResponse
    """

    return sync_detailed(
        client=client,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[LoginResponse]:
    """Refresh

     Exchange the refresh cookie for a fresh access token and a rotated
    refresh token. Reuse of a rotated token ends the whole family; every
    refusal clears both cookies so the browser lands on login instead of
    retrying a dead credential. The refresh cookie's ``Path`` means it
    reaches only this route.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[LoginResponse]
    """

    kwargs = _get_kwargs()

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
) -> LoginResponse | None:
    """Refresh

     Exchange the refresh cookie for a fresh access token and a rotated
    refresh token. Reuse of a rotated token ends the whole family; every
    refusal clears both cookies so the browser lands on login instead of
    retrying a dead credential. The refresh cookie's ``Path`` means it
    reaches only this route.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        LoginResponse
    """

    return (
        await asyncio_detailed(
            client=client,
        )
    ).parsed
