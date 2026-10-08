from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.live_text import LiveText
from ...models.live_text_submit import LiveTextSubmit
from ...types import Response


def _get_kwargs(
    drive_id: str,
    item_id: str,
    *,
    body: LiveTextSubmit,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/files/drives/{drive_id}/items/{item_id}/live".format(
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
) -> ErrorEnvelope | LiveText | None:
    if response.status_code == 200:
        response_200 = LiveText.from_dict(response.json())

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
) -> Response[ErrorEnvelope | LiveText]:
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
    body: LiveTextSubmit,
) -> Response[ErrorEnvelope | LiveText]:
    """Submit Live Text

     Merge the agent's text into the file's live document. Idempotent by
    ``submitId``, so it carries no idempotency key.

    Args:
        drive_id (str):
        item_id (str):
        body (LiveTextSubmit): A whole text, and the state it was made on.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | LiveText]
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
    body: LiveTextSubmit,
) -> ErrorEnvelope | LiveText | None:
    """Submit Live Text

     Merge the agent's text into the file's live document. Idempotent by
    ``submitId``, so it carries no idempotency key.

    Args:
        drive_id (str):
        item_id (str):
        body (LiveTextSubmit): A whole text, and the state it was made on.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | LiveText
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
    body: LiveTextSubmit,
) -> Response[ErrorEnvelope | LiveText]:
    """Submit Live Text

     Merge the agent's text into the file's live document. Idempotent by
    ``submitId``, so it carries no idempotency key.

    Args:
        drive_id (str):
        item_id (str):
        body (LiveTextSubmit): A whole text, and the state it was made on.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | LiveText]
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
    body: LiveTextSubmit,
) -> ErrorEnvelope | LiveText | None:
    """Submit Live Text

     Merge the agent's text into the file's live document. Idempotent by
    ``submitId``, so it carries no idempotency key.

    Args:
        drive_id (str):
        item_id (str):
        body (LiveTextSubmit): A whole text, and the state it was made on.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | LiveText
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            client=client,
            body=body,
        )
    ).parsed
