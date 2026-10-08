from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.compute_grant_read import ComputeGrantRead
from ...models.compute_grant_upsert_request import ComputeGrantUpsertRequest
from ...models.error_envelope import ErrorEnvelope
from ...types import Response


def _get_kwargs(
    org_id: UUID,
    *,
    body: ComputeGrantUpsertRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "put",
        "url": "/admin/v1/orgs/{org_id}/compute".format(
            org_id=quote(str(org_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ComputeGrantRead | ErrorEnvelope | None:
    if response.status_code == 201:
        response_201 = ComputeGrantRead.from_dict(response.json())

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
) -> Response[ComputeGrantRead | ErrorEnvelope]:
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
    body: ComputeGrantUpsertRequest,
) -> Response[ComputeGrantRead | ErrorEnvelope]:
    """Upsert Org Compute Grant

     Grant the org compute, or bring its live grant up to date.

    Answers 201 for a grant that did not exist and 200 when an existing one was
    updated — the same idempotence the provisioning script relies on.

    Args:
        org_id (UUID):
        body (ComputeGrantUpsertRequest): Grant an org compute, or bring its live grant up to
            date.

            Idempotent on ``(team, machine type)``: a second call with the same target
            updates the live grant instead of stacking a second one.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ComputeGrantRead | ErrorEnvelope]
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
    body: ComputeGrantUpsertRequest,
) -> ComputeGrantRead | ErrorEnvelope | None:
    """Upsert Org Compute Grant

     Grant the org compute, or bring its live grant up to date.

    Answers 201 for a grant that did not exist and 200 when an existing one was
    updated — the same idempotence the provisioning script relies on.

    Args:
        org_id (UUID):
        body (ComputeGrantUpsertRequest): Grant an org compute, or bring its live grant up to
            date.

            Idempotent on ``(team, machine type)``: a second call with the same target
            updates the live grant instead of stacking a second one.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ComputeGrantRead | ErrorEnvelope
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
    body: ComputeGrantUpsertRequest,
) -> Response[ComputeGrantRead | ErrorEnvelope]:
    """Upsert Org Compute Grant

     Grant the org compute, or bring its live grant up to date.

    Answers 201 for a grant that did not exist and 200 when an existing one was
    updated — the same idempotence the provisioning script relies on.

    Args:
        org_id (UUID):
        body (ComputeGrantUpsertRequest): Grant an org compute, or bring its live grant up to
            date.

            Idempotent on ``(team, machine type)``: a second call with the same target
            updates the live grant instead of stacking a second one.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ComputeGrantRead | ErrorEnvelope]
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
    body: ComputeGrantUpsertRequest,
) -> ComputeGrantRead | ErrorEnvelope | None:
    """Upsert Org Compute Grant

     Grant the org compute, or bring its live grant up to date.

    Answers 201 for a grant that did not exist and 200 when an existing one was
    updated — the same idempotence the provisioning script relies on.

    Args:
        org_id (UUID):
        body (ComputeGrantUpsertRequest): Grant an org compute, or bring its live grant up to
            date.

            Idempotent on ``(team, machine type)``: a second call with the same target
            updates the live grant instead of stacking a second one.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ComputeGrantRead | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            org_id=org_id,
            client=client,
            body=body,
        )
    ).parsed
