from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.chat_send_admission import ChatSendAdmission
from ...models.error_envelope import ErrorEnvelope
from ...types import UNSET, Response


def _get_kwargs(
    chat_id: UUID,
    *,
    user_id: UUID,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    json_user_id = str(user_id)
    params["user_id"] = json_user_id

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/chats/{chat_id}/send-admission".format(
            chat_id=quote(str(chat_id), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ChatSendAdmission | ErrorEnvelope | None:
    if response.status_code == 200:
        response_200 = ChatSendAdmission.from_dict(response.json())

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
) -> Response[ChatSendAdmission | ErrorEnvelope]:
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
    user_id: UUID,
) -> Response[ChatSendAdmission | ErrorEnvelope]:
    """Send Admission

     Whether ``user_id`` may still send here, asked by the box about to run
    their message (a share can be revoked between the send and the pickup).
    Only the chat's publisher may ask; the answer is the send rule's own, and
    a refusal is filed like a refused send.

    Args:
        chat_id (UUID):
        user_id (UUID): The message's author.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatSendAdmission | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        chat_id=chat_id,
        user_id=user_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    chat_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    user_id: UUID,
) -> ChatSendAdmission | ErrorEnvelope | None:
    """Send Admission

     Whether ``user_id`` may still send here, asked by the box about to run
    their message (a share can be revoked between the send and the pickup).
    Only the chat's publisher may ask; the answer is the send rule's own, and
    a refusal is filed like a refused send.

    Args:
        chat_id (UUID):
        user_id (UUID): The message's author.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatSendAdmission | ErrorEnvelope
    """

    return sync_detailed(
        chat_id=chat_id,
        client=client,
        user_id=user_id,
    ).parsed


async def asyncio_detailed(
    chat_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    user_id: UUID,
) -> Response[ChatSendAdmission | ErrorEnvelope]:
    """Send Admission

     Whether ``user_id`` may still send here, asked by the box about to run
    their message (a share can be revoked between the send and the pickup).
    Only the chat's publisher may ask; the answer is the send rule's own, and
    a refusal is filed like a refused send.

    Args:
        chat_id (UUID):
        user_id (UUID): The message's author.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatSendAdmission | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        chat_id=chat_id,
        user_id=user_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    chat_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    user_id: UUID,
) -> ChatSendAdmission | ErrorEnvelope | None:
    """Send Admission

     Whether ``user_id`` may still send here, asked by the box about to run
    their message (a share can be revoked between the send and the pickup).
    Only the chat's publisher may ask; the answer is the send rule's own, and
    a refusal is filed like a refused send.

    Args:
        chat_id (UUID):
        user_id (UUID): The message's author.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatSendAdmission | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            chat_id=chat_id,
            client=client,
            user_id=user_id,
        )
    ).parsed
