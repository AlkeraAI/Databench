from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.user_ban_create import UserBanCreate
from ...models.user_ban_read import UserBanRead
from ...types import Response


def _get_kwargs(
    *,
    body: UserBanCreate,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/admin/v1/bans/users",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | UserBanRead | None:
    if response.status_code == 201:
        response_201 = UserBanRead.from_dict(response.json())

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
) -> Response[ErrorEnvelope | UserBanRead]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: UserBanCreate,
) -> Response[ErrorEnvelope | UserBanRead]:
    """Ban User

     Ban a user: every session they hold is revoked, and every path that
    resolves them answers as if the account did not exist.

    Args:
        body (UserBanCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | UserBanRead]
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
    body: UserBanCreate,
) -> ErrorEnvelope | UserBanRead | None:
    """Ban User

     Ban a user: every session they hold is revoked, and every path that
    resolves them answers as if the account did not exist.

    Args:
        body (UserBanCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | UserBanRead
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: UserBanCreate,
) -> Response[ErrorEnvelope | UserBanRead]:
    """Ban User

     Ban a user: every session they hold is revoked, and every path that
    resolves them answers as if the account did not exist.

    Args:
        body (UserBanCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | UserBanRead]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: UserBanCreate,
) -> ErrorEnvelope | UserBanRead | None:
    """Ban User

     Ban a user: every session they hold is revoked, and every path that
    resolves them answers as if the account did not exist.

    Args:
        body (UserBanCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | UserBanRead
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
