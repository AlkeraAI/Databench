from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.accepted import Accepted
from ...models.change_environment_api_v1_notebooks_drive_id_item_id_env_action_post_action import (
    ChangeEnvironmentApiV1NotebooksDriveIdItemIdEnvActionPostAction,
)
from ...models.env_change_request import EnvChangeRequest
from ...models.error_envelope import ErrorEnvelope
from ...types import Response


def _get_kwargs(
    drive_id: str,
    item_id: str,
    action: ChangeEnvironmentApiV1NotebooksDriveIdItemIdEnvActionPostAction,
    *,
    body: EnvChangeRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/notebooks/{drive_id}/{item_id}/env/{action}".format(
            drive_id=quote(str(drive_id), safe=""),
            item_id=quote(str(item_id), safe=""),
            action=quote(str(action), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Accepted | ErrorEnvelope | None:
    if response.status_code == 200:
        response_200 = Accepted.from_dict(response.json())

        return response_200

    if response.status_code == 409:
        response_409 = ErrorEnvelope.from_dict(response.json())

        return response_409

    if response.status_code == 422:
        response_422 = ErrorEnvelope.from_dict(response.json())

        return response_422

    if response.status_code == 503:
        response_503 = ErrorEnvelope.from_dict(response.json())

        return response_503

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[Accepted | ErrorEnvelope]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    drive_id: str,
    item_id: str,
    action: ChangeEnvironmentApiV1NotebooksDriveIdItemIdEnvActionPostAction,
    *,
    client: AuthenticatedClient | Client,
    body: EnvChangeRequest,
) -> Response[Accepted | ErrorEnvelope]:
    """Change Environment

     Build the notebook's environment from its spec now, remove
    packages from it, or cancel the build under way, through its kernel. The
    outcome is announced on the notebook's channel (``env.install``, naming
    the ``action``).

    Args:
        drive_id (str):
        item_id (str):
        action (ChangeEnvironmentApiV1NotebooksDriveIdItemIdEnvActionPostAction):
        body (EnvChangeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Accepted | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        action=action,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    drive_id: str,
    item_id: str,
    action: ChangeEnvironmentApiV1NotebooksDriveIdItemIdEnvActionPostAction,
    *,
    client: AuthenticatedClient | Client,
    body: EnvChangeRequest,
) -> Accepted | ErrorEnvelope | None:
    """Change Environment

     Build the notebook's environment from its spec now, remove
    packages from it, or cancel the build under way, through its kernel. The
    outcome is announced on the notebook's channel (``env.install``, naming
    the ``action``).

    Args:
        drive_id (str):
        item_id (str):
        action (ChangeEnvironmentApiV1NotebooksDriveIdItemIdEnvActionPostAction):
        body (EnvChangeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Accepted | ErrorEnvelope
    """

    return sync_detailed(
        drive_id=drive_id,
        item_id=item_id,
        action=action,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    drive_id: str,
    item_id: str,
    action: ChangeEnvironmentApiV1NotebooksDriveIdItemIdEnvActionPostAction,
    *,
    client: AuthenticatedClient | Client,
    body: EnvChangeRequest,
) -> Response[Accepted | ErrorEnvelope]:
    """Change Environment

     Build the notebook's environment from its spec now, remove
    packages from it, or cancel the build under way, through its kernel. The
    outcome is announced on the notebook's channel (``env.install``, naming
    the ``action``).

    Args:
        drive_id (str):
        item_id (str):
        action (ChangeEnvironmentApiV1NotebooksDriveIdItemIdEnvActionPostAction):
        body (EnvChangeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Accepted | ErrorEnvelope]
    """

    kwargs = _get_kwargs(
        drive_id=drive_id,
        item_id=item_id,
        action=action,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    drive_id: str,
    item_id: str,
    action: ChangeEnvironmentApiV1NotebooksDriveIdItemIdEnvActionPostAction,
    *,
    client: AuthenticatedClient | Client,
    body: EnvChangeRequest,
) -> Accepted | ErrorEnvelope | None:
    """Change Environment

     Build the notebook's environment from its spec now, remove
    packages from it, or cancel the build under way, through its kernel. The
    outcome is announced on the notebook's channel (``env.install``, naming
    the ``action``).

    Args:
        drive_id (str):
        item_id (str):
        action (ChangeEnvironmentApiV1NotebooksDriveIdItemIdEnvActionPostAction):
        body (EnvChangeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Accepted | ErrorEnvelope
    """

    return (
        await asyncio_detailed(
            drive_id=drive_id,
            item_id=item_id,
            action=action,
            client=client,
            body=body,
        )
    ).parsed
