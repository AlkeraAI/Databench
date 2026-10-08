from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.org_machine_read import OrgMachineRead
from ...types import UNSET, Response, Unset


def _get_kwargs(
    machine_id: str,
    *,
    now: bool | Unset = False,
    if_match: None | str | Unset = UNSET,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    if not isinstance(if_match, Unset):
        headers["If-Match"] = if_match

    params: dict[str, Any] = {}

    params["now"] = now

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/org/machines/{machine_id}/stop".format(
            machine_id=quote(str(machine_id), safe=""),
        ),
        "params": params,
    }

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
    machine_id: str,
    *,
    client: AuthenticatedClient | Client,
    now: bool | Unset = False,
    if_match: None | str | Unset = UNSET,
) -> Response[ErrorEnvelope | OrgMachineRead]:
    """Stop Machine

     Stop it, keeping its disk. Running chats get a short grace to finish
    unless ``now``.

    Args:
        machine_id (str):
        now (bool | Unset): Stop running chats now instead of letting them finish. Default: False.
        if_match (None | str | Unset): The slot's version as the caller last read it. A write
            whose version no longer matches is refused with a 409; omit it, or send *, to write
            unconditionally.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OrgMachineRead]
    """

    kwargs = _get_kwargs(
        machine_id=machine_id,
        now=now,
        if_match=if_match,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    machine_id: str,
    *,
    client: AuthenticatedClient | Client,
    now: bool | Unset = False,
    if_match: None | str | Unset = UNSET,
) -> ErrorEnvelope | OrgMachineRead | None:
    """Stop Machine

     Stop it, keeping its disk. Running chats get a short grace to finish
    unless ``now``.

    Args:
        machine_id (str):
        now (bool | Unset): Stop running chats now instead of letting them finish. Default: False.
        if_match (None | str | Unset): The slot's version as the caller last read it. A write
            whose version no longer matches is refused with a 409; omit it, or send *, to write
            unconditionally.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OrgMachineRead
    """

    return sync_detailed(
        machine_id=machine_id,
        client=client,
        now=now,
        if_match=if_match,
    ).parsed


async def asyncio_detailed(
    machine_id: str,
    *,
    client: AuthenticatedClient | Client,
    now: bool | Unset = False,
    if_match: None | str | Unset = UNSET,
) -> Response[ErrorEnvelope | OrgMachineRead]:
    """Stop Machine

     Stop it, keeping its disk. Running chats get a short grace to finish
    unless ``now``.

    Args:
        machine_id (str):
        now (bool | Unset): Stop running chats now instead of letting them finish. Default: False.
        if_match (None | str | Unset): The slot's version as the caller last read it. A write
            whose version no longer matches is refused with a 409; omit it, or send *, to write
            unconditionally.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OrgMachineRead]
    """

    kwargs = _get_kwargs(
        machine_id=machine_id,
        now=now,
        if_match=if_match,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    machine_id: str,
    *,
    client: AuthenticatedClient | Client,
    now: bool | Unset = False,
    if_match: None | str | Unset = UNSET,
) -> ErrorEnvelope | OrgMachineRead | None:
    """Stop Machine

     Stop it, keeping its disk. Running chats get a short grace to finish
    unless ``now``.

    Args:
        machine_id (str):
        now (bool | Unset): Stop running chats now instead of letting them finish. Default: False.
        if_match (None | str | Unset): The slot's version as the caller last read it. A write
            whose version no longer matches is refused with a 409; omit it, or send *, to write
            unconditionally.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OrgMachineRead
    """

    return (
        await asyncio_detailed(
            machine_id=machine_id,
            client=client,
            now=now,
            if_match=if_match,
        )
    ).parsed
