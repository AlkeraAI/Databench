from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.item import Item
from ...models.operation_wire import OperationWire
from ...models.patch_item import PatchItem
from ...models.patch_item_api_v1_files_drives_drive_id_items_item_id_patch_conflict_behavior import (
    PatchItemApiV1FilesDrivesDriveIdItemsItemIdPatchConflictBehavior,
)
from ...types import UNSET, Response, Unset


def _get_kwargs(
    drive_id: str,
    item_id: str,
    *,
    body: PatchItem,
    conflict_behavior: PatchItemApiV1FilesDrivesDriveIdItemsItemIdPatchConflictBehavior
    | Unset = PatchItemApiV1FilesDrivesDriveIdItemsItemIdPatchConflictBehavior.FAIL,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    params: dict[str, Any] = {}

    json_conflict_behavior: str | Unset = UNSET
    if not isinstance(conflict_behavior, Unset):
        json_conflict_behavior = conflict_behavior.value

    params["conflict_behavior"] = json_conflict_behavior

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "patch",
        "url": "/api/v1/files/drives/{drive_id}/items/{item_id}".format(
            drive_id=quote(str(drive_id), safe=""),
            item_id=quote(str(item_id), safe=""),
        ),
        "params": params,
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | Item | OperationWire | None:
    if response.status_code == 200:
        response_200 = Item.from_dict(response.json())

        return response_200

    if response.status_code == 202:
        response_202 = OperationWire.from_dict(response.json())

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
) -> Response[ErrorEnvelope | Item | OperationWire]:
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
    body: PatchItem,
    conflict_behavior: PatchItemApiV1FilesDrivesDriveIdItemsItemIdPatchConflictBehavior
    | Unset = PatchItemApiV1FilesDrivesDriveIdItemsItemIdPatchConflictBehavior.FAIL,
) -> Response[ErrorEnvelope | Item | OperationWire]:
    """Patch Item

     Rename, move, and set attributes — in that order, in one transaction.

    The etag rides *inside* each statement rather than being checked first, so
    a caller holding a stale one changes nothing at all: the update matches no
    row and the library raises the 412. The order matters because a rename and
    a move in one call must not race each other for the destination name; doing
    the rename first means the move carries the name the caller asked for.

    Args:
        drive_id (str):
        item_id (str):
        conflict_behavior (PatchItemApiV1FilesDrivesDriveIdItemsItemIdPatchConflictBehavior |
            Unset):  Default: PatchItemApiV1FilesDrivesDriveIdItemsItemIdPatchConflictBehavior.FAIL.
        body (PatchItem): The `PATCH` body: a rename, a move, an attrs change, or any combination.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | Item | OperationWire]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        body=body,
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
    body: PatchItem,
    conflict_behavior: PatchItemApiV1FilesDrivesDriveIdItemsItemIdPatchConflictBehavior
    | Unset = PatchItemApiV1FilesDrivesDriveIdItemsItemIdPatchConflictBehavior.FAIL,
) -> ErrorEnvelope | Item | OperationWire | None:
    """Patch Item

     Rename, move, and set attributes — in that order, in one transaction.

    The etag rides *inside* each statement rather than being checked first, so
    a caller holding a stale one changes nothing at all: the update matches no
    row and the library raises the 412. The order matters because a rename and
    a move in one call must not race each other for the destination name; doing
    the rename first means the move carries the name the caller asked for.

    Args:
        drive_id (str):
        item_id (str):
        conflict_behavior (PatchItemApiV1FilesDrivesDriveIdItemsItemIdPatchConflictBehavior |
            Unset):  Default: PatchItemApiV1FilesDrivesDriveIdItemsItemIdPatchConflictBehavior.FAIL.
        body (PatchItem): The `PATCH` body: a rename, a move, an attrs change, or any combination.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | Item | OperationWire
    """

    return sync_detailed(
        drive_id=drive_id,
        item_id=item_id,
        client=client,
        body=body,
        conflict_behavior=conflict_behavior,
    ).parsed


async def asyncio_detailed(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
    body: PatchItem,
    conflict_behavior: PatchItemApiV1FilesDrivesDriveIdItemsItemIdPatchConflictBehavior
    | Unset = PatchItemApiV1FilesDrivesDriveIdItemsItemIdPatchConflictBehavior.FAIL,
) -> Response[ErrorEnvelope | Item | OperationWire]:
    """Patch Item

     Rename, move, and set attributes — in that order, in one transaction.

    The etag rides *inside* each statement rather than being checked first, so
    a caller holding a stale one changes nothing at all: the update matches no
    row and the library raises the 412. The order matters because a rename and
    a move in one call must not race each other for the destination name; doing
    the rename first means the move carries the name the caller asked for.

    Args:
        drive_id (str):
        item_id (str):
        conflict_behavior (PatchItemApiV1FilesDrivesDriveIdItemsItemIdPatchConflictBehavior |
            Unset):  Default: PatchItemApiV1FilesDrivesDriveIdItemsItemIdPatchConflictBehavior.FAIL.
        body (PatchItem): The `PATCH` body: a rename, a move, an attrs change, or any combination.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | Item | OperationWire]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        body=body,
        conflict_behavior=conflict_behavior,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
    body: PatchItem,
    conflict_behavior: PatchItemApiV1FilesDrivesDriveIdItemsItemIdPatchConflictBehavior
    | Unset = PatchItemApiV1FilesDrivesDriveIdItemsItemIdPatchConflictBehavior.FAIL,
) -> ErrorEnvelope | Item | OperationWire | None:
    """Patch Item

     Rename, move, and set attributes — in that order, in one transaction.

    The etag rides *inside* each statement rather than being checked first, so
    a caller holding a stale one changes nothing at all: the update matches no
    row and the library raises the 412. The order matters because a rename and
    a move in one call must not race each other for the destination name; doing
    the rename first means the move carries the name the caller asked for.

    Args:
        drive_id (str):
        item_id (str):
        conflict_behavior (PatchItemApiV1FilesDrivesDriveIdItemsItemIdPatchConflictBehavior |
            Unset):  Default: PatchItemApiV1FilesDrivesDriveIdItemsItemIdPatchConflictBehavior.FAIL.
        body (PatchItem): The `PATCH` body: a rename, a move, an attrs change, or any combination.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | Item | OperationWire
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            client=client,
            body=body,
            conflict_behavior=conflict_behavior,
        )
    ).parsed
