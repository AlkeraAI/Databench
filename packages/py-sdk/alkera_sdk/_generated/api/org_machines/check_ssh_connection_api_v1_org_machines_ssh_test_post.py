from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.ssh_machine_target import SshMachineTarget
from ...models.ssh_machine_test_read import SshMachineTestRead
from ...types import Response


def _get_kwargs(
    *,
    body: SshMachineTarget,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/org/machines/ssh/test",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | SshMachineTestRead | None:
    if response.status_code == 200:
        response_200 = SshMachineTestRead.from_dict(response.json())

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
) -> Response[ErrorEnvelope | SshMachineTestRead]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: SshMachineTarget,
) -> Response[ErrorEnvelope | SshMachineTestRead]:
    """Check Ssh Connection

     Connect to a host before adding it: whether it answered, the
    fingerprint of the key it presented, and whether it can run a node. 422
    for a host this deployment does not connect to.

    Args:
        body (SshMachineTarget): A host and how to sign in to it.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | SshMachineTestRead]
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
    body: SshMachineTarget,
) -> ErrorEnvelope | SshMachineTestRead | None:
    """Check Ssh Connection

     Connect to a host before adding it: whether it answered, the
    fingerprint of the key it presented, and whether it can run a node. 422
    for a host this deployment does not connect to.

    Args:
        body (SshMachineTarget): A host and how to sign in to it.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | SshMachineTestRead
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: SshMachineTarget,
) -> Response[ErrorEnvelope | SshMachineTestRead]:
    """Check Ssh Connection

     Connect to a host before adding it: whether it answered, the
    fingerprint of the key it presented, and whether it can run a node. 422
    for a host this deployment does not connect to.

    Args:
        body (SshMachineTarget): A host and how to sign in to it.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | SshMachineTestRead]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: SshMachineTarget,
) -> ErrorEnvelope | SshMachineTestRead | None:
    """Check Ssh Connection

     Connect to a host before adding it: whether it answered, the
    fingerprint of the key it presented, and whether it can run a node. 422
    for a host this deployment does not connect to.

    Args:
        body (SshMachineTarget): A host and how to sign in to it.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | SshMachineTestRead
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
