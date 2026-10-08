"""The org a project syncs into (`.alkera/cloud.json`).

A project that has sent anything to the cloud (a chat mirror, a knowledge-base
or context sync, team connections, a Files push, a lineage seed) belongs to the
org it first synced into. The binding records that org so a later sign-in to a
different org can never sync this project's data into it: every cloud
operation for the project compares its credential's org with the binding and
refuses before any request when they differ.

The file is written once, by the first sync, and removed only on purpose (a
hidden ``alkera project unpin``). The CLI owns the policy; this module owns the
location and the persisted shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar
from uuid import UUID

from pydantic import ValidationError

from alkera_core.atomic_io import write_text_atomic
from alkera_core.versioning import VersionedModel

CLOUD_BINDING_NAME = "cloud.json"


class CloudBinding(VersionedModel):
    """The API and org a project's cloud data lives in.

    ``org_name`` is for display only (the refusal names it); the comparison is
    on ``api_url`` and ``org_team_id``."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    api_url: str = ""
    org_team_id: str = ""
    org_name: str = ""

    def matches(self, *, api_url: str, org_team_id: str) -> bool:
        """Whether a credential for ``org_team_id`` at ``api_url`` may sync
        this project. An unknown org never matches: the check fails closed."""
        if not self.org_team_id or not org_team_id:
            return False
        return _canonical(self.org_team_id) == _canonical(org_team_id) and self.api_url.rstrip(
            "/"
        ) == api_url.rstrip("/")


def _canonical(value: str) -> str:
    """A UUID in its hyphenated spelling (a token may carry the bare hex form)."""
    try:
        return str(UUID(value.strip()))
    except ValueError:
        return value


class CloudBindingStore:
    """Read, write and remove one project's :class:`CloudBinding`."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> CloudBinding | None:
        """The binding, or ``None`` when the project has never synced.

        An unreadable file is not "unbound": it reads as a binding to no org,
        which matches no credential, so a corrupt pin refuses every sync until
        it is removed on purpose rather than silently re-binding the project."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            return CloudBinding()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return CloudBinding()
        if not isinstance(data, dict):
            return CloudBinding()
        try:
            return CloudBinding.model_validate(data)
        except ValidationError:
            return CloudBinding()

    def write(self, binding: CloudBinding) -> None:
        """Replace the binding atomically."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(
            self._path, json.dumps(binding.model_dump(mode="json"), indent=2, sort_keys=True)
        )

    def clear(self) -> bool:
        """Remove the binding. True when a file was removed."""
        try:
            self._path.unlink()
        except FileNotFoundError:
            return False
        return True


__all__ = ["CLOUD_BINDING_NAME", "CloudBinding", "CloudBindingStore"]
