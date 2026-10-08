from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.notebook_connections import NotebookConnections
from ...types import Response


def _get_kwargs(
    drive_id: str,
    item_id: str,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/notebooks/{drive_id}/{item_id}/connections".format(
            drive_id=quote(str(drive_id), safe=""),
            item_id=quote(str(item_id), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | NotebookConnections | None:
    if response.status_code == 200:
        response_200 = NotebookConnections.from_dict(response.json())

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
) -> Response[ErrorEnvelope | NotebookConnections]:
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
) -> Response[ErrorEnvelope | NotebookConnections]:
    """Notebook Connections

     The connections the notebook's SQL cells may name: the connections of
    the workspace that holds it, the set its kernel resolves a name among on
    the box (the workspace owner's, shared with everyone the workspace is
    shared with). Files READ on the notebook; each entry says whether this
    reader can use it, and why not. Empty for a notebook in no workspace.

    Args:
        drive_id (str):
        item_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | NotebookConnections]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
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
) -> ErrorEnvelope | NotebookConnections | None:
    """Notebook Connections

     The connections the notebook's SQL cells may name: the connections of
    the workspace that holds it, the set its kernel resolves a name among on
    the box (the workspace owner's, shared with everyone the workspace is
    shared with). Files READ on the notebook; each entry says whether this
    reader can use it, and why not. Empty for a notebook in no workspace.

    Args:
        drive_id (str):
        item_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | NotebookConnections
    """

    return sync_detailed(
        drive_id=drive_id,
        item_id=item_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | NotebookConnections]:
    """Notebook Connections

     The connections the notebook's SQL cells may name: the connections of
    the workspace that holds it, the set its kernel resolves a name among on
    the box (the workspace owner's, shared with everyone the workspace is
    shared with). Files READ on the notebook; each entry says whether this
    reader can use it, and why not. Empty for a notebook in no workspace.

    Args:
        drive_id (str):
        item_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | NotebookConnections]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | NotebookConnections | None:
    """Notebook Connections

     The connections the notebook's SQL cells may name: the connections of
    the workspace that holds it, the set its kernel resolves a name among on
    the box (the workspace owner's, shared with everyone the workspace is
    shared with). Files READ on the notebook; each entry says whether this
    reader can use it, and why not. Empty for a notebook in no workspace.

    Args:
        drive_id (str):
        item_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | NotebookConnections
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            client=client,
        )
    ).parsed
