from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.acquire_body import AcquireBody
from ...models.error_envelope import ErrorEnvelope
from ...models.lease_grant import LeaseGrant
from ...types import Response


def _get_kwargs(
    drive_id: UUID,
    item_id: UUID,
    *,
    body: AcquireBody,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/files/drives/{drive_id}/items/{item_id}/lease".format(
            drive_id=quote(str(drive_id), safe=""),
            item_id=quote(str(item_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | LeaseGrant | None:
    if response.status_code == 200:
        response_200 = LeaseGrant.from_dict(response.json())

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
) -> Response[ErrorEnvelope | LeaseGrant]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    drive_id: UUID,
    item_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: AcquireBody,
) -> Response[ErrorEnvelope | LeaseGrant]:
    """Acquire Lease

     Take the folder. 409 ``files.leased`` when anything above or below it is
    already held — naming the holder only to a caller who may read that node.

    Args:
        drive_id (UUID):
        item_id (UUID):
        body (AcquireBody): What a holder asks for when it takes a folder.

            ``inbound`` is "take what other people write here and keep it for me to
            apply" and ``live`` is "I will run the live plane over this folder": either
            one turns the folder's live plane on, because a folder that keeps drops
            nobody drains and a holder that drains a folder keeping none are the same
            mistake told from opposite ends. A holder that asks for neither gets what
            its purpose means — a chat's or a workspace's lease takes drops, because that is why the
            box
            holds the folder at all; a mount does not, because the machine owns that
            tree outright.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | LeaseGrant]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    drive_id: UUID,
    item_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: AcquireBody,
) -> ErrorEnvelope | LeaseGrant | None:
    """Acquire Lease

     Take the folder. 409 ``files.leased`` when anything above or below it is
    already held — naming the holder only to a caller who may read that node.

    Args:
        drive_id (UUID):
        item_id (UUID):
        body (AcquireBody): What a holder asks for when it takes a folder.

            ``inbound`` is "take what other people write here and keep it for me to
            apply" and ``live`` is "I will run the live plane over this folder": either
            one turns the folder's live plane on, because a folder that keeps drops
            nobody drains and a holder that drains a folder keeping none are the same
            mistake told from opposite ends. A holder that asks for neither gets what
            its purpose means — a chat's or a workspace's lease takes drops, because that is why the
            box
            holds the folder at all; a mount does not, because the machine owns that
            tree outright.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | LeaseGrant
    """

    return sync_detailed(
        drive_id=drive_id,
        item_id=item_id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    drive_id: UUID,
    item_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: AcquireBody,
) -> Response[ErrorEnvelope | LeaseGrant]:
    """Acquire Lease

     Take the folder. 409 ``files.leased`` when anything above or below it is
    already held — naming the holder only to a caller who may read that node.

    Args:
        drive_id (UUID):
        item_id (UUID):
        body (AcquireBody): What a holder asks for when it takes a folder.

            ``inbound`` is "take what other people write here and keep it for me to
            apply" and ``live`` is "I will run the live plane over this folder": either
            one turns the folder's live plane on, because a folder that keeps drops
            nobody drains and a holder that drains a folder keeping none are the same
            mistake told from opposite ends. A holder that asks for neither gets what
            its purpose means — a chat's or a workspace's lease takes drops, because that is why the
            box
            holds the folder at all; a mount does not, because the machine owns that
            tree outright.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | LeaseGrant]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: UUID,
    item_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: AcquireBody,
) -> ErrorEnvelope | LeaseGrant | None:
    """Acquire Lease

     Take the folder. 409 ``files.leased`` when anything above or below it is
    already held — naming the holder only to a caller who may read that node.

    Args:
        drive_id (UUID):
        item_id (UUID):
        body (AcquireBody): What a holder asks for when it takes a folder.

            ``inbound`` is "take what other people write here and keep it for me to
            apply" and ``live`` is "I will run the live plane over this folder": either
            one turns the folder's live plane on, because a folder that keeps drops
            nobody drains and a holder that drains a folder keeping none are the same
            mistake told from opposite ends. A holder that asks for neither gets what
            its purpose means — a chat's or a workspace's lease takes drops, because that is why the
            box
            holds the folder at all; a mount does not, because the machine owns that
            tree outright.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | LeaseGrant
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            client=client,
            body=body,
        )
    ).parsed
