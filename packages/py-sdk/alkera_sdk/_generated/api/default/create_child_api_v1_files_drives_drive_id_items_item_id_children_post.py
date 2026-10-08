from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.create_child import CreateChild
from ...models.error_envelope import ErrorEnvelope
from ...models.item import Item
from ...types import Response


def _get_kwargs(
    drive_id: str,
    item_id: str,
    *,
    body: CreateChild,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/files/drives/{drive_id}/items/{item_id}/children".format(
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
) -> ErrorEnvelope | Item | None:
    if response.status_code == 201:
        response_201 = Item.from_dict(response.json())

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
    body: CreateChild,
) -> Response[ErrorEnvelope | Item]:
    """Create Child

     Add one folder, symlink or special node under a folder.

    ``WRITE`` is decided on the *parent*, which is the node the caller is
    changing; the child does not exist yet and so has no access of its own.

    A body asking for a file is refused here rather than served as a folder:
    this route makes namespace entries, and a file only exists once its first
    version does.

    Args:
        drive_id (str):
        item_id (str):
        body (CreateChild): The `POST …/children` body: one folder, symlink or special node.

            Files are absent on purpose — bytes arrive through a content PUT or an
            upload session, so a `kind: "file"` here would be a node with no version
            that a listing would render as an empty file nobody wrote.

            A body that asks for one anyway is REFUSED rather than quietly rounded to
            the default. ``kind`` used to leave a caller's ``"file"`` out of the
            literal, and a Drive-shaped body naming a ``file`` facet was simply an
            unknown key: either way the caller asked for a file, got a 201 describing a
            folder of that name, and discovered it only when the content PUT that
            followed answered 404 as though the file had vanished.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | Item]
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
    body: CreateChild,
) -> ErrorEnvelope | Item | None:
    """Create Child

     Add one folder, symlink or special node under a folder.

    ``WRITE`` is decided on the *parent*, which is the node the caller is
    changing; the child does not exist yet and so has no access of its own.

    A body asking for a file is refused here rather than served as a folder:
    this route makes namespace entries, and a file only exists once its first
    version does.

    Args:
        drive_id (str):
        item_id (str):
        body (CreateChild): The `POST …/children` body: one folder, symlink or special node.

            Files are absent on purpose — bytes arrive through a content PUT or an
            upload session, so a `kind: "file"` here would be a node with no version
            that a listing would render as an empty file nobody wrote.

            A body that asks for one anyway is REFUSED rather than quietly rounded to
            the default. ``kind`` used to leave a caller's ``"file"`` out of the
            literal, and a Drive-shaped body naming a ``file`` facet was simply an
            unknown key: either way the caller asked for a file, got a 201 describing a
            folder of that name, and discovered it only when the content PUT that
            followed answered 404 as though the file had vanished.

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
        body=body,
    ).parsed


async def asyncio_detailed(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
    body: CreateChild,
) -> Response[ErrorEnvelope | Item]:
    """Create Child

     Add one folder, symlink or special node under a folder.

    ``WRITE`` is decided on the *parent*, which is the node the caller is
    changing; the child does not exist yet and so has no access of its own.

    A body asking for a file is refused here rather than served as a folder:
    this route makes namespace entries, and a file only exists once its first
    version does.

    Args:
        drive_id (str):
        item_id (str):
        body (CreateChild): The `POST …/children` body: one folder, symlink or special node.

            Files are absent on purpose — bytes arrive through a content PUT or an
            upload session, so a `kind: "file"` here would be a node with no version
            that a listing would render as an empty file nobody wrote.

            A body that asks for one anyway is REFUSED rather than quietly rounded to
            the default. ``kind`` used to leave a caller's ``"file"`` out of the
            literal, and a Drive-shaped body naming a ``file`` facet was simply an
            unknown key: either way the caller asked for a file, got a 201 describing a
            folder of that name, and discovered it only when the content PUT that
            followed answered 404 as though the file had vanished.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | Item]
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
    body: CreateChild,
) -> ErrorEnvelope | Item | None:
    """Create Child

     Add one folder, symlink or special node under a folder.

    ``WRITE`` is decided on the *parent*, which is the node the caller is
    changing; the child does not exist yet and so has no access of its own.

    A body asking for a file is refused here rather than served as a folder:
    this route makes namespace entries, and a file only exists once its first
    version does.

    Args:
        drive_id (str):
        item_id (str):
        body (CreateChild): The `POST …/children` body: one folder, symlink or special node.

            Files are absent on purpose — bytes arrive through a content PUT or an
            upload session, so a `kind: "file"` here would be a node with no version
            that a listing would render as an empty file nobody wrote.

            A body that asks for one anyway is REFUSED rather than quietly rounded to
            the default. ``kind`` used to leave a caller's ``"file"`` out of the
            literal, and a Drive-shaped body naming a ``file`` facet was simply an
            unknown key: either way the caller asked for a file, got a 201 describing a
            folder of that name, and discovered it only when the content PUT that
            followed answered 404 as though the file had vanished.

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
            body=body,
        )
    ).parsed
