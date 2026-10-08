from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.item import Item
from ...types import Response


def _get_kwargs(
    drive_id: str,
    item_id: str,
    item_path: str,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/files/drives/{drive_id}/items/{item_id}:/{item_path}".format(
            drive_id=quote(str(drive_id), safe=""),
            item_id=quote(str(item_id), safe=""),
            item_path=quote(str(item_path), safe=""),
        ),
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
    item_path: str,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | Item]:
    """Get Item Under

     The path form, anchored: the item at ``item_path`` BELOW ``item_id``.

    A caller is told a folder's path only from the deepest ancestor it may read
    (:func:`backend.services.files.items.readable_path_bytes`), so a holder
    that reads nothing above the folder it holds — a box on a chat's lease —
    knows the folder's bare name and cannot spell an absolute path to anything
    under it. What is under a node it holds is addressed from that node, by
    the step down: the same lookup the root form makes, started at the anchor,
    and its answer authorized exactly as the id form's is. An anchor the
    caller may not read, a step that names nothing and a step that climbs
    (``..`` is no node's name) all leave the same 404; an empty step is the
    anchor itself.

    Args:
        drive_id (str):
        item_id (str):
        item_path (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | Item]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        item_path=item_path,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    drive_id: str,
    item_id: str,
    item_path: str,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | Item | None:
    """Get Item Under

     The path form, anchored: the item at ``item_path`` BELOW ``item_id``.

    A caller is told a folder's path only from the deepest ancestor it may read
    (:func:`backend.services.files.items.readable_path_bytes`), so a holder
    that reads nothing above the folder it holds — a box on a chat's lease —
    knows the folder's bare name and cannot spell an absolute path to anything
    under it. What is under a node it holds is addressed from that node, by
    the step down: the same lookup the root form makes, started at the anchor,
    and its answer authorized exactly as the id form's is. An anchor the
    caller may not read, a step that names nothing and a step that climbs
    (``..`` is no node's name) all leave the same 404; an empty step is the
    anchor itself.

    Args:
        drive_id (str):
        item_id (str):
        item_path (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | Item
    """

    return sync_detailed(
        drive_id=drive_id,
        item_id=item_id,
        item_path=item_path,
        client=client,
    ).parsed


async def asyncio_detailed(
    drive_id: str,
    item_id: str,
    item_path: str,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | Item]:
    """Get Item Under

     The path form, anchored: the item at ``item_path`` BELOW ``item_id``.

    A caller is told a folder's path only from the deepest ancestor it may read
    (:func:`backend.services.files.items.readable_path_bytes`), so a holder
    that reads nothing above the folder it holds — a box on a chat's lease —
    knows the folder's bare name and cannot spell an absolute path to anything
    under it. What is under a node it holds is addressed from that node, by
    the step down: the same lookup the root form makes, started at the anchor,
    and its answer authorized exactly as the id form's is. An anchor the
    caller may not read, a step that names nothing and a step that climbs
    (``..`` is no node's name) all leave the same 404; an empty step is the
    anchor itself.

    Args:
        drive_id (str):
        item_id (str):
        item_path (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | Item]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        item_path=item_path,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: str,
    item_id: str,
    item_path: str,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | Item | None:
    """Get Item Under

     The path form, anchored: the item at ``item_path`` BELOW ``item_id``.

    A caller is told a folder's path only from the deepest ancestor it may read
    (:func:`backend.services.files.items.readable_path_bytes`), so a holder
    that reads nothing above the folder it holds — a box on a chat's lease —
    knows the folder's bare name and cannot spell an absolute path to anything
    under it. What is under a node it holds is addressed from that node, by
    the step down: the same lookup the root form makes, started at the anchor,
    and its answer authorized exactly as the id form's is. An anchor the
    caller may not read, a step that names nothing and a step that climbs
    (``..`` is no node's name) all leave the same 404; an empty step is the
    anchor itself.

    Args:
        drive_id (str):
        item_id (str):
        item_path (str):

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
            item_path=item_path,
            client=client,
        )
    ).parsed
