from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.team_member_read import TeamMemberRead
from ...types import UNSET, Response, Unset


def _get_kwargs(
    team_id: UUID,
    *,
    include_descendants: bool | Unset = False,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["include_descendants"] = include_descendants

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/teams/{team_id}/members".format(
            team_id=quote(str(team_id), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | list[TeamMemberRead] | None:
    if response.status_code == 200:
        response_200 = []
        _response_200 = response.json()
        for response_200_item_data in _response_200:
            response_200_item = TeamMemberRead.from_dict(response_200_item_data)

            response_200.append(response_200_item)

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
) -> Response[ErrorEnvelope | list[TeamMemberRead]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    team_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    include_descendants: bool | Unset = False,
) -> Response[ErrorEnvelope | list[TeamMemberRead]]:
    """List Team Members

     Everyone who stands on the team: each person with a row on it (their
    direct standing) and each person an admin row above reaches by descent —
    one row per person per team, with the direct role, the descent and the
    standing the two make (see ``TeamMemberRead``). With
    `include_descendants`, the same for every team in the subtree, one team
    after another. Team-admin only (it exposes member emails).

    Args:
        team_id (UUID):
        include_descendants (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | list[TeamMemberRead]]
    """

    kwargs = _get_kwargs(
        team_id=team_id,
        include_descendants=include_descendants,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    team_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    include_descendants: bool | Unset = False,
) -> ErrorEnvelope | list[TeamMemberRead] | None:
    """List Team Members

     Everyone who stands on the team: each person with a row on it (their
    direct standing) and each person an admin row above reaches by descent —
    one row per person per team, with the direct role, the descent and the
    standing the two make (see ``TeamMemberRead``). With
    `include_descendants`, the same for every team in the subtree, one team
    after another. Team-admin only (it exposes member emails).

    Args:
        team_id (UUID):
        include_descendants (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | list[TeamMemberRead]
    """

    return sync_detailed(
        team_id=team_id,
        client=client,
        include_descendants=include_descendants,
    ).parsed


async def asyncio_detailed(
    team_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    include_descendants: bool | Unset = False,
) -> Response[ErrorEnvelope | list[TeamMemberRead]]:
    """List Team Members

     Everyone who stands on the team: each person with a row on it (their
    direct standing) and each person an admin row above reaches by descent —
    one row per person per team, with the direct role, the descent and the
    standing the two make (see ``TeamMemberRead``). With
    `include_descendants`, the same for every team in the subtree, one team
    after another. Team-admin only (it exposes member emails).

    Args:
        team_id (UUID):
        include_descendants (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | list[TeamMemberRead]]
    """

    kwargs = _get_kwargs(
        team_id=team_id,
        include_descendants=include_descendants,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    team_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    include_descendants: bool | Unset = False,
) -> ErrorEnvelope | list[TeamMemberRead] | None:
    """List Team Members

     Everyone who stands on the team: each person with a row on it (their
    direct standing) and each person an admin row above reaches by descent —
    one row per person per team, with the direct role, the descent and the
    standing the two make (see ``TeamMemberRead``). With
    `include_descendants`, the same for every team in the subtree, one team
    after another. Team-admin only (it exposes member emails).

    Args:
        team_id (UUID):
        include_descendants (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | list[TeamMemberRead]
    """

    return (
        await asyncio_detailed(
            team_id=team_id,
            client=client,
            include_descendants=include_descendants,
        )
    ).parsed
