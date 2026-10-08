from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.org_machine_grant import OrgMachineGrant
from ...models.org_machine_read import OrgMachineRead
from ...types import Response


def _get_kwargs(
    org_id: UUID,
    *,
    body: OrgMachineGrant,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/admin/v1/orgs/{org_id}/machines".format(
            org_id=quote(str(org_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | OrgMachineRead | None:
    if response.status_code == 202:
        response_202 = OrgMachineRead.from_dict(response.json())

        return response_202

    if response.status_code == 422:
        response_422 = ErrorEnvelope.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ErrorEnvelope | OrgMachineRead]:
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
    body: OrgMachineGrant,
) -> Response[ErrorEnvelope | OrgMachineRead]:
    """Grant Org Machine

     Give the org a machine, free until ``free_until``. Answers once the
    machine is recorded; it starts in the background.

    Args:
        org_id (UUID):
        body (OrgMachineGrant): A machine Alkera gives an org, free until ``free_until``. Platform
            only.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OrgMachineRead]
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
    body: OrgMachineGrant,
) -> ErrorEnvelope | OrgMachineRead | None:
    """Grant Org Machine

     Give the org a machine, free until ``free_until``. Answers once the
    machine is recorded; it starts in the background.

    Args:
        org_id (UUID):
        body (OrgMachineGrant): A machine Alkera gives an org, free until ``free_until``. Platform
            only.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OrgMachineRead
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
    body: OrgMachineGrant,
) -> Response[ErrorEnvelope | OrgMachineRead]:
    """Grant Org Machine

     Give the org a machine, free until ``free_until``. Answers once the
    machine is recorded; it starts in the background.

    Args:
        org_id (UUID):
        body (OrgMachineGrant): A machine Alkera gives an org, free until ``free_until``. Platform
            only.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OrgMachineRead]
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
    body: OrgMachineGrant,
) -> ErrorEnvelope | OrgMachineRead | None:
    """Grant Org Machine

     Give the org a machine, free until ``free_until``. Answers once the
    machine is recorded; it starts in the background.

    Args:
        org_id (UUID):
        body (OrgMachineGrant): A machine Alkera gives an org, free until ``free_until``. Platform
            only.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OrgMachineRead
    """

    return (
        await asyncio_detailed(
            org_id=org_id,
            client=client,
            body=body,
        )
    ).parsed
