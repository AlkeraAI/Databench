from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...types import UNSET, Response, Unset


def _get_kwargs(
    provider: str,
    *,
    intent: str | Unset = "login",
    invite_token: None | str | Unset = UNSET,
    return_to: None | str | Unset = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["intent"] = intent

    json_invite_token: None | str | Unset
    if isinstance(invite_token, Unset):
        json_invite_token = UNSET
    else:
        json_invite_token = invite_token
    params["invite_token"] = json_invite_token

    json_return_to: None | str | Unset
    if isinstance(return_to, Unset):
        json_return_to = UNSET
    else:
        json_return_to = return_to
    params["return_to"] = json_return_to

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/auth/oauth/{provider}/start".format(
            provider=quote(str(provider), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Any | ErrorEnvelope | None:
    if response.status_code == 200:
        response_200 = response.json()
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
) -> Response[Any | ErrorEnvelope]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    provider: str,
    *,
    client: AuthenticatedClient | Client,
    intent: str | Unset = "login",
    invite_token: None | str | Unset = UNSET,
    return_to: None | str | Unset = UNSET,
) -> Response[Any | ErrorEnvelope]:
    """Start

    Args:
        provider (str):
        intent (str | Unset):  Default: 'login'.
        invite_token (None | str | Unset):
        return_to (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        provider=provider,
        intent=intent,
        invite_token=invite_token,
        return_to=return_to,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    provider: str,
    *,
    client: AuthenticatedClient | Client,
    intent: str | Unset = "login",
    invite_token: None | str | Unset = UNSET,
    return_to: None | str | Unset = UNSET,
) -> Any | ErrorEnvelope | None:
    """Start

    Args:
        provider (str):
        intent (str | Unset):  Default: 'login'.
        invite_token (None | str | Unset):
        return_to (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ErrorEnvelope
    """

    return sync_detailed(
        provider=provider,
        client=client,
        intent=intent,
        invite_token=invite_token,
        return_to=return_to,
    ).parsed


async def asyncio_detailed(
    provider: str,
    *,
    client: AuthenticatedClient | Client,
    intent: str | Unset = "login",
    invite_token: None | str | Unset = UNSET,
    return_to: None | str | Unset = UNSET,
) -> Response[Any | ErrorEnvelope]:
    """Start

    Args:
        provider (str):
        intent (str | Unset):  Default: 'login'.
        invite_token (None | str | Unset):
        return_to (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        provider=provider,
        intent=intent,
        invite_token=invite_token,
        return_to=return_to,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    provider: str,
    *,
    client: AuthenticatedClient | Client,
    intent: str | Unset = "login",
    invite_token: None | str | Unset = UNSET,
    return_to: None | str | Unset = UNSET,
) -> Any | ErrorEnvelope | None:
    """Start

    Args:
        provider (str):
        intent (str | Unset):  Default: 'login'.
        invite_token (None | str | Unset):
        return_to (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            provider=provider,
            client=client,
            intent=intent,
            invite_token=invite_token,
            return_to=return_to,
        )
    ).parsed
