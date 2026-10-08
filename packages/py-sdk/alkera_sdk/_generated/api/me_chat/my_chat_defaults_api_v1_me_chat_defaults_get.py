from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.chat_defaults_read import ChatDefaultsRead
from ...types import Response


def _get_kwargs() -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/me/chat-defaults",
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ChatDefaultsRead | None:
    if response.status_code == 200:
        response_200 = ChatDefaultsRead.from_dict(response.json())

        return response_200

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ChatDefaultsRead]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[ChatDefaultsRead]:
    """My Chat Defaults

     The model + effort a NEW chat should open on, resolved against the live catalog.

    The rules are :func:`alkera_core.chat_defaults.resolve_chat_defaults` — the
    same ones the CLI and the editor obey — and the one that matters here is the
    outage branch: when the gateway cannot be reached the saved values come back
    UNTOUCHED and nothing is written. A resolver that "reset to the first model"
    on a transient 401 would wipe the reader's default on a hiccup they never
    saw.

    A correction (the saved model is no longer offered) IS persisted, so the
    value converges instead of being re-corrected on every read — as "no
    pick", so the reader follows the platform default from then on. A reader
    who never picked is not written at all: the default they land on is the
    platform's, and it must still move when the platform's does.

    The stance rides along because a composer with no chat yet still displays
    one, and the only honest source for it is the resolver the create route
    obeys — :func:`preferences_service.starting_cloud_mode`. It is answered on
    the outage branch too: an outage costs the reader their model seed, and must
    not also cost them the stance their next chat will run in.

    Both branches read the stance LAST, after any correction this read persists,
    because a correction writes the reader's preference row — and a stance read
    before that write can name one the very next create disagrees with.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatDefaultsRead]
    """

    kwargs = _get_kwargs()

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
) -> ChatDefaultsRead | None:
    """My Chat Defaults

     The model + effort a NEW chat should open on, resolved against the live catalog.

    The rules are :func:`alkera_core.chat_defaults.resolve_chat_defaults` — the
    same ones the CLI and the editor obey — and the one that matters here is the
    outage branch: when the gateway cannot be reached the saved values come back
    UNTOUCHED and nothing is written. A resolver that "reset to the first model"
    on a transient 401 would wipe the reader's default on a hiccup they never
    saw.

    A correction (the saved model is no longer offered) IS persisted, so the
    value converges instead of being re-corrected on every read — as "no
    pick", so the reader follows the platform default from then on. A reader
    who never picked is not written at all: the default they land on is the
    platform's, and it must still move when the platform's does.

    The stance rides along because a composer with no chat yet still displays
    one, and the only honest source for it is the resolver the create route
    obeys — :func:`preferences_service.starting_cloud_mode`. It is answered on
    the outage branch too: an outage costs the reader their model seed, and must
    not also cost them the stance their next chat will run in.

    Both branches read the stance LAST, after any correction this read persists,
    because a correction writes the reader's preference row — and a stance read
    before that write can name one the very next create disagrees with.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatDefaultsRead
    """

    return sync_detailed(
        client=client,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[ChatDefaultsRead]:
    """My Chat Defaults

     The model + effort a NEW chat should open on, resolved against the live catalog.

    The rules are :func:`alkera_core.chat_defaults.resolve_chat_defaults` — the
    same ones the CLI and the editor obey — and the one that matters here is the
    outage branch: when the gateway cannot be reached the saved values come back
    UNTOUCHED and nothing is written. A resolver that "reset to the first model"
    on a transient 401 would wipe the reader's default on a hiccup they never
    saw.

    A correction (the saved model is no longer offered) IS persisted, so the
    value converges instead of being re-corrected on every read — as "no
    pick", so the reader follows the platform default from then on. A reader
    who never picked is not written at all: the default they land on is the
    platform's, and it must still move when the platform's does.

    The stance rides along because a composer with no chat yet still displays
    one, and the only honest source for it is the resolver the create route
    obeys — :func:`preferences_service.starting_cloud_mode`. It is answered on
    the outage branch too: an outage costs the reader their model seed, and must
    not also cost them the stance their next chat will run in.

    Both branches read the stance LAST, after any correction this read persists,
    because a correction writes the reader's preference row — and a stance read
    before that write can name one the very next create disagrees with.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ChatDefaultsRead]
    """

    kwargs = _get_kwargs()

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
) -> ChatDefaultsRead | None:
    """My Chat Defaults

     The model + effort a NEW chat should open on, resolved against the live catalog.

    The rules are :func:`alkera_core.chat_defaults.resolve_chat_defaults` — the
    same ones the CLI and the editor obey — and the one that matters here is the
    outage branch: when the gateway cannot be reached the saved values come back
    UNTOUCHED and nothing is written. A resolver that "reset to the first model"
    on a transient 401 would wipe the reader's default on a hiccup they never
    saw.

    A correction (the saved model is no longer offered) IS persisted, so the
    value converges instead of being re-corrected on every read — as "no
    pick", so the reader follows the platform default from then on. A reader
    who never picked is not written at all: the default they land on is the
    platform's, and it must still move when the platform's does.

    The stance rides along because a composer with no chat yet still displays
    one, and the only honest source for it is the resolver the create route
    obeys — :func:`preferences_service.starting_cloud_mode`. It is answered on
    the outage branch too: an outage costs the reader their model seed, and must
    not also cost them the stance their next chat will run in.

    Both branches read the stance LAST, after any correction this read persists,
    because a correction writes the reader's preference row — and a stance read
    before that write can name one the very next create disagrees with.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ChatDefaultsRead
    """

    return (
        await asyncio_detailed(
            client=client,
        )
    ).parsed
