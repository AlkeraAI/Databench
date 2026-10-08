from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.create_org_request import CreateOrgRequest
from ...models.error_envelope import ErrorEnvelope
from ...models.org_created_response import OrgCreatedResponse
from ...types import Response


def _get_kwargs(
    *,
    body: CreateOrgRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/orgs",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | OrgCreatedResponse | None:
    if response.status_code == 201:
        response_201 = OrgCreatedResponse.from_dict(response.json())

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
) -> Response[ErrorEnvelope | OrgCreatedResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CreateOrgRequest,
) -> Response[ErrorEnvelope | OrgCreatedResponse]:
    """Create Org

     Create an org with the caller as its owner. The caller's session stays
    in the org it is in; the client switches into the new one.

    Args:
        body (CreateOrgRequest): `POST /orgs` (and the sign-in landing's `POST
            /auth/refresh/org/new`):
            the new org's name. Held to the same name rules as any org name.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OrgCreatedResponse]
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
    body: CreateOrgRequest,
) -> ErrorEnvelope | OrgCreatedResponse | None:
    """Create Org

     Create an org with the caller as its owner. The caller's session stays
    in the org it is in; the client switches into the new one.

    Args:
        body (CreateOrgRequest): `POST /orgs` (and the sign-in landing's `POST
            /auth/refresh/org/new`):
            the new org's name. Held to the same name rules as any org name.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OrgCreatedResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CreateOrgRequest,
) -> Response[ErrorEnvelope | OrgCreatedResponse]:
    """Create Org

     Create an org with the caller as its owner. The caller's session stays
    in the org it is in; the client switches into the new one.

    Args:
        body (CreateOrgRequest): `POST /orgs` (and the sign-in landing's `POST
            /auth/refresh/org/new`):
            the new org's name. Held to the same name rules as any org name.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OrgCreatedResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: CreateOrgRequest,
) -> ErrorEnvelope | OrgCreatedResponse | None:
    """Create Org

     Create an org with the caller as its owner. The caller's session stays
    in the org it is in; the client switches into the new one.

    Args:
        body (CreateOrgRequest): `POST /orgs` (and the sign-in landing's `POST
            /auth/refresh/org/new`):
            the new org's name. Held to the same name rules as any org name.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OrgCreatedResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
