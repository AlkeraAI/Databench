from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.chart_export_request import ChartExportRequest
from ...models.error_envelope import ErrorEnvelope
from ...types import Response


def _get_kwargs(
    *,
    body: ChartExportRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/charts/export",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | None:
    if response.status_code == 422:
        response_422 = ErrorEnvelope.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ErrorEnvelope]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ChartExportRequest,
) -> Response[ErrorEnvelope]:
    """Export Chart

     Render ``body.spec`` with the Alkera theme. A spec the profile refuses is
    a 422 naming the offending path, never a value from the spec.

    Args:
        body (ChartExportRequest): A chart to render. ``spec`` is an Alkera chart profile spec
            with its
            rows inline; ``scale`` multiplies a PNG's pixel size.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope]
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
    body: ChartExportRequest,
) -> ErrorEnvelope | None:
    """Export Chart

     Render ``body.spec`` with the Alkera theme. A spec the profile refuses is
    a 422 naming the offending path, never a value from the spec.

    Args:
        body (ChartExportRequest): A chart to render. ``spec`` is an Alkera chart profile spec
            with its
            rows inline; ``scale`` multiplies a PNG's pixel size.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ChartExportRequest,
) -> Response[ErrorEnvelope]:
    """Export Chart

     Render ``body.spec`` with the Alkera theme. A spec the profile refuses is
    a 422 naming the offending path, never a value from the spec.

    Args:
        body (ChartExportRequest): A chart to render. ``spec`` is an Alkera chart profile spec
            with its
            rows inline; ``scale`` multiplies a PNG's pixel size.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ChartExportRequest,
) -> ErrorEnvelope | None:
    """Export Chart

     Render ``body.spec`` with the Alkera theme. A spec the profile refuses is
    a 422 naming the offending path, never a value from the spec.

    Args:
        body (ChartExportRequest): A chart to render. ``spec`` is an Alkera chart profile spec
            with its
            rows inline; ``scale`` multiplies a PNG's pixel size.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
