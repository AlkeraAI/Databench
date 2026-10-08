"""Persisted per-PROJECT preferences — `<workspace>/.alkera/preferences.yml`.

The project-level counterpart to `preferences/user.py` (which owns the per-user
`~/.alkera/preferences.yml`). Same concurrency contract — both the CLI and the
daemon (on behalf of the VS Code extension) edit this file — so writes funnel
through ONE concurrency-safe writer:

- `load_project_preferences()` is a lock-free best-effort read (`write_text_atomic`
  swaps via rename, so a reader sees the old or new file whole). Missing / corrupt
  files degrade to defaults.
- `update_project_preferences()` holds the sidecar lock (`threading.Lock` +
  `FileLock`) across the whole read-modify-write so the daemon's thread pool and a
  concurrent CLI can't lose each other's changes. `ProjectPreferences` is a
  `VersionedModel` (`extra="allow"`), so a writer preserves fields a newer peer
  added but this version doesn't know about.

The schema lives in `alkera_core.schemas.project_preferences`; api-core owns the
file LOCATION (`ProjectDirectory.preferences_path`), this module owns the I/O.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import yaml
from alkera_core.project import write_text_atomic
from alkera_core.project.directory import ProjectDirectory
from alkera_core.project.sidecar_lock import sidecar_lock
from alkera_core.schemas.project_preferences import ProjectPreferences


def read_project_preferences(project: ProjectDirectory) -> ProjectPreferences | None:
    """The project's stored preferences, defaults when there is no file to read, and
    ``None`` when a file is there and this build cannot read it.

    The distinction is kept because a caller may have to fail closed on it: a setting
    that says a project's knowledge stays on this machine must not be erased by the file
    holding it going unreadable, and "unset" and "unreadable" are the same object once
    both have degraded to defaults. Never raises."""
    path = project.preferences_path
    if not path.exists():
        return ProjectPreferences()
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return ProjectPreferences.model_validate(data)
    except Exception:  # ValidationError + anything weird
        return None


def load_project_preferences(project: ProjectDirectory) -> ProjectPreferences:
    """Return the project's persisted preferences, or defaults if the file is
    missing / unreadable / corrupt. Never raises, never returns None."""
    stored = read_project_preferences(project)
    return stored if stored is not None else ProjectPreferences()


def update_project_preferences(
    project: ProjectDirectory,
    mutate: Callable[[ProjectPreferences], ProjectPreferences],
) -> ProjectPreferences:
    """Lock-guarded read-modify-write. `mutate` receives the current preferences and
    returns the new object; the sidecar lock is held across the read AND the write so
    a concurrent writer can't clobber the result."""
    path = project.preferences_path
    with sidecar_lock(path):
        updated = mutate(load_project_preferences(project))
        text = yaml.safe_dump(updated.model_dump(mode="json"), sort_keys=False)
        write_text_atomic(path, text)
        return updated


__all__ = [
    "load_project_preferences",
    "read_project_preferences",
    "update_project_preferences",
]
