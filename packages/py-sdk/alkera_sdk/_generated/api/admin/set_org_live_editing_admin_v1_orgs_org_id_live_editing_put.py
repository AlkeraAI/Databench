from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.org_live_editing_read import OrgLiveEditingRead
from ...models.org_live_editing_update import OrgLiveEditingUpdate
from ...types import Response


def _get_kwargs(
    org_id: UUID,
    *,
    body: OrgLiveEditingUpdate,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "put",
        "url": "/admin/v1/orgs/{org_id}/live-editing".format(
            org_id=quote(str(org_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | OrgLiveEditingRead | None:
    if response.status_code == 200:
        response_200 = OrgLiveEditingRead.from_dict(response.json())

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
) -> Response[ErrorEnvelope | OrgLiveEditingRead]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    org_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: OrgLiveEditingUpdate,
) -> Response[ErrorEnvelope | OrgLiveEditingRead]:
    """Set Org Live Editing

     Set (``true``/``false``) or clear (``null``) the org's own switch.

    The setting commits first, so every replica refuses new live opens within
    a few seconds and ends open ones on their next tick. When the change
    leaves live editing off, every session of the org holding edits not yet on
    the drive is then written back before answering (bounded); what is left
    is the unsaved sweep's, and the answer says how many of each.

    Args:
        org_id (UUID):
        body (OrgLiveEditingUpdate): The org's own setting: ``true`` or ``false`` wins over the
            deployment's
            in either direction; ``null`` clears it, so the org follows the
            deployment again. The key is required, so an empty body changes
            nothing by accident; only a JSON boolean is a setting (``"off"`` or
            ``0`` is a 422, never read as false).

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OrgLiveEditingRead]
    """

    kwargs = _get_kwargs(
        org_id=org_id,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    org_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: OrgLiveEditingUpdate,
) -> ErrorEnvelope | OrgLiveEditingRead | None:
    """Set Org Live Editing

     Set (``true``/``false``) or clear (``null``) the org's own switch.

    The setting commits first, so every replica refuses new live opens within
    a few seconds and ends open ones on their next tick. When the change
    leaves live editing off, every session of the org holding edits not yet on
    the drive is then written back before answering (bounded); what is left
    is the unsaved sweep's, and the answer says how many of each.

    Args:
        org_id (UUID):
        body (OrgLiveEditingUpdate): The org's own setting: ``true`` or ``false`` wins over the
            deployment's
            in either direction; ``null`` clears it, so the org follows the
            deployment again. The key is required, so an empty body changes
            nothing by accident; only a JSON boolean is a setting (``"off"`` or
            ``0`` is a 422, never read as false).

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OrgLiveEditingRead
    """

    return sync_detailed(
        org_id=org_id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    org_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: OrgLiveEditingUpdate,
) -> Response[ErrorEnvelope | OrgLiveEditingRead]:
    """Set Org Live Editing

     Set (``true``/``false``) or clear (``null``) the org's own switch.

    The setting commits first, so every replica refuses new live opens within
    a few seconds and ends open ones on their next tick. When the change
    leaves live editing off, every session of the org holding edits not yet on
    the drive is then written back before answering (bounded); what is left
    is the unsaved sweep's, and the answer says how many of each.

    Args:
        org_id (UUID):
        body (OrgLiveEditingUpdate): The org's own setting: ``true`` or ``false`` wins over the
            deployment's
            in either direction; ``null`` clears it, so the org follows the
            deployment again. The key is required, so an empty body changes
            nothing by accident; only a JSON boolean is a setting (``"off"`` or
            ``0`` is a 422, never read as false).

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OrgLiveEditingRead]
    """

    kwargs = _get_kwargs(
        org_id=org_id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    org_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: OrgLiveEditingUpdate,
) -> ErrorEnvelope | OrgLiveEditingRead | None:
    """Set Org Live Editing

     Set (``true``/``false``) or clear (``null``) the org's own switch.

    The setting commits first, so every replica refuses new live opens within
    a few seconds and ends open ones on their next tick. When the change
    leaves live editing off, every session of the org holding edits not yet on
    the drive is then written back before answering (bounded); what is left
    is the unsaved sweep's, and the answer says how many of each.

    Args:
        org_id (UUID):
        body (OrgLiveEditingUpdate): The org's own setting: ``true`` or ``false`` wins over the
            deployment's
            in either direction; ``null`` clears it, so the org follows the
            deployment again. The key is required, so an empty body changes
            nothing by accident; only a JSON boolean is a setting (``"off"`` or
            ``0`` is a 422, never read as false).

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OrgLiveEditingRead
    """

    return (
        await asyncio_detailed(
            org_id=org_id,
            client=client,
            body=body,
        )
    ).parsed
