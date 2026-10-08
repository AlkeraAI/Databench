from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.invitation_accept_response import InvitationAcceptResponse
from ...types import Response


def _get_kwargs(
    token: str,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/invitations/by-token/{token}/accept".format(
            token=quote(str(token), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | InvitationAcceptResponse | None:
    if response.status_code == 200:
        response_200 = InvitationAcceptResponse.from_dict(response.json())

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
) -> Response[ErrorEnvelope | InvitationAcceptResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    token: str,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | InvitationAcceptResponse]:
    """Accept Invitation By Token

     Accept an emailed invitation link while signed in.

    Served only while multi-org is on (a 404 otherwise: a signed-in account
    could only ever accept into the org it is already in, which the
    recipient's list already offers by id). The link is bound to the address
    it was mailed to: a session for any other account is told which address,
    masked, and nothing is created, so a forwarded link is never redeemed by
    whoever happens to be signed in.

    Args:
        token (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | InvitationAcceptResponse]
    """

    kwargs = _get_kwargs(
        token=token,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    token: str,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | InvitationAcceptResponse | None:
    """Accept Invitation By Token

     Accept an emailed invitation link while signed in.

    Served only while multi-org is on (a 404 otherwise: a signed-in account
    could only ever accept into the org it is already in, which the
    recipient's list already offers by id). The link is bound to the address
    it was mailed to: a session for any other account is told which address,
    masked, and nothing is created, so a forwarded link is never redeemed by
    whoever happens to be signed in.

    Args:
        token (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | InvitationAcceptResponse
    """

    return sync_detailed(
        token=token,
        client=client,
    ).parsed


async def asyncio_detailed(
    token: str,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | InvitationAcceptResponse]:
    """Accept Invitation By Token

     Accept an emailed invitation link while signed in.

    Served only while multi-org is on (a 404 otherwise: a signed-in account
    could only ever accept into the org it is already in, which the
    recipient's list already offers by id). The link is bound to the address
    it was mailed to: a session for any other account is told which address,
    masked, and nothing is created, so a forwarded link is never redeemed by
    whoever happens to be signed in.

    Args:
        token (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | InvitationAcceptResponse]
    """

    kwargs = _get_kwargs(
        token=token,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    token: str,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | InvitationAcceptResponse | None:
    """Accept Invitation By Token

     Accept an emailed invitation link while signed in.

    Served only while multi-org is on (a 404 otherwise: a signed-in account
    could only ever accept into the org it is already in, which the
    recipient's list already offers by id). The link is bound to the address
    it was mailed to: a session for any other account is told which address,
    masked, and nothing is created, so a forwarded link is never redeemed by
    whoever happens to be signed in.

    Args:
        token (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | InvitationAcceptResponse
    """

    return (
        await asyncio_detailed(
            token=token,
            client=client,
        )
    ).parsed
