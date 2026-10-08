from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.machine_released import MachineReleased
from ...models.terminate_request import TerminateRequest
from ...types import UNSET, Response, Unset


def _get_kwargs(
    machine_id: UUID,
    *,
    body: None | TerminateRequest | Unset = UNSET,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/admin/v1/machines/{machine_id}/terminate".format(
            machine_id=quote(str(machine_id), safe=""),
        ),
    }

    if isinstance(body, TerminateRequest):
        _kwargs["json"] = body.to_dict()
    else:
        _kwargs["json"] = body

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | MachineReleased | None:
    if response.status_code == 202:
        response_202 = MachineReleased.from_dict(response.json())

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
) -> Response[ErrorEnvelope | MachineReleased]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    machine_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: None | TerminateRequest | Unset = UNSET,
) -> Response[ErrorEnvelope | MachineReleased]:
    """Terminate Machine

     Release the machine: ``409 machine_has_chats`` while it serves any,
    unless ``force``. The answer names the orgs whose dedicated assignment the
    release dropped (they place by the ordinary rules again) and says where
    the machine's chats went — moved to another box, or stranded on it.

    Args:
        machine_id (UUID):
        body (None | TerminateRequest | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | MachineReleased]
    """

    kwargs = _get_kwargs(
        machine_id=machine_id,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    machine_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: None | TerminateRequest | Unset = UNSET,
) -> ErrorEnvelope | MachineReleased | None:
    """Terminate Machine

     Release the machine: ``409 machine_has_chats`` while it serves any,
    unless ``force``. The answer names the orgs whose dedicated assignment the
    release dropped (they place by the ordinary rules again) and says where
    the machine's chats went — moved to another box, or stranded on it.

    Args:
        machine_id (UUID):
        body (None | TerminateRequest | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | MachineReleased
    """

    return sync_detailed(
        machine_id=machine_id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    machine_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: None | TerminateRequest | Unset = UNSET,
) -> Response[ErrorEnvelope | MachineReleased]:
    """Terminate Machine

     Release the machine: ``409 machine_has_chats`` while it serves any,
    unless ``force``. The answer names the orgs whose dedicated assignment the
    release dropped (they place by the ordinary rules again) and says where
    the machine's chats went — moved to another box, or stranded on it.

    Args:
        machine_id (UUID):
        body (None | TerminateRequest | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | MachineReleased]
    """

    kwargs = _get_kwargs(
        machine_id=machine_id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    machine_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: None | TerminateRequest | Unset = UNSET,
) -> ErrorEnvelope | MachineReleased | None:
    """Terminate Machine

     Release the machine: ``409 machine_has_chats`` while it serves any,
    unless ``force``. The answer names the orgs whose dedicated assignment the
    release dropped (they place by the ordinary rules again) and says where
    the machine's chats went — moved to another box, or stranded on it.

    Args:
        machine_id (UUID):
        body (None | TerminateRequest | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | MachineReleased
    """

    return (
        await asyncio_detailed(
            machine_id=machine_id,
            client=client,
            body=body,
        )
    ).parsed
