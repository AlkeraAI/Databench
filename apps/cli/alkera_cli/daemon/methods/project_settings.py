"""Daemon methods for per-project settings.

``project_preferences.get`` / ``project_preferences.set`` read/merge the repo's
``.alkera/preferences.yml`` (the team-shared project file), mirroring the per-user
``preferences.*`` methods but scoped to a project_path.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from alkera_core.schemas.project_preferences import ProjectPreferences

from alkera_cli.app.runtime_pool import runtime_for
from alkera_cli.daemon.protocol import _DaemonModel, method
from alkera_cli.preferences.project import (
    load_project_preferences,
    update_project_preferences,
)

if TYPE_CHECKING:
    from alkera_core.project.directory import ProjectDirectory

    from alkera_cli.daemon.server import JsonRpcServer


def _project_for(server: JsonRpcServer, project_path: str) -> ProjectDirectory:
    return runtime_for(server, project_path).project


# --- project_preferences.get -----------------------------------------------


class ProjectPreferencesGetRequest(_DaemonModel):
    project_path: str


class ProjectPreferencesGetResponse(_DaemonModel):
    preferences: dict[str, Any]


@method("project_preferences.get")
async def project_preferences_get(
    server: JsonRpcServer, params: ProjectPreferencesGetRequest
) -> ProjectPreferencesGetResponse:
    project = _project_for(server, params.project_path)
    loop = asyncio.get_running_loop()
    prefs = await loop.run_in_executor(None, load_project_preferences, project)
    return ProjectPreferencesGetResponse(preferences=prefs.model_dump(mode="json"))


# --- project_preferences.set -----------------------------------------------


class ProjectPreferencesSetRequest(_DaemonModel):
    project_path: str
    preferences: dict[str, Any]
    """The fields to apply. MERGED onto the current file (a key a newer writer
    persisted survives)."""


class ProjectPreferencesSetResponse(_DaemonModel):
    preferences: dict[str, Any]


@method("project_preferences.set")
async def project_preferences_set(
    server: JsonRpcServer, params: ProjectPreferencesSetRequest
) -> ProjectPreferencesSetResponse:
    project = _project_for(server, params.project_path)
    incoming = params.preferences

    def _persist() -> ProjectPreferences:
        def mutate(current: ProjectPreferences) -> ProjectPreferences:
            data = current.model_dump()
            data.update(incoming)
            return ProjectPreferences.model_validate(data)

        return update_project_preferences(project, mutate)

    loop = asyncio.get_running_loop()
    stored = await loop.run_in_executor(None, _persist)
    return ProjectPreferencesSetResponse(preferences=stored.model_dump(mode="json"))


__all__ = [
    "ProjectPreferencesGetRequest",
    "ProjectPreferencesGetResponse",
    "ProjectPreferencesSetRequest",
    "ProjectPreferencesSetResponse",
]
