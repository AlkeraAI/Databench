from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.message_response import MessageResponse
from ...types import Response


def _get_kwargs(
    jti: str,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "delete",
        "url": "/api/v1/auth/sessions/{jti}".format(
            jti=quote(str(jti), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | MessageResponse | None:
    if response.status_code == 200:
        response_200 = MessageResponse.from_dict(response.json())

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
) -> Response[ErrorEnvelope | MessageResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    jti: str,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | MessageResponse]:
    """Revoke Session

     End one of the caller's sessions: a browser session by its family id
    (its live access token goes with it), else a token by jti in the
    request's org. 404 if it isn't theirs, or is their token for another org.

    A browser session belongs to the person, whichever org it is in, so while
    multi-org is on only a session that signed in as the person may end one
    other than its own. A token is ended within the request's org, as
    always.

    Args:
        jti (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | MessageResponse]
    """

    kwargs = _get_kwargs(
        jti=jti,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    jti: str,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | MessageResponse | None:
    """Revoke Session

     End one of the caller's sessions: a browser session by its family id
    (its live access token goes with it), else a token by jti in the
    request's org. 404 if it isn't theirs, or is their token for another org.

    A browser session belongs to the person, whichever org it is in, so while
    multi-org is on only a session that signed in as the person may end one
    other than its own. A token is ended within the request's org, as
    always.

    Args:
        jti (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | MessageResponse
    """

    return sync_detailed(
        jti=jti,
        client=client,
    ).parsed


async def asyncio_detailed(
    jti: str,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | MessageResponse]:
    """Revoke Session

     End one of the caller's sessions: a browser session by its family id
    (its live access token goes with it), else a token by jti in the
    request's org. 404 if it isn't theirs, or is their token for another org.

    A browser session belongs to the person, whichever org it is in, so while
    multi-org is on only a session that signed in as the person may end one
    other than its own. A token is ended within the request's org, as
    always.

    Args:
        jti (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | MessageResponse]
    """

    kwargs = _get_kwargs(
        jti=jti,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    jti: str,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | MessageResponse | None:
    """Revoke Session

     End one of the caller's sessions: a browser session by its family id
    (its live access token goes with it), else a token by jti in the
    request's org. 404 if it isn't theirs, or is their token for another org.

    A browser session belongs to the person, whichever org it is in, so while
    multi-org is on only a session that signed in as the person may end one
    other than its own. A token is ended within the request's org, as
    always.

    Args:
        jti (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | MessageResponse
    """

    return (
        await asyncio_detailed(
            jti=jti,
            client=client,
        )
    ).parsed
