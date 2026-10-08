from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.resolve_request import ResolveRequest
from ...models.resolve_result import ResolveResult
from ...types import Response


def _get_kwargs(
    drive_id: UUID,
    conflict_id: UUID,
    *,
    body: ResolveRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/files/drives/{drive_id}/conflicts/{conflict_id}/resolve".format(
            drive_id=quote(str(drive_id), safe=""),
            conflict_id=quote(str(conflict_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | ResolveResult | None:
    if response.status_code == 200:
        response_200 = ResolveResult.from_dict(response.json())

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
) -> Response[ErrorEnvelope | ResolveResult]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    drive_id: UUID,
    conflict_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: ResolveRequest,
) -> Response[ErrorEnvelope | ResolveResult]:
    """Resolve Conflict

     Close one conflict by promoting a side, keeping the other where asked.

    Args:
        drive_id (UUID):
        conflict_id (UUID):
        body (ResolveRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | ResolveResult]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        conflict_id=conflict_id,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    drive_id: UUID,
    conflict_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: ResolveRequest,
) -> ErrorEnvelope | ResolveResult | None:
    """Resolve Conflict

     Close one conflict by promoting a side, keeping the other where asked.

    Args:
        drive_id (UUID):
        conflict_id (UUID):
        body (ResolveRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | ResolveResult
    """

    return sync_detailed(
        drive_id=drive_id,
        conflict_id=conflict_id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    drive_id: UUID,
    conflict_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: ResolveRequest,
) -> Response[ErrorEnvelope | ResolveResult]:
    """Resolve Conflict

     Close one conflict by promoting a side, keeping the other where asked.

    Args:
        drive_id (UUID):
        conflict_id (UUID):
        body (ResolveRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | ResolveResult]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        conflict_id=conflict_id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: UUID,
    conflict_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: ResolveRequest,
) -> ErrorEnvelope | ResolveResult | None:
    """Resolve Conflict

     Close one conflict by promoting a side, keeping the other where asked.

    Args:
        drive_id (UUID):
        conflict_id (UUID):
        body (ResolveRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | ResolveResult
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            conflict_id=conflict_id,
            client=client,
            body=body,
        )
    ).parsed
