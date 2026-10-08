import datetime
from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    action: None | str | Unset = UNSET,
    actor_email: None | str | Unset = UNSET,
    created_after: datetime.datetime | None | Unset = UNSET,
    created_before: datetime.datetime | None | Unset = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

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
        "url": "/api/v1/org/audit-events/export.csv",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Any | ErrorEnvelope | None:
    if response.status_code == 200:
        response_200 = response.json()
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
) -> Response[Any | ErrorEnvelope]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    action: None | str | Unset = UNSET,
    actor_email: None | str | Unset = UNSET,
    created_after: datetime.datetime | None | Unset = UNSET,
    created_before: datetime.datetime | None | Unset = UNSET,
) -> Response[Any | ErrorEnvelope]:
    """Export Csv

     CSV of the org's events, narrowed by the same filters as the list.

    The whole trail, however long it is. An audit export is what an admin
    hands a regulator, so a cap on it is a silently incomplete answer to the
    one question where that is worst — and building the document in memory to
    apply the cap is what made the cap look necessary. The rows are read in
    batches on a seek and each one is written into the same small buffer and
    sent, so the export costs one batch of memory at any length.

    Args:
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
        Response[Any | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
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
    action: None | str | Unset = UNSET,
    actor_email: None | str | Unset = UNSET,
    created_after: datetime.datetime | None | Unset = UNSET,
    created_before: datetime.datetime | None | Unset = UNSET,
) -> Any | ErrorEnvelope | None:
    """Export Csv

     CSV of the org's events, narrowed by the same filters as the list.

    The whole trail, however long it is. An audit export is what an admin
    hands a regulator, so a cap on it is a silently incomplete answer to the
    one question where that is worst — and building the document in memory to
    apply the cap is what made the cap look necessary. The rows are read in
    batches on a seek and each one is written into the same small buffer and
    sent, so the export costs one batch of memory at any length.

    Args:
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
        Any | ErrorEnvelope
    """

    return sync_detailed(
        client=client,
        action=action,
        actor_email=actor_email,
        created_after=created_after,
        created_before=created_before,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    action: None | str | Unset = UNSET,
    actor_email: None | str | Unset = UNSET,
    created_after: datetime.datetime | None | Unset = UNSET,
    created_before: datetime.datetime | None | Unset = UNSET,
) -> Response[Any | ErrorEnvelope]:
    """Export Csv

     CSV of the org's events, narrowed by the same filters as the list.

    The whole trail, however long it is. An audit export is what an admin
    hands a regulator, so a cap on it is a silently incomplete answer to the
    one question where that is worst — and building the document in memory to
    apply the cap is what made the cap look necessary. The rows are read in
    batches on a seek and each one is written into the same small buffer and
    sent, so the export costs one batch of memory at any length.

    Args:
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
        Response[Any | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
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
    action: None | str | Unset = UNSET,
    actor_email: None | str | Unset = UNSET,
    created_after: datetime.datetime | None | Unset = UNSET,
    created_before: datetime.datetime | None | Unset = UNSET,
) -> Any | ErrorEnvelope | None:
    """Export Csv

     CSV of the org's events, narrowed by the same filters as the list.

    The whole trail, however long it is. An audit export is what an admin
    hands a regulator, so a cap on it is a silently incomplete answer to the
    one question where that is worst — and building the document in memory to
    apply the cap is what made the cap look necessary. The rows are read in
    batches on a seek and each one is written into the same small buffer and
    sent, so the export costs one batch of memory at any length.

    Args:
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
        Any | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            client=client,
            action=action,
            actor_email=actor_email,
            created_after=created_after,
            created_before=created_before,
        )
    ).parsed
