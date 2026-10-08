import datetime
from http import HTTPStatus
from typing import Any
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.security_event_page import SecurityEventPage
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    limit: int | Unset = 50,
    before: datetime.datetime | None | Unset = UNSET,
    before_id: None | Unset | UUID = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["limit"] = limit

    json_before: None | str | Unset
    if isinstance(before, Unset):
        json_before = UNSET
    elif isinstance(before, datetime.datetime):
        json_before = before.isoformat()
    else:
        json_before = before
    params["before"] = json_before

    json_before_id: None | str | Unset
    if isinstance(before_id, Unset):
        json_before_id = UNSET
    elif isinstance(before_id, UUID):
        json_before_id = str(before_id)
    else:
        json_before_id = before_id
    params["before_id"] = json_before_id

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/me/security-events",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | SecurityEventPage | None:
    if response.status_code == 200:
        response_200 = SecurityEventPage.from_dict(response.json())

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
) -> Response[ErrorEnvelope | SecurityEventPage]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    limit: int | Unset = 50,
    before: datetime.datetime | None | Unset = UNSET,
    before_id: None | Unset | UUID = UNSET,
) -> Response[ErrorEnvelope | SecurityEventPage]:
    """List My Security Events

     The caller's security events, newest first, one page at a time. The
    next page starts after the last row of this one (``next_before`` and
    ``next_before_id``), so events sharing a timestamp are never skipped. A
    browser session an org's IdP started reads them without any other org's
    id.

    Args:
        limit (int | Unset):  Default: 50.
        before (datetime.datetime | None | Unset):
        before_id (None | Unset | UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | SecurityEventPage]
    """

    kwargs = _get_kwargs(
        limit=limit,
        before=before,
        before_id=before_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
    limit: int | Unset = 50,
    before: datetime.datetime | None | Unset = UNSET,
    before_id: None | Unset | UUID = UNSET,
) -> ErrorEnvelope | SecurityEventPage | None:
    """List My Security Events

     The caller's security events, newest first, one page at a time. The
    next page starts after the last row of this one (``next_before`` and
    ``next_before_id``), so events sharing a timestamp are never skipped. A
    browser session an org's IdP started reads them without any other org's
    id.

    Args:
        limit (int | Unset):  Default: 50.
        before (datetime.datetime | None | Unset):
        before_id (None | Unset | UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | SecurityEventPage
    """

    return sync_detailed(
        client=client,
        limit=limit,
        before=before,
        before_id=before_id,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    limit: int | Unset = 50,
    before: datetime.datetime | None | Unset = UNSET,
    before_id: None | Unset | UUID = UNSET,
) -> Response[ErrorEnvelope | SecurityEventPage]:
    """List My Security Events

     The caller's security events, newest first, one page at a time. The
    next page starts after the last row of this one (``next_before`` and
    ``next_before_id``), so events sharing a timestamp are never skipped. A
    browser session an org's IdP started reads them without any other org's
    id.

    Args:
        limit (int | Unset):  Default: 50.
        before (datetime.datetime | None | Unset):
        before_id (None | Unset | UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | SecurityEventPage]
    """

    kwargs = _get_kwargs(
        limit=limit,
        before=before,
        before_id=before_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    limit: int | Unset = 50,
    before: datetime.datetime | None | Unset = UNSET,
    before_id: None | Unset | UUID = UNSET,
) -> ErrorEnvelope | SecurityEventPage | None:
    """List My Security Events

     The caller's security events, newest first, one page at a time. The
    next page starts after the last row of this one (``next_before`` and
    ``next_before_id``), so events sharing a timestamp are never skipped. A
    browser session an org's IdP started reads them without any other org's
    id.

    Args:
        limit (int | Unset):  Default: 50.
        before (datetime.datetime | None | Unset):
        before_id (None | Unset | UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | SecurityEventPage
    """

    return (
        await asyncio_detailed(
            client=client,
            limit=limit,
            before=before,
            before_id=before_id,
        )
    ).parsed
