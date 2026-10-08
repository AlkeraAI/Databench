from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.sso_link_read import SsoLinkRead
from ...types import Response


def _get_kwargs() -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/auth/sso-link",
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> SsoLinkRead | None:
    if response.status_code == 200:
        response_200 = SsoLinkRead.from_dict(response.json())

        return response_200

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[SsoLinkRead]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[SsoLinkRead]:
    """Get Sso Link

     The parked request this browser's cookie names, for the signed-in
    identity: 404 when there is none (missing, expired, consumed, or multi-org
    off), 409 ``sso_link_other_account`` when the browser is signed in as an
    identity the request is not about.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[SsoLinkRead]
    """

    kwargs = _get_kwargs()

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
) -> SsoLinkRead | None:
    """Get Sso Link

     The parked request this browser's cookie names, for the signed-in
    identity: 404 when there is none (missing, expired, consumed, or multi-org
    off), 409 ``sso_link_other_account`` when the browser is signed in as an
    identity the request is not about.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        SsoLinkRead
    """

    return sync_detailed(
        client=client,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[SsoLinkRead]:
    """Get Sso Link

     The parked request this browser's cookie names, for the signed-in
    identity: 404 when there is none (missing, expired, consumed, or multi-org
    off), 409 ``sso_link_other_account`` when the browser is signed in as an
    identity the request is not about.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[SsoLinkRead]
    """

    kwargs = _get_kwargs()

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
) -> SsoLinkRead | None:
    """Get Sso Link

     The parked request this browser's cookie names, for the signed-in
    identity: 404 when there is none (missing, expired, consumed, or multi-org
    off), 409 ``sso_link_other_account`` when the browser is signed in as an
    identity the request is not about.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        SsoLinkRead
    """

    return (
        await asyncio_detailed(
            client=client,
        )
    ).parsed
