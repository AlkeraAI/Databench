import datetime
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.activity import Activity
from ...models.error_envelope import ErrorEnvelope
from ...types import UNSET, Response, Unset


def _get_kwargs(
    drive_id: str,
    item_id: str,
    *,
    since: datetime.datetime,
    exclude_actor: None | str | Unset = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    json_since = since.isoformat()
    params["since"] = json_since

    json_exclude_actor: None | str | Unset
    if isinstance(exclude_actor, Unset):
        json_exclude_actor = UNSET
    else:
        json_exclude_actor = exclude_actor
    params["exclude_actor"] = json_exclude_actor

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/notebooks/{drive_id}/{item_id}/activity".format(
            drive_id=quote(str(drive_id), safe=""),
            item_id=quote(str(item_id), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Activity | ErrorEnvelope | None:
    if response.status_code == 200:
        response_200 = Activity.from_dict(response.json())

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
) -> Response[Activity | ErrorEnvelope]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
    since: datetime.datetime,
    exclude_actor: None | str | Unset = UNSET,
) -> Response[Activity | ErrorEnvelope]:
    """Notebook Activity

     Edits and runs since ``since``, oldest first.

    Args:
        drive_id (str):
        item_id (str):
        since (datetime.datetime):
        exclude_actor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Activity | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        since=since,
        exclude_actor=exclude_actor,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
    since: datetime.datetime,
    exclude_actor: None | str | Unset = UNSET,
) -> Activity | ErrorEnvelope | None:
    """Notebook Activity

     Edits and runs since ``since``, oldest first.

    Args:
        drive_id (str):
        item_id (str):
        since (datetime.datetime):
        exclude_actor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Activity | ErrorEnvelope
    """

    return sync_detailed(
        drive_id=drive_id,
        item_id=item_id,
        client=client,
        since=since,
        exclude_actor=exclude_actor,
    ).parsed


async def asyncio_detailed(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
    since: datetime.datetime,
    exclude_actor: None | str | Unset = UNSET,
) -> Response[Activity | ErrorEnvelope]:
    """Notebook Activity

     Edits and runs since ``since``, oldest first.

    Args:
        drive_id (str):
        item_id (str):
        since (datetime.datetime):
        exclude_actor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Activity | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        since=since,
        exclude_actor=exclude_actor,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
    since: datetime.datetime,
    exclude_actor: None | str | Unset = UNSET,
) -> Activity | ErrorEnvelope | None:
    """Notebook Activity

     Edits and runs since ``since``, oldest first.

    Args:
        drive_id (str):
        item_id (str):
        since (datetime.datetime):
        exclude_actor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Activity | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            client=client,
            since=since,
            exclude_actor=exclude_actor,
        )
    ).parsed
