"""Fixtures shared by the cloud-sync suites."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from _helpers.team_conn import LeasingBackend, rec
from alkera_cli.plugins.plugin_base.team_connections import (
    TeamConnectionsState,
    TeamConnectionsStore,
)
from alkera_core.project.directory import ProjectDirectory

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from alkera_cli.plugins.plugin_base.team_connections import TeamConnectionRecord


@pytest.fixture(autouse=True)
def _isolated_shared_lease_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test its own leased-credential cache.

    The cache is a process global, so without this one test's primed lease is
    served to the next and an invalidation assertion can pass for the wrong
    reason."""
    from alkera_cli.cloud_sync import shared_lease

    monkeypatch.setattr(shared_lease, "_cache", {})


@pytest.fixture
def leasing(monkeypatch: pytest.MonkeyPatch) -> LeasingBackend:
    """A signed-in member whose shared-credential leases all go to one counted
    backend."""
    backend = LeasingBackend()
    monkeypatch.setattr("alkera_cli.cloud_sync.client.resolve_connections_client", backend.client)
    return backend


@pytest.fixture
def team_project(tmp_path: Path) -> Callable[..., ProjectDirectory]:
    """A workspace whose team-connections store already holds ``records`` — the
    state a member's machine is in once the sync lane has run."""

    def _make(
        *records: TeamConnectionRecord, member: dict[str, Any] | None = None
    ) -> ProjectDirectory:
        project = ProjectDirectory(tmp_path / ".alkera")
        TeamConnectionsStore(project.team_connections_path).save(
            TeamConnectionsState(records={r.id: r for r in records}, member=dict(member or {}))
        )
        return project

    return _make


@pytest.fixture
def oauth_rec() -> Callable[..., TeamConnectionRecord]:
    """A per-user browser-sign-in row: the admin settled the shape and every
    member brings their own session."""
    shapes: dict[str, dict[str, Any]] = {
        "snowflake": {"handle": "snow", "shared_values": {"account": "acme"}},
        "bigquery": {"handle": "bq", "shared_values": {"project": "acme"}},
    }

    def _make(
        plugin: str = "snowflake", record_id: str = "r1", **over: Any
    ) -> TeamConnectionRecord:
        return rec(
            record_id,
            plugin=plugin,
            auth_mode="per_user",
            auth_method="oauth",
            has_shared_secret=False,
            **{**shapes[plugin], **over},
        )

    return _make
