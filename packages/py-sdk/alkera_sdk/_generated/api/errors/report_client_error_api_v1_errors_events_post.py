from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.client_error_event import ClientErrorEvent
from ...models.error_envelope import ErrorEnvelope
from ...models.error_event_ack import ErrorEventAck
from ...types import Response


def _get_kwargs(
    *,
    body: ClientErrorEvent,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/errors/events",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | ErrorEventAck | None:
    if response.status_code == 200:
        response_200 = ErrorEventAck.from_dict(response.json())

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
) -> Response[ErrorEnvelope | ErrorEventAck]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ClientErrorEvent,
) -> Response[ErrorEnvelope | ErrorEventAck]:
    """Report Client Error

     Passively-captured client error — logged (+ Sentry) so we're aware of it.

    No DB row; this is the high-volume, low-ceremony path for the web app and
    extension's global error handlers.

    Args:
        body (ClientErrorEvent): A passively-captured client error (web / extension), logged not
            stored.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | ErrorEventAck]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
    body: ClientErrorEvent,
) -> ErrorEnvelope | ErrorEventAck | None:
    """Report Client Error

     Passively-captured client error — logged (+ Sentry) so we're aware of it.

    No DB row; this is the high-volume, low-ceremony path for the web app and
    extension's global error handlers.

    Args:
        body (ClientErrorEvent): A passively-captured client error (web / extension), logged not
            stored.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | ErrorEventAck
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ClientErrorEvent,
) -> Response[ErrorEnvelope | ErrorEventAck]:
    """Report Client Error

     Passively-captured client error — logged (+ Sentry) so we're aware of it.

    No DB row; this is the high-volume, low-ceremony path for the web app and
    extension's global error handlers.

    Args:
        body (ClientErrorEvent): A passively-captured client error (web / extension), logged not
            stored.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | ErrorEventAck]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ClientErrorEvent,
) -> ErrorEnvelope | ErrorEventAck | None:
    """Report Client Error

     Passively-captured client error — logged (+ Sentry) so we're aware of it.

    No DB row; this is the high-volume, low-ceremony path for the web app and
    extension's global error handlers.

    Args:
        body (ClientErrorEvent): A passively-captured client error (web / extension), logged not
            stored.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | ErrorEventAck
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
