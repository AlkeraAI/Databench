from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.mfa_enroll_request import MfaEnrollRequest
from ...models.mfa_enroll_response import MfaEnrollResponse
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    body: MfaEnrollRequest | None | Unset = UNSET,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/auth/mfa/enroll",
    }

    if isinstance(body, MfaEnrollRequest):
        _kwargs["json"] = body.to_dict()
    else:
        _kwargs["json"] = body

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | MfaEnrollResponse | None:
    if response.status_code == 200:
        response_200 = MfaEnrollResponse.from_dict(response.json())

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
) -> Response[ErrorEnvelope | MfaEnrollResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: MfaEnrollRequest | None | Unset = UNSET,
) -> Response[ErrorEnvelope | MfaEnrollResponse]:
    """Mfa Enroll

     Generate a pending TOTP secret + the otpauth URI for an authenticator app.
    Activated by /mfa/confirm with a valid code.

    Args:
        body (MfaEnrollRequest | None | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | MfaEnrollResponse]
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
    body: MfaEnrollRequest | None | Unset = UNSET,
) -> ErrorEnvelope | MfaEnrollResponse | None:
    """Mfa Enroll

     Generate a pending TOTP secret + the otpauth URI for an authenticator app.
    Activated by /mfa/confirm with a valid code.

    Args:
        body (MfaEnrollRequest | None | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | MfaEnrollResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: MfaEnrollRequest | None | Unset = UNSET,
) -> Response[ErrorEnvelope | MfaEnrollResponse]:
    """Mfa Enroll

     Generate a pending TOTP secret + the otpauth URI for an authenticator app.
    Activated by /mfa/confirm with a valid code.

    Args:
        body (MfaEnrollRequest | None | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | MfaEnrollResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: MfaEnrollRequest | None | Unset = UNSET,
) -> ErrorEnvelope | MfaEnrollResponse | None:
    """Mfa Enroll

     Generate a pending TOTP secret + the otpauth URI for an authenticator app.
    Activated by /mfa/confirm with a valid code.

    Args:
        body (MfaEnrollRequest | None | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | MfaEnrollResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
