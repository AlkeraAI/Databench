"""Per-PROJECT preferences — persisted to `<workspace>/.alkera/preferences.yml`.

Distinct from the per-USER `~/.alkera/preferences.yml` (`schemas.preferences`):
this file is team-shared (it lives in the repo's `.alkera/`, NOT gitignored) and
holds settings that belong to the project rather than the person. It is edited by
BOTH the CLI and the VS Code extension (via the daemon's `project_preferences.*`
JSON-RPC methods), so — like the user file — it's a `VersionedModel`: `extra=allow`
lets an older reader preserve a newer writer's unknown fields on round-trip.

Every field is defaulted so a missing / partially-corrupt file degrades to defaults
rather than bricking a session.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from alkera_core.versioning import VersionedModel


class ProjectPreferences(VersionedModel):
    """Per-project settings shared by everyone who opens the workspace."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    kb_sync_enabled: bool | None = Field(
        default=None,
        description=(
            "Whether this project's knowledge base syncs with the team's central "
            "store. `None` (the default) INHERITS the org-level setting; `True`/"
            "`False` overrides it for this project. The effective gate is org AND "
            "project — sync runs only when both are on."
        ),
    )


__all__ = ["ProjectPreferences"]
