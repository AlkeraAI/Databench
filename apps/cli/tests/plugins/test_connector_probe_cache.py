"""Durable async-probe cache contracts."""

from __future__ import annotations

from alkera_cli.plugins.plugin_base.surfaces import (
    ConnectionProbeResult,
    cached_probe_result,
    probe_result_payload,
)


def test_probe_payload_round_trips_only_non_secret_measured_evidence() -> None:
    """Cached probe identity, grants, and reachability survive capability assembly."""
    result = ConnectionProbeResult(
        service_identity="cluster-id",
        observed_cluster_ids={"cluster_id": "cluster-id"},
        measured_grants={"profile:shop": ["enabled", "readable"]},
        endpoint_reachability={"broker": True},
    )
    payload = probe_result_payload(result)
    assert cached_probe_result({"connection_probe": payload}) == result
    assert set(payload) == {
        "schema_version",
        "service_identity",
        "observed_cluster_ids",
        "measured_grants",
        "endpoint_reachability",
    }


def test_invalid_probe_cache_fails_closed_instead_of_guessing() -> None:
    """Malformed durable evidence cannot influence identity or capability disclosure."""
    assert cached_probe_result({"connection_probe": {"measured_grants": "all"}}) is None
