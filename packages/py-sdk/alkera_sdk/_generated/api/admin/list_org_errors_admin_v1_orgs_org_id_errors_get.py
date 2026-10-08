import datetime
from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.org_issue_list import OrgIssueList
from ...types import UNSET, Response, Unset


def _get_kwargs(
    org_id: UUID,
    *,
    since: datetime.datetime | None | Unset = UNSET,
    limit: int | Unset = 50,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    json_since: None | str | Unset
    if isinstance(since, Unset):
        json_since = UNSET
    elif isinstance(since, datetime.datetime):
        json_since = since.isoformat()
    else:
        json_since = since
    params["since"] = json_since

    params["limit"] = limit

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/admin/v1/orgs/{org_id}/errors".format(
            org_id=quote(str(org_id), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | OrgIssueList | None:
    if response.status_code == 200:
        response_200 = OrgIssueList.from_dict(response.json())

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
) -> Response[ErrorEnvelope | OrgIssueList]:
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
    since: datetime.datetime | None | Unset = UNSET,
    limit: int | Unset = 50,
) -> Response[ErrorEnvelope | OrgIssueList]:
    """List Org Errors

    Args:
        org_id (UUID):
        since (datetime.datetime | None | Unset):
        limit (int | Unset):  Default: 50.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OrgIssueList]
    """

    kwargs = _get_kwargs(
        org_id=org_id,
        since=since,
        limit=limit,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    org_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    since: datetime.datetime | None | Unset = UNSET,
    limit: int | Unset = 50,
) -> ErrorEnvelope | OrgIssueList | None:
    """List Org Errors

    Args:
        org_id (UUID):
        since (datetime.datetime | None | Unset):
        limit (int | Unset):  Default: 50.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OrgIssueList
    """

    return sync_detailed(
        org_id=org_id,
        client=client,
        since=since,
        limit=limit,
    ).parsed


async def asyncio_detailed(
    org_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    since: datetime.datetime | None | Unset = UNSET,
    limit: int | Unset = 50,
) -> Response[ErrorEnvelope | OrgIssueList]:
    """List Org Errors

    Args:
        org_id (UUID):
        since (datetime.datetime | None | Unset):
        limit (int | Unset):  Default: 50.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OrgIssueList]
    """

    kwargs = _get_kwargs(
        org_id=org_id,
        since=since,
        limit=limit,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    org_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    since: datetime.datetime | None | Unset = UNSET,
    limit: int | Unset = 50,
) -> ErrorEnvelope | OrgIssueList | None:
    """List Org Errors

    Args:
        org_id (UUID):
        since (datetime.datetime | None | Unset):
        limit (int | Unset):  Default: 50.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OrgIssueList
    """

    return (
        await asyncio_detailed(
            org_id=org_id,
            client=client,
            since=since,
            limit=limit,
        )
    ).parsed
