from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.join_org_request import JoinOrgRequest
from ...models.login_response import LoginResponse
from ...types import Response


def _get_kwargs(
    *,
    body: JoinOrgRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/auth/refresh/org/join",
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
    body: JoinOrgRequest,
) -> Response[ErrorEnvelope | LoginResponse]:
    """Join Org From Landing

     Accept an invitation link from the sign-in landing and enter its org.

    The same acceptance the signed-in link route makes: the link is bound to
    the address it was mailed to, so a session for another account is told
    which address (masked) and nothing is created. When the org's sign-in
    policy wants its single sign-on first, the acceptance stands and the
    answer is the switch route's 409, naming the IdP.

    Args:
        body (JoinOrgRequest): `POST /auth/refresh/org/join`: the invitation link's token,
            accepted by a
            signed-in person who has no org to enter yet.

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
    body: JoinOrgRequest,
) -> ErrorEnvelope | LoginResponse | None:
    """Join Org From Landing

     Accept an invitation link from the sign-in landing and enter its org.

    The same acceptance the signed-in link route makes: the link is bound to
    the address it was mailed to, so a session for another account is told
    which address (masked) and nothing is created. When the org's sign-in
    policy wants its single sign-on first, the acceptance stands and the
    answer is the switch route's 409, naming the IdP.

    Args:
        body (JoinOrgRequest): `POST /auth/refresh/org/join`: the invitation link's token,
            accepted by a
            signed-in person who has no org to enter yet.

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
    body: JoinOrgRequest,
) -> Response[ErrorEnvelope | LoginResponse]:
    """Join Org From Landing

     Accept an invitation link from the sign-in landing and enter its org.

    The same acceptance the signed-in link route makes: the link is bound to
    the address it was mailed to, so a session for another account is told
    which address (masked) and nothing is created. When the org's sign-in
    policy wants its single sign-on first, the acceptance stands and the
    answer is the switch route's 409, naming the IdP.

    Args:
        body (JoinOrgRequest): `POST /auth/refresh/org/join`: the invitation link's token,
            accepted by a
            signed-in person who has no org to enter yet.

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
    body: JoinOrgRequest,
) -> ErrorEnvelope | LoginResponse | None:
    """Join Org From Landing

     Accept an invitation link from the sign-in landing and enter its org.

    The same acceptance the signed-in link route makes: the link is bound to
    the address it was mailed to, so a session for another account is told
    which address (masked) and nothing is created. When the org's sign-in
    policy wants its single sign-on first, the acceptance stands and the
    answer is the switch route's 409, naming the IdP.

    Args:
        body (JoinOrgRequest): `POST /auth/refresh/org/join`: the invitation link's token,
            accepted by a
            signed-in person who has no org to enter yet.

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
