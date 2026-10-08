from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.org_storage_limit_update import OrgStorageLimitUpdate
from ...models.org_storage_read import OrgStorageRead
from ...types import Response


def _get_kwargs(
    org_id: UUID,
    *,
    body: OrgStorageLimitUpdate,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "put",
        "url": "/admin/v1/orgs/{org_id}/storage".format(
            org_id=quote(str(org_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | OrgStorageRead | None:
    if response.status_code == 200:
        response_200 = OrgStorageRead.from_dict(response.json())

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
) -> Response[ErrorEnvelope | OrgStorageRead]:
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
    body: OrgStorageLimitUpdate,
) -> Response[ErrorEnvelope | OrgStorageRead]:
    """Set Org Storage

     Set the org's ceiling by hand. ``limit_bytes: null`` is an explicit
    "unlimited"; the plan's figure no longer applies either way.

    Args:
        org_id (UUID):
        body (OrgStorageLimitUpdate): ``null`` is an explicit "unlimited"; a figure is a ceiling
            in bytes.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OrgStorageRead]
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
    body: OrgStorageLimitUpdate,
) -> ErrorEnvelope | OrgStorageRead | None:
    """Set Org Storage

     Set the org's ceiling by hand. ``limit_bytes: null`` is an explicit
    "unlimited"; the plan's figure no longer applies either way.

    Args:
        org_id (UUID):
        body (OrgStorageLimitUpdate): ``null`` is an explicit "unlimited"; a figure is a ceiling
            in bytes.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OrgStorageRead
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
    body: OrgStorageLimitUpdate,
) -> Response[ErrorEnvelope | OrgStorageRead]:
    """Set Org Storage

     Set the org's ceiling by hand. ``limit_bytes: null`` is an explicit
    "unlimited"; the plan's figure no longer applies either way.

    Args:
        org_id (UUID):
        body (OrgStorageLimitUpdate): ``null`` is an explicit "unlimited"; a figure is a ceiling
            in bytes.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OrgStorageRead]
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
    body: OrgStorageLimitUpdate,
) -> ErrorEnvelope | OrgStorageRead | None:
    """Set Org Storage

     Set the org's ceiling by hand. ``limit_bytes: null`` is an explicit
    "unlimited"; the plan's figure no longer applies either way.

    Args:
        org_id (UUID):
        body (OrgStorageLimitUpdate): ``null`` is an explicit "unlimited"; a figure is a ceiling
            in bytes.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OrgStorageRead
    """

    return (
        await asyncio_detailed(
            org_id=org_id,
            client=client,
            body=body,
        )
    ).parsed
