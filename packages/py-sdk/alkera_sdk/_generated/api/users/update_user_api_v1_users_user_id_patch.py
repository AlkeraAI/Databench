from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.org_user_read import OrgUserRead
from ...models.user_read import UserRead
from ...models.user_update import UserUpdate
from ...types import Response


def _get_kwargs(
    user_id: UUID,
    *,
    body: UserUpdate,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "patch",
        "url": "/api/v1/users/{user_id}".format(
            user_id=quote(str(user_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | OrgUserRead | UserRead | None:
    if response.status_code == 200:

        def _parse_response_200(data: object) -> OrgUserRead | UserRead:
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                response_200_type_0 = UserRead.from_dict(data)

                return response_200_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, dict):
                raise TypeError()
            response_200_type_1 = OrgUserRead.from_dict(data)

            return response_200_type_1

        response_200 = _parse_response_200(response.json())

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
) -> Response[ErrorEnvelope | OrgUserRead | UserRead]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    user_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: UserUpdate,
) -> Response[ErrorEnvelope | OrgUserRead | UserRead]:
    """Update User

     Edit a person. Your own account: your identity's names, email and
    password, answered with your full ``UserRead``. Another member, as an org
    admin: the name this org shows for them, answered with ``OrgUserRead``.

    Args:
        user_id (UUID):
        body (UserUpdate): Self-service or admin-driven update. Cannot change platform_role here
            — that lives behind /admin/v1/users/{id}/platform_role.

            On your own account the names, email and password are your identity's.
            ``current_password`` / ``mfa_code`` are the step-up proof required to change
            either credential-grade field (``email``, ``password``); they are ignored for
            a name-only edit.

            An org admin editing a member sets the name the org shows for them
            (``display_name``, or ``first_name`` + ``last_name`` combined), on their
            membership in the org; the person's own name is never written.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OrgUserRead | UserRead]
    """

    kwargs = _get_kwargs(
        user_id=user_id,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    user_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: UserUpdate,
) -> ErrorEnvelope | OrgUserRead | UserRead | None:
    """Update User

     Edit a person. Your own account: your identity's names, email and
    password, answered with your full ``UserRead``. Another member, as an org
    admin: the name this org shows for them, answered with ``OrgUserRead``.

    Args:
        user_id (UUID):
        body (UserUpdate): Self-service or admin-driven update. Cannot change platform_role here
            — that lives behind /admin/v1/users/{id}/platform_role.

            On your own account the names, email and password are your identity's.
            ``current_password`` / ``mfa_code`` are the step-up proof required to change
            either credential-grade field (``email``, ``password``); they are ignored for
            a name-only edit.

            An org admin editing a member sets the name the org shows for them
            (``display_name``, or ``first_name`` + ``last_name`` combined), on their
            membership in the org; the person's own name is never written.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OrgUserRead | UserRead
    """

    return sync_detailed(
        user_id=user_id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    user_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: UserUpdate,
) -> Response[ErrorEnvelope | OrgUserRead | UserRead]:
    """Update User

     Edit a person. Your own account: your identity's names, email and
    password, answered with your full ``UserRead``. Another member, as an org
    admin: the name this org shows for them, answered with ``OrgUserRead``.

    Args:
        user_id (UUID):
        body (UserUpdate): Self-service or admin-driven update. Cannot change platform_role here
            — that lives behind /admin/v1/users/{id}/platform_role.

            On your own account the names, email and password are your identity's.
            ``current_password`` / ``mfa_code`` are the step-up proof required to change
            either credential-grade field (``email``, ``password``); they are ignored for
            a name-only edit.

            An org admin editing a member sets the name the org shows for them
            (``display_name``, or ``first_name`` + ``last_name`` combined), on their
            membership in the org; the person's own name is never written.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OrgUserRead | UserRead]
    """

    kwargs = _get_kwargs(
        user_id=user_id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    user_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: UserUpdate,
) -> ErrorEnvelope | OrgUserRead | UserRead | None:
    """Update User

     Edit a person. Your own account: your identity's names, email and
    password, answered with your full ``UserRead``. Another member, as an org
    admin: the name this org shows for them, answered with ``OrgUserRead``.

    Args:
        user_id (UUID):
        body (UserUpdate): Self-service or admin-driven update. Cannot change platform_role here
            — that lives behind /admin/v1/users/{id}/platform_role.

            On your own account the names, email and password are your identity's.
            ``current_password`` / ``mfa_code`` are the step-up proof required to change
            either credential-grade field (``email``, ``password``); they are ignored for
            a name-only edit.

            An org admin editing a member sets the name the org shows for them
            (``display_name``, or ``first_name`` + ``last_name`` combined), on their
            membership in the org; the person's own name is never written.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OrgUserRead | UserRead
    """

    return (
        await asyncio_detailed(
            user_id=user_id,
            client=client,
            body=body,
        )
    ).parsed
