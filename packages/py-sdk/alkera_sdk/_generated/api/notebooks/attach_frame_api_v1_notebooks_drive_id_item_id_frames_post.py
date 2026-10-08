from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.frame_attach_request import FrameAttachRequest
from ...models.frame_attached import FrameAttached
from ...types import Response


def _get_kwargs(
    drive_id: str,
    item_id: str,
    *,
    body: FrameAttachRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/notebooks/{drive_id}/{item_id}/frames".format(
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
) -> ErrorEnvelope | FrameAttached | None:
    if response.status_code == 200:
        response_200 = FrameAttached.from_dict(response.json())

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
) -> Response[ErrorEnvelope | FrameAttached]:
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
    body: FrameAttachRequest,
) -> Response[ErrorEnvelope | FrameAttached]:
    """Attach Frame

     Attach an output frame at the engine's widget hub: its id and the
    comm-open replays of its models. Later widget events for it arrive on the
    notebook channel addressed by ``frame_id``, to its owner only. A frame is
    where widget messages are sent from, so attaching one is decided by
    ``notebook.run``.

    Args:
        drive_id (str):
        item_id (str):
        body (FrameAttachRequest): ``POST .../frames``: attach an output frame at the engine's
            widget hub.
            ``peer_id`` (the socket's id from its ``welcome``) narrows the frame's
            events to that socket; without it they reach every socket of the person.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | FrameAttached]
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
    body: FrameAttachRequest,
) -> ErrorEnvelope | FrameAttached | None:
    """Attach Frame

     Attach an output frame at the engine's widget hub: its id and the
    comm-open replays of its models. Later widget events for it arrive on the
    notebook channel addressed by ``frame_id``, to its owner only. A frame is
    where widget messages are sent from, so attaching one is decided by
    ``notebook.run``.

    Args:
        drive_id (str):
        item_id (str):
        body (FrameAttachRequest): ``POST .../frames``: attach an output frame at the engine's
            widget hub.
            ``peer_id`` (the socket's id from its ``welcome``) narrows the frame's
            events to that socket; without it they reach every socket of the person.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | FrameAttached
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
    body: FrameAttachRequest,
) -> Response[ErrorEnvelope | FrameAttached]:
    """Attach Frame

     Attach an output frame at the engine's widget hub: its id and the
    comm-open replays of its models. Later widget events for it arrive on the
    notebook channel addressed by ``frame_id``, to its owner only. A frame is
    where widget messages are sent from, so attaching one is decided by
    ``notebook.run``.

    Args:
        drive_id (str):
        item_id (str):
        body (FrameAttachRequest): ``POST .../frames``: attach an output frame at the engine's
            widget hub.
            ``peer_id`` (the socket's id from its ``welcome``) narrows the frame's
            events to that socket; without it they reach every socket of the person.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | FrameAttached]
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
    body: FrameAttachRequest,
) -> ErrorEnvelope | FrameAttached | None:
    """Attach Frame

     Attach an output frame at the engine's widget hub: its id and the
    comm-open replays of its models. Later widget events for it arrive on the
    notebook channel addressed by ``frame_id``, to its owner only. A frame is
    where widget messages are sent from, so attaching one is decided by
    ``notebook.run``.

    Args:
        drive_id (str):
        item_id (str):
        body (FrameAttachRequest): ``POST .../frames``: attach an output frame at the engine's
            widget hub.
            ``peer_id`` (the socket's id from its ``welcome``) narrows the frame's
            events to that socket; without it they reach every socket of the person.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | FrameAttached
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            client=client,
            body=body,
        )
    ).parsed
