from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.item import Item
from ...models.put_content_api_v1_files_drives_drive_id_items_item_id_content_put_conflictbehavior import (
    PutContentApiV1FilesDrivesDriveIdItemsItemIdContentPutConflictbehavior,
)
from ...types import UNSET, Response, Unset


def _get_kwargs(
    drive_id: str,
    item_id: str,
    *,
    conflict_behavior: PutContentApiV1FilesDrivesDriveIdItemsItemIdContentPutConflictbehavior
    | Unset = PutContentApiV1FilesDrivesDriveIdItemsItemIdContentPutConflictbehavior.REPLACE,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    json_conflict_behavior: str | Unset = UNSET
    if not isinstance(conflict_behavior, Unset):
        json_conflict_behavior = conflict_behavior.value

    params["conflictBehavior"] = json_conflict_behavior

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "put",
        "url": "/api/v1/files/drives/{drive_id}/items/{item_id}/content".format(
            drive_id=quote(str(drive_id), safe=""),
            item_id=quote(str(item_id), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | Item | None:
    if response.status_code == 200:
        response_200 = Item.from_dict(response.json())

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
) -> Response[ErrorEnvelope | Item]:
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
    conflict_behavior: PutContentApiV1FilesDrivesDriveIdItemsItemIdContentPutConflictbehavior
    | Unset = PutContentApiV1FilesDrivesDriveIdItemsItemIdContentPutConflictbehavior.REPLACE,
) -> Response[ErrorEnvelope | Item]:
    """Put Content

     Stream a new version onto a file node.

    ``200`` when the node's head already held exactly these bytes — no version
    is written, and the store was asked to do the same work either way, so the
    answer is never an oracle for what the org already stores. ``201`` otherwise.

    Args:
        drive_id (str):
        item_id (str):
        conflict_behavior (PutContentApiV1FilesDrivesDriveIdItemsItemIdContentPutConflictbehavior
            | Unset):  Default:
            PutContentApiV1FilesDrivesDriveIdItemsItemIdContentPutConflictbehavior.REPLACE.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | Item]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        conflict_behavior=conflict_behavior,
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
    conflict_behavior: PutContentApiV1FilesDrivesDriveIdItemsItemIdContentPutConflictbehavior
    | Unset = PutContentApiV1FilesDrivesDriveIdItemsItemIdContentPutConflictbehavior.REPLACE,
) -> ErrorEnvelope | Item | None:
    """Put Content

     Stream a new version onto a file node.

    ``200`` when the node's head already held exactly these bytes — no version
    is written, and the store was asked to do the same work either way, so the
    answer is never an oracle for what the org already stores. ``201`` otherwise.

    Args:
        drive_id (str):
        item_id (str):
        conflict_behavior (PutContentApiV1FilesDrivesDriveIdItemsItemIdContentPutConflictbehavior
            | Unset):  Default:
            PutContentApiV1FilesDrivesDriveIdItemsItemIdContentPutConflictbehavior.REPLACE.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | Item
    """

    return sync_detailed(
        drive_id=drive_id,
        item_id=item_id,
        client=client,
        conflict_behavior=conflict_behavior,
    ).parsed


async def asyncio_detailed(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
    conflict_behavior: PutContentApiV1FilesDrivesDriveIdItemsItemIdContentPutConflictbehavior
    | Unset = PutContentApiV1FilesDrivesDriveIdItemsItemIdContentPutConflictbehavior.REPLACE,
) -> Response[ErrorEnvelope | Item]:
    """Put Content

     Stream a new version onto a file node.

    ``200`` when the node's head already held exactly these bytes — no version
    is written, and the store was asked to do the same work either way, so the
    answer is never an oracle for what the org already stores. ``201`` otherwise.

    Args:
        drive_id (str):
        item_id (str):
        conflict_behavior (PutContentApiV1FilesDrivesDriveIdItemsItemIdContentPutConflictbehavior
            | Unset):  Default:
            PutContentApiV1FilesDrivesDriveIdItemsItemIdContentPutConflictbehavior.REPLACE.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | Item]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        conflict_behavior=conflict_behavior,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
    conflict_behavior: PutContentApiV1FilesDrivesDriveIdItemsItemIdContentPutConflictbehavior
    | Unset = PutContentApiV1FilesDrivesDriveIdItemsItemIdContentPutConflictbehavior.REPLACE,
) -> ErrorEnvelope | Item | None:
    """Put Content

     Stream a new version onto a file node.

    ``200`` when the node's head already held exactly these bytes — no version
    is written, and the store was asked to do the same work either way, so the
    answer is never an oracle for what the org already stores. ``201`` otherwise.

    Args:
        drive_id (str):
        item_id (str):
        conflict_behavior (PutContentApiV1FilesDrivesDriveIdItemsItemIdContentPutConflictbehavior
            | Unset):  Default:
            PutContentApiV1FilesDrivesDriveIdItemsItemIdContentPutConflictbehavior.REPLACE.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | Item
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            client=client,
            conflict_behavior=conflict_behavior,
        )
    ).parsed
