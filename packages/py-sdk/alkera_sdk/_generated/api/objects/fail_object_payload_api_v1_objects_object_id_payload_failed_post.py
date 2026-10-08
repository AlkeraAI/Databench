from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.object_payload_failure import ObjectPayloadFailure
from ...models.workspace_object_read import WorkspaceObjectRead
from ...types import Response


def _get_kwargs(
    object_id: UUID,
    *,
    body: ObjectPayloadFailure,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/objects/{object_id}/payload/failed".format(
            object_id=quote(str(object_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | WorkspaceObjectRead | None:
    if response.status_code == 200:
        response_200 = WorkspaceObjectRead.from_dict(response.json())

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
) -> Response[ErrorEnvelope | WorkspaceObjectRead]:
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
    body: ObjectPayloadFailure,
) -> Response[ErrorEnvelope | WorkspaceObjectRead]:
    """Fail Object Payload

     The machine says the payload behind a promoted result is not coming.

    Authorised exactly as the delivery is (``UPLOAD_PAYLOAD``): whoever may
    deliver the payload may say it will not arrive. Accepted only while the
    object is still waiting — a delivered result is not retro-failed, and a
    second refusal is a conflict rather than a rewrite of the first reason.

    Without this a promote the machine cannot honour left the object in
    ``pending_upload`` for ever: its page said "Saving…", the list said "0
    rows", and the reason lived only in the chat the reader had already left.

    Args:
        object_id (UUID):
        body (ObjectPayloadFailure): The daemon saying the payload behind a promoted result is not
            coming.

            Without it the object would wait in ``pending_upload`` forever, reading as
            "Saving…" on its page and "0 rows" in the list, so the reason is recorded
            on the object itself.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | WorkspaceObjectRead]
    """

    kwargs = _get_kwargs(
        object_id=object_id,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    object_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: ObjectPayloadFailure,
) -> ErrorEnvelope | WorkspaceObjectRead | None:
    """Fail Object Payload

     The machine says the payload behind a promoted result is not coming.

    Authorised exactly as the delivery is (``UPLOAD_PAYLOAD``): whoever may
    deliver the payload may say it will not arrive. Accepted only while the
    object is still waiting — a delivered result is not retro-failed, and a
    second refusal is a conflict rather than a rewrite of the first reason.

    Without this a promote the machine cannot honour left the object in
    ``pending_upload`` for ever: its page said "Saving…", the list said "0
    rows", and the reason lived only in the chat the reader had already left.

    Args:
        object_id (UUID):
        body (ObjectPayloadFailure): The daemon saying the payload behind a promoted result is not
            coming.

            Without it the object would wait in ``pending_upload`` forever, reading as
            "Saving…" on its page and "0 rows" in the list, so the reason is recorded
            on the object itself.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | WorkspaceObjectRead
    """

    return sync_detailed(
        object_id=object_id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    object_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: ObjectPayloadFailure,
) -> Response[ErrorEnvelope | WorkspaceObjectRead]:
    """Fail Object Payload

     The machine says the payload behind a promoted result is not coming.

    Authorised exactly as the delivery is (``UPLOAD_PAYLOAD``): whoever may
    deliver the payload may say it will not arrive. Accepted only while the
    object is still waiting — a delivered result is not retro-failed, and a
    second refusal is a conflict rather than a rewrite of the first reason.

    Without this a promote the machine cannot honour left the object in
    ``pending_upload`` for ever: its page said "Saving…", the list said "0
    rows", and the reason lived only in the chat the reader had already left.

    Args:
        object_id (UUID):
        body (ObjectPayloadFailure): The daemon saying the payload behind a promoted result is not
            coming.

            Without it the object would wait in ``pending_upload`` forever, reading as
            "Saving…" on its page and "0 rows" in the list, so the reason is recorded
            on the object itself.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | WorkspaceObjectRead]
    """

    kwargs = _get_kwargs(
        object_id=object_id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    object_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: ObjectPayloadFailure,
) -> ErrorEnvelope | WorkspaceObjectRead | None:
    """Fail Object Payload

     The machine says the payload behind a promoted result is not coming.

    Authorised exactly as the delivery is (``UPLOAD_PAYLOAD``): whoever may
    deliver the payload may say it will not arrive. Accepted only while the
    object is still waiting — a delivered result is not retro-failed, and a
    second refusal is a conflict rather than a rewrite of the first reason.

    Without this a promote the machine cannot honour left the object in
    ``pending_upload`` for ever: its page said "Saving…", the list said "0
    rows", and the reason lived only in the chat the reader had already left.

    Args:
        object_id (UUID):
        body (ObjectPayloadFailure): The daemon saying the payload behind a promoted result is not
            coming.

            Without it the object would wait in ``pending_upload`` forever, reading as
            "Saving…" on its page and "0 rows" in the list, so the reason is recorded
            on the object itself.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | WorkspaceObjectRead
    """

    return (
        await asyncio_detailed(
            object_id=object_id,
            client=client,
            body=body,
        )
    ).parsed
