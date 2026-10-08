"""Connection compat contract: a 1.0.0 document reads with safe defaults, the
credential_mode/enabled fields round-trip, and a newer writer's unknowns survive.
The 2.0.0 removal of ``max_effect`` is backward-compatible (a pre-2.0.0 document
carrying it still loads — the value lands in the extra-allow bag, ignored).
(The full historical corpus is exercised by ``test_plugin_lineage.py``.)"""

from __future__ import annotations

import pytest
from alkera_cli.plugins.plugin_base import (
    Connection,
    CredentialMode,
    CredentialRef,
    Environment,
)


def test_defaults_are_todays_behavior() -> None:
    """An unannotated connection is a shared-credential, enabled one — exactly
    the pre-1.1.0 semantics, so nothing changes for existing stores."""
    conn = Connection(handle="h", plugin="p")
    assert conn.credential_mode is CredentialMode.SHARED
    assert conn.enabled is True


def test_v1_0_0_document_reads_with_defaults_and_reemits_current() -> None:
    payload = {
        "schema_version": "1.0.0",
        "handle": "snow_prod",
        "plugin": "snowflake",
        "dialect": "snowflake",
        "environment": "prod",
        "max_effect": "write",
        "urn_namespace": "snowflake://acct_x",
        "credential_ref": {"scheme": "keychain", "locator": "alkera/snow_prod"},
        "attributes": {"account": "acct_x"},
    }
    conn = Connection.model_validate(payload)
    assert conn.credential_mode is CredentialMode.SHARED
    assert conn.enabled is True
    dumped = conn.model_dump(mode="json")
    assert dumped["schema_version"] == Connection.SCHEMA_VERSION
    assert dumped["credential_mode"] == "shared"
    assert dumped["enabled"] is True


def test_v1_1_0_document_migration_strips_max_effect() -> None:
    """A real 1.1.0 document (``max_effect`` present — it was added in 1.1.0) migrates to
    2.0.0: the MIGRATIONS ladder STRIPS ``max_effect`` (not left lingering in the extra
    bag) and re-stamps the version. Pins the 1.1.0→2.0.0 rung the corpus fixture
    ``connection/legacy_1_1_0.json`` also exercises."""
    payload = {
        "schema_version": "1.1.0",
        "handle": "snow_prod",
        "plugin": "snowflake",
        "dialect": "snowflake",
        "environment": "prod",
        "max_effect": "write",
        "credential_mode": "shared",
        "enabled": True,
        "urn_namespace": "snowflake://acct_x",
    }
    conn = Connection.model_validate(payload)
    dumped = conn.model_dump(mode="json")
    assert dumped["schema_version"] == "2.0.0"
    assert "max_effect" not in dumped  # stripped by the migration, not riding along
    assert conn.credential_mode is CredentialMode.SHARED
    assert conn.enabled is True


@pytest.mark.parametrize(
    ("mode", "enabled"),
    [
        pytest.param(CredentialMode.PER_USER, True, id="per-user-enabled"),
        pytest.param(CredentialMode.SHARED, False, id="shared-disabled"),
        pytest.param(CredentialMode.PER_USER, False, id="per-user-disabled"),
    ],
)
def test_new_fields_round_trip(mode: CredentialMode, enabled: bool) -> None:
    conn = Connection(
        handle="snow_oauth",
        plugin="snowflake",
        environment=Environment.PROD,
        credential_ref=CredentialRef(scheme="oauth", locator="x/oauth.json"),
        credential_mode=mode,
        enabled=enabled,
    )
    loaded = Connection.model_validate(conn.model_dump(mode="json"))
    assert loaded.credential_mode is mode
    assert loaded.enabled is enabled


def test_newer_writer_unknowns_survive() -> None:
    """Forward compat (extra="allow"): a field from a future Connection version
    round-trips through today's reader untouched."""
    payload = {
        "schema_version": "9.0.0",
        "handle": "h",
        "plugin": "p",
        "future_field": {"nested": True},
    }
    conn = Connection.model_validate(payload)
    dumped = conn.model_dump(mode="json")
    assert dumped["future_field"] == {"nested": True}
    assert dumped["schema_version"] == Connection.SCHEMA_VERSION


def test_identity_ignores_the_new_fields() -> None:
    """(plugin, handle) equality/hash is the registry/dedup key — a disabled or
    per-user variant of the same logical connection must still compare equal so
    it can never be added twice."""
    a = Connection(handle="h", plugin="p")
    b = Connection(handle="h", plugin="p", enabled=False, credential_mode=CredentialMode.PER_USER)
    assert a == b
    assert hash(a) == hash(b)
