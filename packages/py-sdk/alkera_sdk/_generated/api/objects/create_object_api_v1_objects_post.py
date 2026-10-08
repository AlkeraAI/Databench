from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.workspace_object_create import WorkspaceObjectCreate
from ...models.workspace_object_read import WorkspaceObjectRead
from ...types import Response


def _get_kwargs(
    *,
    body: WorkspaceObjectCreate,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/objects",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | WorkspaceObjectRead | None:
    if response.status_code == 201:
        response_201 = WorkspaceObjectRead.from_dict(response.json())

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
) -> Response[ErrorEnvelope | WorkspaceObjectRead]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: WorkspaceObjectCreate,
) -> Response[ErrorEnvelope | WorkspaceObjectRead]:
    """Create Object

     Save a result. Idempotent on the caller's ``client_id``.

    A ``client_id`` that already names an object is a READ of that object,
    decided as one: the row that would be returned is the row that is
    decided on, so a colliding id outside the caller's audience is the
    policy's opaque not-found, never a copy of someone else's result. A new
    object's CREATE decision is filed under the id minted for it.

    A result created here has no payload yet: it waits for its machine like
    a promoted one, and its receipt names the caller as its promoter.

    Args:
        body (WorkspaceObjectCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | WorkspaceObjectRead]
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
    body: WorkspaceObjectCreate,
) -> ErrorEnvelope | WorkspaceObjectRead | None:
    """Create Object

     Save a result. Idempotent on the caller's ``client_id``.

    A ``client_id`` that already names an object is a READ of that object,
    decided as one: the row that would be returned is the row that is
    decided on, so a colliding id outside the caller's audience is the
    policy's opaque not-found, never a copy of someone else's result. A new
    object's CREATE decision is filed under the id minted for it.

    A result created here has no payload yet: it waits for its machine like
    a promoted one, and its receipt names the caller as its promoter.

    Args:
        body (WorkspaceObjectCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | WorkspaceObjectRead
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: WorkspaceObjectCreate,
) -> Response[ErrorEnvelope | WorkspaceObjectRead]:
    """Create Object

     Save a result. Idempotent on the caller's ``client_id``.

    A ``client_id`` that already names an object is a READ of that object,
    decided as one: the row that would be returned is the row that is
    decided on, so a colliding id outside the caller's audience is the
    policy's opaque not-found, never a copy of someone else's result. A new
    object's CREATE decision is filed under the id minted for it.

    A result created here has no payload yet: it waits for its machine like
    a promoted one, and its receipt names the caller as its promoter.

    Args:
        body (WorkspaceObjectCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | WorkspaceObjectRead]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: WorkspaceObjectCreate,
) -> ErrorEnvelope | WorkspaceObjectRead | None:
    """Create Object

     Save a result. Idempotent on the caller's ``client_id``.

    A ``client_id`` that already names an object is a READ of that object,
    decided as one: the row that would be returned is the row that is
    decided on, so a colliding id outside the caller's audience is the
    policy's opaque not-found, never a copy of someone else's result. A new
    object's CREATE decision is filed under the id minted for it.

    A result created here has no payload yet: it waits for its machine like
    a promoted one, and its receipt names the caller as its promoter.

    Args:
        body (WorkspaceObjectCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | WorkspaceObjectRead
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
