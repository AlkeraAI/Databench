from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.message_response import MessageResponse
from ...types import Response


def _get_kwargs() -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/auth/logout-all",
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> MessageResponse | None:
    if response.status_code == 200:
        response_200 = MessageResponse.from_dict(response.json())

        return response_200

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[MessageResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[MessageResponse]:
    """Logout All

     Revoke every session for the caller (bumps token_epoch, the registry and
    every refresh family), then keep the browser that asked signed in.

    The revocation is committed before anything else happens, so nothing that
    follows can undo it. The page promises "You stay signed in on this
    device": a caller that came with the session cookie gets a fresh session
    past the new ``token_epoch``, in the org the request is in, when that
    org's sign-in policy still admits the session it replaces; otherwise this
    browser is signed out too. A Bearer caller has no cookie session to keep
    and ends signed out with everything else.

    While multi-org is on, only a session that signed in as the person (not
    through an org's IdP) may end their sessions in every org.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[MessageResponse]
    """

    kwargs = _get_kwargs()

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
) -> MessageResponse | None:
    """Logout All

     Revoke every session for the caller (bumps token_epoch, the registry and
    every refresh family), then keep the browser that asked signed in.

    The revocation is committed before anything else happens, so nothing that
    follows can undo it. The page promises "You stay signed in on this
    device": a caller that came with the session cookie gets a fresh session
    past the new ``token_epoch``, in the org the request is in, when that
    org's sign-in policy still admits the session it replaces; otherwise this
    browser is signed out too. A Bearer caller has no cookie session to keep
    and ends signed out with everything else.

    While multi-org is on, only a session that signed in as the person (not
    through an org's IdP) may end their sessions in every org.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        MessageResponse
    """

    return sync_detailed(
        client=client,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[MessageResponse]:
    """Logout All

     Revoke every session for the caller (bumps token_epoch, the registry and
    every refresh family), then keep the browser that asked signed in.

    The revocation is committed before anything else happens, so nothing that
    follows can undo it. The page promises "You stay signed in on this
    device": a caller that came with the session cookie gets a fresh session
    past the new ``token_epoch``, in the org the request is in, when that
    org's sign-in policy still admits the session it replaces; otherwise this
    browser is signed out too. A Bearer caller has no cookie session to keep
    and ends signed out with everything else.

    While multi-org is on, only a session that signed in as the person (not
    through an org's IdP) may end their sessions in every org.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[MessageResponse]
    """

    kwargs = _get_kwargs()

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
) -> MessageResponse | None:
    """Logout All

     Revoke every session for the caller (bumps token_epoch, the registry and
    every refresh family), then keep the browser that asked signed in.

    The revocation is committed before anything else happens, so nothing that
    follows can undo it. The page promises "You stay signed in on this
    device": a caller that came with the session cookie gets a fresh session
    past the new ``token_epoch``, in the org the request is in, when that
    org's sign-in policy still admits the session it replaces; otherwise this
    browser is signed out too. A Bearer caller has no cookie session to keep
    and ends signed out with everything else.

    While multi-org is on, only a session that signed in as the person (not
    through an org's IdP) may end their sessions in every org.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        MessageResponse
    """

    return (
        await asyncio_detailed(
            client=client,
        )
    ).parsed
