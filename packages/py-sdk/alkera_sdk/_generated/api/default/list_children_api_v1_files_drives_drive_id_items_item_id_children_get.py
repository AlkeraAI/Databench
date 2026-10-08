from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.children_page import ChildrenPage
from ...models.error_envelope import ErrorEnvelope
from ...types import UNSET, Response, Unset


def _get_kwargs(
    drive_id: str,
    item_id: str,
    *,
    limit: int | Unset = 100,
    order_by: None | str | Unset = UNSET,
    marker: None | str | Unset = UNSET,
    kind: None | str | Unset = UNSET,
    object_type: None | str | Unset = UNSET,
    mime_class: None | str | Unset = UNSET,
    owner: None | str | Unset = UNSET,
    modified_after: None | str | Unset = UNSET,
    modified_before: None | str | Unset = UNSET,
    size_min: None | str | Unset = UNSET,
    size_max: None | str | Unset = UNSET,
    name_flag: None | str | Unset = UNSET,
    starred: None | str | Unset = UNSET,
    shared: None | str | Unset = UNSET,
    leased: None | str | Unset = UNSET,
    trashed: None | str | Unset = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["limit"] = limit

    json_order_by: None | str | Unset
    if isinstance(order_by, Unset):
        json_order_by = UNSET
    else:
        json_order_by = order_by
    params["orderBy"] = json_order_by

    json_marker: None | str | Unset
    if isinstance(marker, Unset):
        json_marker = UNSET
    else:
        json_marker = marker
    params["marker"] = json_marker

    json_kind: None | str | Unset
    if isinstance(kind, Unset):
        json_kind = UNSET
    else:
        json_kind = kind
    params["kind"] = json_kind

    json_object_type: None | str | Unset
    if isinstance(object_type, Unset):
        json_object_type = UNSET
    else:
        json_object_type = object_type
    params["objectType"] = json_object_type

    json_mime_class: None | str | Unset
    if isinstance(mime_class, Unset):
        json_mime_class = UNSET
    else:
        json_mime_class = mime_class
    params["mimeClass"] = json_mime_class

    json_owner: None | str | Unset
    if isinstance(owner, Unset):
        json_owner = UNSET
    else:
        json_owner = owner
    params["owner"] = json_owner

    json_modified_after: None | str | Unset
    if isinstance(modified_after, Unset):
        json_modified_after = UNSET
    else:
        json_modified_after = modified_after
    params["modifiedAfter"] = json_modified_after

    json_modified_before: None | str | Unset
    if isinstance(modified_before, Unset):
        json_modified_before = UNSET
    else:
        json_modified_before = modified_before
    params["modifiedBefore"] = json_modified_before

    json_size_min: None | str | Unset
    if isinstance(size_min, Unset):
        json_size_min = UNSET
    else:
        json_size_min = size_min
    params["sizeMin"] = json_size_min

    json_size_max: None | str | Unset
    if isinstance(size_max, Unset):
        json_size_max = UNSET
    else:
        json_size_max = size_max
    params["sizeMax"] = json_size_max

    json_name_flag: None | str | Unset
    if isinstance(name_flag, Unset):
        json_name_flag = UNSET
    else:
        json_name_flag = name_flag
    params["nameFlag"] = json_name_flag

    json_starred: None | str | Unset
    if isinstance(starred, Unset):
        json_starred = UNSET
    else:
        json_starred = starred
    params["starred"] = json_starred

    json_shared: None | str | Unset
    if isinstance(shared, Unset):
        json_shared = UNSET
    else:
        json_shared = shared
    params["shared"] = json_shared

    json_leased: None | str | Unset
    if isinstance(leased, Unset):
        json_leased = UNSET
    else:
        json_leased = leased
    params["leased"] = json_leased

    json_trashed: None | str | Unset
    if isinstance(trashed, Unset):
        json_trashed = UNSET
    else:
        json_trashed = trashed
    params["trashed"] = json_trashed

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/files/drives/{drive_id}/items/{item_id}/children".format(
            drive_id=quote(str(drive_id), safe=""),
            item_id=quote(str(item_id), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ChildrenPage | ErrorEnvelope | None:
    if response.status_code == 200:
        response_200 = ChildrenPage.from_dict(response.json())

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
) -> Response[ChildrenPage | ErrorEnvelope]:
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
    limit: int | Unset = 100,
    order_by: None | str | Unset = UNSET,
    marker: None | str | Unset = UNSET,
    kind: None | str | Unset = UNSET,
    object_type: None | str | Unset = UNSET,
    mime_class: None | str | Unset = UNSET,
    owner: None | str | Unset = UNSET,
    modified_after: None | str | Unset = UNSET,
    modified_before: None | str | Unset = UNSET,
    size_min: None | str | Unset = UNSET,
    size_max: None | str | Unset = UNSET,
    name_flag: None | str | Unset = UNSET,
    starred: None | str | Unset = UNSET,
    shared: None | str | Unset = UNSET,
    leased: None | str | Unset = UNSET,
    trashed: None | str | Unset = UNSET,
) -> Response[ChildrenPage | ErrorEnvelope]:
    """List Children

     One keyset page of a folder's children.

    The readability predicate is handed to the library so the page is cut
    *after* authorization: a hidden sibling shortens nothing and so cannot be
    inferred from a page size.

    Every chip is declared here rather than read only off the query string, so
    the generated clients can send a filter through their typed call instead of
    smuggling it past them. They are declared as strings because
    `ListFilters.from_query` stays the one parser: it is what turns `sizeMin=x`
    into a `400 files.bad_filter` and an unknown key into a `400
    files.unknown_filter`, and a second parser in the signature would answer a
    different status for the same request.

    Args:
        drive_id (str):
        item_id (str):
        limit (int | Unset):  Default: 100.
        order_by (None | str | Unset):
        marker (None | str | Unset):
        kind (None | str | Unset):
        object_type (None | str | Unset):
        mime_class (None | str | Unset):
        owner (None | str | Unset):
        modified_after (None | str | Unset):
        modified_before (None | str | Unset):
        size_min (None | str | Unset):
        size_max (None | str | Unset):
        name_flag (None | str | Unset):
        starred (None | str | Unset):
        shared (None | str | Unset):
        leased (None | str | Unset):
        trashed (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChildrenPage | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        limit=limit,
        order_by=order_by,
        marker=marker,
        kind=kind,
        object_type=object_type,
        mime_class=mime_class,
        owner=owner,
        modified_after=modified_after,
        modified_before=modified_before,
        size_min=size_min,
        size_max=size_max,
        name_flag=name_flag,
        starred=starred,
        shared=shared,
        leased=leased,
        trashed=trashed,
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
    limit: int | Unset = 100,
    order_by: None | str | Unset = UNSET,
    marker: None | str | Unset = UNSET,
    kind: None | str | Unset = UNSET,
    object_type: None | str | Unset = UNSET,
    mime_class: None | str | Unset = UNSET,
    owner: None | str | Unset = UNSET,
    modified_after: None | str | Unset = UNSET,
    modified_before: None | str | Unset = UNSET,
    size_min: None | str | Unset = UNSET,
    size_max: None | str | Unset = UNSET,
    name_flag: None | str | Unset = UNSET,
    starred: None | str | Unset = UNSET,
    shared: None | str | Unset = UNSET,
    leased: None | str | Unset = UNSET,
    trashed: None | str | Unset = UNSET,
) -> ChildrenPage | ErrorEnvelope | None:
    """List Children

     One keyset page of a folder's children.

    The readability predicate is handed to the library so the page is cut
    *after* authorization: a hidden sibling shortens nothing and so cannot be
    inferred from a page size.

    Every chip is declared here rather than read only off the query string, so
    the generated clients can send a filter through their typed call instead of
    smuggling it past them. They are declared as strings because
    `ListFilters.from_query` stays the one parser: it is what turns `sizeMin=x`
    into a `400 files.bad_filter` and an unknown key into a `400
    files.unknown_filter`, and a second parser in the signature would answer a
    different status for the same request.

    Args:
        drive_id (str):
        item_id (str):
        limit (int | Unset):  Default: 100.
        order_by (None | str | Unset):
        marker (None | str | Unset):
        kind (None | str | Unset):
        object_type (None | str | Unset):
        mime_class (None | str | Unset):
        owner (None | str | Unset):
        modified_after (None | str | Unset):
        modified_before (None | str | Unset):
        size_min (None | str | Unset):
        size_max (None | str | Unset):
        name_flag (None | str | Unset):
        starred (None | str | Unset):
        shared (None | str | Unset):
        leased (None | str | Unset):
        trashed (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChildrenPage | ErrorEnvelope
    """

    return sync_detailed(
        drive_id=drive_id,
        item_id=item_id,
        client=client,
        limit=limit,
        order_by=order_by,
        marker=marker,
        kind=kind,
        object_type=object_type,
        mime_class=mime_class,
        owner=owner,
        modified_after=modified_after,
        modified_before=modified_before,
        size_min=size_min,
        size_max=size_max,
        name_flag=name_flag,
        starred=starred,
        shared=shared,
        leased=leased,
        trashed=trashed,
    ).parsed


async def asyncio_detailed(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
    limit: int | Unset = 100,
    order_by: None | str | Unset = UNSET,
    marker: None | str | Unset = UNSET,
    kind: None | str | Unset = UNSET,
    object_type: None | str | Unset = UNSET,
    mime_class: None | str | Unset = UNSET,
    owner: None | str | Unset = UNSET,
    modified_after: None | str | Unset = UNSET,
    modified_before: None | str | Unset = UNSET,
    size_min: None | str | Unset = UNSET,
    size_max: None | str | Unset = UNSET,
    name_flag: None | str | Unset = UNSET,
    starred: None | str | Unset = UNSET,
    shared: None | str | Unset = UNSET,
    leased: None | str | Unset = UNSET,
    trashed: None | str | Unset = UNSET,
) -> Response[ChildrenPage | ErrorEnvelope]:
    """List Children

     One keyset page of a folder's children.

    The readability predicate is handed to the library so the page is cut
    *after* authorization: a hidden sibling shortens nothing and so cannot be
    inferred from a page size.

    Every chip is declared here rather than read only off the query string, so
    the generated clients can send a filter through their typed call instead of
    smuggling it past them. They are declared as strings because
    `ListFilters.from_query` stays the one parser: it is what turns `sizeMin=x`
    into a `400 files.bad_filter` and an unknown key into a `400
    files.unknown_filter`, and a second parser in the signature would answer a
    different status for the same request.

    Args:
        drive_id (str):
        item_id (str):
        limit (int | Unset):  Default: 100.
        order_by (None | str | Unset):
        marker (None | str | Unset):
        kind (None | str | Unset):
        object_type (None | str | Unset):
        mime_class (None | str | Unset):
        owner (None | str | Unset):
        modified_after (None | str | Unset):
        modified_before (None | str | Unset):
        size_min (None | str | Unset):
        size_max (None | str | Unset):
        name_flag (None | str | Unset):
        starred (None | str | Unset):
        shared (None | str | Unset):
        leased (None | str | Unset):
        trashed (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChildrenPage | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        limit=limit,
        order_by=order_by,
        marker=marker,
        kind=kind,
        object_type=object_type,
        mime_class=mime_class,
        owner=owner,
        modified_after=modified_after,
        modified_before=modified_before,
        size_min=size_min,
        size_max=size_max,
        name_flag=name_flag,
        starred=starred,
        shared=shared,
        leased=leased,
        trashed=trashed,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
    limit: int | Unset = 100,
    order_by: None | str | Unset = UNSET,
    marker: None | str | Unset = UNSET,
    kind: None | str | Unset = UNSET,
    object_type: None | str | Unset = UNSET,
    mime_class: None | str | Unset = UNSET,
    owner: None | str | Unset = UNSET,
    modified_after: None | str | Unset = UNSET,
    modified_before: None | str | Unset = UNSET,
    size_min: None | str | Unset = UNSET,
    size_max: None | str | Unset = UNSET,
    name_flag: None | str | Unset = UNSET,
    starred: None | str | Unset = UNSET,
    shared: None | str | Unset = UNSET,
    leased: None | str | Unset = UNSET,
    trashed: None | str | Unset = UNSET,
) -> ChildrenPage | ErrorEnvelope | None:
    """List Children

     One keyset page of a folder's children.

    The readability predicate is handed to the library so the page is cut
    *after* authorization: a hidden sibling shortens nothing and so cannot be
    inferred from a page size.

    Every chip is declared here rather than read only off the query string, so
    the generated clients can send a filter through their typed call instead of
    smuggling it past them. They are declared as strings because
    `ListFilters.from_query` stays the one parser: it is what turns `sizeMin=x`
    into a `400 files.bad_filter` and an unknown key into a `400
    files.unknown_filter`, and a second parser in the signature would answer a
    different status for the same request.

    Args:
        drive_id (str):
        item_id (str):
        limit (int | Unset):  Default: 100.
        order_by (None | str | Unset):
        marker (None | str | Unset):
        kind (None | str | Unset):
        object_type (None | str | Unset):
        mime_class (None | str | Unset):
        owner (None | str | Unset):
        modified_after (None | str | Unset):
        modified_before (None | str | Unset):
        size_min (None | str | Unset):
        size_max (None | str | Unset):
        name_flag (None | str | Unset):
        starred (None | str | Unset):
        shared (None | str | Unset):
        leased (None | str | Unset):
        trashed (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChildrenPage | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            client=client,
            limit=limit,
            order_by=order_by,
            marker=marker,
            kind=kind,
            object_type=object_type,
            mime_class=mime_class,
            owner=owner,
            modified_after=modified_after,
            modified_before=modified_before,
            size_min=size_min,
            size_max=size_max,
            name_flag=name_flag,
            starred=starred,
            shared=shared,
            leased=leased,
            trashed=trashed,
        )
    ).parsed
