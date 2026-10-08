from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.offering_admin_read import OfferingAdminRead
from ...models.offering_update import OfferingUpdate
from ...types import Response


def _get_kwargs(
    offering_id: UUID,
    *,
    body: OfferingUpdate,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "patch",
        "url": "/admin/v1/compute/offerings/{offering_id}".format(
            offering_id=quote(str(offering_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | OfferingAdminRead | None:
    if response.status_code == 200:
        response_200 = OfferingAdminRead.from_dict(response.json())

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
) -> Response[ErrorEnvelope | OfferingAdminRead]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    offering_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: OfferingUpdate,
) -> Response[ErrorEnvelope | OfferingAdminRead]:
    """Update Offering

    Args:
        offering_id (UUID):
        body (OfferingUpdate): A change to an offering; fields not sent are left as they are.
            ``retired`` true retires it, false brings it back. Platform only.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OfferingAdminRead]
    """

    kwargs = _get_kwargs(
        offering_id=offering_id,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    offering_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: OfferingUpdate,
) -> ErrorEnvelope | OfferingAdminRead | None:
    """Update Offering

    Args:
        offering_id (UUID):
        body (OfferingUpdate): A change to an offering; fields not sent are left as they are.
            ``retired`` true retires it, false brings it back. Platform only.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OfferingAdminRead
    """

    return sync_detailed(
        offering_id=offering_id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    offering_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: OfferingUpdate,
) -> Response[ErrorEnvelope | OfferingAdminRead]:
    """Update Offering

    Args:
        offering_id (UUID):
        body (OfferingUpdate): A change to an offering; fields not sent are left as they are.
            ``retired`` true retires it, false brings it back. Platform only.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | OfferingAdminRead]
    """

    kwargs = _get_kwargs(
        offering_id=offering_id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    offering_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    body: OfferingUpdate,
) -> ErrorEnvelope | OfferingAdminRead | None:
    """Update Offering

    Args:
        offering_id (UUID):
        body (OfferingUpdate): A change to an offering; fields not sent are left as they are.
            ``retired`` true retires it, false brings it back. Platform only.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | OfferingAdminRead
    """

    return (
        await asyncio_detailed(
            offering_id=offering_id,
            client=client,
            body=body,
        )
    ).parsed
