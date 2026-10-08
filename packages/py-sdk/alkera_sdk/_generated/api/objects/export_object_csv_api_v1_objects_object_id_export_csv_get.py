from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...types import Response


def _get_kwargs(
    object_id: UUID,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/objects/{object_id}/export.csv".format(
            object_id=quote(str(object_id), safe=""),
        ),
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
    object_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Response[Any | ErrorEnvelope]:
    """Export Object Csv

     The whole result as CSV.

    Exporting is the owner's or an org admin's (``EXPORT``): a reader keeps the
    rows on screen, the export leaves the product. Every cell is escaped on the
    way out: their rows are stored LLM output, and a cell that begins like a
    formula must arrive in a spreadsheet as text.

    The document is produced as it is sent, a spill page at a time, so the
    export has no row at which it has to start refusing and costs one page of
    memory however long the result is.

    Args:
        object_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        object_id=object_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    object_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Any | ErrorEnvelope | None:
    """Export Object Csv

     The whole result as CSV.

    Exporting is the owner's or an org admin's (``EXPORT``): a reader keeps the
    rows on screen, the export leaves the product. Every cell is escaped on the
    way out: their rows are stored LLM output, and a cell that begins like a
    formula must arrive in a spreadsheet as text.

    The document is produced as it is sent, a spill page at a time, so the
    export has no row at which it has to start refusing and costs one page of
    memory however long the result is.

    Args:
        object_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ErrorEnvelope
    """

    return sync_detailed(
        object_id=object_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    object_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Response[Any | ErrorEnvelope]:
    """Export Object Csv

     The whole result as CSV.

    Exporting is the owner's or an org admin's (``EXPORT``): a reader keeps the
    rows on screen, the export leaves the product. Every cell is escaped on the
    way out: their rows are stored LLM output, and a cell that begins like a
    formula must arrive in a spreadsheet as text.

    The document is produced as it is sent, a spill page at a time, so the
    export has no row at which it has to start refusing and costs one page of
    memory however long the result is.

    Args:
        object_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        object_id=object_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    object_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Any | ErrorEnvelope | None:
    """Export Object Csv

     The whole result as CSV.

    Exporting is the owner's or an org admin's (``EXPORT``): a reader keeps the
    rows on screen, the export leaves the product. Every cell is escaped on the
    way out: their rows are stored LLM output, and a cell that begins like a
    formula must arrive in a spreadsheet as text.

    The document is produced as it is sent, a spill page at a time, so the
    export has no row at which it has to start refusing and costs one page of
    memory however long the result is.

    Args:
        object_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            object_id=object_id,
            client=client,
        )
    ).parsed
