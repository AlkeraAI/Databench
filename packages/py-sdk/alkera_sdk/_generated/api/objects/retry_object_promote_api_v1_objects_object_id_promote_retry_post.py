from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.workspace_object_read import WorkspaceObjectRead
from ...types import Response


def _get_kwargs(
    object_id: UUID,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/objects/{object_id}/promote/retry".format(
            object_id=quote(str(object_id), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | WorkspaceObjectRead | None:
    if response.status_code == 200:
        response_200 = WorkspaceObjectRead.from_dict(response.json())

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
) -> Response[ErrorEnvelope | WorkspaceObjectRead]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    object_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | WorkspaceObjectRead]:
    """Retry Object Promote

     Ask the machine for this result's payload again.

    A promote whose machine never answered ends as ``failed`` with a reason (the
    deadline sweep), and the reader is on the object's own page by then. Without
    this they would have to go back to a chat whose card may be pages up, or
    press Save on a card that now answers with the object they already have —
    the create is idempotent on ``promote:<chat>:<event>``, so a second promote
    lands on this very row and relays nothing.

    So the retry lives here: the object already knows the chat it came out of
    and the transcript event it came from, and the machine checks both before
    honouring the relay. Authorised as the delivery is (``UPLOAD_PAYLOAD``):
    whoever may deliver the payload may ask for it again. Accepted only on a
    FAILED result — one still waiting has an outstanding relay, and one that is
    ready has its rows.

    Args:
        object_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | WorkspaceObjectRead]
    """

    kwargs = _get_kwargs(
        object_id=object_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    object_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | WorkspaceObjectRead | None:
    """Retry Object Promote

     Ask the machine for this result's payload again.

    A promote whose machine never answered ends as ``failed`` with a reason (the
    deadline sweep), and the reader is on the object's own page by then. Without
    this they would have to go back to a chat whose card may be pages up, or
    press Save on a card that now answers with the object they already have —
    the create is idempotent on ``promote:<chat>:<event>``, so a second promote
    lands on this very row and relays nothing.

    So the retry lives here: the object already knows the chat it came out of
    and the transcript event it came from, and the machine checks both before
    honouring the relay. Authorised as the delivery is (``UPLOAD_PAYLOAD``):
    whoever may deliver the payload may ask for it again. Accepted only on a
    FAILED result — one still waiting has an outstanding relay, and one that is
    ready has its rows.

    Args:
        object_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | WorkspaceObjectRead
    """

    return sync_detailed(
        object_id=object_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    object_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> Response[ErrorEnvelope | WorkspaceObjectRead]:
    """Retry Object Promote

     Ask the machine for this result's payload again.

    A promote whose machine never answered ends as ``failed`` with a reason (the
    deadline sweep), and the reader is on the object's own page by then. Without
    this they would have to go back to a chat whose card may be pages up, or
    press Save on a card that now answers with the object they already have —
    the create is idempotent on ``promote:<chat>:<event>``, so a second promote
    lands on this very row and relays nothing.

    So the retry lives here: the object already knows the chat it came out of
    and the transcript event it came from, and the machine checks both before
    honouring the relay. Authorised as the delivery is (``UPLOAD_PAYLOAD``):
    whoever may deliver the payload may ask for it again. Accepted only on a
    FAILED result — one still waiting has an outstanding relay, and one that is
    ready has its rows.

    Args:
        object_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | WorkspaceObjectRead]
    """

    kwargs = _get_kwargs(
        object_id=object_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    object_id: UUID,
    *,
    client: AuthenticatedClient | Client,
) -> ErrorEnvelope | WorkspaceObjectRead | None:
    """Retry Object Promote

     Ask the machine for this result's payload again.

    A promote whose machine never answered ends as ``failed`` with a reason (the
    deadline sweep), and the reader is on the object's own page by then. Without
    this they would have to go back to a chat whose card may be pages up, or
    press Save on a card that now answers with the object they already have —
    the create is idempotent on ``promote:<chat>:<event>``, so a second promote
    lands on this very row and relays nothing.

    So the retry lives here: the object already knows the chat it came out of
    and the transcript event it came from, and the machine checks both before
    honouring the relay. Authorised as the delivery is (``UPLOAD_PAYLOAD``):
    whoever may deliver the payload may ask for it again. Accepted only on a
    FAILED result — one still waiting has an outstanding relay, and one that is
    ready has its rows.

    Args:
        object_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | WorkspaceObjectRead
    """

    return (
        await asyncio_detailed(
            object_id=object_id,
            client=client,
        )
    ).parsed
