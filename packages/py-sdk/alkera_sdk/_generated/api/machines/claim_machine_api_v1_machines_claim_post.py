from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.machine_claim_request import MachineClaimRequest
from ...models.machine_read import MachineRead
from ...types import Response


def _get_kwargs(
    *,
    body: MachineClaimRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/machines/claim",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | MachineRead | None:
    if response.status_code == 200:
        response_200 = MachineRead.from_dict(response.json())

        return response_200

    if response.status_code == 201:
        response_201 = MachineRead.from_dict(response.json())

        return response_201

    if response.status_code == 422:
        response_422 = ErrorEnvelope.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ErrorEnvelope | MachineRead]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: MachineClaimRequest,
) -> Response[ErrorEnvelope | MachineRead]:
    """Claim Machine

     A platform box registers as the machine its credential names.

    Decided by ``compute.machine_credential``: a request without a live
    credential is refused (the header may be absent, or name a revoked one),
    and a credential already claimed by a different pod is an opaque not-found
    — a box that learned another box's id does not become it. The credential
    is the bearer itself when the box speaks on it, or rides beside the box
    user's session in its own header.

    Args:
        body (MachineClaimRequest): A platform box claims the machine its credential was minted
            for. The
            kind, size and tenancy come from the credential, never from the box; the
            box says only which instance it is and what it can hold.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | MachineRead]
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
    body: MachineClaimRequest,
) -> ErrorEnvelope | MachineRead | None:
    """Claim Machine

     A platform box registers as the machine its credential names.

    Decided by ``compute.machine_credential``: a request without a live
    credential is refused (the header may be absent, or name a revoked one),
    and a credential already claimed by a different pod is an opaque not-found
    — a box that learned another box's id does not become it. The credential
    is the bearer itself when the box speaks on it, or rides beside the box
    user's session in its own header.

    Args:
        body (MachineClaimRequest): A platform box claims the machine its credential was minted
            for. The
            kind, size and tenancy come from the credential, never from the box; the
            box says only which instance it is and what it can hold.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | MachineRead
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: MachineClaimRequest,
) -> Response[ErrorEnvelope | MachineRead]:
    """Claim Machine

     A platform box registers as the machine its credential names.

    Decided by ``compute.machine_credential``: a request without a live
    credential is refused (the header may be absent, or name a revoked one),
    and a credential already claimed by a different pod is an opaque not-found
    — a box that learned another box's id does not become it. The credential
    is the bearer itself when the box speaks on it, or rides beside the box
    user's session in its own header.

    Args:
        body (MachineClaimRequest): A platform box claims the machine its credential was minted
            for. The
            kind, size and tenancy come from the credential, never from the box; the
            box says only which instance it is and what it can hold.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | MachineRead]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: MachineClaimRequest,
) -> ErrorEnvelope | MachineRead | None:
    """Claim Machine

     A platform box registers as the machine its credential names.

    Decided by ``compute.machine_credential``: a request without a live
    credential is refused (the header may be absent, or name a revoked one),
    and a credential already claimed by a different pod is an opaque not-found
    — a box that learned another box's id does not become it. The credential
    is the bearer itself when the box speaks on it, or rides beside the box
    user's session in its own header.

    Args:
        body (MachineClaimRequest): A platform box claims the machine its credential was minted
            for. The
            kind, size and tenancy come from the credential, never from the box; the
            box says only which instance it is and what it can hold.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | MachineRead
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
