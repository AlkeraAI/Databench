from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.item import Item
from ...models.tree_create import TreeCreate
from ...types import Response


def _get_kwargs(
    drive_id: str,
    item_id: str,
    *,
    body: TreeCreate,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/files/drives/{drive_id}/items/{item_id}/tree".format(
            drive_id=quote(str(drive_id), safe=""),
            item_id=quote(str(item_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | list[Item] | None:
    if response.status_code == 201:
        response_201 = []
        _response_201 = response.json()
        for response_201_item_data in _response_201:
            response_201_item = Item.from_dict(response_201_item_data)

            response_201.append(response_201_item)

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
) -> Response[ErrorEnvelope | list[Item]]:
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
    body: TreeCreate,
) -> Response[ErrorEnvelope | list[Item]]:
    """Create Tree

     The folder skeleton of a dropped directory, in one call.

    Idempotent by construction rather than only by key: a path that already
    exists is walked into, not re-created, so a retry that lost its answer and
    a re-drop of the same directory both leave one tree.

    The library's ``tree.create_tree`` is the seam this walk belongs behind,
    but it reserves exactly as many inos as it creates folders, so the drive's
    ``next_ino`` would move by the number of folders a caller just made, and a
    counter that moves with another caller's activity is an oracle on it. The
    walk stays until the reservation takes whole blocks.

    Args:
        drive_id (str):
        item_id (str):
        body (TreeCreate): The `POST …/tree` body: the folder skeleton of a dropped directory.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | list[Item]]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        body=body,
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
    body: TreeCreate,
) -> ErrorEnvelope | list[Item] | None:
    """Create Tree

     The folder skeleton of a dropped directory, in one call.

    Idempotent by construction rather than only by key: a path that already
    exists is walked into, not re-created, so a retry that lost its answer and
    a re-drop of the same directory both leave one tree.

    The library's ``tree.create_tree`` is the seam this walk belongs behind,
    but it reserves exactly as many inos as it creates folders, so the drive's
    ``next_ino`` would move by the number of folders a caller just made, and a
    counter that moves with another caller's activity is an oracle on it. The
    walk stays until the reservation takes whole blocks.

    Args:
        drive_id (str):
        item_id (str):
        body (TreeCreate): The `POST …/tree` body: the folder skeleton of a dropped directory.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | list[Item]
    """

    return sync_detailed(
        drive_id=drive_id,
        item_id=item_id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
    body: TreeCreate,
) -> Response[ErrorEnvelope | list[Item]]:
    """Create Tree

     The folder skeleton of a dropped directory, in one call.

    Idempotent by construction rather than only by key: a path that already
    exists is walked into, not re-created, so a retry that lost its answer and
    a re-drop of the same directory both leave one tree.

    The library's ``tree.create_tree`` is the seam this walk belongs behind,
    but it reserves exactly as many inos as it creates folders, so the drive's
    ``next_ino`` would move by the number of folders a caller just made, and a
    counter that moves with another caller's activity is an oracle on it. The
    walk stays until the reservation takes whole blocks.

    Args:
        drive_id (str):
        item_id (str):
        body (TreeCreate): The `POST …/tree` body: the folder skeleton of a dropped directory.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | list[Item]]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
    body: TreeCreate,
) -> ErrorEnvelope | list[Item] | None:
    """Create Tree

     The folder skeleton of a dropped directory, in one call.

    Idempotent by construction rather than only by key: a path that already
    exists is walked into, not re-created, so a retry that lost its answer and
    a re-drop of the same directory both leave one tree.

    The library's ``tree.create_tree`` is the seam this walk belongs behind,
    but it reserves exactly as many inos as it creates folders, so the drive's
    ``next_ino`` would move by the number of folders a caller just made, and a
    counter that moves with another caller's activity is an oracle on it. The
    walk stays until the reservation takes whole blocks.

    Args:
        drive_id (str):
        item_id (str):
        body (TreeCreate): The `POST …/tree` body: the folder skeleton of a dropped directory.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | list[Item]
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            client=client,
            body=body,
        )
    ).parsed
