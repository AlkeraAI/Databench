from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.join_membership_request import JoinMembershipRequest
from ...models.membership_read import MembershipRead
from ...types import Response


def _get_kwargs(
    *,
    body: JoinMembershipRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/auth/memberships/join",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | MembershipRead | None:
    if response.status_code == 200:
        response_200 = MembershipRead.from_dict(response.json())

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
) -> Response[ErrorEnvelope | MembershipRead]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: JoinMembershipRequest,
) -> Response[ErrorEnvelope | MembershipRead]:
    """Join Membership

     Accept an org's pending membership: the org provisioned the person (its
    SCIM), and only the person can say yes. The org in the body is only a
    choice among the caller's own pending memberships; anything else, and
    every call while multi-org is off, is the same 404. The org's sign-in
    policy is asked first, against this browser's login session: a step-up is
    the 409 the switch route answers, naming the org's single sign-on, and
    nothing changes. On success the membership is active and the person may
    switch into it (``POST /auth/refresh/org``).

    Args:
        body (JoinMembershipRequest): `POST /auth/memberships/join`: the org whose pending
            membership the
            caller accepts. Checked against the caller's own pending memberships; it
            never selects anything else.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | MembershipRead]
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
    body: JoinMembershipRequest,
) -> ErrorEnvelope | MembershipRead | None:
    """Join Membership

     Accept an org's pending membership: the org provisioned the person (its
    SCIM), and only the person can say yes. The org in the body is only a
    choice among the caller's own pending memberships; anything else, and
    every call while multi-org is off, is the same 404. The org's sign-in
    policy is asked first, against this browser's login session: a step-up is
    the 409 the switch route answers, naming the org's single sign-on, and
    nothing changes. On success the membership is active and the person may
    switch into it (``POST /auth/refresh/org``).

    Args:
        body (JoinMembershipRequest): `POST /auth/memberships/join`: the org whose pending
            membership the
            caller accepts. Checked against the caller's own pending memberships; it
            never selects anything else.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | MembershipRead
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: JoinMembershipRequest,
) -> Response[ErrorEnvelope | MembershipRead]:
    """Join Membership

     Accept an org's pending membership: the org provisioned the person (its
    SCIM), and only the person can say yes. The org in the body is only a
    choice among the caller's own pending memberships; anything else, and
    every call while multi-org is off, is the same 404. The org's sign-in
    policy is asked first, against this browser's login session: a step-up is
    the 409 the switch route answers, naming the org's single sign-on, and
    nothing changes. On success the membership is active and the person may
    switch into it (``POST /auth/refresh/org``).

    Args:
        body (JoinMembershipRequest): `POST /auth/memberships/join`: the org whose pending
            membership the
            caller accepts. Checked against the caller's own pending memberships; it
            never selects anything else.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | MembershipRead]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: JoinMembershipRequest,
) -> ErrorEnvelope | MembershipRead | None:
    """Join Membership

     Accept an org's pending membership: the org provisioned the person (its
    SCIM), and only the person can say yes. The org in the body is only a
    choice among the caller's own pending memberships; anything else, and
    every call while multi-org is off, is the same 404. The org's sign-in
    policy is asked first, against this browser's login session: a step-up is
    the 409 the switch route answers, naming the org's single sign-on, and
    nothing changes. On success the membership is active and the person may
    switch into it (``POST /auth/refresh/org``).

    Args:
        body (JoinMembershipRequest): `POST /auth/memberships/join`: the org whose pending
            membership the
            caller accepts. Checked against the caller's own pending memberships; it
            never selects anything else.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | MembershipRead
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
