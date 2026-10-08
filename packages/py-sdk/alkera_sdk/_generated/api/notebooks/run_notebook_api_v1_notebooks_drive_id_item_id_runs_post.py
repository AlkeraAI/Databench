from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.run_accepted import RunAccepted
from ...models.run_request import RunRequest
from ...types import Response


def _get_kwargs(
    drive_id: str,
    item_id: str,
    *,
    body: RunRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/notebooks/{drive_id}/{item_id}/runs".format(
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
) -> ErrorEnvelope | RunAccepted | None:
    if response.status_code == 200:
        response_200 = RunAccepted.from_dict(response.json())

        return response_200

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
) -> Response[ErrorEnvelope | RunAccepted]:
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
    body: RunRequest,
) -> Response[ErrorEnvelope | RunAccepted]:
    """Run Notebook

     Run cells: their text is taken at the requester's frontier (waiting up
    to 2 s for the document to include it), the run is recorded and handed to
    the kernel. Idempotent by ``client_run_id``. A notebook whose chat sleeps
    wakes it first and is answered ``notebook.waking``; nothing is recorded,
    and the same request is asked again.

    Args:
        drive_id (str):
        item_id (str):
        body (RunRequest): ``POST .../runs``. ``frontier`` is the document token the requester
            holds: the run waits (up to 2 s) for the document to include it, then
            takes its targets' text from there.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | RunAccepted]
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
    body: RunRequest,
) -> ErrorEnvelope | RunAccepted | None:
    """Run Notebook

     Run cells: their text is taken at the requester's frontier (waiting up
    to 2 s for the document to include it), the run is recorded and handed to
    the kernel. Idempotent by ``client_run_id``. A notebook whose chat sleeps
    wakes it first and is answered ``notebook.waking``; nothing is recorded,
    and the same request is asked again.

    Args:
        drive_id (str):
        item_id (str):
        body (RunRequest): ``POST .../runs``. ``frontier`` is the document token the requester
            holds: the run waits (up to 2 s) for the document to include it, then
            takes its targets' text from there.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | RunAccepted
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
    body: RunRequest,
) -> Response[ErrorEnvelope | RunAccepted]:
    """Run Notebook

     Run cells: their text is taken at the requester's frontier (waiting up
    to 2 s for the document to include it), the run is recorded and handed to
    the kernel. Idempotent by ``client_run_id``. A notebook whose chat sleeps
    wakes it first and is answered ``notebook.waking``; nothing is recorded,
    and the same request is asked again.

    Args:
        drive_id (str):
        item_id (str):
        body (RunRequest): ``POST .../runs``. ``frontier`` is the document token the requester
            holds: the run waits (up to 2 s) for the document to include it, then
            takes its targets' text from there.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | RunAccepted]
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
    body: RunRequest,
) -> ErrorEnvelope | RunAccepted | None:
    """Run Notebook

     Run cells: their text is taken at the requester's frontier (waiting up
    to 2 s for the document to include it), the run is recorded and handed to
    the kernel. Idempotent by ``client_run_id``. A notebook whose chat sleeps
    wakes it first and is answered ``notebook.waking``; nothing is recorded,
    and the same request is asked again.

    Args:
        drive_id (str):
        item_id (str):
        body (RunRequest): ``POST .../runs``. ``frontier`` is the document token the requester
            holds: the run waits (up to 2 s) for the document to include it, then
            takes its targets' text from there.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | RunAccepted
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            client=client,
            body=body,
        )
    ).parsed
