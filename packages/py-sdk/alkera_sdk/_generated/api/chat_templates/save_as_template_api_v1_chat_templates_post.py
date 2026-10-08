from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.chat_template_read import ChatTemplateRead
from ...models.error_envelope import ErrorEnvelope
from ...models.save_as_template import SaveAsTemplate
from ...types import Response


def _get_kwargs(
    *,
    body: SaveAsTemplate,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/chat-templates",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ChatTemplateRead | ErrorEnvelope | None:
    if response.status_code == 201:
        response_201 = ChatTemplateRead.from_dict(response.json())

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
) -> Response[ChatTemplateRead | ErrorEnvelope]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: SaveAsTemplate,
) -> Response[ChatTemplateRead | ErrorEnvelope]:
    """Save As Template

     Save a chat as a template: its brief and the files it was working on.

    Everything but the chat is optional. The title is the chat's, the brief is a
    digest of its transcript, and the destination is the caller's own
    ``Chat Templates`` folder — made on this visit if this is their first
    template. A reader who may read the chat and copy its files may save one
    into their own drive: the template is theirs, its folder is theirs, and
    nothing of the source's sharing comes with it.

    Args:
        body (SaveAsTemplate): Save a chat as the starting point for the next one.

            Everything but the chat is optional because the server can answer for all
            of it: the title from the chat, the brief from its transcript, and the
            destination from the caller's own ``Chat Templates`` folder. A reader who
            wants none of those defaults overrides the one they care about.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatTemplateRead | ErrorEnvelope]
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
    body: SaveAsTemplate,
) -> ChatTemplateRead | ErrorEnvelope | None:
    """Save As Template

     Save a chat as a template: its brief and the files it was working on.

    Everything but the chat is optional. The title is the chat's, the brief is a
    digest of its transcript, and the destination is the caller's own
    ``Chat Templates`` folder — made on this visit if this is their first
    template. A reader who may read the chat and copy its files may save one
    into their own drive: the template is theirs, its folder is theirs, and
    nothing of the source's sharing comes with it.

    Args:
        body (SaveAsTemplate): Save a chat as the starting point for the next one.

            Everything but the chat is optional because the server can answer for all
            of it: the title from the chat, the brief from its transcript, and the
            destination from the caller's own ``Chat Templates`` folder. A reader who
            wants none of those defaults overrides the one they care about.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatTemplateRead | ErrorEnvelope
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: SaveAsTemplate,
) -> Response[ChatTemplateRead | ErrorEnvelope]:
    """Save As Template

     Save a chat as a template: its brief and the files it was working on.

    Everything but the chat is optional. The title is the chat's, the brief is a
    digest of its transcript, and the destination is the caller's own
    ``Chat Templates`` folder — made on this visit if this is their first
    template. A reader who may read the chat and copy its files may save one
    into their own drive: the template is theirs, its folder is theirs, and
    nothing of the source's sharing comes with it.

    Args:
        body (SaveAsTemplate): Save a chat as the starting point for the next one.

            Everything but the chat is optional because the server can answer for all
            of it: the title from the chat, the brief from its transcript, and the
            destination from the caller's own ``Chat Templates`` folder. A reader who
            wants none of those defaults overrides the one they care about.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatTemplateRead | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: SaveAsTemplate,
) -> ChatTemplateRead | ErrorEnvelope | None:
    """Save As Template

     Save a chat as a template: its brief and the files it was working on.

    Everything but the chat is optional. The title is the chat's, the brief is a
    digest of its transcript, and the destination is the caller's own
    ``Chat Templates`` folder — made on this visit if this is their first
    template. A reader who may read the chat and copy its files may save one
    into their own drive: the template is theirs, its folder is theirs, and
    nothing of the source's sharing comes with it.

    Args:
        body (SaveAsTemplate): Save a chat as the starting point for the next one.

            Everything but the chat is optional because the server can answer for all
            of it: the title from the chat, the brief from its transcript, and the
            destination from the caller's own ``Chat Templates`` folder. A reader who
            wants none of those defaults overrides the one they care about.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatTemplateRead | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
