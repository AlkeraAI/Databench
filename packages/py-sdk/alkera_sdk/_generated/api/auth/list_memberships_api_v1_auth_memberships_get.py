from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.membership_list_response import MembershipListResponse
from ...types import Response


def _get_kwargs() -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/auth/memberships",
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> MembershipListResponse | None:
    if response.status_code == 200:
        response_200 = MembershipListResponse.from_dict(response.json())

        return response_200

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[MembershipListResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[MembershipListResponse]:
    """List Memberships

     The caller's own active memberships, most recently used first: the orgs
    this person can switch into; then, with multi-org on, the orgs that
    provisioned them and wait for them to join (``status: "pending"``). Never
    anyone else's, whoever asks.

    A browser session an org's IdP started sees only the org it is in: an
    org's admins run its IdP, and must not learn which other orgs a member
    belongs to.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[MembershipListResponse]
    """

    kwargs = _get_kwargs()

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
) -> MembershipListResponse | None:
    """List Memberships

     The caller's own active memberships, most recently used first: the orgs
    this person can switch into; then, with multi-org on, the orgs that
    provisioned them and wait for them to join (``status: "pending"``). Never
    anyone else's, whoever asks.

    A browser session an org's IdP started sees only the org it is in: an
    org's admins run its IdP, and must not learn which other orgs a member
    belongs to.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        MembershipListResponse
    """

    return sync_detailed(
        client=client,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[MembershipListResponse]:
    """List Memberships

     The caller's own active memberships, most recently used first: the orgs
    this person can switch into; then, with multi-org on, the orgs that
    provisioned them and wait for them to join (``status: "pending"``). Never
    anyone else's, whoever asks.

    A browser session an org's IdP started sees only the org it is in: an
    org's admins run its IdP, and must not learn which other orgs a member
    belongs to.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[MembershipListResponse]
    """

    kwargs = _get_kwargs()

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
) -> MembershipListResponse | None:
    """List Memberships

     The caller's own active memberships, most recently used first: the orgs
    this person can switch into; then, with multi-org on, the orgs that
    provisioned them and wait for them to join (``status: "pending"``). Never
    anyone else's, whoever asks.

    A browser session an org's IdP started sees only the org it is in: an
    org's admins run its IdP, and must not learn which other orgs a member
    belongs to.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        MembershipListResponse
    """

    return (
        await asyncio_detailed(
            client=client,
        )
    ).parsed
