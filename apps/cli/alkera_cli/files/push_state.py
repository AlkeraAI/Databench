"""Where a push keeps its resumable upload sessions, and how it reads and
writes them: one JSON file per ``(tree, destination)`` under the CLI home."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

from alkera_core.project import write_json_atomic
from alkera_core.versioning import VersionedModel
from pydantic import Field, ValidationError, model_validator

from alkera_cli.host import paths as cli_paths


class PushState(VersionedModel):
    """The open upload sessions of one push, keyed by the file each uploads.

    A session is the server's ``uploadId`` and ``partSize`` plus the
    ``contentHash`` and ``size`` it was opened for; it stays an open mapping so
    a field a newer build adds rides through an older one untouched.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    sessions: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _camel_stamp(cls, data: Any) -> Any:
        """Builds before this model stamped the version as ``schemaVersion``,
        on a file of this same shape: read that stamp as the version."""
        if isinstance(data, dict) and "schemaVersion" in data and "schema_version" not in data:
            data = dict(data)
            data["schema_version"] = data.pop("schemaVersion")
        return data

    @model_validator(mode="before")
    @classmethod
    def _drop_unreadable_sessions(cls, data: Any) -> Any:
        """One session that is not a mapping is left behind; the others resume."""
        if isinstance(data, dict) and isinstance(data.get("sessions"), dict):
            data = dict(data)
            data["sessions"] = {
                str(key): value
                for key, value in data["sessions"].items()
                if isinstance(value, dict)
            }
        return data


def push_state_path(root: Path, dest: str, *, home: Path | None = None) -> Path:
    """Where this ``(root, destination)`` pair's resumable sessions live.

    Keyed by a digest of both, so two pushes of the same tree to two org paths
    never share a session and the file name carries no path bytes a filesystem
    could refuse.
    """
    material = os.fsencode(root.resolve()) + b"\0" + dest.encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()
    base = home if home is not None else cli_paths.ALKERA_HOME
    return base / "files" / "pushes" / f"{digest}.json"


def load_state(state_file: Path) -> dict[str, dict[str, Any]]:
    """The sessions this push left open, or none when the file is missing or
    unreadable (a push with no sessions simply opens new ones)."""
    try:
        raw = json.loads(state_file.read_text(encoding="utf-8"))
        return dict(PushState.model_validate(raw).sessions)
    except (OSError, ValueError, ValidationError):
        return {}


def save_state(state_file: Path, sessions: Mapping[str, dict[str, Any]]) -> None:
    """Persist the open sessions atomically and fsynced, readable by this user
    alone — a torn state file would make a resume re-upload the tree, which is
    the failure this file exists to stop."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(state_file, PushState(sessions=dict(sessions)).model_dump(mode="json"))


__all__ = ["PushState", "load_state", "push_state_path", "save_state"]
