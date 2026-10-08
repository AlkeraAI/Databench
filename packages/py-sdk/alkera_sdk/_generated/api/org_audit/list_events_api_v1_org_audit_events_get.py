import datetime
from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.org_audit_page import OrgAuditPage
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    offset: int | Unset = 0,
    limit: int | Unset = 50,
    action: None | str | Unset = UNSET,
    actor_email: None | str | Unset = UNSET,
    created_after: datetime.datetime | None | Unset = UNSET,
    created_before: datetime.datetime | None | Unset = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["offset"] = offset

    params["limit"] = limit

    json_action: None | str | Unset
    if isinstance(action, Unset):
        json_action = UNSET
    else:
        json_action = action
    params["action"] = json_action

    json_actor_email: None | str | Unset
    if isinstance(actor_email, Unset):
        json_actor_email = UNSET
    else:
        json_actor_email = actor_email
    params["actor_email"] = json_actor_email

    json_created_after: None | str | Unset
    if isinstance(created_after, Unset):
        json_created_after = UNSET
    elif isinstance(created_after, datetime.datetime):
        json_created_after = created_after.isoformat()
    else:
        json_created_after = created_after
    params["created_after"] = json_created_after

    json_created_before: None | str | Unset
    if isinstance(created_before, Unset):
        json_created_before = UNSET
    elif isinstance(created_before, datetime.datetime):
        json_created_before = created_before.isoformat()
    else:
        json_created_before = created_before
    params["created_before"] = json_created_before

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/org/audit-events",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | OrgAuditPage | None:
    if response.status_code == 200:
        response_200 = OrgAuditPage.from_dict(response.json())

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
) -> Response[ErrorEnvelope | OrgAuditPage]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    offset: int | Unset = 0,
    limit: int | Unset = 50,
    action: None | str | Unset = UNSET,
    actor_email: None | str | Unset = UNSET,
    created_after: datetime.datetime | None | Unset = UNSET,
    created_before: datetime.datetime | None | Unset = UNSET,
) -> Response[ErrorEnvelope | OrgAuditPage]:
    """List Events

    Args:
        offset (int | Unset):  Default: 0.
        limit (int | Unset):  Default: 50.
        action (None | str | Unset): Action prefix match
        actor_email (None | str | Unset):
        created_after (datetime.datetime | None | Unset): Inclusive lower bound; no offset reads
            as UTC
        created_before (datetime.datetime | None | Unset): Inclusive upper bound; no offset reads
            as UTC

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OrgAuditPage]
    """

    kwargs = _get_kwargs(
        offset=offset,
        limit=limit,
        action=action,
        actor_email=actor_email,
        created_after=created_after,
        created_before=created_before,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
    offset: int | Unset = 0,
    limit: int | Unset = 50,
    action: None | str | Unset = UNSET,
    actor_email: None | str | Unset = UNSET,
    created_after: datetime.datetime | None | Unset = UNSET,
    created_before: datetime.datetime | None | Unset = UNSET,
) -> ErrorEnvelope | OrgAuditPage | None:
    """List Events

    Args:
        offset (int | Unset):  Default: 0.
        limit (int | Unset):  Default: 50.
        action (None | str | Unset): Action prefix match
        actor_email (None | str | Unset):
        created_after (datetime.datetime | None | Unset): Inclusive lower bound; no offset reads
            as UTC
        created_before (datetime.datetime | None | Unset): Inclusive upper bound; no offset reads
            as UTC

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OrgAuditPage
    """

    return sync_detailed(
        client=client,
        offset=offset,
        limit=limit,
        action=action,
        actor_email=actor_email,
        created_after=created_after,
        created_before=created_before,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    offset: int | Unset = 0,
    limit: int | Unset = 50,
    action: None | str | Unset = UNSET,
    actor_email: None | str | Unset = UNSET,
    created_after: datetime.datetime | None | Unset = UNSET,
    created_before: datetime.datetime | None | Unset = UNSET,
) -> Response[ErrorEnvelope | OrgAuditPage]:
    """List Events

    Args:
        offset (int | Unset):  Default: 0.
        limit (int | Unset):  Default: 50.
        action (None | str | Unset): Action prefix match
        actor_email (None | str | Unset):
        created_after (datetime.datetime | None | Unset): Inclusive lower bound; no offset reads
            as UTC
        created_before (datetime.datetime | None | Unset): Inclusive upper bound; no offset reads
            as UTC

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OrgAuditPage]
    """

    kwargs = _get_kwargs(
        offset=offset,
        limit=limit,
        action=action,
        actor_email=actor_email,
        created_after=created_after,
        created_before=created_before,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    offset: int | Unset = 0,
    limit: int | Unset = 50,
    action: None | str | Unset = UNSET,
    actor_email: None | str | Unset = UNSET,
    created_after: datetime.datetime | None | Unset = UNSET,
    created_before: datetime.datetime | None | Unset = UNSET,
) -> ErrorEnvelope | OrgAuditPage | None:
    """List Events

    Args:
        offset (int | Unset):  Default: 0.
        limit (int | Unset):  Default: 50.
        action (None | str | Unset): Action prefix match
        actor_email (None | str | Unset):
        created_after (datetime.datetime | None | Unset): Inclusive lower bound; no offset reads
            as UTC
        created_before (datetime.datetime | None | Unset): Inclusive upper bound; no offset reads
            as UTC

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OrgAuditPage
    """

    return (
        await asyncio_detailed(
            client=client,
            offset=offset,
            limit=limit,
            action=action,
            actor_email=actor_email,
            created_after=created_after,
            created_before=created_before,
        )
    ).parsed
