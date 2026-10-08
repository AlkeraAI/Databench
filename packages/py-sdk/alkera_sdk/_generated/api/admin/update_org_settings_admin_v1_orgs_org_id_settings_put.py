from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.admin_org_settings_update import AdminOrgSettingsUpdate
from ...models.error_envelope import ErrorEnvelope
from ...models.org_settings_read import OrgSettingsRead
from ...types import Response


def _get_kwargs(
    org_id: UUID,
    *,
    body: AdminOrgSettingsUpdate,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "put",
        "url": "/admin/v1/orgs/{org_id}/settings".format(
            org_id=quote(str(org_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | OrgSettingsRead | None:
    if response.status_code == 200:
        response_200 = OrgSettingsRead.from_dict(response.json())

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
) -> Response[ErrorEnvelope | OrgSettingsRead]:
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
    body: AdminOrgSettingsUpdate,
) -> Response[ErrorEnvelope | OrgSettingsRead]:
    """Update Org Settings

    Args:
        org_id (UUID):
        body (AdminOrgSettingsUpdate): The platform-staff update: everything an org admin may
            change, plus the
            chat sandbox limits, which only Alkera staff set (they size the machine the
            org is billed for), and the project-workspace cap. An explicit null resets
            a limit to its default; an absent field leaves it standing.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OrgSettingsRead]
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
    body: AdminOrgSettingsUpdate,
) -> ErrorEnvelope | OrgSettingsRead | None:
    """Update Org Settings

    Args:
        org_id (UUID):
        body (AdminOrgSettingsUpdate): The platform-staff update: everything an org admin may
            change, plus the
            chat sandbox limits, which only Alkera staff set (they size the machine the
            org is billed for), and the project-workspace cap. An explicit null resets
            a limit to its default; an absent field leaves it standing.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OrgSettingsRead
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
    body: AdminOrgSettingsUpdate,
) -> Response[ErrorEnvelope | OrgSettingsRead]:
    """Update Org Settings

    Args:
        org_id (UUID):
        body (AdminOrgSettingsUpdate): The platform-staff update: everything an org admin may
            change, plus the
            chat sandbox limits, which only Alkera staff set (they size the machine the
            org is billed for), and the project-workspace cap. An explicit null resets
            a limit to its default; an absent field leaves it standing.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OrgSettingsRead]
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
    body: AdminOrgSettingsUpdate,
) -> ErrorEnvelope | OrgSettingsRead | None:
    """Update Org Settings

    Args:
        org_id (UUID):
        body (AdminOrgSettingsUpdate): The platform-staff update: everything an org admin may
            change, plus the
            chat sandbox limits, which only Alkera staff set (they size the machine the
            org is billed for), and the project-workspace cap. An explicit null resets
            a limit to its default; an absent field leaves it standing.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OrgSettingsRead
    """

    return (
        await asyncio_detailed(
            org_id=org_id,
            client=client,
            body=body,
        )
    ).parsed
