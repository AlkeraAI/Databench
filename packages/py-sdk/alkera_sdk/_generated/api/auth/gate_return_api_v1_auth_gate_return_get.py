from http import HTTPStatus
from typing import Any, cast

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    next_: None | str | Unset = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    json_next_: None | str | Unset
    if isinstance(next_, Unset):
        json_next_ = UNSET
    else:
        json_next_ = next_
    params["next"] = json_next_

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/auth/gate/return",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Any | ErrorEnvelope | None:
    if response.status_code == 302:
        response_302 = cast(Any, None)
        return response_302

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
    *,
    client: AuthenticatedClient | Client,
    next_: None | str | Unset = UNSET,
) -> Response[Any | ErrorEnvelope]:
    """Send the browser back to the app after a top-level visit to the API host

     Return a browser to the app once it has visited this host top-level.

    An environment may put a sign-in gate in front of the API host itself (an
    OIDC action at the load balancer). Such a gate sets its session cookie only
    on a top-level navigation, and the app's own requests, cross-origin, would
    only ever meet the gate's redirect. The app sends the browser here once; the
    gate runs on the way in, and this answer returns it to where it was: a path
    under the app's own origin, never anywhere else (``safe_return_path``).

    Args:
        next_ (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        next_=next_,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
    next_: None | str | Unset = UNSET,
) -> Any | ErrorEnvelope | None:
    """Send the browser back to the app after a top-level visit to the API host

     Return a browser to the app once it has visited this host top-level.

    An environment may put a sign-in gate in front of the API host itself (an
    OIDC action at the load balancer). Such a gate sets its session cookie only
    on a top-level navigation, and the app's own requests, cross-origin, would
    only ever meet the gate's redirect. The app sends the browser here once; the
    gate runs on the way in, and this answer returns it to where it was: a path
    under the app's own origin, never anywhere else (``safe_return_path``).

    Args:
        next_ (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ErrorEnvelope
    """

    return sync_detailed(
        client=client,
        next_=next_,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    next_: None | str | Unset = UNSET,
) -> Response[Any | ErrorEnvelope]:
    """Send the browser back to the app after a top-level visit to the API host

     Return a browser to the app once it has visited this host top-level.

    An environment may put a sign-in gate in front of the API host itself (an
    OIDC action at the load balancer). Such a gate sets its session cookie only
    on a top-level navigation, and the app's own requests, cross-origin, would
    only ever meet the gate's redirect. The app sends the browser here once; the
    gate runs on the way in, and this answer returns it to where it was: a path
    under the app's own origin, never anywhere else (``safe_return_path``).

    Args:
        next_ (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        next_=next_,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    next_: None | str | Unset = UNSET,
) -> Any | ErrorEnvelope | None:
    """Send the browser back to the app after a top-level visit to the API host

     Return a browser to the app once it has visited this host top-level.

    An environment may put a sign-in gate in front of the API host itself (an
    OIDC action at the load balancer). Such a gate sets its session cookie only
    on a top-level navigation, and the app's own requests, cross-origin, would
    only ever meet the gate's redirect. The app sends the browser here once; the
    gate runs on the way in, and this answer returns it to where it was: a path
    under the app's own origin, never anywhere else (``safe_return_path``).

    Args:
        next_ (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            client=client,
            next_=next_,
        )
    ).parsed
