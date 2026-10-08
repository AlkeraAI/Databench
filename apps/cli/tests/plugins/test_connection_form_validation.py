"""Guardrail: every LIVE-remote plugin that offers a connection form must expose a
validation PROBE capability (RunSQL or IntrospectSchema), so ``validate_connection``
actually hits the remote before activating — instead of silently accepting a bad URL /
token (the bug that let a random Tableau server URL go live).

A pure-FILE form (a DuckDB/SQLite file, a dbt project dir, a Tableau ``.twb``) is the
documented exception: its existence was checked when it was built, so it has no live
surface to probe — those are covered by the path-picker test, not here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alkera_cli.plugins.plugin_base import (
    Connection,
    Environment,
    IntrospectSchemaCapability,
    PluginRegistry,
    RunSQLCapability,
)
from alkera_core.project.directory import ProjectDirectory

# (plugin, minimal attributes a built connection carries). The capability set is constructed
# LAZILY from these attributes (no network), so a probe surface that's missing surfaces here.
_LIVE_REMOTE = [
    pytest.param(
        "postgres", {"host": "h", "port": "5432", "dbname": "d", "user": "u"}, id="postgres"
    ),
    pytest.param("mysql", {"host": "h", "port": "3306", "dbname": "d", "user": "u"}, id="mysql"),
    pytest.param(
        "clickhouse",
        {"host": "h", "port": "8123", "dbname": "default", "user": "default"},
        id="clickhouse",
    ),
    pytest.param(
        "redshift", {"host": "h", "port": "5439", "dbname": "d", "user": "u"}, id="redshift"
    ),
    pytest.param("trino", {"host": "h", "port": "8080", "catalog": "c", "schema": "s"}, id="trino"),
    pytest.param("snowflake", {"account": "acct", "user": "u"}, id="snowflake"),
    pytest.param(
        "databricks",
        {"host": "h", "http_path": "/sql/1.0", "catalog": "c", "schema": "s"},
        id="databricks",
    ),
    pytest.param("bigquery", {"project": "p"}, id="bigquery"),
    pytest.param("generic_sql", {"url": "postgresql://u@h:5432/d"}, id="generic_sql"),
    pytest.param(
        "tableau",
        {"server_url": "https://tableau.example.com", "site": "s", "token_name": "t"},
        id="tableau",
    ),
    pytest.param(
        "airflow", {"api_url": "https://a.example.com/api/v1", "instance": "a"}, id="airflow"
    ),
    # The knowledge plugins are live-remote too: their probe is IntrospectSchema, and
    # without it a bad token would go live and fail on the first sync pass instead.
    pytest.param("notion", {}, id="notion"),
    pytest.param(
        "confluence",
        {
            "server_url": "https://acme.atlassian.net",
            "email": "reader@acme.dev",
            "space_keys": ["ENG"],
        },
        id="confluence",
    ),
    pytest.param("gdocs", {"account": "sync@proj.iam.gserviceaccount.com"}, id="gdocs"),
]


@pytest.mark.parametrize(("plugin", "attributes"), _LIVE_REMOTE)
async def test_live_remote_plugin_exposes_a_validation_probe(
    tmp_path: Path, plugin: str, attributes: dict[str, object]
) -> None:
    project = ProjectDirectory(tmp_path / ".alkera")
    registry = PluginRegistry(project, tmp_path)
    await registry.discover()
    conn = Connection(
        handle=f"{plugin}_t",
        plugin=plugin,
        environment=Environment.DEV,
        attributes=attributes,
    )
    caps = registry.capabilities_for_connection(conn)
    assert caps is not None, f"{plugin} declared no CapabilityProvider — nothing to probe"
    # validate_connection probes RunSQL (SELECT 1) then falls back to IntrospectSchema. A live
    # remote MUST expose one of them or a bogus URL/token is silently accepted.
    has_probe = caps.has(RunSQLCapability) or caps.has(IntrospectSchemaCapability)
    assert has_probe, (
        f"{plugin} offers a live connection form but exposes neither RunSQL nor "
        "IntrospectSchema — validate_connection would no-op and accept a bad connection"
    )
