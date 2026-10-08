from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.chat_template_read import ChatTemplateRead
from ...models.chat_template_update import ChatTemplateUpdate
from ...models.error_envelope import ErrorEnvelope
from ...types import Response


def _get_kwargs(
    template_id: UUID,
    *,
    body: ChatTemplateUpdate,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "put",
        "url": "/api/v1/chat-templates/{template_id}".format(
            template_id=quote(str(template_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ChatTemplateRead | ErrorEnvelope | None:
    if response.status_code == 200:
        response_200 = ChatTemplateRead.from_dict(response.json())

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
) -> Response[ChatTemplateRead | ErrorEnvelope]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    template_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: ChatTemplateUpdate,
) -> Response[ChatTemplateRead | ErrorEnvelope]:
    """Update Chat Template

     Edit the title or the brief, naming the version you read.

    The brief is the whole reason a template is not just a folder of files, and
    changing it speaks in the author's name to every chat started from here —
    so it takes the share ladder's edit rung, not merely the right to read. The
    folder's change token moves with the row, because a client watching the
    drive and one watching the template are looking at one edit.

    Args:
        template_id (UUID):
        body (ChatTemplateUpdate): ``expected_version`` is required and never optional: a write
            that does
            not say which row it read is a write that did not read one.

            Only the two fields a person writes are here. A template's files are
            ordinary files in its folder and are edited there; its spec is not editable
            through the object surface at all.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatTemplateRead | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        template_id=template_id,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    template_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: ChatTemplateUpdate,
) -> ChatTemplateRead | ErrorEnvelope | None:
    """Update Chat Template

     Edit the title or the brief, naming the version you read.

    The brief is the whole reason a template is not just a folder of files, and
    changing it speaks in the author's name to every chat started from here —
    so it takes the share ladder's edit rung, not merely the right to read. The
    folder's change token moves with the row, because a client watching the
    drive and one watching the template are looking at one edit.

    Args:
        template_id (UUID):
        body (ChatTemplateUpdate): ``expected_version`` is required and never optional: a write
            that does
            not say which row it read is a write that did not read one.

            Only the two fields a person writes are here. A template's files are
            ordinary files in its folder and are edited there; its spec is not editable
            through the object surface at all.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatTemplateRead | ErrorEnvelope
    """

    return sync_detailed(
        template_id=template_id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    template_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: ChatTemplateUpdate,
) -> Response[ChatTemplateRead | ErrorEnvelope]:
    """Update Chat Template

     Edit the title or the brief, naming the version you read.

    The brief is the whole reason a template is not just a folder of files, and
    changing it speaks in the author's name to every chat started from here —
    so it takes the share ladder's edit rung, not merely the right to read. The
    folder's change token moves with the row, because a client watching the
    drive and one watching the template are looking at one edit.

    Args:
        template_id (UUID):
        body (ChatTemplateUpdate): ``expected_version`` is required and never optional: a write
            that does
            not say which row it read is a write that did not read one.

            Only the two fields a person writes are here. A template's files are
            ordinary files in its folder and are edited there; its spec is not editable
            through the object surface at all.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatTemplateRead | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        template_id=template_id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    template_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: ChatTemplateUpdate,
) -> ChatTemplateRead | ErrorEnvelope | None:
    """Update Chat Template

     Edit the title or the brief, naming the version you read.

    The brief is the whole reason a template is not just a folder of files, and
    changing it speaks in the author's name to every chat started from here —
    so it takes the share ladder's edit rung, not merely the right to read. The
    folder's change token moves with the row, because a client watching the
    drive and one watching the template are looking at one edit.

    Args:
        template_id (UUID):
        body (ChatTemplateUpdate): ``expected_version`` is required and never optional: a write
            that does
            not say which row it read is a write that did not read one.

            Only the two fields a person writes are here. A template's files are
            ordinary files in its folder and are edited there; its spec is not editable
            through the object surface at all.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatTemplateRead | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            template_id=template_id,
            client=client,
            body=body,
        )
    ).parsed
