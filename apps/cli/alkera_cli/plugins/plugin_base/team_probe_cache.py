"""Cached probe evidence for team connections.

A team connection's successful probe is kept beside that row's private
credentials, bound to the row, its credential version and the connection's
non-secret shape, so a later read attaches the evidence only while every one of
those still matches.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from alkera_core.atomic_io import write_json_atomic

from alkera_cli.plugins.plugin_base.connection import Connection
from alkera_cli.plugins.plugin_base.surfaces import (
    PROBE_RESULT_ATTRIBUTE,
    ConnectionProbeResult,
    probe_result_payload,
)

if TYPE_CHECKING:
    from alkera_cli.plugins.plugin_base.team_connections import (
        TeamConnectionRecord,
        TeamMemberState,
    )

_CACHE_PREFIX = "probe-"


def _fingerprint(record: TeamConnectionRecord, conn: Connection) -> str:
    """Bind cached team probe evidence to the current non-secret connection shape."""
    attributes = dict(conn.attributes)
    attributes.pop(PROBE_RESULT_ATTRIBUTE, None)
    document = {
        "plugin": conn.plugin,
        "handle": conn.handle,
        "dialect": conn.dialect,
        "environment": str(conn.environment),
        "urn_namespace": conn.urn_namespace,
        "credential_mode": str(conn.credential_mode),
        "credential_scheme": conn.credential_ref.scheme if conn.credential_ref else "",
        "auth_method": record.auth_method,
        "attributes": attributes,
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def team_probe_cache_path(
    plugins_path: Path,
    record: TeamConnectionRecord,
    member: TeamMemberState,
    conn: Connection,
) -> Path | None:
    """Return a cache path bound to one row, credential version, and shape."""
    from alkera_cli.plugins.plugin_base.team_connections import team_credential_dir

    local_handle = member.local_handle or record.handle
    directory = team_credential_dir(plugins_path, record.plugin, local_handle)
    if directory is None:
        return None
    binding = json.dumps(
        {
            "record_id": record.id,
            "credential_version": record.credential_version,
            "fingerprint": _fingerprint(record, conn),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(binding.encode()).hexdigest()
    return directory / f"{_CACHE_PREFIX}{digest}.json"


def restore_team_probe(
    plugins_path: Path,
    record: TeamConnectionRecord,
    member: TeamMemberState,
    conn: Connection,
) -> None:
    """Attach cached team probe evidence only when every binding input still matches."""
    path = team_probe_cache_path(plugins_path, record, member, conn)
    if path is None:
        return
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    try:
        result = ConnectionProbeResult.model_validate(payload)
    except ValueError:
        return
    conn.attributes[PROBE_RESULT_ATTRIBUTE] = probe_result_payload(result)


def persist_team_probe(
    plugins_path: Path,
    records: Iterable[tuple[TeamConnectionRecord, TeamMemberState]],
    conn: Connection,
    result: ConnectionProbeResult,
) -> None:
    """Persist successful team probe evidence beside that row's private credentials."""
    record_id = conn._runtime_bindings.get("team_record_id", "")
    if not record_id:
        return
    for record, member in records:
        local_handle = member.local_handle or record.handle
        if record.id != record_id or (record.plugin, local_handle) != (conn.plugin, conn.handle):
            continue
        path = team_probe_cache_path(plugins_path, record, member, conn)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            path.parent.chmod(0o700)
        write_json_atomic(path, probe_result_payload(result))
        return


__all__ = ["persist_team_probe", "restore_team_probe", "team_probe_cache_path"]
