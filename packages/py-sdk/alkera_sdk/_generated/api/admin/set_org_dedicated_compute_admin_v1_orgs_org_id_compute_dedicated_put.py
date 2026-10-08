from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.org_compute_assignment_read import OrgComputeAssignmentRead
from ...models.org_compute_assignment_update import OrgComputeAssignmentUpdate
from ...types import Response


def _get_kwargs(
    org_id: UUID,
    *,
    body: OrgComputeAssignmentUpdate,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "put",
        "url": "/admin/v1/orgs/{org_id}/compute/dedicated".format(
            org_id=quote(str(org_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | OrgComputeAssignmentRead | None:
    if response.status_code == 200:
        response_200 = OrgComputeAssignmentRead.from_dict(response.json())

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
) -> Response[ErrorEnvelope | OrgComputeAssignmentRead]:
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
    body: OrgComputeAssignmentUpdate,
) -> Response[ErrorEnvelope | OrgComputeAssignmentRead]:
    """Set Org Dedicated Compute

     Hold one hand-provisioned dedicated box as the org's granted pool
    machine, or let it go (``machine_id: null``).

    The box must be a ``dedicated`` platform machine that is not past its
    life and has not served another org (``409``): a dedicated box serves one
    org for its whole life. The org's existing granted pool machine is pointed
    at it, or one is created, free for a year, so nothing stops at deploy.

    Args:
        org_id (UUID):
        body (OrgComputeAssignmentUpdate): Point an org's chats at one dedicated box, or return it
            to the pool.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OrgComputeAssignmentRead]
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
    body: OrgComputeAssignmentUpdate,
) -> ErrorEnvelope | OrgComputeAssignmentRead | None:
    """Set Org Dedicated Compute

     Hold one hand-provisioned dedicated box as the org's granted pool
    machine, or let it go (``machine_id: null``).

    The box must be a ``dedicated`` platform machine that is not past its
    life and has not served another org (``409``): a dedicated box serves one
    org for its whole life. The org's existing granted pool machine is pointed
    at it, or one is created, free for a year, so nothing stops at deploy.

    Args:
        org_id (UUID):
        body (OrgComputeAssignmentUpdate): Point an org's chats at one dedicated box, or return it
            to the pool.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OrgComputeAssignmentRead
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
    body: OrgComputeAssignmentUpdate,
) -> Response[ErrorEnvelope | OrgComputeAssignmentRead]:
    """Set Org Dedicated Compute

     Hold one hand-provisioned dedicated box as the org's granted pool
    machine, or let it go (``machine_id: null``).

    The box must be a ``dedicated`` platform machine that is not past its
    life and has not served another org (``409``): a dedicated box serves one
    org for its whole life. The org's existing granted pool machine is pointed
    at it, or one is created, free for a year, so nothing stops at deploy.

    Args:
        org_id (UUID):
        body (OrgComputeAssignmentUpdate): Point an org's chats at one dedicated box, or return it
            to the pool.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OrgComputeAssignmentRead]
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
    body: OrgComputeAssignmentUpdate,
) -> ErrorEnvelope | OrgComputeAssignmentRead | None:
    """Set Org Dedicated Compute

     Hold one hand-provisioned dedicated box as the org's granted pool
    machine, or let it go (``machine_id: null``).

    The box must be a ``dedicated`` platform machine that is not past its
    life and has not served another org (``409``): a dedicated box serves one
    org for its whole life. The org's existing granted pool machine is pointed
    at it, or one is created, free for a year, so nothing stops at deploy.

    Args:
        org_id (UUID):
        body (OrgComputeAssignmentUpdate): Point an org's chats at one dedicated box, or return it
            to the pool.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OrgComputeAssignmentRead
    """

    return (
        await asyncio_detailed(
            org_id=org_id,
            client=client,
            body=body,
        )
    ).parsed
