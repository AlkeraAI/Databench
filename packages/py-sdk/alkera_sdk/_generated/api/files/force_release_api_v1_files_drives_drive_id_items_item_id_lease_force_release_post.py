from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.force_body import ForceBody
from ...models.lease_grant import LeaseGrant
from ...types import UNSET, Response, Unset


def _get_kwargs(
    drive_id: UUID,
    item_id: UUID,
    *,
    body: ForceBody | None | Unset = UNSET,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/files/drives/{drive_id}/items/{item_id}/lease/force-release".format(
            drive_id=quote(str(drive_id), safe=""),
            item_id=quote(str(item_id), safe=""),
        ),
    }

    if isinstance(body, ForceBody):
        _kwargs["json"] = body.to_dict()
    else:
        _kwargs["json"] = body

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
    body: ForceBody | None | Unset = UNSET,
) -> Response[ErrorEnvelope | LeaseGrant]:
    """Force Release

     A manager takes it back. The holder is not cut off mid-write: it has one
    TTL of grace, and learns about it from ``forced`` on its next heartbeat.

    The grace is the deployment's TTL, the same one acquire and heartbeat hand
    out. A grace cut from the build's constant instead would be shorter than
    the beat cadence the holder was told to keep on any deployment that widened
    the window, and the holder would be fenced before it ever saw ``forced``.

    Args:
        drive_id (UUID):
        item_id (UUID):
        body (ForceBody | None | Unset):

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
    body: ForceBody | None | Unset = UNSET,
) -> ErrorEnvelope | LeaseGrant | None:
    """Force Release

     A manager takes it back. The holder is not cut off mid-write: it has one
    TTL of grace, and learns about it from ``forced`` on its next heartbeat.

    The grace is the deployment's TTL, the same one acquire and heartbeat hand
    out. A grace cut from the build's constant instead would be shorter than
    the beat cadence the holder was told to keep on any deployment that widened
    the window, and the holder would be fenced before it ever saw ``forced``.

    Args:
        drive_id (UUID):
        item_id (UUID):
        body (ForceBody | None | Unset):

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
    body: ForceBody | None | Unset = UNSET,
) -> Response[ErrorEnvelope | LeaseGrant]:
    """Force Release

     A manager takes it back. The holder is not cut off mid-write: it has one
    TTL of grace, and learns about it from ``forced`` on its next heartbeat.

    The grace is the deployment's TTL, the same one acquire and heartbeat hand
    out. A grace cut from the build's constant instead would be shorter than
    the beat cadence the holder was told to keep on any deployment that widened
    the window, and the holder would be fenced before it ever saw ``forced``.

    Args:
        drive_id (UUID):
        item_id (UUID):
        body (ForceBody | None | Unset):

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
    body: ForceBody | None | Unset = UNSET,
) -> ErrorEnvelope | LeaseGrant | None:
    """Force Release

     A manager takes it back. The holder is not cut off mid-write: it has one
    TTL of grace, and learns about it from ``forced`` on its next heartbeat.

    The grace is the deployment's TTL, the same one acquire and heartbeat hand
    out. A grace cut from the build's constant instead would be shorter than
    the beat cadence the holder was told to keep on any deployment that widened
    the window, and the holder would be fenced before it ever saw ``forced``.

    Args:
        drive_id (UUID):
        item_id (UUID):
        body (ForceBody | None | Unset):

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
