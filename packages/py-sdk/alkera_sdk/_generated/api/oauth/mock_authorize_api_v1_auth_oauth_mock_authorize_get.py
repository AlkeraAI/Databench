from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    state: str,
    redirect_uri: str,
    email: None | str | Unset = UNSET,
    first_name: str | Unset = "Mock",
    last_name: str | Unset = "User",
    subject: None | str | Unset = UNSET,
    email_verified: bool | Unset = True,
    submit: None | str | Unset = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["state"] = state

    params["redirect_uri"] = redirect_uri

    json_email: None | str | Unset
    if isinstance(email, Unset):
        json_email = UNSET
    else:
        json_email = email
    params["email"] = json_email

    params["first_name"] = first_name

    params["last_name"] = last_name

    json_subject: None | str | Unset
    if isinstance(subject, Unset):
        json_subject = UNSET
    else:
        json_subject = subject
    params["subject"] = json_subject

    params["email_verified"] = email_verified

    json_submit: None | str | Unset
    if isinstance(submit, Unset):
        json_submit = UNSET
    else:
        json_submit = submit
    params["submit"] = json_submit

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/auth/oauth/mock/authorize",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | str | None:
    if response.status_code == 200:
        response_200 = response.text
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
) -> Response[ErrorEnvelope | str]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    state: str,
    redirect_uri: str,
    email: None | str | Unset = UNSET,
    first_name: str | Unset = "Mock",
    last_name: str | Unset = "User",
    subject: None | str | Unset = UNSET,
    email_verified: bool | Unset = True,
    submit: None | str | Unset = UNSET,
) -> Response[ErrorEnvelope | str]:
    """Mock Authorize

     A stand-in for a real provider's consent screen.

    Only mounted when the mock provider is registered (dev/test). Lets a human
    type any identity and bounce back through the real callback — no creds.

    Args:
        state (str):
        redirect_uri (str):
        email (None | str | Unset):
        first_name (str | Unset):  Default: 'Mock'.
        last_name (str | Unset):  Default: 'User'.
        subject (None | str | Unset):
        email_verified (bool | Unset):  Default: True.
        submit (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | str]
    """

    kwargs = _get_kwargs(
        state=state,
        redirect_uri=redirect_uri,
        email=email,
        first_name=first_name,
        last_name=last_name,
        subject=subject,
        email_verified=email_verified,
        submit=submit,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
    state: str,
    redirect_uri: str,
    email: None | str | Unset = UNSET,
    first_name: str | Unset = "Mock",
    last_name: str | Unset = "User",
    subject: None | str | Unset = UNSET,
    email_verified: bool | Unset = True,
    submit: None | str | Unset = UNSET,
) -> ErrorEnvelope | str | None:
    """Mock Authorize

     A stand-in for a real provider's consent screen.

    Only mounted when the mock provider is registered (dev/test). Lets a human
    type any identity and bounce back through the real callback — no creds.

    Args:
        state (str):
        redirect_uri (str):
        email (None | str | Unset):
        first_name (str | Unset):  Default: 'Mock'.
        last_name (str | Unset):  Default: 'User'.
        subject (None | str | Unset):
        email_verified (bool | Unset):  Default: True.
        submit (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | str
    """

    return sync_detailed(
        client=client,
        state=state,
        redirect_uri=redirect_uri,
        email=email,
        first_name=first_name,
        last_name=last_name,
        subject=subject,
        email_verified=email_verified,
        submit=submit,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    state: str,
    redirect_uri: str,
    email: None | str | Unset = UNSET,
    first_name: str | Unset = "Mock",
    last_name: str | Unset = "User",
    subject: None | str | Unset = UNSET,
    email_verified: bool | Unset = True,
    submit: None | str | Unset = UNSET,
) -> Response[ErrorEnvelope | str]:
    """Mock Authorize

     A stand-in for a real provider's consent screen.

    Only mounted when the mock provider is registered (dev/test). Lets a human
    type any identity and bounce back through the real callback — no creds.

    Args:
        state (str):
        redirect_uri (str):
        email (None | str | Unset):
        first_name (str | Unset):  Default: 'Mock'.
        last_name (str | Unset):  Default: 'User'.
        subject (None | str | Unset):
        email_verified (bool | Unset):  Default: True.
        submit (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | str]
    """

    kwargs = _get_kwargs(
        state=state,
        redirect_uri=redirect_uri,
        email=email,
        first_name=first_name,
        last_name=last_name,
        subject=subject,
        email_verified=email_verified,
        submit=submit,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    state: str,
    redirect_uri: str,
    email: None | str | Unset = UNSET,
    first_name: str | Unset = "Mock",
    last_name: str | Unset = "User",
    subject: None | str | Unset = UNSET,
    email_verified: bool | Unset = True,
    submit: None | str | Unset = UNSET,
) -> ErrorEnvelope | str | None:
    """Mock Authorize

     A stand-in for a real provider's consent screen.

    Only mounted when the mock provider is registered (dev/test). Lets a human
    type any identity and bounce back through the real callback — no creds.

    Args:
        state (str):
        redirect_uri (str):
        email (None | str | Unset):
        first_name (str | Unset):  Default: 'Mock'.
        last_name (str | Unset):  Default: 'User'.
        subject (None | str | Unset):
        email_verified (bool | Unset):  Default: True.
        submit (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | str
    """

    return (
        await asyncio_detailed(
            client=client,
            state=state,
            redirect_uri=redirect_uri,
            email=email,
            first_name=first_name,
            last_name=last_name,
            subject=subject,
            email_verified=email_verified,
            submit=submit,
        )
    ).parsed
