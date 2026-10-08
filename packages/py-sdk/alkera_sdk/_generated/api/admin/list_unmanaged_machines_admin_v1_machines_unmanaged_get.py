from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.unmanaged_machine_list import UnmanagedMachineList
from ...types import Response


def _get_kwargs() -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/admin/v1/machines/unmanaged",
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> UnmanagedMachineList | None:
    if response.status_code == 200:
        response_200 = UnmanagedMachineList.from_dict(response.json())

        return response_200

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[UnmanagedMachineList]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[UnmanagedMachineList]:
    """List Unmanaged Machines

     The machines each configured provider holds that no allocation row owns
    (a box started by hand, or one a lost create left behind). Read-only: the
    plane never acts on a machine it has no row for, and neither does this page.
    A provider that cannot be listed is named in ``unavailable``, never read as
    holding nothing.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[UnmanagedMachineList]
    """

    kwargs = _get_kwargs()

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
) -> UnmanagedMachineList | None:
    """List Unmanaged Machines

     The machines each configured provider holds that no allocation row owns
    (a box started by hand, or one a lost create left behind). Read-only: the
    plane never acts on a machine it has no row for, and neither does this page.
    A provider that cannot be listed is named in ``unavailable``, never read as
    holding nothing.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        UnmanagedMachineList
    """

    return sync_detailed(
        client=client,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[UnmanagedMachineList]:
    """List Unmanaged Machines

     The machines each configured provider holds that no allocation row owns
    (a box started by hand, or one a lost create left behind). Read-only: the
    plane never acts on a machine it has no row for, and neither does this page.
    A provider that cannot be listed is named in ``unavailable``, never read as
    holding nothing.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[UnmanagedMachineList]
    """

    kwargs = _get_kwargs()

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
) -> UnmanagedMachineList | None:
    """List Unmanaged Machines

     The machines each configured provider holds that no allocation row owns
    (a box started by hand, or one a lost create left behind). Read-only: the
    plane never acts on a machine it has no row for, and neither does this page.
    A provider that cannot be listed is named in ``unavailable``, never read as
    holding nothing.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        UnmanagedMachineList
    """

    return (
        await asyncio_detailed(
            client=client,
        )
    ).parsed
