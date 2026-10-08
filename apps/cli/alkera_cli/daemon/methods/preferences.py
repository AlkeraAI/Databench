"""Daemon methods for user preferences — `preferences.get` / `preferences.set`.

The VS Code extension reads/writes `~/.alkera/preferences.yml` through the
daemon (the same path auth takes) so that the CLI and the extension share
ONE concurrency-safe writer: `alkera_cli.preferences.user`, which holds a
`FileLock` across each read-modify-write.

`preferences.set` MERGES the provided fields onto the current file rather
than replacing it wholesale — so a field present on disk but absent from
the caller's payload (e.g. a key a newer CLI wrote that the extension's
typed client doesn't know about) is preserved.
"""

from __future__ import annotations

import asyncio
from functools import partial
from typing import TYPE_CHECKING, Any

from alkera_core.schemas.preferences import Preferences

from alkera_cli.account.auth_file import ProfileResolutionError
from alkera_cli.daemon.profiles import project_at, project_profile
from alkera_cli.daemon.protocol import _DaemonModel, method
from alkera_cli.gateway.client import (
    GatewayUnavailableError,
    fetch_models,
    selectable_models,
)
from alkera_cli.host.config import get_settings
from alkera_cli.observability.telemetry import reconcile_sentry
from alkera_cli.preferences.chat_defaults import resolve_and_persist_chat_defaults
from alkera_cli.preferences.user import load_preferences, preferences_org, update_preferences

if TYPE_CHECKING:
    from alkera_cli.daemon.server import JsonRpcServer


# --- preferences.get -------------------------------------------------------


class PreferencesGetRequest(_DaemonModel):
    project_path: str | None = None
    """The project whose sign-in's org the catalog-scoped preferences are read
    for (the daemon's current sign-in when None)."""


class PreferencesGetResponse(_DaemonModel):
    preferences: dict[str, Any]


@method("preferences.get")
async def preferences_get(
    server: JsonRpcServer, params: PreferencesGetRequest
) -> PreferencesGetResponse:
    loop = asyncio.get_running_loop()
    org_id = preferences_org(project_at(params.project_path))
    prefs = await loop.run_in_executor(None, load_preferences, org_id)
    return PreferencesGetResponse(preferences=prefs.model_dump(mode="json"))


# --- preferences.set -------------------------------------------------------


class PreferencesSetRequest(_DaemonModel):
    preferences: dict[str, Any]
    """The preference fields to apply. Merged onto the current file."""
    project_path: str | None = None
    """The project whose sign-in's org takes the catalog-scoped fields (the
    daemon's current sign-in when None)."""


class PreferencesSetResponse(_DaemonModel):
    preferences: dict[str, Any]
    """The full stored preferences after the merge."""


@method("preferences.set")
async def preferences_set(
    server: JsonRpcServer, params: PreferencesSetRequest
) -> PreferencesSetResponse:
    incoming = params.preferences
    org_id = preferences_org(project_at(params.project_path))

    def _persist() -> Preferences:
        def mutate(current: Preferences) -> Preferences:
            data = current.model_dump()
            data.update(incoming)
            return Preferences.model_validate(data)

        return update_preferences(mutate, org_id=org_id)

    loop = asyncio.get_running_loop()
    stored = await loop.run_in_executor(None, _persist)
    # A telemetry toggle must take effect on the long-running daemon immediately
    # (not only on its next start), so re-gate the daemon's own Sentry now that
    # the new `telemetry_enabled` is on disk. Idempotent for any other change.
    await loop.run_in_executor(None, reconcile_sentry, "daemon")
    return PreferencesSetResponse(preferences=stored.model_dump(mode="json"))


# --- preferences.resolve_chat_defaults -------------------------------------


class ResolveChatDefaultsRequest(_DaemonModel):
    """The resolution reads the saved prefs + the gateway catalog as
    ``project_path``'s sign-in (the daemon's when None)."""

    project_path: str | None = None


class ResolveChatDefaultsResponse(_DaemonModel):
    model: str | None = None
    """The model id a NEW chat should seed with (None when the catalog is empty
    AND nothing was ever saved)."""
    effort: str | None = None
    """The reasoning effort to seed with (None when the model has no variants)."""


@method("preferences.resolve_chat_defaults")
async def preferences_resolve_chat_defaults(
    server: JsonRpcServer, params: ResolveChatDefaultsRequest
) -> ResolveChatDefaultsResponse:
    """Resolve the new-chat Default Model + Effort against the live gateway catalog.

    The editor seeds a fresh composer from this. Shares ONE rule set with the TUI
    (`alkera_cli.preferences.chat_defaults.resolve_chat_defaults`). When the catalog is reachable
    and the saved default is missing/stale, the correction is persisted so the value
    converges; when the gateway is down (or the user is signed out) the saved values
    are returned untouched — a transient outage never wipes the user's default.
    """
    loop = asyncio.get_running_loop()
    try:
        auth = project_profile(params.project_path)
    except ProfileResolutionError:
        auth = None
    # The saved default is the copy kept for the org this sign-in acts in: a
    # model another org's catalog offers is never this org's seed.
    org_id = auth.org_team_id if auth is not None and auth.org_team_id else None
    prefs = await loop.run_in_executor(None, load_preferences, org_id)
    saved = ResolveChatDefaultsResponse(
        model=prefs.default_chat_model, effort=prefs.default_chat_effort
    )
    if auth is None or not auth.token:
        return saved
    try:
        catalog = await fetch_models(
            gateway_url=get_settings().alkera_gateway_url, token=auth.token
        )
    except GatewayUnavailableError:
        return saved

    selectable = selectable_models(catalog)
    resolved = await loop.run_in_executor(
        None, partial(resolve_and_persist_chat_defaults, selectable, org_id=org_id)
    )
    return ResolveChatDefaultsResponse(model=resolved.model_id, effort=resolved.effort)


__all__ = [
    "PreferencesGetRequest",
    "PreferencesGetResponse",
    "PreferencesSetRequest",
    "PreferencesSetResponse",
    "ResolveChatDefaultsRequest",
    "ResolveChatDefaultsResponse",
]
