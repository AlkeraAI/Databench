from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.sso_domains_read import SsoDomainsRead
from ...models.sso_domains_update import SsoDomainsUpdate
from ...types import Response


def _get_kwargs(
    org_id: UUID,
    *,
    body: SsoDomainsUpdate,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "put",
        "url": "/admin/v1/orgs/{org_id}/sso/domains".format(
            org_id=quote(str(org_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | SsoDomainsRead | None:
    if response.status_code == 200:
        response_200 = SsoDomainsRead.from_dict(response.json())

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
) -> Response[ErrorEnvelope | SsoDomainsRead]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    org_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: SsoDomainsUpdate,
) -> Response[ErrorEnvelope | SsoDomainsRead]:
    """Set Sso Domains

     Replace the org's SSO email domains with ``domains``.

    400 for a value that is not a domain or is a public mailbox provider's;
    409 ``sso_domain_held`` when another org holds one (which org is not
    said). Nothing changes on a refusal. An org left with no domain stops
    requiring SSO, since its IdP could then sign nobody in.

    Args:
        org_id (UUID):
        body (SsoDomainsUpdate): The org's whole set of SSO email domains, replacing what it had.
            Set by
            platform staff only.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | SsoDomainsRead]
    """

    kwargs = _get_kwargs(
        org_id=org_id,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    org_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: SsoDomainsUpdate,
) -> ErrorEnvelope | SsoDomainsRead | None:
    """Set Sso Domains

     Replace the org's SSO email domains with ``domains``.

    400 for a value that is not a domain or is a public mailbox provider's;
    409 ``sso_domain_held`` when another org holds one (which org is not
    said). Nothing changes on a refusal. An org left with no domain stops
    requiring SSO, since its IdP could then sign nobody in.

    Args:
        org_id (UUID):
        body (SsoDomainsUpdate): The org's whole set of SSO email domains, replacing what it had.
            Set by
            platform staff only.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | SsoDomainsRead
    """

    return sync_detailed(
        org_id=org_id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    org_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: SsoDomainsUpdate,
) -> Response[ErrorEnvelope | SsoDomainsRead]:
    """Set Sso Domains

     Replace the org's SSO email domains with ``domains``.

    400 for a value that is not a domain or is a public mailbox provider's;
    409 ``sso_domain_held`` when another org holds one (which org is not
    said). Nothing changes on a refusal. An org left with no domain stops
    requiring SSO, since its IdP could then sign nobody in.

    Args:
        org_id (UUID):
        body (SsoDomainsUpdate): The org's whole set of SSO email domains, replacing what it had.
            Set by
            platform staff only.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | SsoDomainsRead]
    """

    kwargs = _get_kwargs(
        org_id=org_id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    org_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: SsoDomainsUpdate,
) -> ErrorEnvelope | SsoDomainsRead | None:
    """Set Sso Domains

     Replace the org's SSO email domains with ``domains``.

    400 for a value that is not a domain or is a public mailbox provider's;
    409 ``sso_domain_held`` when another org holds one (which org is not
    said). Nothing changes on a refusal. An org left with no domain stops
    requiring SSO, since its IdP could then sign nobody in.

    Args:
        org_id (UUID):
        body (SsoDomainsUpdate): The org's whole set of SSO email domains, replacing what it had.
            Set by
            platform staff only.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | SsoDomainsRead
    """

    return (
        await asyncio_detailed(
            org_id=org_id,
            client=client,
            body=body,
        )
    ).parsed
