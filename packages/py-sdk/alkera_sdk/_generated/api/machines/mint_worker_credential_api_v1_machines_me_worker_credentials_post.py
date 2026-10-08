from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.machine_worker_credential_read import MachineWorkerCredentialRead
from ...models.machine_worker_credential_request import MachineWorkerCredentialRequest
from ...types import Response


def _get_kwargs(
    *,
    body: MachineWorkerCredentialRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/machines/me/worker-credentials",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | MachineWorkerCredentialRead | None:
    if response.status_code == 201:
        response_201 = MachineWorkerCredentialRead.from_dict(response.json())

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
) -> Response[ErrorEnvelope | MachineWorkerCredentialRead]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: MachineWorkerCredentialRequest,
) -> Response[ErrorEnvelope | MachineWorkerCredentialRead]:
    """Mint Worker Credential

     A box mints the credential its process for ``body.org_id`` runs on.

    Only the machine credential itself may mint one, and only for an org it
    holds work in, by the rule the worker door re-asks on every request: a
    live chat bound to the machine, a workspace it still holds (a notebook
    kernel's sandbox with no chat on the box), or a live Files lease it is
    finishing on (the hand-back of a folder a chat moved off it). A pool box may
    be placed in any org, but a box that holds nothing for an org has no
    process to run for it, and a stolen credential must not mint its way in.
    The credential minted reaches that org alone; the decision is on record in
    that org's audit lane.

    Args:
        body (MachineWorkerCredentialRequest): The org a worker credential is minted for.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | MachineWorkerCredentialRead]
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
    body: MachineWorkerCredentialRequest,
) -> ErrorEnvelope | MachineWorkerCredentialRead | None:
    """Mint Worker Credential

     A box mints the credential its process for ``body.org_id`` runs on.

    Only the machine credential itself may mint one, and only for an org it
    holds work in, by the rule the worker door re-asks on every request: a
    live chat bound to the machine, a workspace it still holds (a notebook
    kernel's sandbox with no chat on the box), or a live Files lease it is
    finishing on (the hand-back of a folder a chat moved off it). A pool box may
    be placed in any org, but a box that holds nothing for an org has no
    process to run for it, and a stolen credential must not mint its way in.
    The credential minted reaches that org alone; the decision is on record in
    that org's audit lane.

    Args:
        body (MachineWorkerCredentialRequest): The org a worker credential is minted for.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | MachineWorkerCredentialRead
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: MachineWorkerCredentialRequest,
) -> Response[ErrorEnvelope | MachineWorkerCredentialRead]:
    """Mint Worker Credential

     A box mints the credential its process for ``body.org_id`` runs on.

    Only the machine credential itself may mint one, and only for an org it
    holds work in, by the rule the worker door re-asks on every request: a
    live chat bound to the machine, a workspace it still holds (a notebook
    kernel's sandbox with no chat on the box), or a live Files lease it is
    finishing on (the hand-back of a folder a chat moved off it). A pool box may
    be placed in any org, but a box that holds nothing for an org has no
    process to run for it, and a stolen credential must not mint its way in.
    The credential minted reaches that org alone; the decision is on record in
    that org's audit lane.

    Args:
        body (MachineWorkerCredentialRequest): The org a worker credential is minted for.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | MachineWorkerCredentialRead]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: MachineWorkerCredentialRequest,
) -> ErrorEnvelope | MachineWorkerCredentialRead | None:
    """Mint Worker Credential

     A box mints the credential its process for ``body.org_id`` runs on.

    Only the machine credential itself may mint one, and only for an org it
    holds work in, by the rule the worker door re-asks on every request: a
    live chat bound to the machine, a workspace it still holds (a notebook
    kernel's sandbox with no chat on the box), or a live Files lease it is
    finishing on (the hand-back of a folder a chat moved off it). A pool box may
    be placed in any org, but a box that holds nothing for an org has no
    process to run for it, and a stolen credential must not mint its way in.
    The credential minted reaches that org alone; the decision is on record in
    that org's audit lane.

    Args:
        body (MachineWorkerCredentialRequest): The org a worker credential is minted for.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | MachineWorkerCredentialRead
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
