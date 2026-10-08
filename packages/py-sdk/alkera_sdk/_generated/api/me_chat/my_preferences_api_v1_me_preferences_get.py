from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.preferences_read import PreferencesRead
from ...types import Response


def _get_kwargs() -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/me/preferences",
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PreferencesRead | None:
    if response.status_code == 200:
        response_200 = PreferencesRead.from_dict(response.json())

        return response_200

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[PreferencesRead]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[PreferencesRead]:
    """My Preferences

     This caller's preferences, with the Default Chat Model resolved.

    A caller who has never saved one gets the DEFAULT document rather than a
    404: "nothing stored" and "the defaults" are the same state, and a client
    that had to branch on which would end up spelling the defaults a second
    time. The saved Default Chat Model reads through the same resolver a new
    chat does, so a model since retired, disabled or never real reads as the
    platform default instead of a value no chat can open on.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PreferencesRead]
    """

    kwargs = _get_kwargs()

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
) -> PreferencesRead | None:
    """My Preferences

     This caller's preferences, with the Default Chat Model resolved.

    A caller who has never saved one gets the DEFAULT document rather than a
    404: "nothing stored" and "the defaults" are the same state, and a client
    that had to branch on which would end up spelling the defaults a second
    time. The saved Default Chat Model reads through the same resolver a new
    chat does, so a model since retired, disabled or never real reads as the
    platform default instead of a value no chat can open on.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PreferencesRead
    """

    return sync_detailed(
        client=client,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[PreferencesRead]:
    """My Preferences

     This caller's preferences, with the Default Chat Model resolved.

    A caller who has never saved one gets the DEFAULT document rather than a
    404: "nothing stored" and "the defaults" are the same state, and a client
    that had to branch on which would end up spelling the defaults a second
    time. The saved Default Chat Model reads through the same resolver a new
    chat does, so a model since retired, disabled or never real reads as the
    platform default instead of a value no chat can open on.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PreferencesRead]
    """

    kwargs = _get_kwargs()

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
) -> PreferencesRead | None:
    """My Preferences

     This caller's preferences, with the Default Chat Model resolved.

    A caller who has never saved one gets the DEFAULT document rather than a
    404: "nothing stored" and "the defaults" are the same state, and a client
    that had to branch on which would end up spelling the defaults a second
    time. The saved Default Chat Model reads through the same resolver a new
    chat does, so a model since retired, disabled or never real reads as the
    platform default instead of a value no chat can open on.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PreferencesRead
    """

    return (
        await asyncio_detailed(
            client=client,
        )
    ).parsed
