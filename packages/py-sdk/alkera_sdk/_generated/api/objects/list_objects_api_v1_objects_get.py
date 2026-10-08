from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.workspace_object_list import WorkspaceObjectList
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    type_: None | str | Unset = UNSET,
    limit: int | Unset = 1000,
    cursor: None | str | Unset = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    json_type_: None | str | Unset
    if isinstance(type_, Unset):
        json_type_ = UNSET
    else:
        json_type_ = type_
    params["type"] = json_type_

    params["limit"] = limit

    json_cursor: None | str | Unset
    if isinstance(cursor, Unset):
        json_cursor = UNSET
    else:
        json_cursor = cursor
    params["cursor"] = json_cursor

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/objects",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | WorkspaceObjectList | None:
    if response.status_code == 200:
        response_200 = WorkspaceObjectList.from_dict(response.json())

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
) -> Response[ErrorEnvelope | WorkspaceObjectList]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    type_: None | str | Unset = UNSET,
    limit: int | Unset = 1000,
    cursor: None | str | Unset = UNSET,
) -> Response[ErrorEnvelope | WorkspaceObjectList]:
    """List Objects

     The org's objects, newest first, cut to what the caller may read.

    Cut by the SAME answer the by-id door gives, per row and per type: a chat is
    decided by the chat's own read predicate (its owner, its machine, a rung on
    its node, and the deployment's admin setting), everything else by its
    audience. The list is the third door onto a chat and was for a while the
    only one still answering the old question — which showed a member's private
    title, owner and spec to an org admin nobody had shared it with, and hid the
    same chat from the colleague holding "Can view".

    Args:
        type_ (None | str | Unset):
        limit (int | Unset):  Default: 1000.
        cursor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | WorkspaceObjectList]
    """

    kwargs = _get_kwargs(
        type_=type_,
        limit=limit,
        cursor=cursor,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
    type_: None | str | Unset = UNSET,
    limit: int | Unset = 1000,
    cursor: None | str | Unset = UNSET,
) -> ErrorEnvelope | WorkspaceObjectList | None:
    """List Objects

     The org's objects, newest first, cut to what the caller may read.

    Cut by the SAME answer the by-id door gives, per row and per type: a chat is
    decided by the chat's own read predicate (its owner, its machine, a rung on
    its node, and the deployment's admin setting), everything else by its
    audience. The list is the third door onto a chat and was for a while the
    only one still answering the old question — which showed a member's private
    title, owner and spec to an org admin nobody had shared it with, and hid the
    same chat from the colleague holding "Can view".

    Args:
        type_ (None | str | Unset):
        limit (int | Unset):  Default: 1000.
        cursor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | WorkspaceObjectList
    """

    return sync_detailed(
        client=client,
        type_=type_,
        limit=limit,
        cursor=cursor,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    type_: None | str | Unset = UNSET,
    limit: int | Unset = 1000,
    cursor: None | str | Unset = UNSET,
) -> Response[ErrorEnvelope | WorkspaceObjectList]:
    """List Objects

     The org's objects, newest first, cut to what the caller may read.

    Cut by the SAME answer the by-id door gives, per row and per type: a chat is
    decided by the chat's own read predicate (its owner, its machine, a rung on
    its node, and the deployment's admin setting), everything else by its
    audience. The list is the third door onto a chat and was for a while the
    only one still answering the old question — which showed a member's private
    title, owner and spec to an org admin nobody had shared it with, and hid the
    same chat from the colleague holding "Can view".

    Args:
        type_ (None | str | Unset):
        limit (int | Unset):  Default: 1000.
        cursor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | WorkspaceObjectList]
    """

    kwargs = _get_kwargs(
        type_=type_,
        limit=limit,
        cursor=cursor,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    type_: None | str | Unset = UNSET,
    limit: int | Unset = 1000,
    cursor: None | str | Unset = UNSET,
) -> ErrorEnvelope | WorkspaceObjectList | None:
    """List Objects

     The org's objects, newest first, cut to what the caller may read.

    Cut by the SAME answer the by-id door gives, per row and per type: a chat is
    decided by the chat's own read predicate (its owner, its machine, a rung on
    its node, and the deployment's admin setting), everything else by its
    audience. The list is the third door onto a chat and was for a while the
    only one still answering the old question — which showed a member's private
    title, owner and spec to an org admin nobody had shared it with, and hid the
    same chat from the colleague holding "Can view".

    Args:
        type_ (None | str | Unset):
        limit (int | Unset):  Default: 1000.
        cursor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | WorkspaceObjectList
    """

    return (
        await asyncio_detailed(
            client=client,
            type_=type_,
            limit=limit,
            cursor=cursor,
        )
    ).parsed
