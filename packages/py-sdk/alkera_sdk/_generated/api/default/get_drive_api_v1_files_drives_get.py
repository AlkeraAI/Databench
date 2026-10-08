from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.drive_wire import DriveWire
from ...models.error_envelope import ErrorEnvelope
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    chat_id: None | str | Unset = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    json_chat_id: None | str | Unset
    if isinstance(chat_id, Unset):
        json_chat_id = UNSET
    else:
        json_chat_id = chat_id
    params["chatId"] = json_chat_id

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/files/drives",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> DriveWire | ErrorEnvelope | None:
    if response.status_code == 200:
        response_200 = DriveWire.from_dict(response.json())

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
) -> Response[DriveWire | ErrorEnvelope]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    chat_id: None | str | Unset = UNSET,
) -> Response[DriveWire | ErrorEnvelope]:
    """Get Drive

     The caller's org drive, and the node their own files live under.

    The drive itself is ensured by the context dependency. The home folder is
    ensured here because this is the first Files request any client makes and
    the org's founder has no membership event to have created hers — see
    :func:`backend.services.files.home.caller_home`. ``background`` is where a
    stray subtree too large to move inside this request is run after the
    response, the way the move route runs its oversized moves.

    ``homeId`` is answered by the server rather than derived by the client from
    the root listing: the root's ``home`` child is the org-wide container, and a
    client that takes it for "my files" shows an org admin every colleague's
    home folder as her own landing screen.

    A box on its machine credential has no drive of its own — it serves chats
    in orgs it is no member of — so for it this route answers the drive of the
    CHAT it names, decided the way every other Files read on that chat's
    folder is: through the policy, as the machine the chat is bound to. A
    chat it does not hold, a chat in an org it does not serve, a chat with no
    folder and no chat named at all are the same opaque not-found. A box has
    no home.

    Args:
        chat_id (None | str | Unset): For a box on its machine credential: the chat whose drive it
            asks for. A person's drive is their own and this is ignored.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DriveWire | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        chat_id=chat_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
    chat_id: None | str | Unset = UNSET,
) -> DriveWire | ErrorEnvelope | None:
    """Get Drive

     The caller's org drive, and the node their own files live under.

    The drive itself is ensured by the context dependency. The home folder is
    ensured here because this is the first Files request any client makes and
    the org's founder has no membership event to have created hers — see
    :func:`backend.services.files.home.caller_home`. ``background`` is where a
    stray subtree too large to move inside this request is run after the
    response, the way the move route runs its oversized moves.

    ``homeId`` is answered by the server rather than derived by the client from
    the root listing: the root's ``home`` child is the org-wide container, and a
    client that takes it for "my files" shows an org admin every colleague's
    home folder as her own landing screen.

    A box on its machine credential has no drive of its own — it serves chats
    in orgs it is no member of — so for it this route answers the drive of the
    CHAT it names, decided the way every other Files read on that chat's
    folder is: through the policy, as the machine the chat is bound to. A
    chat it does not hold, a chat in an org it does not serve, a chat with no
    folder and no chat named at all are the same opaque not-found. A box has
    no home.

    Args:
        chat_id (None | str | Unset): For a box on its machine credential: the chat whose drive it
            asks for. A person's drive is their own and this is ignored.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DriveWire | ErrorEnvelope
    """

    return sync_detailed(
        client=client,
        chat_id=chat_id,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    chat_id: None | str | Unset = UNSET,
) -> Response[DriveWire | ErrorEnvelope]:
    """Get Drive

     The caller's org drive, and the node their own files live under.

    The drive itself is ensured by the context dependency. The home folder is
    ensured here because this is the first Files request any client makes and
    the org's founder has no membership event to have created hers — see
    :func:`backend.services.files.home.caller_home`. ``background`` is where a
    stray subtree too large to move inside this request is run after the
    response, the way the move route runs its oversized moves.

    ``homeId`` is answered by the server rather than derived by the client from
    the root listing: the root's ``home`` child is the org-wide container, and a
    client that takes it for "my files" shows an org admin every colleague's
    home folder as her own landing screen.

    A box on its machine credential has no drive of its own — it serves chats
    in orgs it is no member of — so for it this route answers the drive of the
    CHAT it names, decided the way every other Files read on that chat's
    folder is: through the policy, as the machine the chat is bound to. A
    chat it does not hold, a chat in an org it does not serve, a chat with no
    folder and no chat named at all are the same opaque not-found. A box has
    no home.

    Args:
        chat_id (None | str | Unset): For a box on its machine credential: the chat whose drive it
            asks for. A person's drive is their own and this is ignored.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DriveWire | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        chat_id=chat_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    chat_id: None | str | Unset = UNSET,
) -> DriveWire | ErrorEnvelope | None:
    """Get Drive

     The caller's org drive, and the node their own files live under.

    The drive itself is ensured by the context dependency. The home folder is
    ensured here because this is the first Files request any client makes and
    the org's founder has no membership event to have created hers — see
    :func:`backend.services.files.home.caller_home`. ``background`` is where a
    stray subtree too large to move inside this request is run after the
    response, the way the move route runs its oversized moves.

    ``homeId`` is answered by the server rather than derived by the client from
    the root listing: the root's ``home`` child is the org-wide container, and a
    client that takes it for "my files" shows an org admin every colleague's
    home folder as her own landing screen.

    A box on its machine credential has no drive of its own — it serves chats
    in orgs it is no member of — so for it this route answers the drive of the
    CHAT it names, decided the way every other Files read on that chat's
    folder is: through the policy, as the machine the chat is bound to. A
    chat it does not hold, a chat in an org it does not serve, a chat with no
    folder and no chat named at all are the same opaque not-found. A box has
    no home.

    Args:
        chat_id (None | str | Unset): For a box on its machine credential: the chat whose drive it
            asks for. A person's drive is their own and this is ignored.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DriveWire | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            client=client,
            chat_id=chat_id,
        )
    ).parsed
