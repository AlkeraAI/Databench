from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_envelope import ErrorEnvelope
from ...models.preferences_read import PreferencesRead
from ...models.preferences_update import PreferencesUpdate
from ...types import Response


def _get_kwargs(
    *,
    body: PreferencesUpdate,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "patch",
        "url": "/api/v1/me/preferences",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorEnvelope | PreferencesRead | None:
    if response.status_code == 200:
        response_200 = PreferencesRead.from_dict(response.json())

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
) -> Response[ErrorEnvelope | PreferencesRead]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PreferencesUpdate,
) -> Response[ErrorEnvelope | PreferencesRead]:
    """Update My Preferences

     Apply these fields ON TOP of what is stored, and answer with the whole.

    A MERGE, not a replacement — the same rule the daemon's ``preferences.set``
    obeys. Two clients of different ages write this one document, and a replace
    would let the older one, which cannot even name the newer one's field,
    delete a setting the person chose in a client it has never seen.

    A value the schema refuses — a permission mode that is not one, a timeout
    that is not a number — is a 422 naming the field, not a stored value for
    some later reader to trip over. So is a Default Chat Model the catalog does
    not offer (``model_not_offered``), an effort that model does not offer
    (``effort_not_offered``), or a new pick made while the catalog cannot be
    reached to check it (``model_catalog_unavailable``).

    Args:
        body (PreferencesUpdate): The fields to apply. MERGED onto what is stored, never a
            replacement —
            so a key a newer client wrote survives an older client's save.

            ``extra="forbid"`` where the persisted :class:`Preferences` allows extras,
            and the two are not in tension: the STORED document must keep a field it
            cannot name (a newer client wrote it), while a REQUEST that misses the
            envelope has nothing to preserve and everything to lose. A client PATCHing
            the flat ``{"default_permission_mode": …}`` instead of
            ``{"preferences": {…}}`` gets a 422 rather than a 200 with an empty merge
            that silently drops the setting.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | PreferencesRead]
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
    body: PreferencesUpdate,
) -> ErrorEnvelope | PreferencesRead | None:
    """Update My Preferences

     Apply these fields ON TOP of what is stored, and answer with the whole.

    A MERGE, not a replacement — the same rule the daemon's ``preferences.set``
    obeys. Two clients of different ages write this one document, and a replace
    would let the older one, which cannot even name the newer one's field,
    delete a setting the person chose in a client it has never seen.

    A value the schema refuses — a permission mode that is not one, a timeout
    that is not a number — is a 422 naming the field, not a stored value for
    some later reader to trip over. So is a Default Chat Model the catalog does
    not offer (``model_not_offered``), an effort that model does not offer
    (``effort_not_offered``), or a new pick made while the catalog cannot be
    reached to check it (``model_catalog_unavailable``).

    Args:
        body (PreferencesUpdate): The fields to apply. MERGED onto what is stored, never a
            replacement —
            so a key a newer client wrote survives an older client's save.

            ``extra="forbid"`` where the persisted :class:`Preferences` allows extras,
            and the two are not in tension: the STORED document must keep a field it
            cannot name (a newer client wrote it), while a REQUEST that misses the
            envelope has nothing to preserve and everything to lose. A client PATCHing
            the flat ``{"default_permission_mode": …}`` instead of
            ``{"preferences": {…}}`` gets a 422 rather than a 200 with an empty merge
            that silently drops the setting.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | PreferencesRead
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PreferencesUpdate,
) -> Response[ErrorEnvelope | PreferencesRead]:
    """Update My Preferences

     Apply these fields ON TOP of what is stored, and answer with the whole.

    A MERGE, not a replacement — the same rule the daemon's ``preferences.set``
    obeys. Two clients of different ages write this one document, and a replace
    would let the older one, which cannot even name the newer one's field,
    delete a setting the person chose in a client it has never seen.

    A value the schema refuses — a permission mode that is not one, a timeout
    that is not a number — is a 422 naming the field, not a stored value for
    some later reader to trip over. So is a Default Chat Model the catalog does
    not offer (``model_not_offered``), an effort that model does not offer
    (``effort_not_offered``), or a new pick made while the catalog cannot be
    reached to check it (``model_catalog_unavailable``).

    Args:
        body (PreferencesUpdate): The fields to apply. MERGED onto what is stored, never a
            replacement —
            so a key a newer client wrote survives an older client's save.

            ``extra="forbid"`` where the persisted :class:`Preferences` allows extras,
            and the two are not in tension: the STORED document must keep a field it
            cannot name (a newer client wrote it), while a REQUEST that misses the
            envelope has nothing to preserve and everything to lose. A client PATCHing
            the flat ``{"default_permission_mode": …}`` instead of
            ``{"preferences": {…}}`` gets a 422 rather than a 200 with an empty merge
            that silently drops the setting.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorEnvelope | PreferencesRead]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PreferencesUpdate,
) -> ErrorEnvelope | PreferencesRead | None:
    """Update My Preferences

     Apply these fields ON TOP of what is stored, and answer with the whole.

    A MERGE, not a replacement — the same rule the daemon's ``preferences.set``
    obeys. Two clients of different ages write this one document, and a replace
    would let the older one, which cannot even name the newer one's field,
    delete a setting the person chose in a client it has never seen.

    A value the schema refuses — a permission mode that is not one, a timeout
    that is not a number — is a 422 naming the field, not a stored value for
    some later reader to trip over. So is a Default Chat Model the catalog does
    not offer (``model_not_offered``), an effort that model does not offer
    (``effort_not_offered``), or a new pick made while the catalog cannot be
    reached to check it (``model_catalog_unavailable``).

    Args:
        body (PreferencesUpdate): The fields to apply. MERGED onto what is stored, never a
            replacement —
            so a key a newer client wrote survives an older client's save.

            ``extra="forbid"`` where the persisted :class:`Preferences` allows extras,
            and the two are not in tension: the STORED document must keep a field it
            cannot name (a newer client wrote it), while a REQUEST that misses the
            envelope has nothing to preserve and everything to lose. A client PATCHing
            the flat ``{"default_permission_mode": …}`` instead of
            ``{"preferences": {…}}`` gets a 422 rather than a 200 with an empty merge
            that silently drops the setting.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorEnvelope | PreferencesRead
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
