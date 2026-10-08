from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.object_payload_upload import ObjectPayloadUpload
from ...models.workspace_object_read import WorkspaceObjectRead
from ...types import Response


def _get_kwargs(
    object_id: UUID,
    *,
    body: ObjectPayloadUpload,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/objects/{object_id}/payload".format(
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
    body: ObjectPayloadUpload,
) -> Response[ErrorEnvelope | WorkspaceObjectRead]:
    """Upload Object Payload

     The machine delivers the payload behind a promoted result.

    Authorised as an AGENT acting for a user who could have written the object
    (``UPLOAD_PAYLOAD``), or as the box on its own machine credential when the
    result came out of a chat bound to its machine. Accepted only while the object is still waiting:
    a second upload against a ready result is a conflict, not a rewrite — the
    first payload is the one its receipt describes. Two deliveries that race
    past that check meet at the row's lock, and the loser is told the live
    version rather than overwriting the winner.

    Args:
        object_id (UUID):
        body (ObjectPayloadUpload): The daemon's promote upload: the envelope and the receipt that
            proves it.

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
    body: ObjectPayloadUpload,
) -> ErrorEnvelope | WorkspaceObjectRead | None:
    """Upload Object Payload

     The machine delivers the payload behind a promoted result.

    Authorised as an AGENT acting for a user who could have written the object
    (``UPLOAD_PAYLOAD``), or as the box on its own machine credential when the
    result came out of a chat bound to its machine. Accepted only while the object is still waiting:
    a second upload against a ready result is a conflict, not a rewrite — the
    first payload is the one its receipt describes. Two deliveries that race
    past that check meet at the row's lock, and the loser is told the live
    version rather than overwriting the winner.

    Args:
        object_id (UUID):
        body (ObjectPayloadUpload): The daemon's promote upload: the envelope and the receipt that
            proves it.

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
    body: ObjectPayloadUpload,
) -> Response[ErrorEnvelope | WorkspaceObjectRead]:
    """Upload Object Payload

     The machine delivers the payload behind a promoted result.

    Authorised as an AGENT acting for a user who could have written the object
    (``UPLOAD_PAYLOAD``), or as the box on its own machine credential when the
    result came out of a chat bound to its machine. Accepted only while the object is still waiting:
    a second upload against a ready result is a conflict, not a rewrite — the
    first payload is the one its receipt describes. Two deliveries that race
    past that check meet at the row's lock, and the loser is told the live
    version rather than overwriting the winner.

    Args:
        object_id (UUID):
        body (ObjectPayloadUpload): The daemon's promote upload: the envelope and the receipt that
            proves it.

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
    body: ObjectPayloadUpload,
) -> ErrorEnvelope | WorkspaceObjectRead | None:
    """Upload Object Payload

     The machine delivers the payload behind a promoted result.

    Authorised as an AGENT acting for a user who could have written the object
    (``UPLOAD_PAYLOAD``), or as the box on its own machine credential when the
    result came out of a chat bound to its machine. Accepted only while the object is still waiting:
    a second upload against a ready result is a conflict, not a rewrite — the
    first payload is the one its receipt describes. Two deliveries that race
    past that check meet at the row's lock, and the loser is told the live
    version rather than overwriting the winner.

    Args:
        object_id (UUID):
        body (ObjectPayloadUpload): The daemon's promote upload: the envelope and the receipt that
            proves it.

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
