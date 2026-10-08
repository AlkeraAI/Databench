"""Per-environment build records, kept under the env root (never in the tree).

A record holds what was last built (the spec's hash and its files), who
asked and when, whether the last build failed, and its log. An environment
builds whenever its spec changes; nothing waits for an approval.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alkera_notebook.tree_io import Tree


@dataclass
class BuildRecord:
    #: The spec the build in use was made from, and its files.
    built_spec_hash: str | None = None
    built_spec: dict[str, str] = field(default_factory=dict)
    #: Who asked for that build (an actor's label; empty for a build a
    #: kernel start made) and when it finished (ISO 8601).
    built_by: str = ""
    built_at: str = ""
    #: No usable build exists because the last one failed.
    failed: bool = False
    log: str = ""
    #: Why the last build attempt failed or stopped, kept beside a good
    #: build that is still in use; empty once a build succeeds.
    last_failure: str = ""
    # What uv was asked for (``--python``) and what the build reported.
    python_request: str = ""
    python_build: str = ""
    python_version: str = ""


class BuildRecords:
    def __init__(self, env_root: Path) -> None:
        self._tree = Tree(env_root)

    @staticmethod
    def _path(env_id: str) -> str:
        digest = hashlib.sha256(env_id.encode("utf-8")).hexdigest()[:24]
        return f"records/{digest}.json"

    def get(self, env_id: str) -> BuildRecord:
        try:
            raw: Any = json.loads(self._tree.read_text(self._path(env_id)))
        except (OSError, ValueError):
            return BuildRecord()
        if not isinstance(raw, dict) or raw.get("env_id") != env_id:
            return BuildRecord()
        # A record an earlier release wrote names the build's spec "approved".
        spec = raw.get("built_spec", raw.get("approved_spec"))
        built = raw.get("built_spec_hash", raw.get("approved_spec_hash"))
        return BuildRecord(
            built_spec_hash=built if isinstance(built, str) else None,
            built_spec=(
                {str(k): str(v) for k, v in spec.items()} if isinstance(spec, dict) else {}
            ),
            built_by=str(raw.get("built_by", "")),
            built_at=str(raw.get("built_at", "")),
            failed=bool(raw.get("failed", False)),
            log=str(raw.get("log", "")),
            last_failure=str(raw.get("last_failure", "")),
            python_request=str(raw.get("python_request", "")),
            python_build=str(raw.get("python_build", "")),
            python_version=str(raw.get("python_version", "")),
        )

    def put(self, env_id: str, record: BuildRecord) -> None:
        payload = {
            "env_id": env_id,
            "built_spec_hash": record.built_spec_hash,
            "built_spec": record.built_spec,
            "built_by": record.built_by[:255],
            "built_at": record.built_at,
            "failed": record.failed,
            "log": record.log[-200_000:],
            "last_failure": record.last_failure[:2000],
            "python_request": record.python_request,
            "python_build": record.python_build,
            "python_version": record.python_version,
        }
        self._tree.write_text(self._path(env_id), json.dumps(payload), mode=0o600)
