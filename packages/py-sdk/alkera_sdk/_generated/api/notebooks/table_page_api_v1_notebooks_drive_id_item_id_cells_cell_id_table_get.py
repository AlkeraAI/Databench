from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.table_page import TablePage
from ...types import UNSET, Response, Unset


def _get_kwargs(
    drive_id: str,
    item_id: str,
    cell_id: str,
    *,
    offset: int | Unset = 0,
    limit: int | Unset = 50,
    sort: None | str | Unset = UNSET,
    filter_sql: None | str | Unset = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["offset"] = offset

    params["limit"] = limit

    json_sort: None | str | Unset
    if isinstance(sort, Unset):
        json_sort = UNSET
    else:
        json_sort = sort
    params["sort"] = json_sort

    json_filter_sql: None | str | Unset
    if isinstance(filter_sql, Unset):
        json_filter_sql = UNSET
    else:
        json_filter_sql = filter_sql
    params["filter_sql"] = json_filter_sql

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/notebooks/{drive_id}/{item_id}/cells/{cell_id}/table".format(
            drive_id=quote(str(drive_id), safe=""),
            item_id=quote(str(item_id), safe=""),
            cell_id=quote(str(cell_id), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | TablePage | None:
    if response.status_code == 200:
        response_200 = TablePage.from_dict(response.json())

        return response_200

    if response.status_code == 404:
        response_404 = ErrorEnvelope.from_dict(response.json())

        return response_404

    if response.status_code == 409:
        response_409 = ErrorEnvelope.from_dict(response.json())

        return response_409

    if response.status_code == 422:
        response_422 = ErrorEnvelope.from_dict(response.json())

        return response_422

    if response.status_code == 503:
        response_503 = ErrorEnvelope.from_dict(response.json())

        return response_503

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ErrorEnvelope | TablePage]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    drive_id: str,
    item_id: str,
    cell_id: str,
    *,
    client: AuthenticatedClient | Client,
    offset: int | Unset = 0,
    limit: int | Unset = 50,
    sort: None | str | Unset = UNSET,
    filter_sql: None | str | Unset = UNSET,
) -> Response[ErrorEnvelope | TablePage]:
    """Table Page

     A page of a table output, read by the kernel's ``inspect.frame``
    (``filter_sql`` is one ``SELECT`` over a table named ``frame``; the
    kernel refuses anything else).

    Args:
        drive_id (str):
        item_id (str):
        cell_id (str):
        offset (int | Unset):  Default: 0.
        limit (int | Unset):  Default: 50.
        sort (None | str | Unset): col:asc,col2:desc (direction defaults to asc)
        filter_sql (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | TablePage]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        cell_id=cell_id,
        offset=offset,
        limit=limit,
        sort=sort,
        filter_sql=filter_sql,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    drive_id: str,
    item_id: str,
    cell_id: str,
    *,
    client: AuthenticatedClient | Client,
    offset: int | Unset = 0,
    limit: int | Unset = 50,
    sort: None | str | Unset = UNSET,
    filter_sql: None | str | Unset = UNSET,
) -> ErrorEnvelope | TablePage | None:
    """Table Page

     A page of a table output, read by the kernel's ``inspect.frame``
    (``filter_sql`` is one ``SELECT`` over a table named ``frame``; the
    kernel refuses anything else).

    Args:
        drive_id (str):
        item_id (str):
        cell_id (str):
        offset (int | Unset):  Default: 0.
        limit (int | Unset):  Default: 50.
        sort (None | str | Unset): col:asc,col2:desc (direction defaults to asc)
        filter_sql (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | TablePage
    """

    return sync_detailed(
        drive_id=drive_id,
        item_id=item_id,
        cell_id=cell_id,
        client=client,
        offset=offset,
        limit=limit,
        sort=sort,
        filter_sql=filter_sql,
    ).parsed


async def asyncio_detailed(
    drive_id: str,
    item_id: str,
    cell_id: str,
    *,
    client: AuthenticatedClient | Client,
    offset: int | Unset = 0,
    limit: int | Unset = 50,
    sort: None | str | Unset = UNSET,
    filter_sql: None | str | Unset = UNSET,
) -> Response[ErrorEnvelope | TablePage]:
    """Table Page

     A page of a table output, read by the kernel's ``inspect.frame``
    (``filter_sql`` is one ``SELECT`` over a table named ``frame``; the
    kernel refuses anything else).

    Args:
        drive_id (str):
        item_id (str):
        cell_id (str):
        offset (int | Unset):  Default: 0.
        limit (int | Unset):  Default: 50.
        sort (None | str | Unset): col:asc,col2:desc (direction defaults to asc)
        filter_sql (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | TablePage]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        cell_id=cell_id,
        offset=offset,
        limit=limit,
        sort=sort,
        filter_sql=filter_sql,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: str,
    item_id: str,
    cell_id: str,
    *,
    client: AuthenticatedClient | Client,
    offset: int | Unset = 0,
    limit: int | Unset = 50,
    sort: None | str | Unset = UNSET,
    filter_sql: None | str | Unset = UNSET,
) -> ErrorEnvelope | TablePage | None:
    """Table Page

     A page of a table output, read by the kernel's ``inspect.frame``
    (``filter_sql`` is one ``SELECT`` over a table named ``frame``; the
    kernel refuses anything else).

    Args:
        drive_id (str):
        item_id (str):
        cell_id (str):
        offset (int | Unset):  Default: 0.
        limit (int | Unset):  Default: 50.
        sort (None | str | Unset): col:asc,col2:desc (direction defaults to asc)
        filter_sql (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | TablePage
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            cell_id=cell_id,
            client=client,
            offset=offset,
            limit=limit,
            sort=sort,
            filter_sql=filter_sql,
        )
    ).parsed
