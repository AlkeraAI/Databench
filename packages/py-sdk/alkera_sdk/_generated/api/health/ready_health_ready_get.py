from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.ready_status import ReadyStatus
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    strict: bool | Unset = False,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["strict"] = strict

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/health/ready",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | ReadyStatus | None:
    if response.status_code == 200:
        response_200 = ReadyStatus.from_dict(response.json())

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
) -> Response[ErrorEnvelope | ReadyStatus]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    strict: bool | Unset = False,
) -> Response[ErrorEnvelope | ReadyStatus]:
    """Ready

     Readiness — we can talk to the database. 503 if not, once past the grace
    window a task that has been ready is given for a dependency every task
    shares; the body reads ``degraded`` from the first failed probe.

    Asked on a connection of the probe's own, so the answer is about the
    database and not about how busy the tenants' pool is: a task whose pool is
    drained is still in rotation, shedding with retryable 503s, rather than
    pulled by the load balancer so its neighbours drain in turn. The pool's
    level is reported from here as well, because a fully stuck pool has no
    checkout left to report it.

    ``?strict=1`` answers 503 the moment any dependency is unreachable, with
    no grace window: for operators and checks that read the truth now, never
    for a load balancer.

    Args:
        strict (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | ReadyStatus]
    """

    kwargs = _get_kwargs(
        strict=strict,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
    strict: bool | Unset = False,
) -> ErrorEnvelope | ReadyStatus | None:
    """Ready

     Readiness — we can talk to the database. 503 if not, once past the grace
    window a task that has been ready is given for a dependency every task
    shares; the body reads ``degraded`` from the first failed probe.

    Asked on a connection of the probe's own, so the answer is about the
    database and not about how busy the tenants' pool is: a task whose pool is
    drained is still in rotation, shedding with retryable 503s, rather than
    pulled by the load balancer so its neighbours drain in turn. The pool's
    level is reported from here as well, because a fully stuck pool has no
    checkout left to report it.

    ``?strict=1`` answers 503 the moment any dependency is unreachable, with
    no grace window: for operators and checks that read the truth now, never
    for a load balancer.

    Args:
        strict (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | ReadyStatus
    """

    return sync_detailed(
        client=client,
        strict=strict,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    strict: bool | Unset = False,
) -> Response[ErrorEnvelope | ReadyStatus]:
    """Ready

     Readiness — we can talk to the database. 503 if not, once past the grace
    window a task that has been ready is given for a dependency every task
    shares; the body reads ``degraded`` from the first failed probe.

    Asked on a connection of the probe's own, so the answer is about the
    database and not about how busy the tenants' pool is: a task whose pool is
    drained is still in rotation, shedding with retryable 503s, rather than
    pulled by the load balancer so its neighbours drain in turn. The pool's
    level is reported from here as well, because a fully stuck pool has no
    checkout left to report it.

    ``?strict=1`` answers 503 the moment any dependency is unreachable, with
    no grace window: for operators and checks that read the truth now, never
    for a load balancer.

    Args:
        strict (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | ReadyStatus]
    """

    kwargs = _get_kwargs(
        strict=strict,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    strict: bool | Unset = False,
) -> ErrorEnvelope | ReadyStatus | None:
    """Ready

     Readiness — we can talk to the database. 503 if not, once past the grace
    window a task that has been ready is given for a dependency every task
    shares; the body reads ``degraded`` from the first failed probe.

    Asked on a connection of the probe's own, so the answer is about the
    database and not about how busy the tenants' pool is: a task whose pool is
    drained is still in rotation, shedding with retryable 503s, rather than
    pulled by the load balancer so its neighbours drain in turn. The pool's
    level is reported from here as well, because a fully stuck pool has no
    checkout left to report it.

    ``?strict=1`` answers 503 the moment any dependency is unreachable, with
    no grace window: for operators and checks that read the truth now, never
    for a load balancer.

    Args:
        strict (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | ReadyStatus
    """

    return (
        await asyncio_detailed(
            client=client,
            strict=strict,
        )
    ).parsed
