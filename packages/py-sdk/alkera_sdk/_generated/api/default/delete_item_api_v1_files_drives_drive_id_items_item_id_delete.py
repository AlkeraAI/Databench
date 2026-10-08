from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.operation_wire import OperationWire
from ...types import UNSET, Response, Unset


def _get_kwargs(
    drive_id: str,
    item_id: str,
    *,
    permanent: bool | Unset = False,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["permanent"] = permanent

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "delete",
        "url": "/api/v1/files/drives/{drive_id}/items/{item_id}".format(
            drive_id=quote(str(drive_id), safe=""),
            item_id=quote(str(item_id), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Any | ErrorEnvelope | OperationWire | None:
    if response.status_code == 200:
        response_200 = OperationWire.from_dict(response.json())

        return response_200

    if response.status_code == 204:
        response_204 = cast(Any, None)
        return response_204

    if response.status_code == 422:
        response_422 = ErrorEnvelope.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[Any | ErrorEnvelope | OperationWire]:
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
    permanent: bool | Unset = False,
) -> Response[Any | ErrorEnvelope | OperationWire]:
    """Delete Item

     Trash by default; ``?permanent=true`` purges.

    They are two different rungs, so they are two different decisions. The
    ladder puts trashing beside rename and move on the *writer* rung: it is
    undoable, the row stays, and a writer who may replace a file's bytes may
    obviously put it in the bin. ``DELETE`` is the owner rung and means the
    purge, bytes and row gone with nothing to restore. Deciding both as
    ``DELETE`` made a writer unable to trash their own upload; deciding both as
    ``WRITE`` would hand every writer the unrecoverable one.

    Trashing is undoable, so it answers with its ``Operation`` (one undo path);
    the purge has no inverse and answers 204. A trash first has the machines
    holding folders under the item push, so their work goes into the trash too.

    Args:
        drive_id (str):
        item_id (str):
        permanent (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ErrorEnvelope | OperationWire]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        permanent=permanent,
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
    permanent: bool | Unset = False,
) -> Any | ErrorEnvelope | OperationWire | None:
    """Delete Item

     Trash by default; ``?permanent=true`` purges.

    They are two different rungs, so they are two different decisions. The
    ladder puts trashing beside rename and move on the *writer* rung: it is
    undoable, the row stays, and a writer who may replace a file's bytes may
    obviously put it in the bin. ``DELETE`` is the owner rung and means the
    purge, bytes and row gone with nothing to restore. Deciding both as
    ``DELETE`` made a writer unable to trash their own upload; deciding both as
    ``WRITE`` would hand every writer the unrecoverable one.

    Trashing is undoable, so it answers with its ``Operation`` (one undo path);
    the purge has no inverse and answers 204. A trash first has the machines
    holding folders under the item push, so their work goes into the trash too.

    Args:
        drive_id (str):
        item_id (str):
        permanent (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ErrorEnvelope | OperationWire
    """

    return sync_detailed(
        drive_id=drive_id,
        item_id=item_id,
        client=client,
        permanent=permanent,
    ).parsed


async def asyncio_detailed(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
    permanent: bool | Unset = False,
) -> Response[Any | ErrorEnvelope | OperationWire]:
    """Delete Item

     Trash by default; ``?permanent=true`` purges.

    They are two different rungs, so they are two different decisions. The
    ladder puts trashing beside rename and move on the *writer* rung: it is
    undoable, the row stays, and a writer who may replace a file's bytes may
    obviously put it in the bin. ``DELETE`` is the owner rung and means the
    purge, bytes and row gone with nothing to restore. Deciding both as
    ``DELETE`` made a writer unable to trash their own upload; deciding both as
    ``WRITE`` would hand every writer the unrecoverable one.

    Trashing is undoable, so it answers with its ``Operation`` (one undo path);
    the purge has no inverse and answers 204. A trash first has the machines
    holding folders under the item push, so their work goes into the trash too.

    Args:
        drive_id (str):
        item_id (str):
        permanent (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ErrorEnvelope | OperationWire]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        permanent=permanent,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
    permanent: bool | Unset = False,
) -> Any | ErrorEnvelope | OperationWire | None:
    """Delete Item

     Trash by default; ``?permanent=true`` purges.

    They are two different rungs, so they are two different decisions. The
    ladder puts trashing beside rename and move on the *writer* rung: it is
    undoable, the row stays, and a writer who may replace a file's bytes may
    obviously put it in the bin. ``DELETE`` is the owner rung and means the
    purge, bytes and row gone with nothing to restore. Deciding both as
    ``DELETE`` made a writer unable to trash their own upload; deciding both as
    ``WRITE`` would hand every writer the unrecoverable one.

    Trashing is undoable, so it answers with its ``Operation`` (one undo path);
    the purge has no inverse and answers 204. A trash first has the machines
    holding folders under the item push, so their work goes into the trash too.

    Args:
        drive_id (str):
        item_id (str):
        permanent (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ErrorEnvelope | OperationWire
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            client=client,
            permanent=permanent,
        )
    ).parsed
