from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.chat_promote_request import ChatPromoteRequest
from ...models.error_envelope import ErrorEnvelope
from ...models.workspace_object_read import WorkspaceObjectRead
from ...types import Response


def _get_kwargs(
    chat_id: UUID,
    *,
    body: ChatPromoteRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/chats/{chat_id}/promote".format(
            chat_id=quote(str(chat_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | WorkspaceObjectRead | None:
    if response.status_code == 201:
        response_201 = WorkspaceObjectRead.from_dict(response.json())

        return response_201

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
    chat_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: ChatPromoteRequest,
) -> Response[ErrorEnvelope | WorkspaceObjectRead]:
    """Promote Result

     Pin a result the chat produced.

    Promoting creates a durable object, so it is the chat owner's or an org
    admin's. Whoever presses it, the result belongs to the chat it came out
    of — its owner is the CHAT's owner — because the daemon binds every relay
    to the chat's owner and the two surfaces must name the same person; the
    promoter is recorded on the receipt's principal chain instead. The result
    is as private as the conversation it was lifted out of: its rows reach
    another member only through a share on the result's own node, never by
    the org-wide audience a team-less object would otherwise default to. The
    cloud creates the object NOW, in ``pending_upload``,
    and asks the machine for the payload behind it. Until the upload lands
    the object exists, is listed, and has no rows — which is honest, and is
    what lets the UI show "saving…" instead of inventing a result.

    Args:
        chat_id (UUID):
        body (ChatPromoteRequest): Pin a result the chat produced: the cloud creates the object
            now and the
            daemon uploads the payload behind it.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | WorkspaceObjectRead]
    """

    kwargs = _get_kwargs(
        chat_id=chat_id,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    chat_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: ChatPromoteRequest,
) -> ErrorEnvelope | WorkspaceObjectRead | None:
    """Promote Result

     Pin a result the chat produced.

    Promoting creates a durable object, so it is the chat owner's or an org
    admin's. Whoever presses it, the result belongs to the chat it came out
    of — its owner is the CHAT's owner — because the daemon binds every relay
    to the chat's owner and the two surfaces must name the same person; the
    promoter is recorded on the receipt's principal chain instead. The result
    is as private as the conversation it was lifted out of: its rows reach
    another member only through a share on the result's own node, never by
    the org-wide audience a team-less object would otherwise default to. The
    cloud creates the object NOW, in ``pending_upload``,
    and asks the machine for the payload behind it. Until the upload lands
    the object exists, is listed, and has no rows — which is honest, and is
    what lets the UI show "saving…" instead of inventing a result.

    Args:
        chat_id (UUID):
        body (ChatPromoteRequest): Pin a result the chat produced: the cloud creates the object
            now and the
            daemon uploads the payload behind it.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | WorkspaceObjectRead
    """

    return sync_detailed(
        chat_id=chat_id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    chat_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: ChatPromoteRequest,
) -> Response[ErrorEnvelope | WorkspaceObjectRead]:
    """Promote Result

     Pin a result the chat produced.

    Promoting creates a durable object, so it is the chat owner's or an org
    admin's. Whoever presses it, the result belongs to the chat it came out
    of — its owner is the CHAT's owner — because the daemon binds every relay
    to the chat's owner and the two surfaces must name the same person; the
    promoter is recorded on the receipt's principal chain instead. The result
    is as private as the conversation it was lifted out of: its rows reach
    another member only through a share on the result's own node, never by
    the org-wide audience a team-less object would otherwise default to. The
    cloud creates the object NOW, in ``pending_upload``,
    and asks the machine for the payload behind it. Until the upload lands
    the object exists, is listed, and has no rows — which is honest, and is
    what lets the UI show "saving…" instead of inventing a result.

    Args:
        chat_id (UUID):
        body (ChatPromoteRequest): Pin a result the chat produced: the cloud creates the object
            now and the
            daemon uploads the payload behind it.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | WorkspaceObjectRead]
    """

    kwargs = _get_kwargs(
        chat_id=chat_id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    chat_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: ChatPromoteRequest,
) -> ErrorEnvelope | WorkspaceObjectRead | None:
    """Promote Result

     Pin a result the chat produced.

    Promoting creates a durable object, so it is the chat owner's or an org
    admin's. Whoever presses it, the result belongs to the chat it came out
    of — its owner is the CHAT's owner — because the daemon binds every relay
    to the chat's owner and the two surfaces must name the same person; the
    promoter is recorded on the receipt's principal chain instead. The result
    is as private as the conversation it was lifted out of: its rows reach
    another member only through a share on the result's own node, never by
    the org-wide audience a team-less object would otherwise default to. The
    cloud creates the object NOW, in ``pending_upload``,
    and asks the machine for the payload behind it. Until the upload lands
    the object exists, is listed, and has no rows — which is honest, and is
    what lets the UI show "saving…" instead of inventing a result.

    Args:
        chat_id (UUID):
        body (ChatPromoteRequest): Pin a result the chat produced: the cloud creates the object
            now and the
            daemon uploads the payload behind it.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | WorkspaceObjectRead
    """

    return (
        await asyncio_detailed(
            chat_id=chat_id,
            client=client,
            body=body,
        )
    ).parsed
