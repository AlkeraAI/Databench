from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...types import Response


def _get_kwargs(
    drive_id: str,
    item_id: str,
    frame_id: str,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "delete",
        "url": "/api/v1/notebooks/{drive_id}/{item_id}/frames/{frame_id}".format(
            drive_id=quote(str(drive_id), safe=""),
            item_id=quote(str(item_id), safe=""),
            frame_id=quote(str(frame_id), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Any | ErrorEnvelope | None:
    if response.status_code == 204:
        response_204 = cast(Any, None)
        return response_204

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
) -> Response[Any | ErrorEnvelope]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    drive_id: str,
    item_id: str,
    frame_id: str,
    *,
    client: AuthenticatedClient | Client,
) -> Response[Any | ErrorEnvelope]:
    """Detach Frame

     Detach one of the caller's output frames. Not decided by
    ``notebook.run``: it only removes a frame the signed id names as the
    caller's and reaches no code in the kernel, and a person who lost Can
    edit must still be able to drop the frames they attached before.

    Args:
        drive_id (str):
        item_id (str):
        frame_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        frame_id=frame_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    drive_id: str,
    item_id: str,
    frame_id: str,
    *,
    client: AuthenticatedClient | Client,
) -> Any | ErrorEnvelope | None:
    """Detach Frame

     Detach one of the caller's output frames. Not decided by
    ``notebook.run``: it only removes a frame the signed id names as the
    caller's and reaches no code in the kernel, and a person who lost Can
    edit must still be able to drop the frames they attached before.

    Args:
        drive_id (str):
        item_id (str):
        frame_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ErrorEnvelope
    """

    return sync_detailed(
        drive_id=drive_id,
        item_id=item_id,
        frame_id=frame_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    drive_id: str,
    item_id: str,
    frame_id: str,
    *,
    client: AuthenticatedClient | Client,
) -> Response[Any | ErrorEnvelope]:
    """Detach Frame

     Detach one of the caller's output frames. Not decided by
    ``notebook.run``: it only removes a frame the signed id names as the
    caller's and reaches no code in the kernel, and a person who lost Can
    edit must still be able to drop the frames they attached before.

    Args:
        drive_id (str):
        item_id (str):
        frame_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        frame_id=frame_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: str,
    item_id: str,
    frame_id: str,
    *,
    client: AuthenticatedClient | Client,
) -> Any | ErrorEnvelope | None:
    """Detach Frame

     Detach one of the caller's output frames. Not decided by
    ``notebook.run``: it only removes a frame the signed id names as the
    caller's and reaches no code in the kernel, and a person who lost Can
    edit must still be able to drop the frames they attached before.

    Args:
        drive_id (str):
        item_id (str):
        frame_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            frame_id=frame_id,
            client=client,
        )
    ).parsed
