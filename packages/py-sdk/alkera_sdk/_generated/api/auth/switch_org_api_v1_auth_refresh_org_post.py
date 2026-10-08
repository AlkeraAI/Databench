from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.login_response import LoginResponse
from ...models.switch_org_request import SwitchOrgRequest
from ...types import Response


def _get_kwargs(
    *,
    body: SwitchOrgRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/auth/refresh/org",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | LoginResponse | None:
    if response.status_code == 200:
        response_200 = LoginResponse.from_dict(response.json())

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
    body: SwitchOrgRequest,
) -> Response[ErrorEnvelope | LoginResponse]:
    """Switch Org

     Move this browser's login session into another of the person's orgs.

    It lives under the refresh cookie's path, so the session is named by the
    refresh cookie (never by anything the body says about who is asking), and
    the org in the body is only a choice among the caller's own active
    memberships: anything else is the same 404, so the route never says
    whether an org exists. The org's sign-in policy is asked before anything
    moves; a step-up is a 409 naming the org's single sign-on, and nothing is
    rotated. On success the refresh token rotates (reuse ends the family, as on
    the refresh route), the family names the new org, a new access token is
    minted for that membership, and the access token this browser held for the
    old org is revoked, so a tab still in the old org can never act again.

    Args:
        body (SwitchOrgRequest): `POST /auth/refresh/org`: the org to switch this browser into.
            Checked
            against the caller's own active memberships; it never selects anything a
            membership does not already grant.

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
    body: SwitchOrgRequest,
) -> ErrorEnvelope | LoginResponse | None:
    """Switch Org

     Move this browser's login session into another of the person's orgs.

    It lives under the refresh cookie's path, so the session is named by the
    refresh cookie (never by anything the body says about who is asking), and
    the org in the body is only a choice among the caller's own active
    memberships: anything else is the same 404, so the route never says
    whether an org exists. The org's sign-in policy is asked before anything
    moves; a step-up is a 409 naming the org's single sign-on, and nothing is
    rotated. On success the refresh token rotates (reuse ends the family, as on
    the refresh route), the family names the new org, a new access token is
    minted for that membership, and the access token this browser held for the
    old org is revoked, so a tab still in the old org can never act again.

    Args:
        body (SwitchOrgRequest): `POST /auth/refresh/org`: the org to switch this browser into.
            Checked
            against the caller's own active memberships; it never selects anything a
            membership does not already grant.

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
    body: SwitchOrgRequest,
) -> Response[ErrorEnvelope | LoginResponse]:
    """Switch Org

     Move this browser's login session into another of the person's orgs.

    It lives under the refresh cookie's path, so the session is named by the
    refresh cookie (never by anything the body says about who is asking), and
    the org in the body is only a choice among the caller's own active
    memberships: anything else is the same 404, so the route never says
    whether an org exists. The org's sign-in policy is asked before anything
    moves; a step-up is a 409 naming the org's single sign-on, and nothing is
    rotated. On success the refresh token rotates (reuse ends the family, as on
    the refresh route), the family names the new org, a new access token is
    minted for that membership, and the access token this browser held for the
    old org is revoked, so a tab still in the old org can never act again.

    Args:
        body (SwitchOrgRequest): `POST /auth/refresh/org`: the org to switch this browser into.
            Checked
            against the caller's own active memberships; it never selects anything a
            membership does not already grant.

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
    body: SwitchOrgRequest,
) -> ErrorEnvelope | LoginResponse | None:
    """Switch Org

     Move this browser's login session into another of the person's orgs.

    It lives under the refresh cookie's path, so the session is named by the
    refresh cookie (never by anything the body says about who is asking), and
    the org in the body is only a choice among the caller's own active
    memberships: anything else is the same 404, so the route never says
    whether an org exists. The org's sign-in policy is asked before anything
    moves; a step-up is a 409 naming the org's single sign-on, and nothing is
    rotated. On success the refresh token rotates (reuse ends the family, as on
    the refresh route), the family names the new org, a new access token is
    minted for that membership, and the access token this browser held for the
    old org is revoked, so a tab still in the old org can never act again.

    Args:
        body (SwitchOrgRequest): `POST /auth/refresh/org`: the org to switch this browser into.
            Checked
            against the caller's own active memberships; it never selects anything a
            membership does not already grant.

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
