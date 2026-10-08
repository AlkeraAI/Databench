from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.admin_created_user_read import AdminCreatedUserRead
from ...models.error_envelope import ErrorEnvelope
from ...models.user_create import UserCreate
from ...types import Response


def _get_kwargs(
    *,
    body: UserCreate,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/users",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> AdminCreatedUserRead | ErrorEnvelope | None:
    if response.status_code == 201:
        response_201 = AdminCreatedUserRead.from_dict(response.json())

        return response_201

    if response.status_code == 202:
        response_202 = AdminCreatedUserRead.from_dict(response.json())

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
) -> Response[AdminCreatedUserRead | ErrorEnvelope]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: UserCreate,
) -> Response[AdminCreatedUserRead | ErrorEnvelope]:
    """Add a person to this organization by email (deprecated: use invitations)

     Deprecated: kept for the clients that script it; invitations are the way
    to add people.

    It no longer lets an admin choose a person's password or reach an identity
    that belongs elsewhere. An address with no account gets one WITHOUT a
    password (201) and the email that lets the person set their own; a password
    in the request is not used and the answer says so (``password_ignored``).
    An address whose identity belongs to another org gets a pending invitation
    into this org instead (202, ``invitation_pending``). An address already in
    this org is a 409, as it always was.

    Args:
        body (UserCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[AdminCreatedUserRead | ErrorEnvelope]
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
    body: UserCreate,
) -> AdminCreatedUserRead | ErrorEnvelope | None:
    """Add a person to this organization by email (deprecated: use invitations)

     Deprecated: kept for the clients that script it; invitations are the way
    to add people.

    It no longer lets an admin choose a person's password or reach an identity
    that belongs elsewhere. An address with no account gets one WITHOUT a
    password (201) and the email that lets the person set their own; a password
    in the request is not used and the answer says so (``password_ignored``).
    An address whose identity belongs to another org gets a pending invitation
    into this org instead (202, ``invitation_pending``). An address already in
    this org is a 409, as it always was.

    Args:
        body (UserCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        AdminCreatedUserRead | ErrorEnvelope
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: UserCreate,
) -> Response[AdminCreatedUserRead | ErrorEnvelope]:
    """Add a person to this organization by email (deprecated: use invitations)

     Deprecated: kept for the clients that script it; invitations are the way
    to add people.

    It no longer lets an admin choose a person's password or reach an identity
    that belongs elsewhere. An address with no account gets one WITHOUT a
    password (201) and the email that lets the person set their own; a password
    in the request is not used and the answer says so (``password_ignored``).
    An address whose identity belongs to another org gets a pending invitation
    into this org instead (202, ``invitation_pending``). An address already in
    this org is a 409, as it always was.

    Args:
        body (UserCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[AdminCreatedUserRead | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: UserCreate,
) -> AdminCreatedUserRead | ErrorEnvelope | None:
    """Add a person to this organization by email (deprecated: use invitations)

     Deprecated: kept for the clients that script it; invitations are the way
    to add people.

    It no longer lets an admin choose a person's password or reach an identity
    that belongs elsewhere. An address with no account gets one WITHOUT a
    password (201) and the email that lets the person set their own; a password
    in the request is not used and the answer says so (``password_ignored``).
    An address whose identity belongs to another org gets a pending invitation
    into this org instead (202, ``invitation_pending``). An address already in
    this org is a 409, as it always was.

    Args:
        body (UserCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        AdminCreatedUserRead | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
