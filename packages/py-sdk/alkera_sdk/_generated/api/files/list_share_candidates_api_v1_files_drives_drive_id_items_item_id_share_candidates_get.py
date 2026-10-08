from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.share_candidate_list import ShareCandidateList
from ...types import UNSET, Response, Unset


def _get_kwargs(
    drive_id: UUID,
    item_id: UUID,
    *,
    q: str | Unset = "",
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["q"] = q

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/files/drives/{drive_id}/items/{item_id}/share-candidates".format(
            drive_id=quote(str(drive_id), safe=""),
            item_id=quote(str(item_id), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | ShareCandidateList | None:
    if response.status_code == 200:
        response_200 = ShareCandidateList.from_dict(response.json())

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
) -> Response[ErrorEnvelope | ShareCandidateList]:
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
    q: str | Unset = "",
) -> Response[ErrorEnvelope | ShareCandidateList]:
    """List Share Candidates

     The org's people and teams matching ``q``, for a caller who may share this node.

    Decided as a share of the node, because that is the one thing the answer is
    for: whoever may add a grant here may name any principal the grant route
    admits, and that set is exactly what this returns. A caller who may only
    read the node gets the visible refusal the grant route would give them, and
    a caller who cannot read it the opaque not-found, so the read is no wider a
    window onto the directory than the share button already is. ``q`` is
    unbounded on the wire and cut in the service, so a stranger's drive id is
    refused by the policy before anything about the query is looked at. The
    node's owner is left out.

    Args:
        drive_id (UUID):
        item_id (UUID):
        q (str | Unset):  Default: ''.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | ShareCandidateList]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        q=q,
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
    q: str | Unset = "",
) -> ErrorEnvelope | ShareCandidateList | None:
    """List Share Candidates

     The org's people and teams matching ``q``, for a caller who may share this node.

    Decided as a share of the node, because that is the one thing the answer is
    for: whoever may add a grant here may name any principal the grant route
    admits, and that set is exactly what this returns. A caller who may only
    read the node gets the visible refusal the grant route would give them, and
    a caller who cannot read it the opaque not-found, so the read is no wider a
    window onto the directory than the share button already is. ``q`` is
    unbounded on the wire and cut in the service, so a stranger's drive id is
    refused by the policy before anything about the query is looked at. The
    node's owner is left out.

    Args:
        drive_id (UUID):
        item_id (UUID):
        q (str | Unset):  Default: ''.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | ShareCandidateList
    """

    return sync_detailed(
        drive_id=drive_id,
        item_id=item_id,
        client=client,
        q=q,
    ).parsed


async def asyncio_detailed(
    drive_id: UUID,
    item_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    q: str | Unset = "",
) -> Response[ErrorEnvelope | ShareCandidateList]:
    """List Share Candidates

     The org's people and teams matching ``q``, for a caller who may share this node.

    Decided as a share of the node, because that is the one thing the answer is
    for: whoever may add a grant here may name any principal the grant route
    admits, and that set is exactly what this returns. A caller who may only
    read the node gets the visible refusal the grant route would give them, and
    a caller who cannot read it the opaque not-found, so the read is no wider a
    window onto the directory than the share button already is. ``q`` is
    unbounded on the wire and cut in the service, so a stranger's drive id is
    refused by the policy before anything about the query is looked at. The
    node's owner is left out.

    Args:
        drive_id (UUID):
        item_id (UUID):
        q (str | Unset):  Default: ''.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | ShareCandidateList]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        q=q,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: UUID,
    item_id: UUID,
    *,
    client: AuthenticatedClient | Client,
    q: str | Unset = "",
) -> ErrorEnvelope | ShareCandidateList | None:
    """List Share Candidates

     The org's people and teams matching ``q``, for a caller who may share this node.

    Decided as a share of the node, because that is the one thing the answer is
    for: whoever may add a grant here may name any principal the grant route
    admits, and that set is exactly what this returns. A caller who may only
    read the node gets the visible refusal the grant route would give them, and
    a caller who cannot read it the opaque not-found, so the read is no wider a
    window onto the directory than the share button already is. ``q`` is
    unbounded on the wire and cut in the service, so a stranger's drive id is
    refused by the policy before anything about the query is looked at. The
    node's owner is left out.

    Args:
        drive_id (UUID):
        item_id (UUID):
        q (str | Unset):  Default: ''.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | ShareCandidateList
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            client=client,
            q=q,
        )
    ).parsed
