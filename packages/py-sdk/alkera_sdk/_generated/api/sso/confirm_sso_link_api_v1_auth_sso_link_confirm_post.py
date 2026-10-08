from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.sso_link_confirm_response import SsoLinkConfirmResponse
from ...types import Response


def _get_kwargs() -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/auth/sso-link/confirm",
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> SsoLinkConfirmResponse | None:
    if response.status_code == 200:
        response_200 = SsoLinkConfirmResponse.from_dict(response.json())

        return response_200

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[SsoLinkConfirmResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[SsoLinkConfirmResponse]:
    """Confirm Sso Link

     Link the org's IdP subject to the signed-in identity and join the org
    when it asked for them (a pending invitation, or a membership its SCIM
    provisioned). Single use. Refusals: 404 (no live request), 409
    ``sso_link_other_account``, 409 ``sso_subject_linked`` (the subject is
    another identity's), 409 ``sso_provider_linked`` (this identity holds a
    different subject at the same IdP), 403 ``account_deactivated``.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[SsoLinkConfirmResponse]
    """

    kwargs = _get_kwargs()

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
) -> SsoLinkConfirmResponse | None:
    """Confirm Sso Link

     Link the org's IdP subject to the signed-in identity and join the org
    when it asked for them (a pending invitation, or a membership its SCIM
    provisioned). Single use. Refusals: 404 (no live request), 409
    ``sso_link_other_account``, 409 ``sso_subject_linked`` (the subject is
    another identity's), 409 ``sso_provider_linked`` (this identity holds a
    different subject at the same IdP), 403 ``account_deactivated``.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        SsoLinkConfirmResponse
    """

    return sync_detailed(
        client=client,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[SsoLinkConfirmResponse]:
    """Confirm Sso Link

     Link the org's IdP subject to the signed-in identity and join the org
    when it asked for them (a pending invitation, or a membership its SCIM
    provisioned). Single use. Refusals: 404 (no live request), 409
    ``sso_link_other_account``, 409 ``sso_subject_linked`` (the subject is
    another identity's), 409 ``sso_provider_linked`` (this identity holds a
    different subject at the same IdP), 403 ``account_deactivated``.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[SsoLinkConfirmResponse]
    """

    kwargs = _get_kwargs()

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
) -> SsoLinkConfirmResponse | None:
    """Confirm Sso Link

     Link the org's IdP subject to the signed-in identity and join the org
    when it asked for them (a pending invitation, or a membership its SCIM
    provisioned). Single use. Refusals: 404 (no live request), 409
    ``sso_link_other_account``, 409 ``sso_subject_linked`` (the subject is
    another identity's), 409 ``sso_provider_linked`` (this identity holds a
    different subject at the same IdP), 403 ``account_deactivated``.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        SsoLinkConfirmResponse
    """

    return (
        await asyncio_detailed(
            client=client,
        )
    ).parsed
