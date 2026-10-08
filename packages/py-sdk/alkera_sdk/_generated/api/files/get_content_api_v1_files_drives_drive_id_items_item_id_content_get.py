from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...types import Response


def _get_kwargs(
    drive_id: str,
    item_id: str,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/files/drives/{drive_id}/items/{item_id}/content".format(
            drive_id=quote(str(drive_id), safe=""),
            item_id=quote(str(item_id), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Any | ErrorEnvelope | None:
    if response.status_code == 302:
        response_302 = cast(Any, None)
        return response_302

    if response.status_code == 409:
        response_409 = ErrorEnvelope.from_dict(response.json())

        return response_409

    if response.status_code == 422:
        response_422 = ErrorEnvelope.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[Any | ErrorEnvelope]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
) -> Response[Any | ErrorEnvelope]:
    """Get Content

     Redirect to a signed, single-use URL for this node's head version.

    The target lives on the content domain and carries the whole credential; a
    ``Range`` on this request is folded into the grant, so the signed URL can
    only ever fetch the span it was minted for.

    A node whose content is *derived* has no head version to mint against, so
    it is served inline from here instead — see :func:`rendered_content`.

    Args:
        drive_id (str):
        item_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
) -> Any | ErrorEnvelope | None:
    """Get Content

     Redirect to a signed, single-use URL for this node's head version.

    The target lives on the content domain and carries the whole credential; a
    ``Range`` on this request is folded into the grant, so the signed URL can
    only ever fetch the span it was minted for.

    A node whose content is *derived* has no head version to mint against, so
    it is served inline from here instead — see :func:`rendered_content`.

    Args:
        drive_id (str):
        item_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ErrorEnvelope
    """

    return sync_detailed(
        drive_id=drive_id,
        item_id=item_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
) -> Response[Any | ErrorEnvelope]:
    """Get Content

     Redirect to a signed, single-use URL for this node's head version.

    The target lives on the content domain and carries the whole credential; a
    ``Range`` on this request is folded into the grant, so the signed URL can
    only ever fetch the span it was minted for.

    A node whose content is *derived* has no head version to mint against, so
    it is served inline from here instead — see :func:`rendered_content`.

    Args:
        drive_id (str):
        item_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: str,
    item_id: str,
    *,
    client: AuthenticatedClient | Client,
) -> Any | ErrorEnvelope | None:
    """Get Content

     Redirect to a signed, single-use URL for this node's head version.

    The target lives on the content domain and carries the whole credential; a
    ``Range`` on this request is folded into the grant, so the signed URL can
    only ever fetch the span it was minted for.

    A node whose content is *derived* has no head version to mint against, so
    it is served inline from here instead — see :func:`rendered_content`.

    Args:
        drive_id (str):
        item_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            client=client,
        )
    ).parsed
