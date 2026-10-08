from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.domain_ban_create import DomainBanCreate
from ...models.domain_ban_read import DomainBanRead
from ...models.error_envelope import ErrorEnvelope
from ...types import Response


def _get_kwargs(
    *,
    body: DomainBanCreate,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/admin/v1/bans/domains",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> DomainBanRead | ErrorEnvelope | None:
    if response.status_code == 201:
        response_201 = DomainBanRead.from_dict(response.json())

        return response_201

    if response.status_code == 422:
        response_422 = ErrorEnvelope.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[DomainBanRead | ErrorEnvelope]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: DomainBanCreate,
) -> Response[DomainBanRead | ErrorEnvelope]:
    """Ban Domain

     Ban every address at a domain (exact match, lower-cased; a subdomain is
    a different domain). Existing non-staff accounts there lose their sessions
    now; new signups and invitations at the domain are refused.

    Args:
        body (DomainBanCreate): ``domain`` is normalized on the way in — lower-cased, surrounding
            whitespace and a leading ``@`` dropped — and refused unless what remains is
            a bare hostname, so a full address or a URL is a 422 rather than a ban that
            matches nobody.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DomainBanRead | ErrorEnvelope]
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
    body: DomainBanCreate,
) -> DomainBanRead | ErrorEnvelope | None:
    """Ban Domain

     Ban every address at a domain (exact match, lower-cased; a subdomain is
    a different domain). Existing non-staff accounts there lose their sessions
    now; new signups and invitations at the domain are refused.

    Args:
        body (DomainBanCreate): ``domain`` is normalized on the way in — lower-cased, surrounding
            whitespace and a leading ``@`` dropped — and refused unless what remains is
            a bare hostname, so a full address or a URL is a 422 rather than a ban that
            matches nobody.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DomainBanRead | ErrorEnvelope
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: DomainBanCreate,
) -> Response[DomainBanRead | ErrorEnvelope]:
    """Ban Domain

     Ban every address at a domain (exact match, lower-cased; a subdomain is
    a different domain). Existing non-staff accounts there lose their sessions
    now; new signups and invitations at the domain are refused.

    Args:
        body (DomainBanCreate): ``domain`` is normalized on the way in — lower-cased, surrounding
            whitespace and a leading ``@`` dropped — and refused unless what remains is
            a bare hostname, so a full address or a URL is a 422 rather than a ban that
            matches nobody.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DomainBanRead | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: DomainBanCreate,
) -> DomainBanRead | ErrorEnvelope | None:
    """Ban Domain

     Ban every address at a domain (exact match, lower-cased; a subdomain is
    a different domain). Existing non-staff accounts there lose their sessions
    now; new signups and invitations at the domain are refused.

    Args:
        body (DomainBanCreate): ``domain`` is normalized on the way in — lower-cased, surrounding
            whitespace and a leading ``@`` dropped — and refused unless what remains is
            a bare hostname, so a full address or a URL is a 422 rather than a ban that
            matches nobody.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DomainBanRead | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
