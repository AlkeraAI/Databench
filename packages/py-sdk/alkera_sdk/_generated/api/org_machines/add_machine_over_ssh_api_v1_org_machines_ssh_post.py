from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.org_machine_read import OrgMachineRead
from ...models.ssh_machine_add import SshMachineAdd
from ...types import Response


def _get_kwargs(
    *,
    body: SshMachineAdd,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/org/machines/ssh",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | OrgMachineRead | None:
    if response.status_code == 202:
        response_202 = OrgMachineRead.from_dict(response.json())

        return response_202

    if response.status_code == 422:
        response_422 = ErrorEnvelope.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ErrorEnvelope | OrgMachineRead]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: SshMachineAdd,
) -> Response[ErrorEnvelope | OrgMachineRead]:
    """Add Machine Over Ssh

     Add a host the org runs as an org machine. 409 when the host presents a
    key other than the confirmed fingerprint or the name is in use, 422 when
    it cannot be reached, refuses the credential or lacks a prerequisite.

    Args:
        body (SshMachineAdd):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OrgMachineRead]
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
    body: SshMachineAdd,
) -> ErrorEnvelope | OrgMachineRead | None:
    """Add Machine Over Ssh

     Add a host the org runs as an org machine. 409 when the host presents a
    key other than the confirmed fingerprint or the name is in use, 422 when
    it cannot be reached, refuses the credential or lacks a prerequisite.

    Args:
        body (SshMachineAdd):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OrgMachineRead
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: SshMachineAdd,
) -> Response[ErrorEnvelope | OrgMachineRead]:
    """Add Machine Over Ssh

     Add a host the org runs as an org machine. 409 when the host presents a
    key other than the confirmed fingerprint or the name is in use, 422 when
    it cannot be reached, refuses the credential or lacks a prerequisite.

    Args:
        body (SshMachineAdd):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OrgMachineRead]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: SshMachineAdd,
) -> ErrorEnvelope | OrgMachineRead | None:
    """Add Machine Over Ssh

     Add a host the org runs as an org machine. 409 when the host presents a
    key other than the confirmed fingerprint or the name is in use, 422 when
    it cannot be reached, refuses the credential or lacks a prerequisite.

    Args:
        body (SshMachineAdd):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OrgMachineRead
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
