"""A deployment's pod prefix is its environment's, so a laptop cannot reap
production's machines.

``alkera`` is production's prefix. A local worker left on the default, holding
a key to production's provider account, used to list production's pods, find
no row for them in its own database and terminate them past the grace.
"""

from __future__ import annotations

import pytest
from alkera_core.compute import reconcile
from alkera_core.compute.reconcile import is_our_pod_name, pod_name_base
from alkera_core.config import settings

HEX = "0123456789ab"


@pytest.mark.parametrize(
    ("env", "configured", "base"),
    [
        pytest.param("production", "alkera", "alkera", id="production-keeps-its-name"),
        pytest.param("local", "alkera", "alkera-local", id="local-default-is-namespaced"),
        pytest.param("staging", "alkera", "alkera-staging", id="staging-default-is-namespaced"),
        pytest.param("staging", "alkera-staging", "alkera-staging", id="explicit-staging"),
        pytest.param("local", "alkera-dev-robin", "alkera-dev-robin", id="explicit-local"),
        pytest.param("production", "alkera-prod", "alkera-prod", id="explicit-production"),
    ],
)
def test_the_prefix_is_namespaced_off_production(env: str, configured: str, base: str) -> None:
    config = settings.model_copy(update={"app_env": env, "compute_pod_name_prefix": configured})
    assert pod_name_base(config) == base


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(f"alkera-{HEX}", id="production-default"),
        pytest.param(f"alkera-prod-{HEX}", id="alkera-prod"),
        pytest.param(f"alkera-production-{HEX}", id="alkera-production"),
        pytest.param(f"alkera-staging-{HEX}", id="staging"),
        pytest.param(f"alkera-local-{HEX}0", id="longer-id"),
        pytest.param(f"alkera-local-x-{HEX}", id="extended-local"),
    ],
)
def test_a_local_worker_never_selects_another_environments_pod(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "app_env", "local")
    monkeypatch.setattr(settings, "compute_pod_name_prefix", "alkera")
    assert not is_our_pod_name(name)


def test_a_local_worker_names_and_selects_its_own_pods(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "app_env", "local")
    monkeypatch.setattr(settings, "compute_pod_name_prefix", "alkera")
    assert reconcile.pod_name_prefix() == "alkera-local-"
    assert is_our_pod_name(f"alkera-local-{HEX}")


def test_production_does_not_select_a_local_pod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "compute_pod_name_prefix", "alkera")
    assert is_our_pod_name(f"alkera-{HEX}")
    assert not is_our_pod_name(f"alkera-local-{HEX}")
