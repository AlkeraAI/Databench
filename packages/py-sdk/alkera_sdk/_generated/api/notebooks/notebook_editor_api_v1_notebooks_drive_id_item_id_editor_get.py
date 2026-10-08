from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.notebook_editor import NotebookEditor
from ...types import Response


def _get_kwargs(
    drive_id: str,
    item_id: str,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/notebooks/{drive_id}/{item_id}/editor".format(
            drive_id=quote(str(drive_id), safe=""),
            item_id=quote(str(item_id), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | NotebookEditor | None:
    if response.status_code == 200:
        response_200 = NotebookEditor.from_dict(response.json())

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
) -> Response[ErrorEnvelope | NotebookEditor]:
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
) -> Response[ErrorEnvelope | NotebookEditor]:
    """Notebook Editor

     Where the notebook editor opens the notebook ``item_id``, or a new
    notebook in the folder ``item_id``: the chat whose workspace pane runs it
    (the chat the same wake a run asks for goes through), else nowhere. Files
    READ on the node; the chat is named only to a caller its policy lets send
    in it, since the editor is opened to edit and run.

    Args:
        drive_id (str):
        item_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | NotebookEditor]
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
) -> ErrorEnvelope | NotebookEditor | None:
    """Notebook Editor

     Where the notebook editor opens the notebook ``item_id``, or a new
    notebook in the folder ``item_id``: the chat whose workspace pane runs it
    (the chat the same wake a run asks for goes through), else nowhere. Files
    READ on the node; the chat is named only to a caller its policy lets send
    in it, since the editor is opened to edit and run.

    Args:
        drive_id (str):
        item_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | NotebookEditor
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
) -> Response[ErrorEnvelope | NotebookEditor]:
    """Notebook Editor

     Where the notebook editor opens the notebook ``item_id``, or a new
    notebook in the folder ``item_id``: the chat whose workspace pane runs it
    (the chat the same wake a run asks for goes through), else nowhere. Files
    READ on the node; the chat is named only to a caller its policy lets send
    in it, since the editor is opened to edit and run.

    Args:
        drive_id (str):
        item_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | NotebookEditor]
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
) -> ErrorEnvelope | NotebookEditor | None:
    """Notebook Editor

     Where the notebook editor opens the notebook ``item_id``, or a new
    notebook in the folder ``item_id``: the chat whose workspace pane runs it
    (the chat the same wake a run asks for goes through), else nowhere. Files
    READ on the node; the chat is named only to a caller its policy lets send
    in it, since the editor is opened to edit and run.

    Args:
        drive_id (str):
        item_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | NotebookEditor
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            client=client,
        )
    ).parsed
