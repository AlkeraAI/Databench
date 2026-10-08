"""The sign-in a daemon method acts as.

One daemon serves every project its editor window opens, and each project may
be pinned to a different org, so a method that reaches the cloud resolves the
profile for the project it names, never one profile for the whole process. A
refusal (a pinned project, an asked-for org with no sign-in) propagates as
``ProfileResolutionError``; the server answers it with the same one-line text
the CLI prints.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from alkera_cli.account.auth_file import Profile
from alkera_cli.account.binding import profile_for_project
from alkera_cli.daemon.server import AuthRequiredError
from alkera_cli.host.paths import existing_project_directory

if TYPE_CHECKING:
    from alkera_core.project import ProjectDirectory


def project_at(project_path: str | None) -> ProjectDirectory | None:
    """The workspace at ``project_path``, or None when it has no ``.alkera/``
    yet (a folder nothing has run in has no pin to hold)."""
    return existing_project_directory(Path(project_path) if project_path else None)


def project_profile(project_path: str | None) -> Profile | None:
    """The profile a call for ``project_path`` acts as, or None signed out."""
    return profile_for_project(project_at(project_path))


def require_project_profile(project_path: str | None) -> Profile:
    """:func:`project_profile`, answering AUTH_REQUIRED when signed out."""
    profile = project_profile(project_path)
    if profile is None or not profile.token:
        raise AuthRequiredError("missing")
    return profile


__all__ = ["project_at", "project_profile", "require_project_profile"]
