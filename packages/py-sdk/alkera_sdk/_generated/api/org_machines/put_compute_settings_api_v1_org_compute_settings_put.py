from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.org_compute_settings_read import OrgComputeSettingsRead
from ...models.org_compute_settings_update import OrgComputeSettingsUpdate
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    body: OrgComputeSettingsUpdate,
    if_match: None | str | Unset = UNSET,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    if not isinstance(if_match, Unset):
        headers["If-Match"] = if_match

    _kwargs: dict[str, Any] = {
        "method": "put",
        "url": "/api/v1/org/compute/settings",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | OrgComputeSettingsRead | None:
    if response.status_code == 200:
        response_200 = OrgComputeSettingsRead.from_dict(response.json())

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
) -> Response[ErrorEnvelope | OrgComputeSettingsRead]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: OrgComputeSettingsUpdate,
    if_match: None | str | Unset = UNSET,
) -> Response[ErrorEnvelope | OrgComputeSettingsRead]:
    """Put Compute Settings

     Change the settings. ``default_org_machine_id`` must name a live
    machine of the org (anything else is the same 404 a missing machine is);
    sent as null it clears the default.

    Args:
        if_match (None | str | Unset): The slot's version as the caller last read it. A write
            whose version no longer matches is refused with a 409; omit it, or send *, to write
            unconditionally.
        body (OrgComputeSettingsUpdate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OrgComputeSettingsRead]
    """

    kwargs = _get_kwargs(
        body=body,
        if_match=if_match,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
    body: OrgComputeSettingsUpdate,
    if_match: None | str | Unset = UNSET,
) -> ErrorEnvelope | OrgComputeSettingsRead | None:
    """Put Compute Settings

     Change the settings. ``default_org_machine_id`` must name a live
    machine of the org (anything else is the same 404 a missing machine is);
    sent as null it clears the default.

    Args:
        if_match (None | str | Unset): The slot's version as the caller last read it. A write
            whose version no longer matches is refused with a 409; omit it, or send *, to write
            unconditionally.
        body (OrgComputeSettingsUpdate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OrgComputeSettingsRead
    """

    return sync_detailed(
        client=client,
        body=body,
        if_match=if_match,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: OrgComputeSettingsUpdate,
    if_match: None | str | Unset = UNSET,
) -> Response[ErrorEnvelope | OrgComputeSettingsRead]:
    """Put Compute Settings

     Change the settings. ``default_org_machine_id`` must name a live
    machine of the org (anything else is the same 404 a missing machine is);
    sent as null it clears the default.

    Args:
        if_match (None | str | Unset): The slot's version as the caller last read it. A write
            whose version no longer matches is refused with a 409; omit it, or send *, to write
            unconditionally.
        body (OrgComputeSettingsUpdate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OrgComputeSettingsRead]
    """

    kwargs = _get_kwargs(
        body=body,
        if_match=if_match,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: OrgComputeSettingsUpdate,
    if_match: None | str | Unset = UNSET,
) -> ErrorEnvelope | OrgComputeSettingsRead | None:
    """Put Compute Settings

     Change the settings. ``default_org_machine_id`` must name a live
    machine of the org (anything else is the same 404 a missing machine is);
    sent as null it clears the default.

    Args:
        if_match (None | str | Unset): The slot's version as the caller last read it. A write
            whose version no longer matches is refused with a 409; omit it, or send *, to write
            unconditionally.
        body (OrgComputeSettingsUpdate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OrgComputeSettingsRead
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
            if_match=if_match,
        )
    ).parsed
